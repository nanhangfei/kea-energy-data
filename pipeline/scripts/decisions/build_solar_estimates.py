"""Estimate rooftop-solar savings and payback, and write kea.solar_estimate.

    python3 build_solar_estimates.py --dry-run    # estimate everything, write nothing
    python3 build_solar_estimates.py              # estimate and insert

Sprint 4 item B. One row per account per system size (3, 5 and 7 kW) and
battery size (none, and 13.5 kWh), for open generated-cohort accounts with no
export register -- a household that already exports has panels, and "what
would panels save" is not its question.

⚠️ APPEND-ONLY, AND NEVER IN `serving.TABLES` -- the recommender's argument.
`computed_at` is left to the column default, so every row one publication
writes carries its transaction's time; publish_serving.validate() uses that.

HOW ONE ESTIMATE IS MADE.

  1. The account's real half-hours over the recommender's year
     (estimate_common.usage_window).
  2. A day's output is the yield table's kWh per kW for that region and
     month, times the system size, spread over the daylight half-hours along
     a half-sine between NOAA sunrise and sunset. Done twice: with the low
     and the high yield.
  3. Each real half-hour is netted on its own: panels first cover that
     half-hour's import, and only the rest is exported. On a battery row the
     surplus charges the battery before any of it is exported, and import
     the panels left is met from the battery (see THE BATTERY below). Then
     the netted import and export are summed into weekday x half-hour cells
     (see estimate_common for why that order is the whole point).
  4. Today's profile and the solar profile are priced on the account's
     current plan with the recommender's tariffs -- `tariff.py`, the same
     line losses, the plan's own buy-back rate -- and the difference is the
     saving, GST-inclusive.
  5. Payback is simple: installed cost / annual saving. The best case is the
     low cost over the high saving, the worst case the high cost over the
     low saving. On a battery row the cost is panels plus battery. No panel
     or battery degradation, no price change; the answer must say so.

THE BATTERY. battery_kwh = 0 is panels alone, and is figure for figure what
this job wrote before it knew about batteries. Above zero, the half-hours are
walked in time order with a state of charge:

  * surplus charges the battery, up to its power for half an hour and its
    free capacity; the round-trip loss is taken on the way in;
  * import is met from the battery, up to its power and what it holds;
  * a gap in the readings empties it, so no energy is carried across
    half-hours nobody metered;
  * it is NEVER charged from the grid. On Time of Use that leaves the cheap
    night rate unused, and the answer must say so.

⚠️ A BATTERY ROW IS NOT ALWAYS THE BETTER ROW. It saves more kWh of import
and costs far more, and where a plan's buy-back is close to the rate the
stored energy avoids, the round-trip loss can make it save LESS than panels
alone. Such a row is published, not suppressed: compare it with its
battery_kwh = 0 sibling.

Generation is counted on the metered half-hours only, so a year with gaps
reports a little less generation along with the saving it priced. That keeps
the three figures of one row about the same half-hours. An account with less
than MIN_WINDOW_COVERAGE of the year read gets no estimate at all.

All assumptions are in estimate_assumptions.py; every row names its version.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
from decimal import Decimal

import build_plan_recommendations as plans
import estimate_assumptions as A
import estimate_common as common

HALF_HOUR = dt.timedelta(minutes=30)

# --------------------------------------------------------------- the sun


def sun_times(day: dt.date, lat: float, lon: float,
              utc_offset_min: int) -> tuple[float, float]:
    """Sunrise and sunset, in local minutes after midnight.

    NOAA's general solar position equations (fractional year, equation of
    time, declination), with the standard 90.833 degree zenith for refraction
    and the sun's radius. Latitude south and longitude east are negative and
    positive, so Auckland is (-37.0, 174.8). Minutes can fall outside 0-1440
    at extreme latitudes; New Zealand never gets there.
    """
    g = 2 * math.pi / 365 * (day.timetuple().tm_yday - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    phi = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl))
              - math.tan(phi) * math.tan(decl))
    ha = math.degrees(math.acos(max(-1.0, min(1.0, cos_ha))))
    rise = 720 - 4 * (lon + ha) - eqtime + utc_offset_min
    sets = 720 - 4 * (lon - ha) - eqtime + utc_offset_min
    return rise, sets


def daylight_share(rise: float, sets: float, start_minute: int,
                   minutes: int = 30) -> float:
    """The share of a day's output produced in [start, start + minutes).

    Output follows sin(pi * (t - rise) / day length), whose integral from
    sunrise is (1 - cos(pi * elapsed / day length)) / 2 of the day's total.
    The shares of a day's half-hours therefore add up to exactly 1.
    """
    length = sets - rise
    if length <= 0:
        return 0.0

    def done(t: float) -> float:
        x = min(max(t - rise, 0.0), length)
        return (1 - math.cos(math.pi * x / length)) / 2

    return done(start_minute + minutes) - done(start_minute)


# --------------------------------------------------------------- netting


def net_half_hour(import_kwh: float, export_kwh: float,
                  generation_kwh: float) -> tuple[float, float, float]:
    """(import, export, self-used) for one real half-hour with panels.

    The panels first cover what the home is drawing, then export the rest.
    Import can fall to zero and never below it.
    """
    self_used = min(import_kwh, generation_kwh)
    return (import_kwh - self_used,
            export_kwh + generation_kwh - self_used,
            self_used)


def battery_half_hour(import_kwh: float, surplus_kwh: float, stored_kwh: float,
                      capacity_kwh: float) -> tuple[float, float, float, float]:
    """(import, surplus, stored, discharged) after the battery's half-hour.

    `import_kwh` and `surplus_kwh` are what net_half_hour left, so at most one
    of them is above zero. `stored_kwh` is energy the home can draw: the
    round-trip loss is taken as the surplus goes in.
    """
    step = float(A.BATTERY_POWER_KW) / 2
    efficiency = float(A.BATTERY_ROUND_TRIP)
    taken = min(surplus_kwh, step, max(capacity_kwh - stored_kwh, 0.0) / efficiency)
    stored_kwh += taken * efficiency
    discharged = min(import_kwh, step, stored_kwh)
    return (import_kwh - discharged, surplus_kwh - taken,
            stored_kwh - discharged, discharged)


def base_profile(half_hours: list[common.HalfHour]) -> common.Profile:
    p = common.Profile()
    for h in half_hours:
        p.add(h.loc, h.import_kwh, exported=False)
        p.add(h.loc, h.export_kwh, exported=True)
    return p


def solar_shares(half_hours: list[common.HalfHour], lat: float,
                 lon: float) -> list[float]:
    """Each real half-hour's share of its day's output. The same for every
    size and yield, so worked out once per account."""
    cache: dict[tuple[dt.date, int], tuple[float, float]] = {}
    out = []
    for h in half_hours:
        key = (h.loc.date(), h.utc_offset_min)
        if key not in cache:
            cache[key] = sun_times(key[0], lat, lon, key[1])
        rise, sets = cache[key]
        out.append(daylight_share(rise, sets, common.minute_of_day(h.loc)))
    return out


def simulate(half_hours: list[common.HalfHour], shares: list[float],
             kwh_per_kw_day: dict[int, float], system_kw: float,
             battery_kwh: float = 0.0) -> tuple[common.Profile, float, float]:
    """(netted profile, generation kWh, self-used kWh) for one size and yield.

    Self-used is what the home drew from the panels directly plus, with a
    battery, what it drew from the battery. `half_hours` must be in time
    order: the battery's state of charge runs through them.
    """
    p = common.Profile()
    generated = self_used = stored = 0.0
    previous = None
    for h, share in zip(half_hours, shares, strict=True):
        g = kwh_per_kw_day[h.loc.month] * system_kw * share
        imp, exp, used = net_half_hour(h.import_kwh, h.export_kwh, g)
        if battery_kwh:
            if previous is not None and h.at - previous != HALF_HOUR:
                stored = 0.0
            previous = h.at
            imp, surplus, stored, discharged = battery_half_hour(
                imp, g - used, stored, battery_kwh)
            exp = h.export_kwh + surplus
            used += discharged
        p.add(h.loc, imp, exported=False)
        p.add(h.loc, exp, exported=True)
        generated += g
        self_used += used
    return p, generated, self_used


def payback_range(cost_low: Decimal, cost_high: Decimal, saving_low: Decimal,
                  saving_high: Decimal) -> tuple[Decimal | None, Decimal | None]:
    """(best, worst) simple payback in years, to 0.1.

    None where the saving it divides by is not positive: there is no payback
    to state. If even the best case has none, neither has the worst.
    """
    if saving_high <= 0:
        return None, None
    best = round(cost_low / saving_high, 1)
    worst = round(cost_high / saving_low, 1) if saving_low > 0 else None
    return best, worst


def self_used_pct(self_used: float, generated: float) -> Decimal:
    if generated <= 0:
        return Decimal("0")
    return round(Decimal(str(self_used / generated * 100)), 2)


# --------------------------------------------------------------- inputs


def load_yields(path=A.SOLAR_YIELD_CSV) -> dict[str, dict]:
    """region -> {'low': {month: kWh}, 'high': {...}, 'lat', 'lon'}."""
    out: dict[str, dict] = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            region = out.setdefault(r["weather_region"], {
                "low": {}, "high": {},
                "lat": float(r["latitude"]), "lon": float(r["longitude"])})
            month = int(r["month"])
            region["low"][month] = float(r["kwh_per_kw_day_low"])
            region["high"][month] = float(r["kwh_per_kw_day_high"])
    for name, region in out.items():
        for bound in ("low", "high"):
            if sorted(region[bound]) != list(range(1, 13)):
                raise SystemExit(f"{path}: {name} does not have all 12 months")
        if any(region["low"][m] > region["high"][m] for m in range(1, 13)):
            raise SystemExit(f"{path}: {name} has a low above its high")
    return out


def load_region_map(path=A.REGION_SEED_CSV) -> dict[str, str]:
    with open(path, newline="") as f:
        return {r["climate_region"]: r["weather_region"] for r in csv.DictReader(f)}


def weather_region(climate_region: str | None, region_map: dict[str, str],
                   yields: dict) -> str | None:
    """dim_icp's rule: the seed's region, else the climate region itself."""
    if climate_region is None:
        return None
    region = region_map.get(climate_region, climate_region)
    return region if region in yields else None


# --------------------------------------------------------------- main


def install_cost(system_kw: Decimal, battery_kwh: Decimal) -> tuple[Decimal, Decimal]:
    """(low, high) installed cost: the panels, plus the battery if there is one."""
    low, high = (per_kw * system_kw for per_kw in A.INSTALL_COST_NZD_PER_KW)
    if battery_kwh:
        low, high = low + A.BATTERY_COST_NZD[0], high + A.BATTERY_COST_NZD[1]
    return low, high


def estimate_account(half_hours, tariff, region: dict,
                     system_kws=A.SYSTEM_SIZES_KW,
                     battery_kwhs=A.BATTERY_SIZES_KWH) -> list[dict]:
    """One dict per system size and battery size; pure given its inputs."""
    today = common.annual_bill(base_profile(half_hours), tariff)
    shares = solar_shares(half_hours, region["lat"], region["lon"])
    out = []
    for kw, battery in ((k, b) for k in system_kws for b in battery_kwhs):
        runs = {}
        for bound in ("low", "high"):
            profile, generated, used = simulate(half_hours, shares, region[bound],
                                                float(kw), float(battery))
            runs[bound] = (today - common.annual_bill(profile, tariff),
                           generated, used)
        savings = sorted(r[0] for r in runs.values())
        pcts = sorted(self_used_pct(r[2], r[1]) for r in runs.values())
        cost_low, cost_high = install_cost(kw, battery)
        best, worst = payback_range(cost_low, cost_high, savings[0], savings[1])
        out.append(dict(
            system_kw=kw, battery_kwh=battery,
            generation_low=round(Decimal(str(runs["low"][1])), 1),
            generation_high=round(Decimal(str(runs["high"][1])), 1),
            self_used_low=pcts[0], self_used_high=pcts[1],
            saving_low=savings[0], saving_high=savings[1],
            cost_low=round(cost_low, 2), cost_high=round(cost_high, 2),
            payback_low=best, payback_high=worst,
        ))
    return out


def main(argv: list[str] | None = None, *, connection=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="estimate but do not insert")
    ap.add_argument("--account", type=int, action="append",
                    help="restrict to these account_ids (repeatable)")
    args = ap.parse_args(argv)

    if connection is not None and args.dry_run:
        raise ValueError("dry_run cannot share a publication transaction")
    conn = connection if connection is not None else plans.connect_serving()
    if connection is None:
        conn.autocommit = False
    cur = conn.cursor()

    yields = load_yields()
    region_map = load_region_map()
    window = common.usage_window(cur)
    losses = plans.loss_factor(cur)
    active, tariffs = plans.load_tariffs(cur, window.end, losses)
    accounts = common.load_accounts(cur)
    if args.account:
        accounts = [a for a in accounts if a["account_id"] in set(args.account)]
    print(f"solar: window {window.start} .. {window.end}, {len(accounts)} open "
          f"accounts, assumptions {A.ASSUMPTIONS_VERSION}", flush=True)

    rows: list[tuple] = []
    skipped: list[str] = []
    for acct in accounts:
        aid = acct["account_id"]
        if acct["has_export"]:
            skipped.append(f"account {aid}: already exports")
            continue
        if acct["plan_id"] not in active:
            skipped.append(f"account {aid}: plan {acct['plan_id']} not active")
            continue
        region_name = weather_region(acct["climate_region"], region_map, yields)
        if region_name is None:
            skipped.append(f"account {aid}: no yield for region "
                           f"{acct['climate_region']!r}")
            continue
        half_hours = common.load_half_hours(cur, acct["icp_id"], window)
        if not common.covers_window(half_hours, window):
            skipped.append(f"account {aid}: {len(half_hours):,} half-hours read, "
                           f"under {A.MIN_WINDOW_COVERAGE:.0%} of the window")
            continue
        for e in estimate_account(half_hours, tariffs[acct["plan_id"]],
                                  yields[region_name]):
            rows.append((
                aid, acct["plan_id"], e["system_kw"], e["battery_kwh"], region_name,
                window.start, window.end,
                e["generation_low"], e["generation_high"],
                e["self_used_low"], e["self_used_high"],
                e["saving_low"], e["saving_high"],
                e["cost_low"], e["cost_high"],
                e["payback_low"], e["payback_high"],
                A.ASSUMPTIONS_VERSION,
            ))

    print(f"solar: {len(rows):,} estimates for "
          f"{len({r[0] for r in rows}):,} accounts", flush=True)
    for s in skipped:
        print(f"  skipped {s}")

    if args.dry_run:
        conn.rollback()
        return 0

    from psycopg2.extras import execute_values
    execute_values(cur, """
        insert into kea.solar_estimate
            (account_id, plan_id, system_kw, battery_kwh, weather_region,
             basis_from, basis_to,
             annual_generation_kwh_low, annual_generation_kwh_high,
             self_used_pct_low, self_used_pct_high,
             annual_saving_nzd_low, annual_saving_nzd_high,
             install_cost_nzd_low, install_cost_nzd_high,
             payback_years_low, payback_years_high, assumptions_version)
        values %s""", rows, page_size=500)
    if connection is None:
        conn.commit()
    print(f"solar: inserted {len(rows):,} rows", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
