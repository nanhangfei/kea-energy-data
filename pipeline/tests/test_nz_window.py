"""Tests for the extract window. No database, no lake, no Spark, no dataset.

Stage 2 proved the UK -> NZ transform with 30 unit tests before generating a
single row, and it caught two silent DST faults that way. The window is this
stage's equivalent: it runs on a date and a timezone, it is wrong silently,
and it can be settled before anything slow exists.

The numbers here are not invented. They were measured against the live source
database and are quoted in the Stage 3 plan.
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from kea_pipeline import window as m  # noqa: E402

UTC = dt.timezone.utc

#: The planted late revision (06_revisions.sql). A 2026-07-20 reading,
#: corrected and delivered 36 days later. Stage 3's first exit condition is
#: that an ordinary nightly run picks it up.
LATE_REVISION_ARRIVAL = dt.datetime(2026, 8, 24, 21, 15, tzinfo=UTC)


class TestTheLoadDateIsAnNZDay(unittest.TestCase):
    """The single most consequential decision in the module.

    Measured on the live source: the NZ-day window for 2026-08-25 holds 26,925
    rows including all 48 of the planted revision; the UTC-day window holds
    26,623 and none of it.
    """

    def test_the_late_revision_belongs_to_the_nz_date_not_the_utc_one(self):
        assert LATE_REVISION_ARRIVAL.date() == dt.date(2026, 8, 24)  # UTC
        assert m.load_date_for(LATE_REVISION_ARRIVAL) == dt.date(2026, 8, 25)  # NZ

    def test_the_nz_window_contains_it(self):
        lo, hi = m.nz_day_window(dt.date(2026, 8, 25))
        assert lo <= LATE_REVISION_ARRIVAL < hi

    def test_a_utc_day_window_would_miss_it(self):
        """The regression guard for the trap. If someone 'simplifies' the
        window to a UTC day, this is what breaks -- and nothing else would."""
        utc_lo = dt.datetime(2026, 8, 25, tzinfo=UTC)
        utc_hi = utc_lo + dt.timedelta(days=1)
        assert not (utc_lo <= LATE_REVISION_ARRIVAL < utc_hi)


class TestDayLength(unittest.TestCase):
    """A New Zealand day is 23, 24 or 25 hours. Never assume 24.

    These four transitions fall inside the source's `received_at` span
    (2024-09-18 .. 2026-10-11), so the extract really does meet them.
    """

    def test_the_long_days(self):
        for day in (dt.date(2025, 4, 6), dt.date(2026, 4, 5)):
            assert m.day_length(day) == dt.timedelta(hours=25), day

    def test_the_short_days(self):
        for day in (dt.date(2024, 9, 29), dt.date(2025, 9, 28), dt.date(2026, 9, 27)):
            assert m.day_length(day) == dt.timedelta(hours=23), day

    def test_an_ordinary_day(self):
        assert m.day_length(dt.date(2026, 8, 25)) == dt.timedelta(hours=24)

    def test_hi_is_not_lo_plus_twenty_four_hours(self):
        """Assuming the day is 24 hours long drops or duplicates an hour.

        This is the trap the window actually has. On 2025-04-06 the extra hour
        of arrivals would be missed; on 2025-09-28 the window would run an hour
        into the next day and those rows would appear in two partitions -- both
        of which look like ordinary variation in a daily row count.
        """
        for day in (dt.date(2025, 4, 6), dt.date(2025, 9, 28)):
            lo, hi = m.nz_day_window(day)
            assert hi != lo + dt.timedelta(hours=24), day

    def test_localising_the_next_midnight_is_not_the_trap(self):
        """Stated so the docstring's precision is enforced, not just asserted.

        A wall-clock `+1 day` on midnight DOES resolve to the right instant,
        because astimezone() re-resolves the offset. Recording that here stops
        someone 'fixing' a bug that is not present, and keeps the real one --
        the 24-hour assumption above -- distinguishable from it.
        """
        for day in (dt.date(2025, 4, 6), dt.date(2025, 9, 28)):
            _, hi = m.nz_day_window(day)
            wall_clock = (
                dt.datetime.combine(day, dt.time.min, tzinfo=m.NZ) + dt.timedelta(days=1)
            ).astimezone(UTC)
            assert hi == wall_clock, day


class TestThePartitionProperty(unittest.TestCase):
    """Consecutive load dates must tile the timeline exactly.

    This is what makes the gate's strongest assertion checkable -- that the sum
    of the bronze partitions equals the source over the covered range. If the
    windows overlapped, rows would be double-counted; if they gapped, rows
    would vanish, and both look like ordinary variation in a daily row count.
    """

    def _range(self):
        day = dt.date(2024, 9, 18)
        end = dt.date(2026, 10, 12)
        while day < end:
            yield day
            day += dt.timedelta(days=1)

    def test_windows_abut_across_the_whole_source_span(self):
        previous_hi = None
        for day in self._range():
            lo, hi = m.nz_day_window(day)
            assert lo < hi
            if previous_hi is not None:
                assert lo == previous_hi, f"gap or overlap at {day}"
            previous_hi = hi

    def test_every_boundary_instant_belongs_to_exactly_one_day(self):
        """Half-open, so the shared instant goes to the later day and only there."""
        for day in (dt.date(2025, 4, 6), dt.date(2025, 9, 28), dt.date(2026, 8, 25)):
            lo, _ = m.nz_day_window(day)
            assert m.load_date_for(lo) == day
            assert m.load_date_for(lo - dt.timedelta(microseconds=1)) == day - dt.timedelta(days=1)

    def test_load_date_round_trips_for_arrivals_through_a_transition(self):
        instant = m.nz_midnight(dt.date(2025, 4, 5))
        for _ in range(48):
            day = m.load_date_for(instant)
            lo, hi = m.nz_day_window(day)
            assert lo <= instant < hi
            instant += dt.timedelta(hours=1)


class TestNoClockDependence(unittest.TestCase):
    """0.06% of source rows carry a future `received_at`, out to 2026-10-11.

    They are not a defect and must not be clamped -- they belong to a future
    load date. What must never happen is the window consulting the clock to
    exclude them, because then a replay is not a no-op.
    """

    def test_a_future_dated_arrival_belongs_to_its_own_future_load_date(self):
        far = dt.datetime(2026, 10, 11, 9, 57, tzinfo=ZoneInfo("Pacific/Auckland"))
        day = m.load_date_for(far)
        assert day == dt.date(2026, 10, 11)
        lo, hi = m.nz_day_window(day)
        assert lo <= far < hi

    def test_generated_sql_carries_literal_bounds_only(self):
        sql = m.window_sql("received_at", dt.date(2026, 8, 25))
        assert "2026-08-24 12:00:00+00" in sql
        assert "2026-08-25 12:00:00+00" in sql
        m.assert_no_volatile_sql(sql)

    def test_a_clock_bounded_window_is_refused(self):
        for bad in (
            "received_at <= now()",
            "received_at < CURRENT_DATE",
            "received_at <= current_timestamp",
            "received_at < clock_timestamp()",
        ):
            with self.assertRaises(m.WindowError):
                m.assert_no_volatile_sql(bad)

    def test_the_window_is_the_same_whenever_it_is_asked(self):
        first = m.window_sql("received_at", dt.date(2026, 8, 25))
        second = m.window_sql("received_at", dt.date(2026, 8, 25))
        assert first == second


class TestHourlyPredicates(unittest.TestCase):
    """Design 5.1's partitioned read, expressed as one predicate per hour."""

    def test_the_count_follows_the_day_length(self):
        assert len(m.hourly_predicates(dt.date(2026, 8, 25))) == 24
        assert len(m.hourly_predicates(dt.date(2025, 4, 6))) == 25
        assert len(m.hourly_predicates(dt.date(2025, 9, 28))) == 23

    def test_they_tile_the_window_with_no_gap_and_no_overlap(self):
        for day in (dt.date(2026, 8, 25), dt.date(2025, 4, 6), dt.date(2025, 9, 28)):
            lo, hi = m.nz_day_window(day)
            preds = m.hourly_predicates(day)
            bounds = [
                (p.split("'")[1], p.split("'")[3]) for p in preds
            ]
            assert bounds[0][0] == m._literal(lo), day
            assert bounds[-1][1] == m._literal(hi), day
            for (_, end), (start, _) in zip(bounds, bounds[1:]):
                assert end == start, day

    def test_no_predicate_depends_on_the_clock(self):
        for pred in m.hourly_predicates(dt.date(2025, 4, 6)):
            m.assert_no_volatile_sql(pred)


class TestRefusals(unittest.TestCase):
    def test_a_naive_instant_has_no_load_date(self):
        with self.assertRaisesRegex(m.WindowError, "naive"):
            m.load_date_for(dt.datetime(2026, 8, 25, 9, 15))


if __name__ == "__main__":
    unittest.main()


class TestLoadDateForFireTime(unittest.TestCase):
    """Which NZ day a nightly run covers.

    Airflow 3's @daily has ZERO interval -- data_interval_start is the fire
    time, not the start of a covered period -- so the DAG must go back a day
    itself, and must do it on the calendar rather than by a duration.
    """

    def _fire(self, iso):
        return dt.datetime.fromisoformat(iso).replace(tzinfo=m.NZ)

    def test_a_run_covers_the_day_that_just_ended(self):
        assert m.load_date_for_fire_time(self._fire("2026-08-21T00:00")) == dt.date(2026, 8, 20)

    def test_the_23_hour_day_is_not_skipped(self):
        """A fixed 24h subtraction lands at 2026-09-26 23:00 here, emitting
        2026-09-26 twice and losing 2026-09-27 altogether."""
        assert m.load_date_for_fire_time(self._fire("2026-09-28T00:00")) == dt.date(2026, 9, 27)

    def test_the_25_hour_day_is_not_duplicated(self):
        assert m.load_date_for_fire_time(self._fire("2026-04-06T00:00")) == dt.date(2026, 4, 5)

    def test_every_day_across_a_transition_appears_exactly_once(self):
        fires = [self._fire(f"2026-09-{d:02d}T00:00") for d in range(24, 31)]
        got = [m.load_date_for_fire_time(f) for f in fires]
        assert len(set(got)) == len(got), got
        assert dt.date(2026, 9, 27) in got

    def test_a_naive_fire_time_is_refused(self):
        with self.assertRaisesRegex(m.WindowError, "naive"):
            m.load_date_for_fire_time(dt.datetime(2026, 8, 21))
