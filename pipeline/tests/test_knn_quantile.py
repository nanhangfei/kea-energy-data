"""The kNN quantile model (Botman et al. 2025), its month-end extension and
its scores.

Needs numpy, so the host `make test` skips it; `make backtest-reproduce` runs
it inside dataeng/dbt first. The leakage tests are the important ones: a
backtest that can see day d scores beautifully and means nothing.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import knn_quantile as m  # noqa: E402

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
if HAS_NUMPY:
    import numpy as np

# 2026-09-27: NZ clocks go forward at 02:00 (46 half-hours).
# 2027-04-04: NZ clocks go back at 03:00 (50 half-hours).
DST_START = dt.date(2026, 9, 27)
DST_END = dt.date(2027, 4, 4)


def utc_minutes(cal, day, count):
    """The first `count` UTC half-hours of calendar day `day`."""
    start = int(cal.bounds[day])
    return [start + 30 * i for i in range(count)]


def cohort(households=3, days=60, seed=7):
    rng = np.random.default_rng(seed)
    values = rng.gamma(2.0, 0.2, size=(households, days, m.SLOTS))
    return values, np.ones((households, days), dtype=bool)


def weekly(days, weeks_pattern):
    """A household that repeats one week exactly."""
    return np.stack([weeks_pattern[i % 7] for i in range(days)])


@unittest.skipUnless(HAS_NUMPY, "numpy is not installed on this host")
class TestDayMatrix(unittest.TestCase):
    def test_calendar_counts_the_half_hours_of_each_local_day(self):
        cal = m.calendar(DST_START - dt.timedelta(days=1), 3)
        self.assertEqual(cal.expected.sum(axis=1).tolist(), [48, 46, 48])
        self.assertEqual(cal.expected[1, 4:6].tolist(), [0, 0])

        cal = m.calendar(DST_END, 1)
        self.assertEqual(cal.expected.sum(), 50)
        self.assertEqual(cal.expected[0, 4:6].tolist(), [2, 2])

    def test_an_ordinary_full_day_is_complete_and_in_clock_order(self):
        cal = m.calendar(dt.date(2027, 1, 10), 1)
        values, complete = m.day_matrix(cal, utc_minutes(cal, 0, 48),
                                        [float(i) for i in range(48)])
        self.assertTrue(complete[0])
        self.assertEqual(values[0].tolist(), [float(i) for i in range(48)])

    def test_the_skipped_hour_is_interpolated(self):
        cal = m.calendar(DST_START, 1)
        starts = utc_minutes(cal, 0, 46)
        # Clock slots 0-3, then 6-47: 46 reads. Slot 3 reads 3, slot 6 reads 6.
        kwh = [0.0, 1.0, 2.0, 3.0] + [float(s) for s in range(6, 48)]
        values, complete = m.day_matrix(cal, starts, kwh)
        self.assertTrue(complete[0])
        self.assertEqual(values.shape, (1, 48))
        self.assertAlmostEqual(values[0, 4], 4.0)
        self.assertAlmostEqual(values[0, 5], 5.0)
        self.assertEqual(values[0, 6], 6.0)

    def test_the_repeated_hour_is_summed_into_its_two_slots(self):
        cal = m.calendar(DST_END, 1)
        values, complete = m.day_matrix(cal, utc_minutes(cal, 0, 50), [1.0] * 50)
        self.assertTrue(complete[0])
        self.assertEqual(values.shape, (1, 48))
        self.assertEqual(values[0, 4:6].tolist(), [2.0, 2.0])
        self.assertEqual(values[0].sum(), 50.0)

    def test_a_partly_read_repeated_hour_is_incomplete(self):
        cal = m.calendar(DST_END, 1)
        starts = utc_minutes(cal, 0, 50)
        del starts[6]           # the second 02:00 reading
        _, complete = m.day_matrix(cal, starts, [1.0] * 49)
        self.assertFalse(complete[0])

    def test_one_isolated_gap_is_interpolated(self):
        cal = m.calendar(dt.date(2027, 1, 10), 1)
        starts = utc_minutes(cal, 0, 48)
        kwh = [float(i) for i in range(48)]
        del starts[20], kwh[20]
        values, complete = m.day_matrix(cal, starts, kwh)
        self.assertTrue(complete[0])
        self.assertAlmostEqual(values[0, 20], 20.0)

    def test_two_adjacent_gaps_or_a_gap_at_the_edge_make_the_day_incomplete(self):
        cal = m.calendar(dt.date(2027, 1, 10), 1)
        for drop in ([20, 21], [0], [47]):
            starts = [s for i, s in enumerate(utc_minutes(cal, 0, 48)) if i not in drop]
            _, complete = m.day_matrix(cal, starts, [1.0] * len(starts))
            self.assertFalse(complete[0], drop)

    def test_every_register_must_read_each_half_hour(self):
        cal = m.calendar(dt.date(2027, 1, 10), 1)
        both = utc_minutes(cal, 0, 48) * 2
        values, complete = m.day_matrix(cal, both, [1.0] * 96, registers=2)
        self.assertTrue(complete[0])
        self.assertEqual(values[0, 0], 2.0)
        _, complete = m.day_matrix(cal, both[:-1], [1.0] * 95, registers=2)
        self.assertFalse(complete[0])

    def test_reads_outside_the_calendar_are_ignored(self):
        cal = m.calendar(dt.date(2027, 1, 10), 1)
        starts = [int(cal.bounds[0]) - 30] + utc_minutes(cal, 0, 48) + [int(cal.bounds[1])]
        values, complete = m.day_matrix(cal, starts, [9.0] + [1.0] * 48 + [9.0])
        self.assertTrue(complete[0])
        self.assertEqual(values.sum(), 48.0)


@unittest.skipUnless(HAS_NUMPY, "numpy is not installed on this host")
class TestLeakage(unittest.TestCase):
    def test_no_window_or_next_day_reaches_the_target_day(self):
        values, complete = cohort()
        library = m.window_library(values, complete)
        for d in range(20, 60):
            queries = values[:, d - 7:d].reshape(3, -1)
            rows = m.neighbours(library, m.normalise(queries)[0], before=d, k=10)
            self.assertTrue((library.last_day[rows] < d).all(), d)
            self.assertTrue((library.start[rows] + 7 < d).all(), d)

    def test_another_households_matching_week_is_unusable_until_its_next_day_is_past(self):
        values, complete = cohort()
        d = 40
        # Household 1's days d-7..d-1 are an exact copy of household 0's, so
        # its window ending d-1 is a perfect neighbour -- but its next day is d.
        values[1, d - 7:d] = values[0, d - 7:d]
        library = m.window_library(values, complete)
        twin = np.flatnonzero((library.household == 1) & (library.start == d - 7))
        self.assertEqual(twin.size, 1)
        query = m.normalise(values[0, d - 7:d].reshape(1, -1))[0]

        self.assertNotIn(twin[0], m.neighbours(library, query, before=d, k=10)[0])
        # A day later it is one of the two exact matches (the other is the
        # query's own week, now also history).
        self.assertIn(twin[0], m.neighbours(library, query, before=d + 1, k=2)[0])

    def test_an_incomplete_day_is_never_in_a_window_or_a_continuation(self):
        values, complete = cohort(households=1, days=30)
        complete[0, 15] = False
        library = m.window_library(values, complete)
        covered = library.start[:, None] + np.arange(8)
        self.assertFalse((covered == 15).any())
        self.assertEqual(len(library.start), (15 - 8 + 1) + (30 - 16 - 8 + 1))

    def test_the_library_is_in_last_day_order(self):
        values, complete = cohort()
        complete[1, 30] = False
        library = m.window_library(values, complete)
        self.assertTrue((np.diff(library.last_day) >= 0).all())
        self.assertEqual(len(library.last_day), 3 * 53 - 8)

    def test_an_all_zero_window_is_left_out(self):
        values, complete = cohort(households=1, days=20)
        values[0, :7] = 0.0
        library = m.window_library(values, complete)
        self.assertNotIn(0, library.start.tolist())

    def test_too_few_usable_windows_is_an_error_not_a_smaller_ensemble(self):
        values, complete = cohort(households=1, days=20)
        library = m.window_library(values, complete)
        with self.assertRaisesRegex(ValueError, "k=10"):
            m.neighbours(library, library.windows[:1], before=12, k=10)


@unittest.skipUnless(HAS_NUMPY, "numpy is not installed on this host")
class TestForecast(unittest.TestCase):
    def test_a_household_repeating_one_week_is_forecast_exactly(self):
        rng = np.random.default_rng(3)
        week = rng.gamma(2.0, 0.2, size=(7, m.SLOTS))
        values = weekly(70, week)[None]
        library = m.window_library(values, np.ones((1, 70), dtype=bool))
        d = 63
        # 8 earlier windows (starts 0, 7, ..., 49) share d's phase and have
        # next days before d; any 7 of them forecast day d exactly.
        ensemble = m.knn_forecast(library, values[:, d - 7:d].reshape(1, -1),
                                  before=d, k=7)
        self.assertEqual(ensemble.shape, (1, 7, m.SLOTS))
        np.testing.assert_allclose(ensemble[0], np.repeat(values[:, d], 7, axis=0))
        self.assertAlmostEqual(
            float(m.crps_ensemble(ensemble[0].T, values[0, d]).max()), 0.0)

    def test_three_times_the_usage_gives_three_times_the_forecast(self):
        values, complete = cohort()
        library = m.window_library(values, complete)
        query = values[0, 33:40].reshape(1, -1)
        once = m.knn_forecast(library, query, before=40, k=10)
        thrice = m.knn_forecast(library, 3 * query, before=40, k=10)
        np.testing.assert_allclose(thrice, 3 * once)

    def test_the_neighbours_own_scale_is_removed_before_the_querys_is_applied(self):
        values, complete = cohort(households=2)
        values[0, 26:33] = values[0, 33:40]  # an earlier copy of the query week
        values[1] = 10 * values[0]           # the same shapes, ten times larger
        library = m.window_library(values, complete)
        query = values[0, 33:40].reshape(1, -1)
        ensemble = m.knn_forecast(library, query, before=40, k=2)
        # Both households' window starting on day 26 matches exactly, and
        # each continues with its own day 33, on its own scale.
        np.testing.assert_allclose(ensemble[0, 0], values[0, 33])
        np.testing.assert_allclose(ensemble[0, 1], values[0, 33])

    def test_an_all_zero_week_forecasts_zero(self):
        values, complete = cohort()
        library = m.window_library(values, complete)
        ensemble = m.knn_forecast(library, np.zeros((1, 7 * m.SLOTS)), before=40, k=5)
        self.assertEqual(float(np.abs(ensemble).max()), 0.0)


@unittest.skipUnless(HAS_NUMPY, "numpy is not installed on this host")
class TestMonthEnd(unittest.TestCase):
    def test_each_total_is_the_whole_continuation_over_the_weeks_maximum(self):
        values, complete = cohort(households=2, days=30)
        library = m.trajectory_library(values, complete, max_horizon=5)
        i = np.flatnonzero((library.household == 1) & (library.start == 10))[0]
        scale = values[1, 10:17].max()
        for r in range(6):
            self.assertAlmostEqual(library.totals[i, r],
                                   values[1, 17:17 + r].sum() / scale)
        # The week starting on day 20 has days 27-29 after it, and no more.
        j = np.flatnonzero((library.household == 0) & (library.start == 20))[0]
        self.assertTrue(np.isfinite(library.totals[j, 3]))
        self.assertTrue(np.isnan(library.totals[j, 4:]).all())

    def test_an_incomplete_day_ends_every_continuation_that_reaches_it(self):
        values, complete = cohort(households=1, days=30)
        complete[0, 20] = False
        library = m.trajectory_library(values, complete, max_horizon=6)
        i = np.flatnonzero(library.start == 10)[0]   # continuation from day 17
        self.assertTrue(np.isfinite(library.totals[i, 3]))
        self.assertTrue(np.isnan(library.totals[i, 4:]).all())
        self.assertFalse(((library.start <= 20) & (library.start + 7 > 20)).any())

    def test_the_p90_of_the_totals_is_not_the_sum_of_the_daily_p90s(self):
        rng = np.random.default_rng(13)
        week = rng.gamma(2.0, 0.2, size=(7, m.SLOTS))
        day = np.full(m.SLOTS, 0.1)
        values = np.zeros((4, 9, m.SLOTS))
        values[:, :7] = week
        values[:2, 7] = day       # two households use the day's energy first,
        values[2:, 8] = day       # two use it second
        library = m.trajectory_library(values, np.ones((4, 9), dtype=bool),
                                       max_horizon=2)
        ensemble = m.remaining_ensemble(library, week.reshape(1, -1),
                                        as_at=8, horizons=[2], k=4)
        total = day.sum()
        np.testing.assert_allclose(ensemble, [[total] * 4])
        daily_p90 = np.quantile(values[:, 7:9].sum(axis=2), 0.9, axis=0).sum()
        self.assertAlmostEqual(daily_p90, 2 * total)
        self.assertAlmostEqual(float(np.quantile(ensemble, 0.9)), total)

    def test_no_week_or_continuation_passes_the_as_at_day(self):
        values, complete = cohort(households=3, days=80)
        complete[1, 45] = False
        library = m.trajectory_library(values, complete, max_horizon=20)
        for as_at in (40, 55, 79):
            for r in (1, 7, 20):
                mask = m.trajectory_candidates(library, as_at, r)
                ends = library.start + 7 + r - 1
                self.assertTrue((ends[mask] <= as_at).all(), (as_at, r))
                expected = (ends <= as_at) & np.isfinite(library.totals[:, r])
                self.assertEqual(mask.tolist(), expected.tolist())
        query = m.normalise(values[:2, 44:51].reshape(2, -1))[0]
        rows = m.trajectory_neighbours(library, query, 50, [7, 20], k=10)
        self.assertTrue((library.start[rows[0]] + 7 + 7 - 1 <= 50).all())
        self.assertTrue((library.start[rows[1]] + 7 + 20 - 1 <= 50).all())

    def test_nothing_after_the_as_at_day_changes_the_forecast(self):
        values, complete = cohort(households=3, days=80)
        as_at = 50
        queries = values[:2, as_at - 6:as_at + 1].reshape(2, -1)
        before = m.remaining_ensemble(
            m.trajectory_library(values, complete, 25), queries, as_at,
            [10, 25], k=10)
        rng = np.random.default_rng(99)
        values[:, as_at + 1:] = rng.gamma(5.0, 1.0,
                                          size=values[:, as_at + 1:].shape)
        complete[:, as_at + 1::3] = False
        after = m.remaining_ensemble(
            m.trajectory_library(values, complete, 25), queries, as_at,
            [10, 25], k=10)
        np.testing.assert_array_equal(before, after)

    def test_a_household_repeating_one_week_is_forecast_exactly_to_the_end(self):
        rng = np.random.default_rng(3)
        week = rng.gamma(2.0, 0.2, size=(7, m.SLOTS))
        values = weekly(120, week)[None]
        library = m.trajectory_library(values, np.ones((1, 120), dtype=bool),
                                       max_horizon=30)
        as_at, r = 90, 23
        # Usable weeks start on days 0-61; the 9 starting on 0, 7, ..., 56
        # share the query week's phase and match it exactly.
        ensemble = m.remaining_ensemble(
            library, values[:, as_at - 6:as_at + 1].reshape(1, -1), as_at,
            [r], k=9)
        truth = values[0, as_at + 1:as_at + 1 + r].sum()
        low, mid, high = np.quantile(ensemble[0], [0.1, 0.5, 0.9])
        for q in (low, mid, high):
            self.assertAlmostEqual(q, truth)
        self.assertAlmostEqual(float(m.crps_ensemble(ensemble, [truth])[0]), 0.0)

    def test_three_times_the_usage_gives_three_times_the_remainder(self):
        values, complete = cohort(households=3, days=80)
        library = m.trajectory_library(values, complete, max_horizon=15)
        query = values[0, 54:61].reshape(1, -1)
        once = m.remaining_ensemble(library, query, 60, [15], k=10)
        thrice = m.remaining_ensemble(library, 3 * query, 60, [15], k=10)
        np.testing.assert_allclose(thrice, 3 * once)

    def test_too_few_usable_weeks_is_an_error_not_a_smaller_ensemble(self):
        values, complete = cohort(households=1, days=40)
        library = m.trajectory_library(values, complete, max_horizon=20)
        # as_at 30, 20 days to go: only weeks starting on days 0-4 qualify.
        self.assertEqual(m.trajectory_candidates(library, 30, 20).sum(), 5)
        with self.assertRaisesRegex(ValueError, "k=6"):
            m.trajectory_neighbours(library, library.windows[:1], 30, [20], k=6)
        m.trajectory_neighbours(library, library.windows[:1], 30, [20], k=5)


@unittest.skipUnless(HAS_NUMPY, "numpy is not installed on this host")
class TestStage3Dials(unittest.TestCase):
    """The seasonal library and the normalisations, off by default."""

    def test_the_seasonal_library_never_reaches_outside_the_thirty_day_window(self):
        values, complete = cohort(households=2, days=800)
        library = m.trajectory_library(values, complete, max_horizon=20)
        starts = library.start + library.window_days   # continuation day 1
        for as_at in (400, 500, 799):
            allowed = m.seasonal_rows(library, as_at)
            lag = as_at - starts[allowed]
            into_year = lag % m.SEASON
            self.assertTrue(allowed.any(), as_at)
            self.assertTrue((np.minimum(into_year, m.SEASON - into_year)
                             <= m.SEASONAL_DAYS).all(), as_at)
            self.assertTrue((lag >= m.SEASONAL_MIN_LAG).all(), as_at)
            # ... and every neighbour the filter allows is still in the past.
            for r in (1, 20):
                mask = m.trajectory_candidates(library, as_at, r, allowed)
                ends = library.start + library.window_days + r - 1
                self.assertTrue((ends[mask] <= as_at).all(), (as_at, r))
                self.assertTrue(mask[~allowed].sum() == 0)

    def test_a_seasonal_neighbour_is_a_year_back_not_last_week(self):
        values, complete = cohort(households=1, days=800)
        library = m.trajectory_library(values, complete, max_horizon=5)
        as_at = 760
        allowed = m.seasonal_rows(library, as_at)
        lag = as_at - (library.start + library.window_days)
        self.assertFalse(allowed[lag < m.SEASONAL_MIN_LAG].any())
        a_year_back = np.flatnonzero(np.abs(lag - m.SEASON) <= 5)
        self.assertTrue(allowed[a_year_back].all())
        # Half a year back is the opposite season, and out.
        self.assertFalse(allowed[np.abs(lag - 180) <= 5].any())

    def test_only_allowed_weeks_are_picked_and_the_forecast_changes(self):
        values, complete = cohort(households=3, days=800)
        library = m.trajectory_library(values, complete, max_horizon=12)
        as_at, r, k = 760, 12, 20
        query = values[0, as_at - 6:as_at + 1].reshape(1, -1)
        allowed = m.seasonal_rows(library, as_at)
        seasonal = m.trajectory_neighbours(library, m.normalise(query)[0],
                                           as_at, [r], k=k, allowed=allowed)
        every = m.trajectory_neighbours(library, m.normalise(query)[0],
                                        as_at, [r], k=k)
        self.assertTrue(allowed[seasonal[0]].all())
        self.assertFalse(allowed[every[0]].all())
        self.assertFalse(np.array_equal(
            m.remaining_ensemble(library, query, as_at, [r], k=k,
                                 allowed=allowed),
            m.remaining_ensemble(library, query, as_at, [r], k=k)))

    def test_the_scale_is_the_maximum_the_mean_or_one(self):
        rows = np.array([[1.0, 3.0, 2.0], [0.0, 0.0, 0.0]])
        np.testing.assert_allclose(m.scale_of(rows, "week"), [3.0, 0.0])
        np.testing.assert_allclose(m.scale_of(rows, "profile"), [2.0, 0.0])
        np.testing.assert_allclose(m.scale_of(rows, "none"), [1.0, 1.0])
        with self.assertRaisesRegex(ValueError, "norm must be one of"):
            m.scale_of(rows, "median")
        normalised, scale = m.normalise(rows, "profile")
        np.testing.assert_allclose(normalised[0], [0.5, 1.5, 1.0])
        np.testing.assert_allclose(normalised[1], [0.0, 0.0, 0.0])
        np.testing.assert_allclose(scale, [2.0, 0.0])

    def test_week_and_profile_compare_shape_while_none_compares_size(self):
        rng = np.random.default_rng(11)
        shape = rng.gamma(2.0, 0.2, size=(7, m.SLOTS))
        other = np.roll(shape, m.SLOTS // 2, axis=1)
        values = np.zeros((2, 16, m.SLOTS))
        values[0, :] = 4 * shape[np.arange(16) % 7]   # same shape, 4x the size
        values[1, :] = other[np.arange(16) % 7]       # same size, other shape
        complete = np.ones((2, 16), dtype=bool)
        query = shape.reshape(1, -1)
        for norm, wanted in (("week", 0), ("profile", 0), ("none", 1)):
            library = m.trajectory_library(values, complete, max_horizon=1,
                                           norm=norm)
            normalised, _ = m.normalise(query, norm)
            rows = m.trajectory_neighbours(library, normalised, as_at=14,
                                           horizons=[1], k=1)
            self.assertEqual(int(library.household[rows[0, 0]]), wanted, norm)

    def test_none_leaves_the_neighbours_own_kwh_alone(self):
        values, complete = cohort(households=3, days=40)
        library = m.trajectory_library(values, complete, max_horizon=5,
                                       norm="none")
        np.testing.assert_allclose(library.windows,
                                   m.trajectory_library(
                                       values, complete, max_horizon=5,
                                       norm="none").windows)
        i = np.flatnonzero((library.household == 2) & (library.start == 10))[0]
        self.assertAlmostEqual(library.totals[i, 4],
                               values[2, 17:21].sum())
        query = values[0, 24:31].reshape(1, -1)
        ensemble = m.remaining_ensemble(library, query, 30, [5], k=6,
                                        norm="none")
        rows = m.trajectory_neighbours(
            library, query, 30, [5], k=6)
        np.testing.assert_allclose(ensemble[0], library.totals[rows[0], 5])

    def test_a_household_left_out_of_the_pool_brings_no_weeks(self):
        values, complete = cohort(households=4, days=40)
        pool = complete.copy()
        pool[1:3] = False
        library = m.trajectory_library(values, pool, max_horizon=5)
        self.assertEqual(sorted(set(library.household.tolist())), [0, 3])
        whole = m.trajectory_library(values, complete, max_horizon=5)
        self.assertEqual(len(library.start), len(whole.start) // 2)


@unittest.skipUnless(HAS_NUMPY, "numpy is not installed on this host")
class TestBaselines(unittest.TestCase):
    def test_historical_sampling_uses_earlier_complete_days_of_the_same_weekday(self):
        values = np.arange(30, dtype=float)[None, :, None] * np.ones((1, 30, m.SLOTS))
        complete = np.ones((1, 30), dtype=bool)
        complete[0, 9] = False
        members = m.historical_sampling(values, complete, h=0, d=23)
        self.assertEqual(members[:, 0].tolist(), [2.0, 16.0])

    def test_persistence_is_yesterday_plus_past_changes_clipped_at_zero(self):
        days = np.array([9.0, 1.0, 4.0, 4.5, 2.0, 9.0])
        values = days[None, :, None] * np.ones((1, 6, m.SLOTS))
        complete = np.ones((1, 6), dtype=bool)
        members = m.persistence(values, complete, h=0, d=4)
        # Yesterday 4.5; changes from day pairs (0,1), (1,2), (2,3) are -8,
        # +3 and +0.5, and day 4 is unseen. 4.5 - 8 is clipped to 0.
        self.assertEqual(sorted(members[:, 0].tolist()), [0.0, 5.0, 7.5])

    def test_persistence_skips_pairs_with_an_incomplete_day(self):
        values = np.arange(10, dtype=float)[None, :, None] * np.ones((1, 10, m.SLOTS))
        complete = np.ones((1, 10), dtype=bool)
        complete[0, 3] = False
        members = m.persistence(values, complete, h=0, d=8)
        self.assertEqual(len(members), 5)       # pairs ending 1, 2, 5, 6, 7


@unittest.skipUnless(HAS_NUMPY, "numpy is not installed on this host")
class TestScores(unittest.TestCase):
    def test_one_member_scores_its_absolute_error(self):
        self.assertAlmostEqual(float(m.crps_ensemble([2.5], 1.0)), 1.5)
        np.testing.assert_allclose(
            m.crps_ensemble([[1.0], [4.0]], [3.0, 3.0]), [2.0, 1.0])

    def test_two_members_by_hand(self):
        # F is 0 below 0, 1/2 on [0, 2), 1 from 2.
        # y = 1: 1/4 * 1 + 1/4 * 1 = 0.5.  y = 3: 1/4 * 2 + 1 * 1 = 1.5.
        self.assertAlmostEqual(float(m.crps_ensemble([2.0, 0.0], 1.0)), 0.5)
        self.assertAlmostEqual(float(m.crps_ensemble([0.0, 2.0], 3.0)), 1.5)

    def test_the_sorted_identity_matches_the_pairwise_definition(self):
        rng = np.random.default_rng(11)
        x = rng.gamma(2.0, 0.3, size=(4, 37))
        y = rng.gamma(2.0, 0.3, size=4)
        pairwise = (np.abs(x - y[:, None]).mean(axis=1)
                    - 0.5 * np.abs(x[:, :, None] - x[:, None, :]).mean(axis=(1, 2)))
        np.testing.assert_allclose(m.crps_ensemble(x, y), pairwise)

    def test_the_papers_quantile_form_is_half_the_crps_for_a_large_ensemble(self):
        rng = np.random.default_rng(5)
        x = rng.gamma(2.0, 0.3, size=(200, 2000))
        y = rng.gamma(2.0, 0.3, size=200)
        ratio = m.quantile_crps(x, y).sum() / m.crps_ensemble(x, y).sum()
        self.assertAlmostEqual(float(ratio), 0.5, delta=0.02)

    def test_the_papers_quantile_form_for_one_member(self):
        # Every quantile is 2; y = 3 is above all of them: mean(q) * 1.
        self.assertAlmostEqual(float(m.quantile_crps([2.0], 3.0)), 0.505)

    def test_the_band_holds_its_ends(self):
        ensemble = np.arange(11, dtype=float)          # P10 = 1, P90 = 9
        self.assertEqual(
            m.in_band(np.tile(ensemble, (4, 1)), [0.5, 1.0, 9.0, 9.5]).tolist(),
            [False, True, True, False])


if __name__ == "__main__":
    unittest.main()
