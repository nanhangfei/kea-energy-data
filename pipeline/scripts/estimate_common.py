"""What the solar and EV estimate jobs share: the window, real half-hours, and
the weekday x half-hour profile they are priced from.

WHY THE HALF-HOURS ARE REAL AND THE PROFILE COMES AFTER.

build_plan_recommendations.py sums a year into 7 x 48 cells in SQL, and that
is exact there because nothing changes a reading before it is priced. Here
something does: panels or a charger change each half-hour's import, and
whatever is left over is exported. That netting is not linear -- a sunny
Saturday noon exports while a cloudy one still imports -- so it has to happen
on each real half-hour, and only the netted import and export are summed
into cells. Summing first would let one week's export cancel another week's
import. Once netted, the cells are exact for the reason the recommender's
docstring gives: `cost_of` reads only the weekday and the clock.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import NamedTuple

import build_plan_recommendations as plans
import estimate_assumptions as A

NZ = plans.NZ
WINDOW_DAYS = plans.WINDOW_DAYS

# One row per real half-hour on the ICP, import and export summed over the
# registers of each direction. The UTC offset comes from the server so the
# script needs no time-zone database: `loc` is the wall clock, and loc minus
# the offset is the instant.
HALF_HOURS_SQL = f"""
select h.interval_start,
       h.interval_start at time zone '{NZ}' as loc,
       (extract(epoch from (h.interval_start at time zone '{NZ}')
                         - (h.interval_start at time zone 'UTC')) / 60)::int
                                            as utc_offset_min,
       coalesce(sum(h.kwh) filter (where r.direction = 'import'), 0) as import_kwh,
       coalesce(sum(h.kwh) filter (where r.direction = 'export'), 0) as export_kwh
from metering.meter_read_hh h
join metering.meter_register r on r.register_id = h.register_id
where h.icp_id = %(icp_id)s                 -- the primary key's lead column
  and h.interval_start >= %(lo)s and h.interval_start < %(hi)s
group by h.interval_start
order by h.interval_start
"""

# The recommender's account rule -- open, generated cohort, not quarantined --
# with what the estimates also need.
ACCOUNT_SQL = """
select a.account_id, a.plan_id, a.icp_id, i.network_id, c.climate_region,
       exists (select 1 from metering.meter_register r
               where r.icp_id = a.icp_id and r.direction = 'export') as has_export
from billing.account a
join metering.icp i on i.icp_id = a.icp_id
join kea.mart_account_context c on c.account_id = a.account_id
where a.end_date is null
  and i.icp_number like '09%%'
  and not c.is_quarantined
order by a.account_id
"""


class HalfHour(NamedTuple):
    at: dt.datetime          # the instant, as the driver returns it
    loc: dt.datetime         # local wall clock, naive
    utc_offset_min: int
    import_kwh: float
    export_kwh: float


class Window(NamedTuple):
    start: dt.date
    end: dt.date
    lo: dt.datetime
    hi: dt.datetime


def usage_window(cur) -> Window:
    """The recommender's year: WINDOW_DAYS local days to the newest reading.

    The same window, so an estimate and a recommendation made in one
    publication describe the same year of the same household.
    """
    cur.execute(plans.MAX_READ_SQL)
    end = cur.fetchone()[0]
    start = end - dt.timedelta(days=WINDOW_DAYS - 1)
    cur.execute(
        f"select (%s::date)::timestamp at time zone '{NZ}', "
        f"       ((%s::date + 1))::timestamp at time zone '{NZ}'",
        (start, end))
    lo, hi = cur.fetchone()
    return Window(start, end, lo, hi)


def load_accounts(cur) -> list[dict]:
    cur.execute(ACCOUNT_SQL)
    cols = ("account_id", "plan_id", "icp_id", "network_id", "climate_region",
            "has_export")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load_half_hours(cur, icp_id: int, window: Window) -> list[HalfHour]:
    cur.execute(HALF_HOURS_SQL, {"icp_id": icp_id, "lo": window.lo, "hi": window.hi})
    return [HalfHour(at, loc, int(off), float(imp), float(exp))
            for at, loc, off, imp, exp in cur.fetchall()]


def covers_window(half_hours: list[HalfHour], window: Window) -> bool:
    """Whether the readings cover enough of the window to call a year."""
    expected = (window.hi - window.lo) / dt.timedelta(minutes=30)
    return len(half_hours) >= A.MIN_WINDOW_COVERAGE * expected


def minute_of_day(t: dt.datetime) -> int:
    return t.hour * 60 + t.minute


class Profile:
    """Netted kWh summed into weekday x half-hour x direction cells."""

    def __init__(self) -> None:
        self.cells: dict[tuple[int, int, bool], float] = {}

    def add(self, loc: dt.datetime, kwh: float, *, exported: bool) -> None:
        if kwh < 0:
            raise ValueError(f"negative {'export' if exported else 'import'} "
                             f"{kwh} at {loc} -- netting went wrong")
        if kwh == 0:
            return
        key = (loc.isoweekday(), minute_of_day(loc) // 30 * 30, exported)
        self.cells[key] = self.cells.get(key, 0.0) + kwh

    def total(self, *, exported: bool) -> float:
        return sum(v for (_, _, e), v in self.cells.items() if e == exported)

    def readings(self) -> list:
        """`Reading`s at the recommender's representative week (2024-01-01
        is a Monday)."""
        return [plans.Reading(at=dt.datetime(2024, 1, dow, minute // 60, minute % 60),
                              kwh=Decimal(str(round(kwh, 6))), exported=exported)
                for (dow, minute, exported), kwh in sorted(self.cells.items())]


def annual_bill(profile: Profile, tariff, days: int = WINDOW_DAYS) -> Decimal:
    """The GST-inclusive modelled bill, to the cent, as a customer sees it.

    Rounded before any subtraction, so a difference of two stored bills is
    exactly the stored difference (the recommender's bill-evidence rule).
    """
    return round(plans.customer_amount(
        plans.cost_of(profile.readings(), tariff, days).total), 2)
