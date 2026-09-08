"""Re-rate every account against eligible plans, and write the ranking.

    python3 build_plan_recommendations.py --check      # build tariffs only, price nothing
    python3 build_plan_recommendations.py --dry-run    # price everything, write nothing
    python3 build_plan_recommendations.py              # price and insert

Sprint 3 item C. `kea.plan_recommendation` is the ranked annual decision the
agent reads; `kea.plan_bill_example` is the bill-sized evidence the dashboard
uses to explain it. Nothing computes a recommendation on the request path,
because a comparison over a year of half-hourly readings is a batch job.

⚠️ BOTH OUTPUT TABLES ARE APPEND-ONLY, AND NEVER IN `serving.TABLES`.

Every run inserts a fresh generation stamped with one `computed_at`. Stage 5's
loader must not know these tables exist -- they are outputs of the platform,
not exports of the warehouse, and a replica load would truncate the history
that makes "what did we tell this customer in August" answerable. Same argument
as `kea.bill_forecast` in load_export_to_serving.py.

THE PRICING IS `analytics/tariff.py`, NOT A SECOND IMPLEMENTATION.

That module already carries the two rules that make a plan comparison correct --
one banding pass per charge group, and a tiling gap rejected at construction --
and it is unit-tested against rates transcribed from `02_catalogue.sql`. A
re-rating written here would be the second implementation of the same
arithmetic, and the two would drift on the first rate change. So this script
does the SQL and the ranking, and imports the pricing.

WHY A 7 x 48 PROFILE IS EXACT, NOT AN APPROXIMATION.

Pricing every account against three candidate plans over a year of half hours
still creates millions of band lookups in Python. It is also unnecessary:
every band in this catalogue is
a set of weekdays and a span of the clock, so two readings that share a weekday
and a minute-of-day are priced identically on every plan. Summing them first
loses nothing, because

  * `cost_of` is linear in kwh -- rate x (a + b) = rate x a + rate x b;
  * no band is restricted by month (checked: `tou_band.months` is null on all
    707 rows), so the calendar cannot separate two cells that share a bucket;
  * the daily charges are driven by `days`, which is passed in rather than
    counted from the readings, so collapsing dates cannot lose them.

That turns a year's readings into at most 7 x 48 x 2 profile cells per account;
the compact demo then prices only three Standard User plans on Vector.

⚠️ THE RATE ATTACHES TO A BAND *LABEL*, NOT TO A BAND ROW.

`catalogue.tou_band` may hold several rows for one (plan, group, label), while
`catalogue.tariff_component` carries one rate for that label. A label appears
once per contiguous window -- Vector's
off-peak is three separate rows, 00:00-07:00, 11:00-17:00 and 21:30-24:00 --
and the source biller joins the rate on `(charge_scope, band_label)`:

    LEFT JOIN band_totals bt ON bt.charge_scope = rc.charge_scope
                            AND bt.band_label   = w.band_label

(gen_source/04_rate_bills.sql). The component's own `window_id` only names which
label it prices. So the lookup here is keyed on the label too. Free Weekends
uses a zero bundled usage component in its `FREE` label, making the advertised
window genuinely free of per-kWh import charges.

Reading it any other way breaks in one of two directions and neither errors: key
on `band_id` and disconnected periods are lost; drop a rate-less label and
`Tariff` refuses to build because its tiling has a hole.

⚠️ `end_time = '00:00:00'` MEANS MIDNIGHT AT THE END OF THE DAY.

The source writes `TIME '24:00'`, which Postgres accepts and Snowflake does not
survive; by the time it lands in serving it is 00:00. `Band.end_minute` is
minutes from midnight for exactly this reason, so the translation is 0 -> 1440.
A band never legitimately ends at minute zero, so the rule needs no exception.

WHY THE CURRENT COST IS RE-RATED RATHER THAN SUMMED FROM `billing.bill`.

`usage_saving` is a difference, and a difference is only meaningful if both
sides come out of the same engine over the same readings. Billed totals carry
prompt-payment discounts, mid-period plan versions and the odd estimated read;
subtracting a modelled candidate from a billed actual would attribute all of
that to the plan change. Whether the engine reproduces a real bill is a
separate question, and Sprint 3 item D is the test that asks it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from decimal import Decimal
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))

# The pricing engine lives with the analytics that serve it. ONE implementation
# of the tariff arithmetic in this repo, imported across the two trees rather
# than copied into both -- see the module docstring.
#
# The path is an environment variable because this script runs in two places
# with two different layouts: from the host it sits inside the checkout, and
# under `make recommend-plans` only ./pipeline and ../services/api are mounted,
# so there is no repo root to count parents back to. The repo-relative default
# is what makes the host case need no setup.
_API_APP = Path(os.environ.get(
    "KEA_API_DIR", _HERE.parents[3] / "services" / "api"))
if not (_API_APP / "app" / "analytics" / "tariff.py").exists():
    raise SystemExit(
        f"the pricing engine is not at {_API_APP}/app/analytics/tariff.py.\n"
        f"Set KEA_API_DIR to the services/api directory.")
sys.path.insert(0, str(_API_APP))

from app.analytics.tariff import (  # noqa: E402
    Band, FlatCharge, Reading, Tariff, cost_of,
)
from load_export_to_serving import _connect as connect_serving  # noqa: E402

NZ = "Pacific/Auckland"

#: A year, so `current_annual_cost` needs no scaling. The window is anchored on
#: the data's own newest reading rather than on today, the same rule the export
#: retention uses: a fixed extract must not silently shorten its own period
#: because the wall clock moved.
WINDOW_DAYS = 365

# Three consecutive invoices are enough to show that a saving is not a single
# lucky month, while keeping the customer story readable on one dashboard.
BILL_EXAMPLES_PER_ACCOUNT = 3

# The catalogue stores tariff components ex-GST while a customer sees the
# GST-inclusive bill total. Term credits are advertised GST-inclusive too.
# Keep every dollar stored in kea.plan_recommendation on that customer-facing
# basis; mixing the two makes a "$300 credit" add unlike quantities to the
# usage saving beside it.
GST_MULTIPLIER = Decimal("1.15")


def customer_amount(ex_gst: Decimal) -> Decimal:
    """Convert a catalogue/engine amount to the GST-inclusive customer amount."""
    return ex_gst * GST_MULTIPLIER


def term_credit(term_months: int, monthly_credit: Decimal) -> Decimal:
    """The maximum conditional credit earned by completing this term.

    The term credit is deliberately *not* annualised.  A six-month offer is
    worth six monthly credits, not twelve: describing it as a yearly saving
    would make the shorter commitment look twice as valuable as it is.
    """
    return Decimal(term_months) * monthly_credit


def ranked_cost(candidate_cost: Decimal, conditional_credit: Decimal) -> Decimal:
    """Customer cost used to rank terms of the same tariff.

    `candidate_annual_cost` remains the unconditional usage price in the
    persisted row.  The conditional credit is separate for honesty, but it is
    still part of the total offer and may change its order among candidates.
    """
    return candidate_cost - conditional_credit


def is_switch_recommendation(*, rank: int, candidate_plan_id: int,
                             current_plan_id: int,
                             usage_saving: Decimal) -> bool:
    """Whether the top-ranked offer may be presented as a switch recommendation.

    A credit rewards staying for a term and can decide which offer ranks first,
    but it must never manufacture a "saving" from a more expensive tariff.
    This deliberately does not inspect `term_credit`: it is conditional on
    completing the term, and early-exit charges are outside this annual usage
    comparison.
    """
    return (rank == 1
            and candidate_plan_id != current_plan_id
            and usage_saving > Decimal("0"))


# --------------------------------------------------------------- tariffs

def _minutes(t: dt.time, *, end: bool) -> int:
    m = t.hour * 60 + t.minute
    return 1440 if (end and m == 0) else m


# The uplift is a literal in the source biller (gen_source/04_rate_bills.sql,
# `ROUND(p.qty * 0.0511, 2)`) and has no column in the catalogue, so there is
# nothing to join to. Measuring it off the bills that were actually issued is
# closer to the truth than copying the constant: if the biller's factor ever
# changes, this follows it, and if the two ever disagree the ratio stops being
# flat and the assertion below says so.
LOSS_SQL = """
select sum(loss_kwh) / nullif(sum(quantity), 0)
from billing.bill_line
where loss_kwh is not null and quantity > 0
"""


def loss_factor(cur) -> Decimal:
    cur.execute(LOSS_SQL)
    (ratio,) = cur.fetchone()
    if ratio is None:
        return Decimal("0")
    ratio = Decimal(str(ratio)).quantize(Decimal("0.000001"))
    if not (Decimal("0") < ratio < Decimal("0.15")):
        raise SystemExit(f"line-loss ratio {ratio} is not plausible -- "
                         f"billing.bill_line.loss_kwh has changed shape")
    return ratio


def build_tariff(name: str, bands: list[dict], comps: list[dict],
                 losses: Decimal = Decimal("0")) -> Tariff:
    """Assemble one plan's rate card out of its catalogue rows.

    Raises `TariffError` (from `Tariff.__post_init__`) if the bands leave a hole
    in the week. That check is the reason this is a build step rather than a
    dict lookup: a plan that cannot tile is a catalogue defect, and finding it
    here costs a stack trace instead of a plausible, too-low bill.
    """
    # Import and export components may deliberately share a band label (for
    # example Anytime's consumption and solar buy-back). Keeping
    # them in one mapping lets the later export row overwrite the consumption
    # rate, which silently prices imported power at the much lower buy-back
    # rate.  Import bands and export bands are assembled separately below, so
    # this lookup must contain import components only.
    rates = {(c["charge_group"], c["band_name"]): Decimal(str(c["rate"]))
             for c in comps
             if c["band_name"] is not None and c["direction"] == "import"}

    banded = tuple(
        Band(charge_group=b["charge_group"],
             label=b["band_name"],
             days=frozenset(int(d) for d in b["days_of_week"]),
             start_minute=_minutes(b["start_time"], end=False),
             end_minute=_minutes(b["end_time"], end=True),
             # A label with no component is free. See the module docstring.
             rate=rates.get((b["charge_group"], b["band_name"]), Decimal("0")))
        for b in bands if b["direction_is_import"]
    )

    flat = tuple(
        FlatCharge(charge_group=c["charge_group"],
                   description=c["component_name"],
                   rate=Decimal(str(c["rate"])),
                   per="day" if c["basis"] == "per_day" else "kwh")
        for c in comps
        if c["band_name"] is None and c["direction"] == "import"
    )

    # An export rate attaches to a BAND LABEL, just as an import rate does.
    # It does *not* attach only to the one `band_id` a component stores.  A
    # label can deliberately span disconnected windows: Time of Use has two
    # peak and two off-peak periods under the same labels. The
    # catalogue component points at one representative window, so building an
    # export Band from that component alone silently loses the other one.
    #
    # Expand through `bands` for every matching (group, label), matching the
    # import rate lookup above.  A bandless export component is not currently
    # allowed by the serving schema, but retaining the all-day fallback keeps
    # the assembler correct for the older fixture tariff too.
    export = []
    for c in comps:
        if c["direction"] != "export":
            continue
        if c["band_name"] is None:
            export.append(Band(
                charge_group=c["charge_group"], label=c["component_name"],
                days=frozenset(range(1, 8)), start_minute=0, end_minute=1440,
                rate=Decimal(str(c["rate"]))))
            continue
        windows = [b for b in bands
                   if b["charge_group"] == c["charge_group"]
                   and b["band_name"] == c["band_name"]]
        if not windows:
            raise ValueError(f"{name}: export component {c['component_name']!r} "
                             f"has no {c['charge_group']}/{c['band_name']} band")
        export.extend(
            Band(charge_group=c["charge_group"], label=c["band_name"],
                 days=frozenset(int(d) for d in b["days_of_week"]),
                 start_minute=_minutes(b["start_time"], end=False),
                 end_minute=_minutes(b["end_time"], end=True),
                 rate=Decimal(str(c["rate"])))
            for b in windows
        )

    return Tariff(name=name, bands=banded, flat=flat, export_bands=tuple(export),
                  loss_factor=losses)


PLAN_SQL = """
select p.plan_id, p.plan_code, p.plan_name, p.network_id, p.user_type::text,
       p.is_tou, p.prompt_payment_discount_pct
from catalogue.plan p
where p.is_active
order by p.plan_id
"""

BAND_SQL = """
select b.plan_id, b.charge_group::text, b.band_name, b.days_of_week,
       b.start_time, b.end_time
from catalogue.tou_band b
join catalogue.plan p on p.plan_id = b.plan_id and p.is_active
order by b.plan_id, b.charge_group, b.start_time
"""

# effective_to is exclusive, matching 04_rate_bills.sql's `p_to < valid_to`.
COMP_SQL = """
select c.plan_id, c.charge_group::text, c.component_name, c.basis::text,
       c.direction::text, c.rate, b.band_name, b.days_of_week as days,
       b.start_time, b.end_time
from catalogue.tariff_component c
join catalogue.plan p on p.plan_id = c.plan_id and p.is_active
left join catalogue.tou_band b on b.band_id = c.band_id
where c.effective_from <= %(asof)s
  and (c.effective_to is null or %(asof)s < c.effective_to)
order by c.plan_id, c.component_id
"""


def load_tariffs(cur, asof: dt.date,
                 losses: Decimal = Decimal("0"),
                 ) -> tuple[dict[int, dict], dict[int, Tariff]]:
    cur.execute(PLAN_SQL)
    plans = {r[0]: dict(plan_id=r[0], plan_code=r[1], plan_name=r[2],
                        network_id=r[3], user_type=r[4], is_tou=r[5],
                        discount_pct=r[6]) for r in cur.fetchall()}

    bands: dict[int, list[dict]] = {pid: [] for pid in plans}
    cur.execute(BAND_SQL)
    for pid, grp, label, dows, t0, t1 in cur.fetchall():
        bands[pid].append(dict(charge_group=grp, band_name=label,
                               days_of_week=dows, start_time=t0, end_time=t1,
                               direction_is_import=True))

    comps: dict[int, list[dict]] = {pid: [] for pid in plans}
    cur.execute(COMP_SQL, {"asof": asof})
    for pid, grp, nm, basis, direction, rate, label, days, t0, t1 in cur.fetchall():
        comps[pid].append(dict(charge_group=grp, component_name=nm, basis=basis,
                               direction=direction, rate=rate, band_name=label,
                               days=days, start_time=t0, end_time=t1))

    tariffs = {pid: build_tariff(p["plan_code"], bands[pid], comps[pid], losses)
               for pid, p in plans.items()}
    return plans, tariffs


# --------------------------------------------------------------- usage

# isodow and minute-of-day IN LOCAL TIME. Every band is a wall clock, and a NZ
# local day is 46 or 50 half hours across a daylight-saving boundary -- banding
# in UTC would rotate every window by an hour for half the year.
PROFILE_SQL = f"""
select a.account_id,
       r.direction::text                                          as direction,
       extract(isodow from l.loc)::int                            as dow,
       (extract(hour from l.loc) * 60 + extract(minute from l.loc))::int as minute,
       sum(h.kwh)                                                 as kwh
from metering.meter_read_hh h
join metering.meter_register r on r.register_id = h.register_id
join billing.account a on a.icp_id = r.icp_id and a.end_date is null
cross join lateral (select h.interval_start at time zone '{NZ}' as loc) l
where h.interval_start >= %(lo)s and h.interval_start < %(hi)s
group by 1, 2, 3, 4
"""

ACCOUNT_SQL = """
select a.account_id, a.account_number, a.plan_id, i.network_id, a.term_months
from billing.account a
join metering.icp i on i.icp_id = a.icp_id
join kea.mart_account_context c on c.account_id = a.account_id
where a.end_date is null
  and i.icp_number like '09%%'
  and not c.is_quarantined
order by a.account_id
"""

# catalogue.term_credit is a current offer table: one currently applicable row
# per commitment length.  Flexible is intentionally absent from the catalogue
# because zero dollars is not a priced offer; the Python caller adds it.
TERM_SQL = """
select term_months, monthly_credit
from catalogue.term_credit
where effective_from <= %(asof)s
  and (effective_to is null or %(asof)s < effective_to)
order by term_months
"""

MAX_READ_SQL = f"""
select max(h.interval_start at time zone '{NZ}')::date from metering.meter_read_hh h
"""

BILL_EXAMPLE_SQL = f"""
with ranked as (
    select b.bill_id, b.account_id, b.period_start, b.period_end,
           b.period_days, b.total_charges,
           row_number() over (
               partition by b.account_id order by b.period_end desc, b.bill_id desc
           ) as rn
    from billing.bill b
    where b.account_id = any(%(account_ids)s)
      and b.period_start >= %(period_start)s
      and b.period_end <= %(period_end)s
), picked as (
    select * from ranked where rn <= %(limit)s
)
select b.bill_id, b.account_id, b.period_start, b.period_end,
       b.period_days, b.total_charges,
       r.direction::text,
       extract(isodow from l.loc)::int as dow,
       (extract(hour from l.loc) * 60 + extract(minute from l.loc))::int as minute,
       sum(h.kwh) as kwh
from picked b
join billing.account a on a.account_id = b.account_id
join metering.meter_register r on r.icp_id = a.icp_id
join metering.meter_read_hh h on h.register_id = r.register_id
cross join lateral (select h.interval_start at time zone '{NZ}' as loc) l
where l.loc::date between b.period_start and b.period_end
group by b.bill_id, b.account_id, b.period_start, b.period_end,
         b.period_days, b.total_charges, r.direction, dow, minute
order by b.account_id, b.period_end desc, r.direction, dow, minute
"""


def load_profiles(cur, lo: dt.datetime, hi: dt.datetime) -> dict[int, list[Reading]]:
    """One `Reading` per (weekday, half hour, direction) bucket, per account.

    The datetime handed to `Reading` is a REPRESENTATIVE, not a real timestamp:
    `cost_of` reads only `isoweekday()` and the time of day off it. 2024-01-01
    is a Monday, so weekday 1..7 maps onto 2024-01-01..07 and the arithmetic
    stays in the module that owns it.
    """
    cur.execute(PROFILE_SQL, {"lo": lo, "hi": hi})
    out: dict[int, list[Reading]] = {}
    for account_id, direction, dow, minute, kwh in cur.fetchall():
        at = dt.datetime(2024, 1, dow, minute // 60, minute % 60)
        out.setdefault(account_id, []).append(
            Reading(at=at, kwh=Decimal(str(kwh)), exported=(direction == "export")))
    return out


def load_bill_examples(cur, account_ids: list[int], period_start: dt.date,
                       period_end: dt.date) -> dict[tuple[int, int], dict]:
    """Load real invoice periods as compact, exact 7x48 pricing profiles."""
    if not account_ids:
        return {}
    cur.execute(BILL_EXAMPLE_SQL, {
        "account_ids": account_ids,
        "period_start": period_start,
        "period_end": period_end,
        "limit": BILL_EXAMPLES_PER_ACCOUNT,
    })
    out: dict[tuple[int, int], dict] = {}
    for (bill_id, account_id, start, end, days, actual, direction,
         dow, minute, kwh) in cur.fetchall():
        key = (account_id, bill_id)
        example = out.setdefault(key, {
            "bill_id": bill_id,
            "account_id": account_id,
            "period_start": start,
            "period_end": end,
            "period_days": days,
            "actual_bill_amount": Decimal(str(actual)),
            "readings": [],
        })
        at = dt.datetime(2024, 1, dow, minute // 60, minute % 60)
        example["readings"].append(
            Reading(at=at, kwh=Decimal(str(kwh)), exported=(direction == "export")))
    return out


def load_term_options(cur, asof: dt.date) -> list[tuple[int, Decimal]]:
    """Return the specified three offer terms, or fail before writing a lie."""
    cur.execute(TERM_SQL, {"asof": asof})
    rows = [(int(months), Decimal(str(credit))) for months, credit in cur.fetchall()]
    seen = {months for months, _ in rows}
    if 0 in seen or not seen.issubset({6, 12}):
        raise SystemExit(f"invalid term-credit rows in catalogue.term_credit: {rows}")
    if len(seen) != len(rows):
        raise SystemExit("catalogue.term_credit has more than one active row for a term")
    if seen != {6, 12}:
        raise SystemExit("catalogue.term_credit must expose both 6- and 12-month "
                         "offers before plan recommendations can be generated")
    return [(0, Decimal("0"))] + rows


# --------------------------------------------------------------- ranking

def _rationale(cand: dict, cur_plan: dict, saving: Decimal, kwh: Decimal,
               free_kwh: Decimal, term_months: int,
               monthly_credit: Decimal) -> str:
    parts = [f"{cand['plan_name']} costs ${abs(saving):,.2f}/yr "
             f"{'less' if saving > 0 else 'more'} than {cur_plan['plan_name']} "
             f"on {kwh:,.0f} kWh."]
    if free_kwh > 0 and kwh > 0:
        parts.append(f"{free_kwh / kwh:.1%} of that usage falls in a "
                     f"free or half-price window.")
    if term_months:
        credit = term_credit(term_months, monthly_credit)
        parts.append(
            f"A {term_months}-month term offers ${credit:,.2f} in conditional "
            "credits, conditional on completing the term; early-exit charges "
            "are not included in this saving.")
    return " ".join(parts)


def _free_kwh(breakdown) -> Decimal:
    """kWh sitting in an energy band that prices at zero or half.

    Read off the breakdown rather than re-derived: `kwh_by_band` is keyed on the
    same labels the catalogue uses, so this cannot disagree with what was
    charged.
    """
    return sum((k for (grp, label), k in breakdown.kwh_by_band.items()
                if grp == "energy" and label in ("FREE", "HALF")), Decimal("0"))


def _group_amount(breakdown, group: str) -> Decimal:
    """GST-inclusive amount for one tariff charge group."""
    return customer_amount(breakdown.by_group.get(group, Decimal("0")))


def candidate_sets(plans: dict[int, dict]) -> dict[tuple[int, str], list[int]]:
    """Group customer-facing candidates by lines company and user type.

    These are eligibility constraints, not dimensions to optimise over. Keeping
    both in the key prevents a future LOW tariff from silently doubling the
    offer list or being recommended to a Standard User account.
    """
    grouped: dict[tuple[int, str], list[int]] = {}
    for pid, plan in plans.items():
        if plan["plan_code"].startswith("KEA_"):
            key = (plan["network_id"], plan["user_type"])
            grouped.setdefault(key, []).append(pid)
    return grouped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="build every active tariff and stop -- catches a "
                         "catalogue hole without touching a reading")
    ap.add_argument("--dry-run", action="store_true", help="price but do not insert")
    ap.add_argument("--account", type=int, action="append",
                    help="restrict to these account_ids (repeatable)")
    args = ap.parse_args(argv)

    conn = connect_serving()
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(MAX_READ_SQL)
    period_end = cur.fetchone()[0]
    period_start = period_end - dt.timedelta(days=WINDOW_DAYS - 1)

    losses = loss_factor(cur)
    plans, tariffs = load_tariffs(cur, period_end, losses)
    terms = load_term_options(cur, period_end)
    print(f"tariffs: {len(tariffs)} active plans built, as at {period_end}, "
          f"line losses {losses:.4%} of import energy", flush=True)
    print("terms: " + ", ".join(
        "flexible" if months == 0 else f"{months}mo @ ${credit:.2f}/month"
        for months, credit in terms), flush=True)
    if args.check:
        for pid, t in sorted(tariffs.items()):
            print(f"  {plans[pid]['plan_code']:<34} "
                  f"bands={len(t.bands):>2} flat={len(t.flat)} "
                  f"export={len(t.export_bands)}")
        return 0

    # Local midnight to local midnight, converted by the server so the DST
    # boundaries inside the window are the ones Pacific/Auckland actually had.
    cur.execute(
        f"select (%s::date)::timestamp at time zone '{NZ}', "
        f"       ((%s::date + 1))::timestamp at time zone '{NZ}'",
        (period_start, period_end))
    lo, hi = cur.fetchone()

    cur.execute(ACCOUNT_SQL)
    accounts = [dict(account_id=r[0], account_number=r[1], plan_id=r[2],
                     network_id=r[3], term_months=r[4]) for r in cur.fetchall()]
    if args.account:
        wanted = set(args.account)
        accounts = [a for a in accounts if a["account_id"] in wanted]

    print(f"usage window {period_start} .. {period_end} "
          f"({WINDOW_DAYS} days), {len(accounts)} open accounts", flush=True)
    profiles = load_profiles(cur, lo, hi)
    print(f"profiles: {sum(len(v) for v in profiles.values()):,} cells "
          f"for {len(profiles):,} accounts", flush=True)

    by_eligibility = candidate_sets(plans)

    computed_at = dt.datetime.now(dt.timezone.utc)
    rows: list[tuple] = []
    recommended_offers: list[dict] = []
    skipped: list[str] = []

    for acct in accounts:
        readings = profiles.get(acct["account_id"])
        if not readings:
            skipped.append(f"{acct['account_number']}: no readings in window")
            continue
        cur_plan = plans.get(acct["plan_id"])
        if cur_plan is None:
            # The account sits on a superseded plan version. Its price is not a
            # current option, so there is nothing honest to compare against.
            skipped.append(f"{acct['account_number']}: plan {acct['plan_id']} not active")
            continue

        # Tariff components are ex-GST; recommendation rows are customer-facing
        # GST-inclusive dollars so they can be compared directly with the
        # advertised term credit.
        base = customer_amount(
            cost_of(readings, tariffs[acct["plan_id"]], WINDOW_DAYS).total)
        kwh = sum((r.kwh for r in readings if not r.exported), Decimal("0"))

        scored = []
        eligible = (acct["network_id"], cur_plan["user_type"])
        for cid in by_eligibility.get(eligible, []):
            bd = cost_of(readings, tariffs[cid], WINDOW_DAYS)
            cost = customer_amount(bd.total)
            for months, monthly in terms:
                credit = term_credit(months, monthly)
                scored.append((cid, cost, _free_kwh(bd), months, monthly, credit))

        # A longer term can change the ordering because its credit is part of
        # the offer.  It does not change whether an offer is recommended:
        # `is_recommended` below still demands a positive usage-only saving.
        scored.sort(key=lambda s: (ranked_cost(s[1], s[5]), s[0], s[3]))
        for rank, (cid, cost, free, months, monthly, credit) in enumerate(scored, start=1):
            usage_saving = base - cost
            annual_saving = usage_saving + credit
            recommended = is_switch_recommendation(
                rank=rank, candidate_plan_id=cid,
                current_plan_id=acct["plan_id"], usage_saving=usage_saving)
            rows.append((
                acct["account_id"], cid, months, monthly, acct["plan_id"],
                period_start, period_end,
                round(base, 2), round(cost, 2), round(usage_saving, 2),
                round(credit, 2), round(annual_saving, 2), rank,
                recommended,
                _rationale(plans[cid], cur_plan, usage_saving, kwh, free,
                           months, monthly),
                computed_at,
            ))
            if recommended:
                recommended_offers.append({
                    "account_id": acct["account_id"],
                    "current_plan_id": acct["plan_id"],
                    "candidate_plan_id": cid,
                    "term_months": months,
                    "monthly_credit": monthly,
                })

    recommended = len(recommended_offers)
    print(f"scored {len(rows):,} candidate rows, "
          f"{recommended:,} accounts have a cheaper plan", flush=True)
    for s in skipped:
        print(f"  skipped {s}")

    bill_profiles = load_bill_examples(
        cur, [r["account_id"] for r in recommended_offers],
        period_start, period_end)
    example_rows: list[tuple] = []
    offer_by_account = {r["account_id"]: r for r in recommended_offers}
    for (account_id, bill_id), example in bill_profiles.items():
        offer = offer_by_account[account_id]
        readings = example["readings"]
        days = example["period_days"]
        current = cost_of(readings, tariffs[offer["current_plan_id"]], days)
        candidate = cost_of(readings, tariffs[offer["candidate_plan_id"]], days)
        # The evidence table is customer-facing to the cent.  Derive its
        # saving from those two displayed amounts, not from higher-precision
        # intermediates, otherwise a half-cent boundary can make the visible
        # subtraction disagree by one cent.
        current_total = round(customer_amount(current.total), 2)
        candidate_total = round(customer_amount(candidate.total), 2)
        import_kwh = sum((r.kwh for r in readings if not r.exported), Decimal("0"))
        export_kwh = sum((r.kwh for r in readings if r.exported), Decimal("0"))
        example_rows.append((
            account_id, bill_id, offer["current_plan_id"], offer["candidate_plan_id"],
            offer["term_months"], example["period_start"], example["period_end"],
            days, round(import_kwh, 4), round(export_kwh, 4),
            round(_free_kwh(current), 4), round(_free_kwh(candidate), 4),
            round(example["actual_bill_amount"], 2), current_total,
            candidate_total, current_total - candidate_total,
            round(offer["monthly_credit"], 2),
            round(_group_amount(current, "energy"), 2),
            round(_group_amount(candidate, "energy"), 2),
            round(_group_amount(current, "network"), 2),
            round(_group_amount(candidate, "network"), 2),
            round(_group_amount(current, "other"), 2),
            round(_group_amount(candidate, "other"), 2),
            computed_at,
        ))
    print(f"bill evidence: {len(example_rows):,} completed invoices re-rated", flush=True)

    if args.dry_run:
        conn.rollback()
        return 0

    from psycopg2.extras import execute_values
    execute_values(cur, """
        insert into kea.plan_recommendation
            (account_id, candidate_plan_id, term_months, monthly_credit,
             current_plan_id, usage_period_start, usage_period_end,
             current_annual_cost, candidate_annual_cost, usage_saving,
             term_credit, annual_saving, rank, is_recommended, rationale,
             computed_at)
        values %s""", rows, page_size=500)
    if example_rows:
        execute_values(cur, """
            insert into kea.plan_bill_example
                (account_id, bill_id, current_plan_id, candidate_plan_id,
                 term_months, period_start, period_end, period_days,
                 import_kwh, export_kwh, current_discount_kwh,
                 candidate_discount_kwh, actual_bill_amount,
                 current_modelled_amount, candidate_modelled_amount,
                 usage_saving, monthly_credit, current_energy_amount,
                 candidate_energy_amount, current_network_amount,
                 candidate_network_amount, current_other_amount,
                 candidate_other_amount, computed_at)
            values %s""", example_rows, page_size=500)
    conn.commit()
    print(f"inserted {len(rows):,} recommendation and {len(example_rows):,} "
          f"bill-example rows at {computed_at.isoformat()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
