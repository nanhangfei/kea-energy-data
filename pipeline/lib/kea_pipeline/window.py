"""The extract window: which rows belong to which load date.

This module is small and it is the whole of Stage 3's correctness argument.
Everything else in the extract is plumbing; this is the part that is wrong
SILENTLY when it is wrong, because a bad window still returns thousands of
plausible rows and every downstream number still adds up.

Three decisions are encoded here, each of which was measured against the real
source data rather than assumed.

1. A LOAD DATE IS A NEW ZEALAND CALENDAR DAY, NOT A UTC ONE.

   The planted late revision that Stage 3 exists to find arrives at
   `2026-08-24 21:15 UTC`, which is `2026-08-25 09:15` in Pacific/Auckland.
   Measured on the live source:

       window for load_date=2026-08-25     rows    planted revision
       UTC day                            26,623                  0
       NZ day                             26,925                 48

   A UTC window misses it entirely and reports 26,623 perfectly reasonable
   rows. The exit criterion would fail with nothing to show for it. Every
   date in this platform is a Pacific/Auckland date -- the serving schema
   derives them that way and the bills are printed that way -- and the load
   date is no exception.

2. THE WINDOW IS A PURE FUNCTION OF THE LOAD DATE. NO CLOCK, NO STORED STATE.

   0.06% of rows in the source (12,176 of them) carry a `received_at` in the
   FUTURE, out to 2026-10-11, because a revision lands 3-45 days after the
   reading it corrects. That is not a defect; it is what a revision backlog
   looks like. But it means:

     * `... AND received_at <= now()` returns a different row set every time
       the clock advances. Re-running a load date is then not a no-op, and the
       counts go UP, which reads like data arriving rather than like a bug.
     * `... > (SELECT stored_watermark)` is worse: the window stops being a
       function of the load date at all, so replaying 2026-08-25 after
       2026-08-26 has run returns nothing, and a backfill needs a second code
       path -- which design 11 explicitly forbids.

   So: literal bounds, derived from the date and nothing else. A future-dated
   row is not clamped and not skipped; it simply belongs to a future load
   date, and it will be extracted when that date runs. `window_sql()` refuses
   to emit a volatile function, and there is a test that greps for one.

3. THE WINDOW IS HALF-OPEN: [midnight D, midnight D+1).

   So consecutive load dates partition the timeline exactly -- no instant in
   two windows, none in zero. That is what makes "the sum of the partitions
   equals the source" checkable, which is the strongest assertion in the gate.

A NEW ZEALAND DAY IS 23, 24 OR 25 HOURS, and this module must never assume
otherwise -- the same rule Stage 2 lived by when a local day turned out to be
46, 48 or 50 half-hours. NZ daylight saving moves at 2am/3am, so local
midnight itself is never skipped or repeated, which is why the boundaries are
well defined at all.

`hi` is therefore built by localising the NEXT DATE's midnight, never by
adding 24 hours to `lo`. Be precise about why, because the neighbouring claim
is false and repeating it would mislead: localising midnight and adding a
wall-clock day both land on the right instant, since astimezone() re-resolves
the offset. What is wrong is assuming the ELAPSED length -- `lo + 24h` misses
`hi` by an hour on the two transition days each year, silently dropping or
double-counting an hour of arrivals.

The wall-clock trap proper bites one level down, in `hourly_predicates`:
stepping an Auckland-aware datetime by an hour at a time yields 24 slots on
every day, because addition adds to the naive part and keeps the tzinfo. The
slots here are stepped over UTC instants for exactly that reason, and the
count comes out 23, 24 or 25. That is the shape of the bug that made every
generated day 48 half-hours in Stage 2.

Stdlib only, so this can be proved with no database, no lake, no Spark and no
dataset -- which is the point of doing it first.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

NZ = ZoneInfo("Pacific/Auckland")
UTC = dt.timezone.utc

#: Anything that would make the generated SQL depend on when it ran. The
#: window must be reproducible from the load date alone, so none of these may
#: appear in it -- see `assert_no_volatile_sql`.
VOLATILE_SQL = re.compile(
    r"\b(now\s*\(|current_timestamp|current_date|localtimestamp|localtime|"
    r"statement_timestamp\s*\(|transaction_timestamp\s*\(|clock_timestamp\s*\()",
    re.IGNORECASE,
)


class WindowError(ValueError):
    """The window is not a well-formed half-open NZ day."""


def nz_midnight(day: dt.date) -> dt.datetime:
    """The instant at which `day` begins in New Zealand, as UTC.

    NZ transitions at 2am/3am, so midnight is always a real, unambiguous local
    time and this is total over every date. Asserted rather than assumed: if a
    zone ever moved its transition to midnight, this would raise instead of
    silently choosing one side of the fold.
    """
    local = dt.datetime.combine(day, dt.time.min, tzinfo=NZ)
    if local.utcoffset() != local.replace(fold=1).utcoffset():
        raise WindowError(
            f"local midnight on {day} is ambiguous in {NZ.key}; "
            "the window boundaries are not well defined"
        )
    return local.astimezone(UTC)


def nz_day_window(load_date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """The half-open UTC interval [lo, hi) covering the NZ calendar day.

    `hi` comes from the NEXT DATE's midnight, not from `lo + 24h`. See the
    module docstring: the second is wall-clock arithmetic and is wrong by an
    hour on the two transition days each year.
    """
    lo = nz_midnight(load_date)
    hi = nz_midnight(load_date + dt.timedelta(days=1))
    if hi <= lo:
        raise WindowError(f"window for {load_date} does not run forwards: {lo} -> {hi}")
    return lo, hi


def day_length(load_date: dt.date) -> dt.timedelta:
    """Elapsed time in the NZ day -- 23h, 24h or 25h.

    Both operands are UTC, so this is real elapsed time. Subtracting two
    Auckland-aware datetimes would also be correct in CPython, but only
    because subtraction consults utcoffset() where addition does not; relying
    on that asymmetry in either direction is how the Stage 2 bug happened.
    """
    lo, hi = nz_day_window(load_date)
    return hi - lo


def load_date_for(instant: dt.datetime) -> dt.date:
    """Which load date an arrival belongs to. The inverse of `nz_day_window`.

    This is what makes the partition claim testable: for any row,
    `lo <= instant < hi` must hold for exactly the date this returns.
    """
    if instant.tzinfo is None:
        raise WindowError("naive datetime has no load date; the zone is the whole point")
    return instant.astimezone(NZ).date()


def load_date_for_fire_time(fire_time: dt.datetime) -> dt.date:
    """The NZ day a nightly run covers: the one that just ENDED.

    Airflow 3 maps `@daily` to CronTriggerTimetable, whose interval is
    timedelta(0) -- so `data_interval_start`, `data_interval_end` and
    `run_after` are all the same instant: the moment the run fires. A run
    firing at NZ midnight on the 21st must extract the 20th; taking the fire
    time's own date would extract a day that has barely begun, write _SUCCESS
    over a nearly empty partition, and never come back for it.

    THE SUBTRACTION IS ON A CALENDAR DATE, NEVER ON A DURATION. Going back 24
    hours from midnight on 2026-09-28 lands at 23:00 on 2026-09-26, because
    2026-09-27 is 23 hours long -- so a duration-based rule emits 2026-09-26
    twice and SKIPS the DST day completely. Measured, not assumed:

        fires 2026-09-28 00:00 NZ   minus 24h  -> 2026-09-26 23:00   WRONG
                                    minus 1 day -> 2026-09-27         right

    This is the same rule the window itself follows -- build from dates, and
    never let a timedelta stand in for a day.
    """
    if fire_time.tzinfo is None:
        raise WindowError("naive fire time has no NZ day; the zone is the whole point")
    return fire_time.astimezone(NZ).date() - dt.timedelta(days=1)


def _literal(instant: dt.datetime) -> str:
    """A UTC timestamp Postgres will read unambiguously, with an explicit offset."""
    return instant.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S+00")


def window_sql(column: str, load_date: dt.date) -> str:
    """The half-open predicate, with both bounds as literals.

    Returned as a fragment rather than bound parameters because it also goes
    into Spark's `predicates=[...]`, which takes strings; keeping one form
    means the nightly path and the partitioned path cannot drift.
    """
    return range_sql(column, *nz_day_window(load_date))


def range_sql(column: str, lo: dt.datetime, hi: dt.datetime) -> str:
    """window_sql's predicate for any half-open [lo, hi), not just one NZ day.

    A demo generation partition spans every day from its --from-date to its
    load date, and records that span in its manifest; the gate re-selects it
    at source through this, so both shapes share one literal form.
    """
    sql = f"{column} >= TIMESTAMPTZ '{_literal(lo)}' AND {column} < TIMESTAMPTZ '{_literal(hi)}'"
    assert_no_volatile_sql(sql)
    return sql


def hourly_predicates(load_date: dt.date, column: str = "received_at") -> list[str]:
    """One predicate per hour of the NZ day, for Spark's partitioned JDBC read.

    Design 5.1: split the meter-read extract "by `received_at` date range
    rather than by row id -- it aligns with the incremental watermark, keeps
    partitions evenly sized as history grows, and makes a failed partition
    re-runnable on its own." One predicate is one Spark partition is one JDBC
    connection is one index range scan on ix_reads_received.

    The list is 23, 24 or 25 long depending on the day, and it tiles [lo, hi)
    exactly -- which is the same property the load dates themselves have, one
    level down.
    """
    lo, hi = nz_day_window(load_date)
    edges = [lo]
    while edges[-1] < hi:
        edges.append(min(edges[-1] + dt.timedelta(hours=1), hi))
    return [
        f"{column} >= TIMESTAMPTZ '{_literal(a)}' AND {column} < TIMESTAMPTZ '{_literal(b)}'"
        for a, b in zip(edges, edges[1:])
    ]


def assert_no_volatile_sql(sql: str) -> None:
    """Refuse SQL whose result depends on when it ran.

    Cheap, and it guards the one mistake that would quietly un-do this whole
    module: someone adding `AND received_at <= now()` to keep future-dated
    rows out.
    """
    found = VOLATILE_SQL.search(sql)
    if found:
        raise WindowError(
            f"the extract window must not depend on the clock, found {found.group(0)!r}: {sql}"
        )
