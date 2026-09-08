"""Tests for the UK -> NZ transform. No database, no dataset, no network.

The whole point of isolating this module is that it can be proved correct
before anyone spends an hour generating twenty million rows.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uk_to_nz as m  # noqa: E402


class TestDeriveShift(unittest.TestCase):
    def test_the_three_properties_hold_for_the_pinned_span(self):
        shift = m.derive_shift_days()
        # 4564 until 2026-09-04, when NZ_SPAN_END moved +364 days so the span
        # would reach past today. See the constant for why December is illegal.
        assert shift == 4928
        assert shift % 7 == 0
        assert 0.48 <= (shift / m.MEAN_TROPICAL_YEAR) % 1 <= 0.52

    def test_day_of_week_survives(self):
        shift = m.derive_shift_days()
        for uk in (m.LCL_START, m.LCL_END, dt.date(2013, 1, 15)):
            assert m.nz_local_date_for(uk, shift).weekday() == uk.weekday()

    def test_seasons_flip(self):
        """UK deep winter must land in NZ deep winter, or the weather driver
        in the bill explainer reads backwards."""
        shift = m.derive_shift_days()
        assert m.nz_local_date_for(dt.date(2012, 1, 15), shift).month == 7
        assert m.nz_local_date_for(dt.date(2012, 7, 15), shift).month == 1

    def test_a_shift_that_is_not_whole_weeks_is_refused(self):
        with self.assertRaisesRegex(m.ShiftError, "whole number of weeks"):
            m.derive_shift_days(nz_end=m.NZ_SPAN_END + dt.timedelta(days=1))

    def test_a_shift_that_does_not_flip_the_seasons_is_refused(self):
        with self.assertRaisesRegex(m.ShiftError, "seasons would not flip"):
            m.derive_shift_days(nz_end=m.LCL_END + dt.timedelta(days=364))

    def test_a_backwards_shift_is_refused(self):
        with self.assertRaisesRegex(m.ShiftError, "forwards"):
            m.derive_shift_days(nz_end=m.LCL_END - dt.timedelta(days=7))

    def test_the_inverse_round_trips(self):
        shift = m.derive_shift_days()
        for uk in (m.LCL_START, dt.date(2013, 6, 30), m.LCL_END):
            assert m.uk_local_date_for(m.nz_local_date_for(uk, shift), shift) == uk


class TestDayLength(unittest.TestCase):
    """The regression guard for the bug that made every day 48.

    Python's arithmetic on two aware datetimes sharing a tzinfo is wall-clock,
    not elapsed. It returns 1 day across a transition, which looks right.
    """

    def test_an_ordinary_day_is_48(self):
        assert m.nz_day_length_half_hours(dt.date(2024, 6, 1)) == 48

    def test_daylight_saving_starts_a_23_hour_day(self):
        for day in [dt.date(2024, 9, 29), dt.date(2025, 9, 28)]:
            with self.subTest(day=day):
                assert m.nz_day_length_half_hours(day) == 46

    def test_daylight_saving_ends_a_25_hour_day(self):
        for day in [dt.date(2025, 4, 6), dt.date(2026, 4, 5)]:
            with self.subTest(day=day):
                assert m.nz_day_length_half_hours(day) == 50

    def test_the_span_contains_two_of_each(self):
        shift = m.derive_shift_days()
        d, lengths = m.nz_local_date_for(m.LCL_START, shift), []
        while d <= m.NZ_SPAN_END:
            lengths.append(m.nz_day_length_half_hours(d))
            d += dt.timedelta(days=1)
        assert lengths.count(46) == 2
        assert lengths.count(50) == 2

    def test_grid_length_matches_the_stated_length(self):
        for day in (dt.date(2024, 6, 1), dt.date(2024, 9, 29), dt.date(2025, 4, 6)):
            assert len(m.nz_half_hour_grid(day)) == m.nz_day_length_half_hours(day)

    def test_grid_slots_are_strictly_30_minutes_apart_in_real_time(self):
        """Absolute spacing, which is what a meter measures - not wall clock."""
        for day in (dt.date(2024, 9, 29), dt.date(2025, 4, 6)):
            grid = [g.astimezone(dt.timezone.utc) for g in m.nz_half_hour_grid(day)]
            gaps = {b - a for a, b in zip(grid, grid[1:])}
            assert gaps == {m.HALF_HOUR}


class TestWallClockMapping(unittest.TestCase):
    def test_the_clock_is_preserved(self):
        """18:30 in London becomes 18:30 in Auckland. This is the decision."""
        shift = m.derive_shift_days()
        grid = m.nz_half_hour_grid(dt.date(2024, 7, 15))
        evening = [g for g in grid if g.astimezone(m.NZ).time() == dt.time(18, 30)]
        assert len(evening) == 1
        uk_date, uk_time = m.uk_lookup_key(evening[0], shift)
        assert uk_time == dt.time(18, 30)
        assert uk_date == m.uk_local_date_for(dt.date(2024, 7, 15), shift)
        assert uk_date.month == 1  # NZ July evening came from a UK January evening

    def test_spring_forward_drops_the_two_missing_wall_clocks(self):
        """No NZ slot has a 02:00 or 02:30 clock, so those UK readings are
        never asked for. They are dropped, not silently misplaced."""
        shift = m.derive_shift_days()
        asked = {m.uk_lookup_key(g, shift)[1] for g in m.nz_half_hour_grid(dt.date(2024, 9, 29))}
        assert dt.time(2, 0) not in asked
        assert dt.time(2, 30) not in asked
        assert dt.time(1, 30) in asked and dt.time(3, 0) in asked

    def test_fall_back_asks_for_the_same_reading_twice(self):
        """Two distinct instants share the 02:00 wall clock, so the UK reading
        is duplicated - deliberately, and without rescaling the day."""
        shift = m.derive_shift_days()
        keys = [m.uk_lookup_key(g, shift) for g in m.nz_half_hour_grid(dt.date(2025, 4, 6))]
        times = [t for _, t in keys]
        assert times.count(dt.time(2, 0)) == 2
        assert times.count(dt.time(2, 30)) == 2
        assert len(keys) == 50

    def test_every_slot_maps_to_the_same_uk_date(self):
        shift = m.derive_shift_days()
        for day in (dt.date(2024, 6, 1), dt.date(2024, 9, 29), dt.date(2025, 4, 6)):
            dates = {m.uk_lookup_key(g, shift)[0] for g in m.nz_half_hour_grid(day)}
            assert dates == {m.uk_local_date_for(day, shift)}


class TestFillingADay(unittest.TestCase):
    @staticmethod
    def _uk_day(n=48):
        return {dt.time(i // 2, 30 * (i % 2)): float(i) for i in range(n)}

    def test_an_ordinary_day_is_filled_completely(self):
        shift = m.derive_shift_days()
        filled = m.nz_day_from_uk_readings(dt.date(2024, 6, 1), self._uk_day(), shift)
        assert len(filled) == 48
        assert all(v is not None for _, v in filled)

    def test_the_25_hour_day_repeats_one_reading_and_totals_more(self):
        shift = m.derive_shift_days()
        filled = m.nz_day_from_uk_readings(dt.date(2025, 4, 6), self._uk_day(), shift)
        assert len(filled) == 50
        assert all(v is not None for _, v in filled)
        # Energy is deliberately NOT conserved: a 25-hour day really does use more.
        assert sum(v for _, v in filled) > sum(self._uk_day().values())

    def test_the_23_hour_day_drops_two_readings_and_totals_less(self):
        shift = m.derive_shift_days()
        filled = m.nz_day_from_uk_readings(dt.date(2024, 9, 29), self._uk_day(), shift)
        assert len(filled) == 46
        assert sum(v for _, v in filled) < sum(self._uk_day().values())

    def test_a_missing_uk_reading_becomes_none_not_a_guess(self):
        shift = m.derive_shift_days()
        day = self._uk_day()
        del day[dt.time(9, 0)]
        filled = m.nz_day_from_uk_readings(dt.date(2024, 6, 1), day, shift)
        missing = [i for i, (_, v) in enumerate(filled) if v is None]
        assert len(missing) == 1
        assert filled[missing[0]][0].astimezone(m.NZ).time() == dt.time(9, 0)


class TestUkWallClock(unittest.TestCase):
    """`tstp` is UTC (probe_tstp.py). The London wall clock is derived."""

    def test_winter_utc_is_london_local(self):
        d, t = m.uk_wall_clock(dt.datetime(2012, 1, 15, 18, 30))
        assert (d, t) == (dt.date(2012, 1, 15), dt.time(18, 30))  # GMT, no offset

    def test_summer_utc_is_an_hour_behind_london(self):
        d, t = m.uk_wall_clock(dt.datetime(2012, 7, 15, 18, 30))
        assert (d, t) == (dt.date(2012, 7, 15), dt.time(19, 30))  # BST, UTC+1

    def test_an_aware_input_is_accepted_unchanged(self):
        naive = m.uk_wall_clock(dt.datetime(2012, 7, 15, 18, 30))
        aware = m.uk_wall_clock(dt.datetime(2012, 7, 15, 18, 30, tzinfo=dt.timezone.utc))
        assert naive == aware

    def test_the_uk_short_day_loses_two_wall_clocks(self):
        """48 UTC readings become 46 distinct London wall clocks on the day
        the UK springs forward. This is where the UK 46/50 appear."""
        start = dt.datetime(2012, 3, 25, 0, 0, tzinfo=dt.timezone.utc)
        walls = {m.uk_wall_clock(start + i * m.HALF_HOUR) for i in range(48)}
        on_the_day = {t for d, t in walls if d == dt.date(2012, 3, 25)}
        assert dt.time(1, 0) not in on_the_day
        assert dt.time(1, 30) not in on_the_day

    def test_the_uk_long_day_repeats_two_wall_clocks(self):
        start = dt.datetime(2012, 10, 28, 0, 0, tzinfo=dt.timezone.utc)
        walls = [m.uk_wall_clock(start + i * m.HALF_HOUR) for i in range(48)]
        times = [t for d, t in walls if d == dt.date(2012, 10, 28)]
        assert times.count(dt.time(1, 0)) == 2
        assert times.count(dt.time(1, 30)) == 2


class TestUkDaylightSavingEdges(unittest.TestCase):
    """The UK edges bite because the shift is a whole number of days.

    A UK spring-forward day maps onto an NZ day whose grid still asks for
    01:00 and 01:30. Every household in the cohort is then two readings short
    on the same two days of the span - which looks exactly like a data gap,
    and would have been written off as one.
    """

    def test_an_ordinary_uk_day_has_no_absent_wall_clocks(self):
        for day in (dt.date(2013, 6, 1), dt.date(2013, 1, 15), dt.date(2012, 11, 4)):
            with self.subTest(day=day):
                assert m.uk_absent_wall_clocks(day) == []

    def test_spring_forward_skips_0100_and_0130(self):
        for day in (dt.date(2012, 3, 25), dt.date(2013, 3, 31)):
            with self.subTest(day=day):
                assert m.uk_absent_wall_clocks(day) == [dt.time(1, 0), dt.time(1, 30)]

    def test_fall_back_has_none_absent_but_two_repeated(self):
        """Nothing is missing on the long day - two clocks occur twice.

        Grouping readings by wall clock therefore collides, and last-wins
        would silently keep 02:00 GMT's value in place of 02:00 BST's.
        """
        day = dt.date(2013, 10, 27)
        assert m.uk_absent_wall_clocks(day) == []
        start = m._utc_midnight(day, m.UK)
        clocks = [(start + i * m.HALF_HOUR).astimezone(m.UK).time() for i in range(50)]
        assert len(clocks) == 50 and len(set(clocks)) == 48

    def test_the_absent_clocks_land_on_nz_days_inside_the_span(self):
        """Regression guard: if the span or shift moves, this says where the
        estimated reads went, rather than leaving them to be rediscovered."""
        shift = m.derive_shift_days()
        hit = [m.nz_local_date_for(d, shift)
               for d in (dt.date(2012, 3, 25), dt.date(2013, 3, 31))]
        # Was [2024-09-22, 2025-09-28] before NZ_SPAN_END moved +364 days.
        # Both landings moved by exactly 52 weeks, which is the point of
        # constraining the shift to whole weeks.
        #
        # The second one lands on the last Sunday in September -- an NZ
        # spring-forward day, so that NZ day is 46 hours long AND two of its
        # wall clocks were never recorded in London. That collision is not new:
        # the old mapping put it on 2025-09-28, also a last Sunday. A 52-week
        # shift preserves it because both calendars' transitions move with it.
        assert hit == [dt.date(2025, 9, 21), dt.date(2026, 9, 27)]


if __name__ == "__main__":
    unittest.main()
