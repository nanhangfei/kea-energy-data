"""Prove that the recommendation engine can reproduce issued bills.

    python3 verify_bill_reproduction.py
    python3 verify_bill_reproduction.py --account 100001 --verbose

Sprint 3 item D is an acceptance gate, not a dashboard approximation.  For
every non-quarantined open account this script takes its latest already-issued bill, reads the
actual half-hourly import and export, builds the tariff version stored on that
bill, and compares the engine's ex-GST result with ``billing.bill``.  A plan
comparison must not be shipped when this check is outside one dollar.

Accounts carrying a declared integrity quarantine are listed and excluded from
the gate.  Their readings cannot be attributed honestly (the planted ambiguous
register is the concrete case), and the platform must decline to recommend a
plan for them rather than turn a known data defect into a plausible price.

The recommendation job deliberately prices customer-facing GST-inclusive
amounts.  This verifier does not: tariff components and bill lines are ex-GST,
so comparing on that basis isolates tariff arithmetic from GST rounding.

It imports the tariff assembler from ``build_plan_recommendations.py`` rather
than implementing bands, losses, exports or free windows a second time.  The
only difference is time: recommendations build active tariffs as at the data
window, while this script builds the exact historical tariff version that the
invoice says it used.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import sys
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))

from build_plan_recommendations import (  # noqa: E402
    GST_MULTIPLIER,
    NZ,
    build_tariff,
    loss_factor,
)
from app.analytics.tariff import Reading, cost_of  # noqa: E402
from load_export_to_serving import _connect as connect_serving  # noqa: E402


# This is the acceptance criterion in docs/sprints/sprint-3/data-plan.md.
# It permits the source biller's per-line rounding without concealing a
# missing band or an absent export credit.
TOLERANCE = Decimal("1.00")


# One already-issued period per open account. A bill retains the plan version
# which rated it, even if the account has since switched; using account.plan_id
# would re-rate history against today's plan and create exactly the false pass
# this check exists to prevent.
BILLS_SQL = """
with latest_bill as (
    select distinct on (b.account_id)
           b.bill_id, b.account_id, b.invoice_number, b.plan_id,
           b.period_start, b.period_end, b.period_days, b.charges_excl_gst,
           a.icp_id, a.plan_id as account_plan_id,
           a.term_months as account_term_months, a.term_start as account_term_start
    from billing.bill b
    join billing.account a on a.account_id = b.account_id
    join kea.mart_account_context c on c.account_id = a.account_id
    where a.end_date is null
      and not c.is_quarantined
      and b.status in ('issued', 'paid', 'overdue')
    order by b.account_id, b.period_end desc, b.bill_id desc
)
select b.bill_id, b.account_id, a.account_number, b.invoice_number, b.plan_id,
       b.period_start, b.period_end, b.period_days, b.charges_excl_gst, b.icp_id,
       case when h.history_id is not null then h.term_months
            when b.account_plan_id = b.plan_id then b.account_term_months
            else 0 end as term_months,
       case when h.history_id is not null then h.valid_from
            when b.account_plan_id = b.plan_id then b.account_term_start
            else null end as term_start
from latest_bill b
join billing.account a on a.account_id = b.account_id
left join lateral (
    select h.history_id, h.term_months, h.valid_from
    from billing.account_plan_history h
    where h.account_id = b.account_id
      and h.plan_id = b.plan_id
      and h.valid_from <= b.period_end
      and (h.valid_to is null or b.period_end < h.valid_to)
    order by h.valid_from desc, h.history_id desc
    limit 1
) h on true
order by b.account_id
"""

OPEN_ACCOUNTS_SQL = """
select a.account_id, a.account_number
from billing.account a
join kea.mart_account_context c using (account_id)
where a.end_date is null
  and not c.is_quarantined
order by a.account_id
"""

QUARANTINED_ACCOUNTS_SQL = """
select a.account_number, c.quarantine_reason
from billing.account a
join kea.mart_account_context c on c.account_id = a.account_id
where a.end_date is null
  and c.is_quarantined
order by a.account_id
"""

TARIFF_PLAN_SQL = """
select plan_id, plan_code
from catalogue.plan
where plan_id = %(plan_id)s
"""

TARIFF_BAND_SQL = """
select b.charge_group::text, b.band_name, b.days_of_week,
       b.start_time, b.end_time, b.months
from catalogue.tou_band b
where b.plan_id = %(plan_id)s
order by b.charge_group, b.start_time
"""

TARIFF_COMPONENT_SQL = """
select c.charge_group::text, c.component_name, c.basis::text,
       c.direction::text, c.rate, b.band_name, b.days_of_week,
       b.start_time, b.end_time
from catalogue.tariff_component c
left join catalogue.tou_band b on b.band_id = c.band_id
where c.plan_id = %(plan_id)s
  and c.effective_from <= %(asof)s
  and (c.effective_to is null or %(asof)s < c.effective_to)
order by c.component_id
"""

READINGS_SQL = f"""
select h.interval_start at time zone '{NZ}' as local_at,
       h.kwh,
       (r.direction = 'export') as exported
from metering.meter_read_hh h
join metering.meter_register r on r.register_id = h.register_id
where h.icp_id = %(icp_id)s
  and (h.interval_start at time zone '{NZ}')::date
      between %(period_start)s and %(period_end)s
order by h.interval_start, h.register_id
"""

TERM_CREDIT_SQL = """
select monthly_credit
from catalogue.term_credit
where term_months = %(term_months)s
  and effective_from <= %(asof)s
  and (effective_to is null or %(asof)s < effective_to)
order by effective_from desc
limit 1
"""


@dataclass(frozen=True)
class Bill:
    bill_id: int
    account_id: int
    account_number: str
    invoice_number: str
    plan_id: int
    period_start: dt.date
    period_end: dt.date
    period_days: int
    charges_excl_gst: Decimal
    icp_id: int
    term_months: int
    term_start: dt.date | None


@dataclass(frozen=True)
class Result:
    bill: Bill
    modelled: Decimal | None
    difference: Decimal | None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return (self.error is None
                and self.difference is not None
                and within_tolerance(self.difference))


def add_months(value: dt.date, months: int) -> dt.date:
    """Match PostgreSQL ``date + interval 'N months'`` for a calendar term."""
    if months < 0:
        raise ValueError("months must not be negative")
    index = value.year * 12 + (value.month - 1) + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    return dt.date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def term_is_active(term_months: int, term_start: dt.date | None,
                   period_end: dt.date) -> bool:
    """Use the source biller's end-date and exclusive term boundary exactly."""
    return (term_months in (6, 12)
            and term_start is not None
            and period_end >= term_start
            and period_end < add_months(term_start, term_months))


def within_tolerance(difference: Decimal,
                     tolerance: Decimal = TOLERANCE) -> bool:
    return abs(difference) <= tolerance


def _load_bills(cur, account_ids: set[int] | None) -> list[Bill]:
    cur.execute(BILLS_SQL)
    bills = [Bill(
        bill_id=row[0], account_id=row[1], account_number=row[2],
        invoice_number=row[3], plan_id=row[4], period_start=row[5],
        period_end=row[6], period_days=row[7],
        charges_excl_gst=Decimal(str(row[8])), icp_id=row[9],
        term_months=int(row[10]), term_start=row[11],
    ) for row in cur.fetchall()]
    if account_ids is not None:
        bills = [bill for bill in bills if bill.account_id in account_ids]
    return bills


def _open_accounts(cur, account_ids: set[int] | None) -> list[tuple[int, str]]:
    cur.execute(OPEN_ACCOUNTS_SQL)
    accounts = [(int(account_id), account_number) for account_id, account_number in cur.fetchall()]
    if account_ids is not None:
        accounts = [account for account in accounts if account[0] in account_ids]
    return accounts


def _quarantined_accounts(cur) -> list[tuple[str, str]]:
    cur.execute(QUARANTINED_ACCOUNTS_SQL)
    return [(account_number, reason) for account_number, reason in cur.fetchall()]


def _load_tariff(cur, plan_id: int, asof: dt.date, losses: Decimal):
    """Build the plan version present on an invoice, whether active or retired."""
    cur.execute(TARIFF_PLAN_SQL, {"plan_id": plan_id})
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"plan {plan_id} is absent from catalogue.plan")
    _, plan_code = row

    cur.execute(TARIFF_BAND_SQL, {"plan_id": plan_id})
    bands = []
    for group, label, days, start, end, months in cur.fetchall():
        # The shared engine deliberately has no seasonal-band feature yet.
        # Claiming this invoice reproduced while ignoring its months would be
        # worse than a loud unsupported-plan failure.
        if months is not None:
            raise ValueError(f"{plan_code} has seasonal band {label!r}; "
                             "the tariff engine cannot price it")
        bands.append(dict(charge_group=group, band_name=label,
                          days_of_week=days, start_time=start, end_time=end,
                          direction_is_import=True))

    cur.execute(TARIFF_COMPONENT_SQL, {"plan_id": plan_id, "asof": asof})
    comps = [dict(charge_group=group, component_name=name, basis=basis,
                  direction=direction, rate=rate, band_name=label,
                  days=days, start_time=start, end_time=end)
             for group, name, basis, direction, rate, label, days, start, end
             in cur.fetchall()]
    if not comps:
        raise ValueError(f"{plan_code} has no effective components at {asof}")
    return plan_code, build_tariff(plan_code, bands, comps, losses)


def _load_readings(cur, bill: Bill) -> list[Reading]:
    cur.execute(READINGS_SQL, {
        "icp_id": bill.icp_id,
        "period_start": bill.period_start,
        "period_end": bill.period_end,
    })
    return [Reading(at=local_at, kwh=Decimal(str(kwh)), exported=exported)
            for local_at, kwh, exported in cur.fetchall()]


def _term_credit_ex_gst(cur, bill: Bill) -> Decimal:
    if not term_is_active(bill.term_months, bill.term_start, bill.period_end):
        return Decimal("0")
    cur.execute(TERM_CREDIT_SQL, {
        "term_months": bill.term_months,
        "asof": bill.period_end,
    })
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"{bill.account_number}: active {bill.term_months}-month "
                         f"term has no credit effective on {bill.period_end}")
    # catalogue.term_credit is advertised GST-inclusive; Tariff.monthly_credit
    # models the negative ex-GST bill line.
    return Decimal(str(row[0])) / GST_MULTIPLIER


def _verify_one(cur, bill: Bill, losses: Decimal, cache: dict[tuple[int, dt.date], object]) -> Result:
    try:
        key = (bill.plan_id, bill.period_end)
        tariff = cache.get(key)
        if tariff is None:
            _, tariff = _load_tariff(cur, bill.plan_id, bill.period_end, losses)
            cache[key] = tariff
        readings = _load_readings(cur, bill)
        if not readings:
            raise ValueError("no half-hourly readings in the invoiced period")
        tariff = replace(tariff, monthly_credit=_term_credit_ex_gst(cur, bill))
        modelled = cost_of(readings, tariff, bill.period_days).total
        return Result(bill=bill, modelled=modelled,
                      difference=modelled - bill.charges_excl_gst)
    except Exception as exc:  # report every affected account, not only the first
        return Result(bill=bill, modelled=None, difference=None, error=str(exc))


def _show(result: Result) -> str:
    bill = result.bill
    prefix = f"{bill.account_number} / {bill.invoice_number} ({bill.period_end})"
    if result.error:
        return f"FAIL {prefix}: {result.error}"
    return (f"{'PASS' if result.passed else 'FAIL'} {prefix}: "
            f"modelled ${result.modelled:.2f}, billed ${bill.charges_excl_gst:.2f}, "
            f"difference ${result.difference:+.2f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account", type=int, action="append",
                    help="restrict to account_id (repeatable; useful to diagnose a failure)")
    ap.add_argument("--verbose", action="store_true", help="also print passing bills")
    args = ap.parse_args(argv)

    conn = connect_serving()
    conn.autocommit = False
    try:
        cur = conn.cursor()
        requested = set(args.account) if args.account else None
        accounts = _open_accounts(cur, requested)
        bills = _load_bills(cur, requested)
        quarantined = _quarantined_accounts(cur) if requested is None else []
        if not bills:
            print("no issued bill found for the requested open account(s)", file=sys.stderr)
            return 1

        billed_account_ids = {bill.account_id for bill in bills}
        missing = [account for account in accounts if account[0] not in billed_account_ids]
        unknown = sorted(requested - {account_id for account_id, _ in accounts}) if requested else []

        losses = loss_factor(cur)
        cache: dict[tuple[int, dt.date], object] = {}
        results = [_verify_one(cur, bill, losses, cache) for bill in bills]
        failures = [result for result in results if not result.passed]

        print(f"bill reproduction: {len(results) - len(failures)}/{len(results)} billed "
              f"non-quarantined open accounts within ${TOLERANCE:.2f} ex-GST; "
              f"line losses {losses:.4%}")
        for result in results:
            if args.verbose or not result.passed:
                print(_show(result))
        for _, account_number in missing:
            print(f"FAIL {account_number}: no issued bill exists to reproduce")
        for account_id in unknown:
            print(f"FAIL account {account_id}: not an open account")
        for account_number, reason in quarantined:
            print(f"SKIP {account_number}: quarantined ({reason})")
        return 1 if failures or missing or unknown else 0
    finally:
        # It is intentionally read-only; rolling back also releases the
        # snapshot immediately if it was run alongside the normal loader.
        conn.rollback()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
