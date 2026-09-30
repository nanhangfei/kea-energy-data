"""Price EV charging what-ifs, and write kea.ev_scenario.

    python3 build_ev_scenarios.py --dry-run    # price everything, write nothing
    python3 build_ev_scenarios.py              # price and insert

Sprint 4 item B. Two scenarios, for open generated-cohort accounts.

  add_ev          Every account. "If you bought an EV": a year's charging
                  (estimate_assumptions: km a year x kWh per km at the plug)
                  as one session a day at the wallbox rate, starting at 18:00
                  (evening, the baseline) or at 23:00 (night), priced on each
                  Kea plan the account is eligible for.
  shift_detected  Accounts with charging Kea can see in
                  kea.mart_charging_session. "If you moved the charging you
                  already do": each session's kwh_low as it happened
                  (as_detected, the baseline), or moved to start at 23:00 the
                  same night, priced on the current plan and on Time of Use.
                  A session that already starts in the night window stays
                  where it is. On a flat plan the saving is honestly $0.

SOLAR_SOAK, BOTH SCENARIOS, for an account whose meter actually exported in
the basis window. "Charge into your own sun": the charging goes into the
half-hours the export register itself recorded, most first -- add_ev at the
wallbox rate, shift_detected capped at each half-hour's own export, so it
only ever claims the free part and whatever will not fit stays exactly where
the meter found it.

⚠️ solar_soak MAY LOSE, and a losing row is published rather than suppressed.
On Time of Use, midday sits in the expensive window, so for a household whose
array cannot cover the session the honest answer is "your night rate still
wins" -- and a recommendation engine that only ever shows its own idea
winning is the silent wrongness this work exists to remove.

⚠️ THE MASKED SESSIONS ARE THE POINT. detect-v2 publishes a PV-shaved wallbox
with charger_class NULL, because a shaved wallbox and a portable charger are
the same signal on this meter. Filtering on 'high_power' alone would
therefore have withdrawn EV advice from exactly the households with most to
gain from it, so the session filter takes NULL with masked half-hours too.

⚠️ APPEND-ONLY, AND NEVER IN `serving.TABLES`; `computed_at` is the column
default. Same rules as build_solar_estimates.py.

WHAT EACH MONEY COLUMN MEANS.

  annual_bill_nzd         the whole household's modelled bill with that
                          charging, so plans compare directly
  annual_ev_cost_nzd      that bill minus the same household's bill on the
                          same plan without the charging
  saving_vs_baseline_nzd  the baseline pattern's bill minus this one; 0 on
                          the baseline row itself, which V005 enforces

All three are GST-inclusive, rounded to the cent BEFORE any subtraction, so
each difference is exactly the difference of the stored bills.

THE CHARGING IS ADDED TO REAL HALF-HOURS, then netted, then profiled -- the
estimate_common rule. For a solar household an 18:00 summer session first
uses power that would otherwise have been exported. A half-hour with no
reading gets no charging, so annual_ev_kwh is what was actually priced, and
the as-detected and night rows of one account can differ slightly for that
reason.

NOTHING HERE MODELS THE PANELS. Every half-hour solar_soak charges into is
one the export register recorded, and the cap is that register's own kWh.
Generation is not metered and is not reconstructed: design §15.2, "evidence,
not a breakdown".

For shift_detected the charging being moved is the detected LOW bound spread
evenly over the session's half-hours, never more than a half-hour imported,
and exactly what is taken off is put back at night. The low bound is what
the detection is sure of; the high bound also contains the household's own
evening use, which does not move.
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal
from typing import NamedTuple

import build_plan_recommendations as plans
import estimate_assumptions as A
import estimate_common as common

NZ = common.NZ
HALF_HOUR = dt.timedelta(minutes=30)

# The same-night 23:00 is worked out by the server, in local time, so a
# session on a daylight-saving day still moves to the wall clock's 23:00.
SESSIONS_SQL = f"""
select s.session_start,
       s.half_hours,
       s.kwh_low,
       (s.session_start at time zone '{NZ}')::time              as local_start,
       (((s.session_start at time zone '{NZ}')::date
         + %(night)s::time) at time zone '{NZ}')                 as night_start
from kea.mart_charging_session s
where s.icp_id = %(icp_id)s
  -- high_power, or a run the panels hid: detect-v2 withholds the class on a
  -- masked run rather than guessing it, and those are the very sessions a
  -- solar household wants advice about. ⚠️ NULL with no masking is not an EV
  -- claim at all and stays out.
  and (s.charger_class = 'high_power'
       or (s.charger_class is null and s.masked_half_hours > 0))
  and s.session_start >= %(lo)s and s.session_start < %(hi)s
order by s.session_start
"""


class Session(NamedTuple):
    start: dt.datetime
    half_hours: int
    kwh_low: float
    local_start: dt.time
    night_start: dt.datetime


# --------------------------------------------------------------- add_ev


def session_slots(kwh: float, charger_kw: float) -> list[float]:
    """A session's kWh per half-hour: full rate, then what is left."""
    per = charger_kw / 2
    slots = []
    left = kwh
    while left > 1e-9:
        slots.append(min(per, left))
        left -= slots[-1]
    return slots


def add_daily_charging(half_hours: list[common.HalfHour], start: dt.time,
                       slots: list[float]) -> tuple[common.Profile, float]:
    """(netted profile, kWh charged) with one session a day from `start`.

    Each real half-hour finds the session that covers it by its wall clock:
    the session that began on the local date `start` before it. So a session
    that runs past midnight charges the first half-hours of the next date,
    and the first date's early half-hours get the tail of the session that
    began the evening before the window. Every date's charging is then one
    whole session, so the kWh charged is days x the session.
    """
    start_min = start.hour * 60 + start.minute
    p = common.Profile()
    charged = 0.0
    for h in half_hours:
        since = h.loc - dt.timedelta(minutes=start_min)
        began = dt.datetime.combine(since.date(), dt.time()) + dt.timedelta(minutes=start_min)
        k = int((h.loc - began) / HALF_HOUR)
        ev = slots[k] if k < len(slots) else 0.0
        imp, exp, _ = charge_half_hour(h.import_kwh, h.export_kwh, ev)
        p.add(h.loc, imp, exported=False)
        p.add(h.loc, exp, exported=True)
        charged += ev
    return p, charged


def add_soak_charging(half_hours: list[common.HalfHour], start: dt.time,
                      slots: list[float]) -> tuple[common.Profile, float]:
    """(netted profile, kWh charged) with each day's session charged into the sun.

    The day's kWh goes into that NZ date's half-hours in the order the export
    register recorded them, most first, at most one slot's worth in any of
    them. Only half-hours that really exported are used -- charging at midday
    for its own sake is not the recommendation.

    What the array could not cover is charged from `start`, the same fallback
    a household without enough roof would actually use, so every pattern
    charges the same kWh a day and the bills compare. The fallback stays
    inside the local date; with a 3-half-hour session and an 18:00 start
    there are twelve half-hours of room before midnight.
    """
    per_cap = max(slots, default=0.0)
    daily = sum(slots)
    by_date: dict[dt.date, list[common.HalfHour]] = {}
    for h in half_hours:
        by_date.setdefault(h.loc.date(), []).append(h)

    add: dict[dt.datetime, float] = {}
    for day in by_date.values():
        left = daily
        for h in sorted(day, key=lambda h: (-h.export_kwh, h.at)):
            if left <= 1e-9 or h.export_kwh <= 0:
                break
            take = min(per_cap, left)
            add[h.at] = add.get(h.at, 0.0) + take
            left -= take
        for h in sorted(day, key=lambda h: h.loc):
            if left <= 1e-9:
                break
            if h.loc.time() < start:
                continue
            take = min(per_cap - add.get(h.at, 0.0), left)
            if take <= 1e-9:
                continue
            add[h.at] = add.get(h.at, 0.0) + take
            left -= take

    p = common.Profile()
    charged = 0.0
    for h in half_hours:
        ev = add.get(h.at, 0.0)
        imp, exp, _ = charge_half_hour(h.import_kwh, h.export_kwh, ev)
        p.add(h.loc, imp, exported=False)
        p.add(h.loc, exp, exported=True)
        charged += ev
    return p, charged


def charge_half_hour(import_kwh: float, export_kwh: float,
                     ev_kwh: float) -> tuple[float, float, float]:
    """(import, export, charged from export) for one real half-hour.

    Charging first uses what the house would have exported, then imports.
    """
    from_export = min(export_kwh, ev_kwh)
    return import_kwh + ev_kwh - from_export, export_kwh - from_export, from_export


# --------------------------------------------------------------- shift


def in_night(t: dt.time) -> bool:
    return t >= A.NIGHT_START or t < A.NIGHT_END


def removable_by_session(half_hours: list[common.HalfHour],
                         sessions: list[Session]) -> list[dict[dt.datetime, float]]:
    """kWh taken off each instant, one dict per session.

    Each session's kwh_low is spread evenly over its half-hours, but a
    half-hour never gives up more than it imported, and a half-hour with no
    reading gives up nothing. Sessions of one ICP never overlap (a dbt test).
    """
    imports = {h.at: h.import_kwh for h in half_hours}
    taken: dict[dt.datetime, float] = {}
    per_session = []
    for s in sessions:
        per = s.kwh_low / s.half_hours
        mine: dict[dt.datetime, float] = {}
        for k in range(s.half_hours):
            t = s.start + k * HALF_HOUR
            if t in imports:
                take = min(per, imports[t] - taken.get(t, 0.0))
                if take > 0:
                    mine[t] = take
                    taken[t] = taken.get(t, 0.0) + take
        per_session.append(mine)
    return per_session


def removable(half_hours: list[common.HalfHour],
              sessions: list[Session]) -> tuple[dict[dt.datetime, float], list[float]]:
    """(kWh taken off each instant, kWh taken off each session)."""
    per_session = removable_by_session(half_hours, sessions)
    taken: dict[dt.datetime, float] = {}
    for mine in per_session:
        for t, kwh in mine.items():
            taken[t] = taken.get(t, 0.0) + kwh
    return taken, [sum(mine.values()) for mine in per_session]


def shift_sessions(half_hours: list[common.HalfHour], sessions: list[Session],
                   *, to_night: bool | None
                   ) -> tuple[common.Profile, float]:
    """(netted profile, kWh charged) with the detected sessions changed.

    to_night None  -- every session removed: the household without charging
    to_night False -- as detected: the metered half-hours themselves
    to_night True  -- each daytime or evening session moved to its night start;
                      a session already in the night window stays as metered

    What a moved session gives up (`removable`) is exactly what is put back
    at night, spread evenly, before netting against export. So a flat plan's
    bill cannot change, and "kWh charged" is the same removable amount in
    every pattern, less any night half-hour that has no reading.
    """
    moving = {None: sessions, False: [],
              True: [s for s in sessions if not in_night(s.local_start)]}[to_night]
    remove, moved = removable(half_hours, moving)
    add: dict[dt.datetime, float] = {}
    if to_night:
        for s, kwh in zip(moving, moved, strict=True):
            for k in range(s.half_hours):
                t = s.night_start + k * HALF_HOUR
                add[t] = add.get(t, 0.0) + kwh / s.half_hours
    p = common.Profile()
    added = 0.0
    for h in half_hours:
        imp = h.import_kwh - remove.get(h.at, 0.0)
        ev = add.get(h.at, 0.0)
        imp, exp, _ = charge_half_hour(max(imp, 0.0), h.export_kwh, ev)  # float noise
        p.add(h.loc, imp, exported=False)
        p.add(h.loc, exp, exported=True)
        added += ev
    if to_night is None:
        return p, 0.0
    _, detected = removable(half_hours, sessions)
    if to_night is False:
        return p, sum(detected)
    staying = sum(kwh for s, kwh in zip(sessions, detected, strict=True)
                  if in_night(s.local_start))
    return p, staying + added


def soak_sessions(half_hours: list[common.HalfHour],
                  sessions: list[Session]) -> tuple[common.Profile, float]:
    """(netted profile, kWh charged) with each session moved into its own day's sun.

    A session's removable kWh goes into the half-hours of the SAME NZ date
    that the export register recorded, most first, and never more than that
    half-hour exported -- so every moved kWh displaces an export and none of
    it creates a new midday import. Whatever will not fit is put back exactly
    where the meter found it, in the proportions it was taken.

    The kWh charged is therefore the same as as_detected: nothing is dropped,
    only relocated, and the two bills differ only by where the charging sat.
    """
    per_session = removable_by_session(half_hours, sessions)
    remove: dict[dt.datetime, float] = {}
    for mine in per_session:
        for t, kwh in mine.items():
            remove[t] = remove.get(t, 0.0) + kwh

    by_date: dict[dt.date, list[common.HalfHour]] = {}
    at_date = {}
    for h in half_hours:
        by_date.setdefault(h.loc.date(), []).append(h)
        at_date[h.at] = h.loc.date()

    add: dict[dt.datetime, float] = {}
    for mine in per_session:
        total = sum(mine.values())
        if total <= 1e-9:
            continue
        date = next((at_date[t] for t in sorted(mine) if t in at_date), None)
        left = total
        for h in sorted(by_date.get(date, []), key=lambda h: (-h.export_kwh, h.at)):
            if left <= 1e-9 or h.export_kwh <= 0:
                break
            take = min(h.export_kwh - add.get(h.at, 0.0), left)
            if take <= 1e-9:
                continue
            add[h.at] = add.get(h.at, 0.0) + take
            left -= take
        if left > 1e-9:
            share = left / total
            for t, kwh in mine.items():
                add[t] = add.get(t, 0.0) + kwh * share

    p = common.Profile()
    charged = 0.0
    for h in half_hours:
        imp = max(h.import_kwh - remove.get(h.at, 0.0), 0.0)  # float noise
        ev = add.get(h.at, 0.0)
        imp, exp, _ = charge_half_hour(imp, h.export_kwh, ev)
        p.add(h.loc, imp, exported=False)
        p.add(h.loc, exp, exported=True)
        charged += ev
    return p, charged


def load_sessions(cur, icp_id: int, window: common.Window) -> list[Session]:
    cur.execute(SESSIONS_SQL, {"icp_id": icp_id, "lo": window.lo, "hi": window.hi,
                               "night": A.NIGHT_START})
    return [Session(start, int(n), float(kwh), local, night)
            for start, n, kwh, local, night in cur.fetchall()]


# --------------------------------------------------------------- rows


def scenario_rows(scenario: str, plan_ids: list[int], tariffs: dict,
                  without, patterns: list[tuple[str, tuple]]) -> list[dict]:
    """One dict per plan x pattern. The first pattern is the baseline.

    `without` is the profile with no charging; each pattern is a
    (profile, kWh charged) pair. Bills are rounded before they are compared.
    """
    out = []
    for pid in plan_ids:
        tariff = tariffs[pid]
        bill_without = common.annual_bill(without, tariff)
        bills = [(name, common.annual_bill(profile, tariff), kwh)
                 for name, (profile, kwh) in patterns]
        baseline = bills[0][1]
        for name, bill, kwh in bills:
            out.append(dict(
                scenario=scenario, plan_id=pid, charging_pattern=name,
                annual_ev_kwh=round(Decimal(str(kwh)), 1),
                annual_bill=bill,
                annual_ev_cost=bill - bill_without,
                saving=baseline - bill,
            ))
    return out


def time_of_use_plan(plan_ids: list[int], active: dict) -> int | None:
    tou = [p for p in plan_ids
           if active[p]["plan_code"].startswith(A.TIME_OF_USE_PREFIX)]
    return tou[0] if len(tou) == 1 else None


def main(argv: list[str] | None = None, *, connection=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="price but do not insert")
    ap.add_argument("--account", type=int, action="append",
                    help="restrict to these account_ids (repeatable)")
    args = ap.parse_args(argv)

    if connection is not None and args.dry_run:
        raise ValueError("dry_run cannot share a publication transaction")
    conn = connection if connection is not None else plans.connect_serving()
    if connection is None:
        conn.autocommit = False
    cur = conn.cursor()

    window = common.usage_window(cur)
    losses = plans.loss_factor(cur)
    active, tariffs = plans.load_tariffs(cur, window.end, losses)
    by_eligibility = plans.candidate_sets(active)
    accounts = common.load_accounts(cur)
    if args.account:
        accounts = [a for a in accounts if a["account_id"] in set(args.account)]
    slots = session_slots(float(A.annual_ev_kwh()) / common.WINDOW_DAYS,
                          float(A.CHARGER_KW))
    print(f"ev: window {window.start} .. {window.end}, {len(accounts)} open "
          f"accounts, {len(slots)} half-hour session a day, "
          f"assumptions {A.ASSUMPTIONS_VERSION}", flush=True)

    rows: list[tuple] = []
    skipped: list[str] = []
    shifted = soaking = 0
    for acct in accounts:
        aid, current = acct["account_id"], acct["plan_id"]
        if current not in active:
            skipped.append(f"account {aid}: plan {current} not active")
            continue
        half_hours = common.load_half_hours(cur, acct["icp_id"], window)
        if not common.covers_window(half_hours, window):
            skipped.append(f"account {aid}: {len(half_hours):,} half-hours read, "
                           f"under {A.MIN_WINDOW_COVERAGE:.0%} of the window")
            continue
        eligible = by_eligibility.get(
            (acct["network_id"], active[current]["user_type"]), [])

        without, _ = shift_sessions(half_hours, [], to_night=False)
        # Offered only where the meter itself exported in the window: with no
        # export there is nothing measured to soak into, and an estimated
        # array is exactly what this must not invent.
        exporter = any(h.export_kwh > 0 for h in half_hours)
        add_patterns = [
            ("evening", add_daily_charging(half_hours, A.EVENING_START, slots)),
            ("night", add_daily_charging(half_hours, A.NIGHT_START, slots)),
        ]
        if exporter:
            add_patterns.append(
                ("solar_soak", add_soak_charging(half_hours, A.EVENING_START, slots)))
            soaking += 1
        found = scenario_rows("add_ev", eligible, tariffs, without, add_patterns)

        sessions = load_sessions(cur, acct["icp_id"], window)
        if sessions:
            tou = time_of_use_plan(eligible, active)
            plan_ids = sorted({current} | ({tou} if tou is not None else set()))
            removed, _ = shift_sessions(half_hours, sessions, to_night=None)
            shift_patterns = [
                ("as_detected", shift_sessions(half_hours, sessions, to_night=False)),
                ("night", shift_sessions(half_hours, sessions, to_night=True)),
            ]
            if exporter:
                shift_patterns.append(
                    ("solar_soak", soak_sessions(half_hours, sessions)))
            found += scenario_rows("shift_detected", plan_ids, tariffs, removed,
                                   shift_patterns)
            shifted += 1

        rows.extend((
            aid, r["scenario"], r["plan_id"], current, r["charging_pattern"],
            window.start, window.end, r["annual_ev_kwh"], r["annual_bill"],
            r["annual_ev_cost"], r["saving"], A.ASSUMPTIONS_VERSION,
        ) for r in found)

    print(f"ev: {len(rows):,} scenario rows; {shifted} accounts have "
          f"charging to shift, {soaking} export and are offered solar_soak",
          flush=True)
    for s in skipped:
        print(f"  skipped {s}")

    if args.dry_run:
        conn.rollback()
        return 0

    from psycopg2.extras import execute_values
    execute_values(cur, """
        insert into kea.ev_scenario
            (account_id, scenario, plan_id, current_plan_id, charging_pattern,
             basis_from, basis_to, annual_ev_kwh, annual_bill_nzd,
             annual_ev_cost_nzd, saving_vs_baseline_nzd, assumptions_version)
        values %s""", rows, page_size=500)
    if connection is None:
        conn.commit()
    print(f"ev: inserted {len(rows):,} rows", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
