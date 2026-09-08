"""Map Low Carbon London half-hourly readings onto a New Zealand calendar.

LCL is London data: ~5,567 households, Nov 2011 - Feb 2014. Kea is a New
Zealand retailer, and the serving schema derives every date through
`Pacific/Auckland`. Two things therefore have to change, and only two:

LCL's `tstp` is UTC - established empirically, not assumed; see probe_tstp.py.
So there are two conversions, not one: UTC to a London wall clock, then that
wall clock to an Auckland instant.

  1. WHEN in the year a reading sits. UK winter must land in NZ winter, or the
     bill explainer's weather driver reads backwards - a cold snap would show
     up in January.
  2. WHICH instant a local wall clock corresponds to. NZ daylight saving moves
     on different dates from the UK's, so a local day here is 46, 48 or 50
     half-hours, and never the same days as in London.

What does NOT change is the wall clock itself. A reading taken at 18:30 London
local becomes a reading at 18:30 Auckland local. People cook dinner at dinner
time in both hemispheres, so preserving the clock preserves the load shape,
which is the whole reason for using real data rather than a simulation.

The shift is therefore a whole number of DAYS. Adding hours would smear the
evening peak across the wrong part of the day.

Stdlib only, matching data/sample/generate_scenarios.py.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
NZ = ZoneInfo("Pacific/Auckland")

HALF_HOUR = dt.timedelta(minutes=30)
MEAN_TROPICAL_YEAR = 365.2425

#: The LCL trial's span, as published.
LCL_PUBLISHED_START = dt.date(2011, 11, 23)
LCL_END = dt.date(2014, 2, 28)

#: The span we actually use. LCL recruited in waves through 2012-13, so the
#: published start is not the start of a usable panel: only 77 households hold
#: 99% of the 829 days. Starting later trades window length for panel size, and
#: the window has to keep three things:
#:
#:   * all four NZ DST transitions (2025-09-28, 2026-04-05, 2026-09-27,
#:     2027-04-04), so every household gets two 46-hour days AND two 50-hour
#:     days rather than one of each;
#:   * two NZ winters and two summers, so the season-flip assertion has
#:     something on both sides;
#:   * A FULL YEAR BEFORE THE FIRST BILL ANYONE WILL LOOK AT. See below.
#:
#: MOVED 2026-09-04, from 2012-03-21 (which put the NZ span at 2025-09-17).
#: The third requirement is the one that moved it, and it is not about having
#: "enough" data -- it is about mart_forecast_input.same_period_last_year_kwh,
#: which is a plain SUM over the year-ago window with NO guard for a window
#: that is only partly covered:
#:
#:     sum(u.import_kwh) ... where u.usage_date between
#:         dateadd(year, -1, s.period_start) and dateadd(year, -1, s.period_end)
#:
#: A half-covered window does not come back NULL. It comes back HALF, so this
#: year reads as a doubling -- a fabricated bill shock, on every household at
#: once, in the one product feature whose whole job is to tell a real one from
#: a false alarm. With the span starting 2025-09-17 the September-2026 bills
#: would have covered 14 of 30 year-ago days and done exactly that.
#:
#: 2025-08-01 is a MONTH BOUNDARY, and that is the whole point: bill periods
#: are monthly, so a start mid-month leaves the first month's year-ago window
#: part-covered and distorted. Every bill from August 2026 on now has a
#: complete year-ago window.
#:
#: The cost is panel size. Computed off cohort.py's coverage.json cache, so
#: these are exact rather than the 28-day steps --report-windows prints:
#:
#:     UK start      days    NZ start     >=99%
#:     2012-01-27     764  2025-07-25       545
#:     2012-02-03     757  2025-08-01       609   <- here
#:     2012-02-10     750  2025-08-08       643
#:     2012-02-15     745  2025-08-13       680
#:     2012-03-21     710  2025-09-17      1083   <- the old pin
#:
#: 609 against a cohort of 500 is the margin. Going earlier buys nothing a
#: monthly bill can use until 2025-07-01, by which point the panel is short.
LCL_START = dt.date(2012, 2, 3)

#: Where the NZ span ends. PINNED ON PURPOSE - do not replace with
#: date.today(). The shift is derived from this, so a moving anchor would give
#: a different shift every day: the cohort, every timestamp, every assertion
#: and every `received_at` would drift, and re-running would not reproduce.
#: Bump it deliberately when the span should move.
#:
#: MOVED 2026-09-04, from 2026-08-28 (+364 days, 52 whole weeks). The old pin
#: put the newest reading in the past, so the pipeline could not produce a
#: fresh day however often it ran. This one reaches ~11.7 months past today,
#: which is what lets the nightly extracts find real work and the Stage 9
#: replay carry TODAY'S readings with no timestamp rewriting anywhere.
#:
#: ⚠️ ONLY FOUR END DATES ARE LEGAL IN A TWO-YEAR RANGE, and December is not
#: among them. derive_shift_days() below asserts whole weeks AND a season flip,
#: and the second one is what rules out the obvious choices - enumerated, not
#: guessed:
#:
#:     2026-08-28  shift 4564  frac=0.4958  OK   <- the old pin
#:     2026-09-04  shift 4571  frac=0.5150  OK   <- lands on today, no runway
#:     2027-08-27  shift 4928  frac=0.4924  OK   <- this one
#:     2027-09-03  shift 4935  frac=0.5116  OK
#:
#:     2026-11-01  frac=0.6738  REJECT       2026-12-01  frac=0.7559  REJECT
#:     2027-02-26  frac=0.9941  REJECT       2026-12-31  frac=0.8380  REJECT
#:
#: The shift has to be roughly N years PLUS HALF A YEAR, or UK winter stops
#: landing in NZ winter. Moving the end by three months moves the seasons by
#: three months, which is the exact thing the assertion exists to catch. So the
#: next bump is +364 again (2028-08-25), not "a few months on".
NZ_SPAN_END = dt.date(2027, 8, 27)


class ShiftError(ValueError):
    """The derived shift fails one of the three properties it must have."""


def derive_shift_days(
    uk_end: dt.date = LCL_END,
    nz_end: dt.date = NZ_SPAN_END,
) -> int:
    """Days to add to a UK date to get its NZ counterpart.

    Derived rather than hardcoded, and asserted, so that changing the span
    cannot silently produce a shift that breaks one of the three properties
    the rest of the pipeline depends on:

    * a whole number of WEEKS, so Monday stays Monday. Load shape is strongly
      weekday/weekend dependent, `mart_hourly_profile` keys on `is_weekday`,
      and TOU windows are day-of-week scoped via `days_csv`.
    * close to half a year, so the seasons flip. This is the point.
    * lands on the pinned end date.
    """
    shift = (nz_end - uk_end).days

    if shift <= 0:
        raise ShiftError(f"shift must move forwards, got {shift} days")

    if shift % 7:
        raise ShiftError(
            f"shift of {shift} days is not a whole number of weeks "
            f"({shift % 7} days over); day-of-week would not survive"
        )

    frac = (shift / MEAN_TROPICAL_YEAR) % 1
    if not 0.48 <= frac <= 0.52:
        raise ShiftError(
            f"shift of {shift} days is {frac:.4f} of a year past a whole year; "
            f"the seasons would not flip (need 0.48-0.52)"
        )

    return shift


def uk_wall_clock(tstp: dt.datetime) -> tuple[dt.date, dt.time]:
    """A raw LCL `tstp` as a London wall clock.

    `tstp` is UTC. That was settled from the data rather than assumed - see
    probe_tstp.py and data/dictionary/lcl-source-data.md - because published
    descriptions of the dataset disagree and everything here depends on it.

    Converting to Europe/London is what produces the UK 46/50-hour days: they
    are not present in the raw file, where every date holds 48 readings.
    """
    if tstp.tzinfo is None:
        tstp = tstp.replace(tzinfo=dt.timezone.utc)
    local = tstp.astimezone(UK)
    return local.date(), local.time()


def uk_absent_wall_clocks(uk_date: dt.date) -> list[dt.time]:
    """Nominal half-hour wall clocks that do not exist in London on this date.

    Empty on all but the last Sunday in March, where 01:00 and 01:30 are
    skipped as GMT becomes BST.

    This matters because the shift is a whole number of days, so a UK
    spring-forward day maps onto some NZ day whose grid still asks for those
    two wall clocks. Nothing else notices: the result is simply a day two
    readings short, indistinguishable from a data gap, on exactly two days of
    the span for every household at once.
    """
    start = _utc_midnight(uk_date, UK)
    end = _utc_midnight(uk_date + dt.timedelta(days=1), UK)
    n = int((end - start).total_seconds() // HALF_HOUR.total_seconds())
    present = {(start + i * HALF_HOUR).astimezone(UK).time() for i in range(n)}
    nominal = [dt.time(h, m) for h in range(24) for m in (0, 30)]
    return [t for t in nominal if t not in present]


def nz_local_date_for(uk_date: dt.date, shift: int) -> dt.date:
    """The NZ calendar date a UK date maps to."""
    return uk_date + dt.timedelta(days=shift)


def uk_local_date_for(nz_date: dt.date, shift: int) -> dt.date:
    """The UK calendar date an NZ date came from. The inverse."""
    return nz_date - dt.timedelta(days=shift)


def _utc_midnight(day: dt.date, zone: ZoneInfo) -> dt.datetime:
    """Local midnight in `zone`, as an absolute instant."""
    return dt.datetime.combine(day, dt.time(0, 0), tzinfo=zone).astimezone(dt.timezone.utc)


def _nz_midnight_utc(nz_date: dt.date) -> dt.datetime:
    """Local midnight in Auckland, as an absolute instant.

    Everything below does its arithmetic in UTC, and that is not fussiness.
    Python's datetime arithmetic on two aware values that share a tzinfo is
    WALL-CLOCK arithmetic, not elapsed time: subtracting consecutive local
    midnights returns exactly 1 day even across a transition, and adding a
    timedelta steps the wall clock rather than the instant. Both silently give
    48 half-hours for every day, which is plausible enough to ship. Converting
    to UTC first - where there is no daylight saving to absorb - is what makes
    the 46 and the 50 appear.
    """
    return _utc_midnight(nz_date, NZ)


def nz_day_length_half_hours(nz_date: dt.date) -> int:
    """46, 48 or 50. Derived from the zone, never assumed.

    A local day in Auckland is 23 hours when daylight saving starts (last
    Sunday in September) and 25 hours when it ends (first Sunday in April).
    """
    elapsed = _nz_midnight_utc(nz_date + dt.timedelta(days=1)) - _nz_midnight_utc(nz_date)
    return int(elapsed.total_seconds() // HALF_HOUR.total_seconds())


def nz_half_hour_grid(nz_date: dt.date) -> list[dt.datetime]:
    """Every half-hour instant in one NZ local day, in order.

    Built forward from local midnight in ABSOLUTE time, which is what makes
    the 46/50 fall out by construction. Localising each of 48 wall clocks
    independently does not work: on the spring-forward day local 02:00 and
    02:30 do not exist, and on the fall-back day they exist twice - so you get
    48 rows with a hole and a collision instead of 46 and 50.

    This mirrors the `generate_series(... AT TIME ZONE 'Pacific/Auckland')`
    idiom already used in data/sample/generate_scenarios.py.
    """
    start = _nz_midnight_utc(nz_date)
    n = nz_day_length_half_hours(nz_date)
    return [(start + i * HALF_HOUR).astimezone(NZ) for i in range(n)]


def uk_lookup_key(nz_instant: dt.datetime, shift: int) -> tuple[dt.date, dt.time]:
    """The UK (date, wall-clock time) whose reading fills this NZ instant.

    Reads the NZ instant's own local wall clock and steps back `shift` days.
    Because the wall clock is preserved, an 18:30 slot asks for an 18:30
    reading.

    The two daylight-saving edges resolve themselves, which is the payoff of
    iterating the NZ grid rather than the UK one:

    * 46-hour day: no NZ slot ever has a 02:00 or 02:30 wall clock, so those
      two UK readings are never asked for, and are dropped.
    * 50-hour day: two NZ slots share the 02:00 wall clock (once in NZDT, once
      in NZST), so both ask for the same UK reading, and it is duplicated.

    Duplicating is deliberate. Do NOT rescale the day to conserve energy: a
    household living through a 25-hour day genuinely uses about 4% more, and
    preserving the per-interval level keeps interval_count equal to the
    expected count without a fudge factor.
    """
    local = nz_instant.astimezone(NZ)
    return local.date() - dt.timedelta(days=shift), local.time()


def nz_day_from_uk_readings(
    nz_date: dt.date,
    uk_day_readings: dict[dt.time, float],
    shift: int,
) -> list[tuple[dt.datetime, float | None]]:
    """Fill one NZ local day from a UK day keyed by wall-clock time.

    Returns one entry per NZ half-hour. The value is None where the UK day has
    no reading at that wall clock, which happens on a UK daylight-saving gap
    and wherever the source data is simply missing. The caller decides whether
    that becomes an estimate or a hole - this function does not invent data.
    """
    out: list[tuple[dt.datetime, float | None]] = []
    for instant in nz_half_hour_grid(nz_date):
        _, wall = uk_lookup_key(instant, shift)
        out.append((instant, uk_day_readings.get(wall)))
    return out


if __name__ == "__main__":  # a quick look at what the constants produce
    shift = derive_shift_days()
    nz_start = nz_local_date_for(LCL_START, shift)
    print(f"shift            {shift} days ({shift / 7:.0f} weeks, "
          f"{shift / MEAN_TROPICAL_YEAR:.4f} years)")
    print(f"UK span          {LCL_START} .. {LCL_END}")
    print(f"NZ span          {nz_start} .. {NZ_SPAN_END}")
    print(f"weekday survives {LCL_START.strftime('%a')} -> {nz_start.strftime('%a')}")
    print("\nNZ days that are not 48 half-hours in the span:")
    d, n = nz_start, 0
    while d <= NZ_SPAN_END:
        length = nz_day_length_half_hours(d)
        if length != 48:
            print(f"  {d}  {d.strftime('%a')}  {length} half-hours")
            n += 1
        d += dt.timedelta(days=1)
    print(f"  ({n} of them)")
