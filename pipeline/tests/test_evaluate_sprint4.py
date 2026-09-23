"""The Sprint 4 evaluation's own arithmetic: planted slots, session matching,
the heating fit's rule, and the hardship replay's departures from the job."""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_hardship_assessments as H  # noqa: E402
import evaluate_sprint4 as m  # noqa: E402

WALLBOX = "ev_wallbox_7kw"
NZ_WINTER = dt.timezone(dt.timedelta(hours=12))   # Auckland in July: NZST, no DST


def at(day: dt.date, hour: int, minute: int = 0) -> int:
    return int(dt.datetime.combine(day, dt.time(hour, minute), NZ_WINTER).timestamp())


class TestPlantedSlots(unittest.TestCase):
    def test_full_half_hours_then_the_remainder(self):
        self.assertEqual(m.planted_slots(WALLBOX, 10.0, 3), (3.6, 3.6, 2.8))
        self.assertEqual(m.planted_slots("hot_water_3kw", 4.5, 3), (1.5, 1.5, 1.5))

    def test_a_session_cut_at_the_end_keeps_its_full_slots(self):
        self.assertEqual(m.planted_slots(WALLBOX, 10.0, 2), (3.6, 3.6))

    def test_a_remainder_rounded_away_comes_back_as_zero(self):
        self.assertEqual(m.planted_slots(WALLBOX, 7.2, 3), (3.6, 3.6, 0.0))

    def test_a_length_the_total_cannot_fill_is_an_error(self):
        with self.assertRaises(ValueError):
            m.planted_slots(WALLBOX, 3.6, 3)


class TestSessionMatching(unittest.TestCase):
    def test_half_of_each_session_must_overlap(self):
        self.assertTrue(m.sessions_match(0, 3600, 1800, 5400))
        self.assertFalse(m.sessions_match(0, 3600, 1800, 9000))
        self.assertFalse(m.sessions_match(0, 3600, 3600, 7200))

    def test_the_sweep_finds_each_pair_once(self):
        planted = [(10_000, 13_600), (0, 3600), (50_000, 53_600)]
        detected = [(20_000, 21_800), (1800, 5400), (50_000, 51_800)]
        self.assertEqual(m.match_sessions(planted, detected), ({1, 2}, {1, 2}))
        self.assertEqual(m.match_sessions([], detected), (set(), set()))

    def test_bounds_allow_the_rounding_tolerance(self):
        self.assertTrue(m.in_bounds(4.995, 5.0, 6.0))
        self.assertTrue(m.in_bounds(6.005, 5.0, 6.0))
        self.assertFalse(m.in_bounds(4.98, 5.0, 6.0))
        self.assertFalse(m.in_bounds(6.02, 5.0, 6.0))

    def test_the_distance_outside_is_zero_inside_and_signed_nowhere(self):
        self.assertEqual(m.outside_by(5.5, 5.0, 6.0), 0.0)
        self.assertEqual(m.outside_by(4.995, 5.0, 6.0), 0.0)   # inside the tolerance
        self.assertAlmostEqual(m.outside_by(4.5, 5.0, 6.0), 0.5)
        self.assertAlmostEqual(m.outside_by(6.25, 5.0, 6.0), 0.25)


class TestHeatingFit(unittest.TestCase):
    def test_ols_follows_snowflakes_regr_functions(self):
        self.assertEqual(m.ols([0, 1, 2], [1, 3, 5]), (2.0, 1.0, 1.0))
        self.assertEqual(m.ols([1, 1, 1], [1, 2, 3]), (None, None, None))
        self.assertEqual(m.ols([0, 1, 2], [4, 4, 4]), (0.0, 4.0, 1.0))
        self.assertEqual(m.ols([1], [1]), (None, None, None))

    @staticmethod
    def month(first: dt.date, days: int, hdd: float) -> list[tuple[dt.date, float, float]]:
        return [(first + dt.timedelta(days=i), hdd, 10 + 2 * hdd) for i in range(days)]

    def test_six_months_of_twenty_days_give_a_fit(self):
        days = [row for k in range(6)
                for row in self.month(dt.date(2026, k + 1, 1), 20, float(k))]
        fit = m.monthly_fit(days)
        self.assertEqual(fit.months, 6)
        self.assertAlmostEqual(fit.slope, 2.0)
        self.assertAlmostEqual(fit.intercept, 10.0)
        self.assertTrue(fit.usable)

    def test_a_short_month_is_dropped_and_five_months_give_none(self):
        days = [row for k in range(6)
                for row in self.month(dt.date(2026, k + 1, 1), 19 if k == 5 else 20, float(k))]
        fit = m.monthly_fit(days)
        self.assertEqual(fit.months, 5)
        self.assertIsNone(fit.r2)
        self.assertFalse(fit.usable)


class TestHouseholdDays(unittest.TestCase):
    def household(self) -> m.Household:
        start = at(dt.date(2026, 7, 10), 23)
        return m.Household("x", WALLBOX, "timer", "wallbox, timer, 1",
                           sessions=[(start, start + 3 * m.HALF_HOUR_S, (3.6, 3.6, 2.8))])

    def test_a_session_splits_its_kwh_at_nz_midnight(self):
        h = self.household()
        days = h.daily_kwh()
        self.assertAlmostEqual(days[dt.date(2026, 7, 10)], 7.2)
        self.assertAlmostEqual(days[dt.date(2026, 7, 11)], 2.8)
        self.assertEqual(h.first_day, dt.date(2026, 7, 10))
        self.assertTrue(h.is_ev)

    def test_half_hours_are_counted_only_where_the_load_drew(self):
        start = at(dt.date(2026, 7, 10), 23)
        h = m.Household("x", WALLBOX, "timer", "wallbox, timer, 1",
                        sessions=[(start, start + 3 * m.HALF_HOUR_S, (3.6, 3.6, 0.0))])
        self.assertEqual(h.daily_half_hours(), {dt.date(2026, 7, 10): 2})

    def test_a_session_counts_only_if_every_date_it_touches_counts(self):
        d1, d2 = dt.date(2026, 7, 10), dt.date(2026, 7, 11)
        start, end = at(d1, 23, 30), at(d2, 0, 30)
        self.assertFalse(m.eligible_days({d1}, set(), start, end))
        self.assertTrue(m.eligible_days({d1, d2}, set(), start, end))
        self.assertFalse(m.eligible_days({d1, d2}, {d2}, start, end))

    def test_a_naive_timestamp_is_refused(self):
        with self.assertRaises(ValueError):
            m._ts(dt.datetime(2026, 7, 10, 23))


class TestHardshipReplay(unittest.TestCase):
    def test_one_alarm_per_run_of_weeks(self):
        self.assertEqual(m.episodes([False, True, True, False, True]), 2)
        self.assertEqual(m.episodes([True]), 1)
        self.assertEqual(m.episodes([]), 0)

    def test_usage_window_end_at_a_past_date(self):
        as_of = dt.date(2026, 3, 1)
        self.assertIsNone(m.replay_usage_to(None, as_of, 31))
        self.assertEqual(m.replay_usage_to(dt.date(2026, 3, 10), as_of, 31), as_of)
        self.assertEqual(m.replay_usage_to(dt.date(2026, 2, 1), as_of, 31), dt.date(2026, 2, 1))
        self.assertIsNone(m.replay_usage_to(dt.date(2026, 1, 1), as_of, 31))

    def test_paid_is_summed_by_date_not_read_from_the_bill(self):
        self.assertNotIn("payments_applied", m.REPLAY_BILL_SQL)

    def test_the_replay_selects_the_same_bills_as_the_job(self):
        def where(sql: str) -> str:
            return " ".join(sql.split("\nwhere ", 1)[1].split("\ngroup by", 1)[0].split())
        self.assertEqual(where(m.REPLAY_BILL_SQL), where(H.BILL_SQL))

    def test_early_is_the_alert_jobs_own_rule(self):
        day = dt.date(2026, 3, 1)
        self.assertTrue(m.is_early({"bill_above_usual_payment", "payment_drift"}, 1, day))
        self.assertTrue(m.is_early({"bill_above_usual_payment", "first_shortfall"}, 1, day))
        self.assertFalse(m.is_early({"bill_above_usual_payment"}, 1, day))
        self.assertFalse(m.is_early({"payment_drift", "first_shortfall"}, 1, day))
        self.assertFalse(m.is_early(
            {"bill_above_usual_payment", "payment_drift", "late_payment_streak"}, 1, day))


class TestReportParts(unittest.TestCase):
    def test_formatting(self):
        self.assertEqual(m.fmt(None), "–")
        self.assertEqual(m.fmt(0.9123, pct=True), "91.2%")
        self.assertEqual(m.fmt(1234), "1,234")
        self.assertEqual(m.verdict(None), "NO DATA")
        self.assertIsNone(m.share(1, 0))
        self.assertIsNone(m.median([None]))

    def test_generated_artifacts_use_lf_and_one_final_newline(self):
        rep = m.Report()
        rep.add("# Report", "", "A result.", "")
        rep.metric("sessions", "precision", 1.0, 0.9, True)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            m.write_results(out, rep)
            report = (out / "report.md").read_bytes()
            metrics = (out / "metrics.csv").read_bytes()
        self.assertTrue(report.endswith(b"\n"))
        self.assertFalse(report.endswith(b"\n\n"))
        self.assertNotIn(b"\r", metrics)
        self.assertTrue(metrics.endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()


SOLAR = "ev_solar_wallbox_7kw"


class TestMaskingSplit(unittest.TestCase):
    """§1a: the split the solar+EV households exist to produce."""

    DAY = dt.date(2026, 7, 10)
    EARLIER = dt.date(2026, 7, 1)

    def setUp(self):
        d, d0 = self.DAY, self.EARLIER
        self.clear = at(d, 23)                      # 23:00, the meter sees it whole
        self.shaded = at(d, 12)                     # midday, under the array
        self.h = m.Household("09X", SOLAR, "midday", "solar wallbox, midday, 1", icp_id=7,
                             sessions=[(self.shaded, self.shaded + 2 * m.HALF_HOUR_S,
                                        (3.6, 3.6)),
                                       (self.clear, self.clear + 2 * m.HALF_HOUR_S,
                                        (3.6, 3.6))])
        self.assessed = {7: {d0, d}}
        self.excluded: dict[str, set[dt.date]] = {}
        # Exporting or between the day's first and last export, read from silver.
        self.hidden = {"09X": {at(d, 12), at(d, 12, 30), at(d, 13), at(d, 13, 30)}}
        self.detected = {7: [
            (self.clear, self.clear + 2 * m.HALF_HOUR_S, "high_power", 0),
            (self.shaded, self.shaded + 2 * m.HALF_HOUR_S, None, 2),
            (at(d, 13), at(d, 13) + 2 * m.HALF_HOUR_S, "low_power", 1),
            (at(d0, 19), at(d0, 19) + 2 * m.HALF_HOUR_S, "high_power", 0),
        ]}

    def run_split(self) -> dict:
        rep = m.Report()
        m.measure_masking_split(rep, [self.h], self.detected, self.assessed,
                                self.excluded, self.hidden)
        return {x.measure: x for x in rep.metrics}

    def test_one_hidden_half_hour_is_enough_and_the_end_is_not_one(self):
        start = at(self.DAY, 12)
        self.assertTrue(m.is_hidden(start, start + 2 * m.HALF_HOUR_S, {at(self.DAY, 12, 30)}))
        # The instant a session ends belongs to the next half-hour, not to it.
        self.assertFalse(m.is_hidden(start, start + 2 * m.HALF_HOUR_S, {at(self.DAY, 13)}))
        self.assertFalse(m.is_hidden(start, start + 2 * m.HALF_HOUR_S, set()))

    def test_a_withheld_class_still_counts_as_found(self):
        got = self.run_split()
        # The midday session is published with charger_class NULL. Scoring only
        # high_power here would read detect-v2's refusal as a lost detection.
        self.assertEqual(got["solar_recall_hidden"].value, 1.0)
        self.assertEqual(got["solar_recall_clear"].value, 1.0)

    def test_low_power_on_a_hidden_run_is_a_miss_against_a_hard_zero(self):
        got = self.run_split()
        self.assertEqual(got["low_power_claims_on_masked_runs"].value, 1)
        self.assertIs(got["low_power_claims_on_masked_runs"].passed, False)
        self.assertEqual(m.TARGETS["low_power_claims_on_masked_runs"], 0)

    def test_a_low_power_run_the_panels_did_not_touch_is_not_a_miss(self):
        d = self.DAY
        self.detected[7][2] = (at(d, 20), at(d, 20) + 2 * m.HALF_HOUR_S, "low_power", 0)
        got = self.run_split()
        self.assertEqual(got["low_power_claims_on_masked_runs"].value, 0)
        self.assertIs(got["low_power_claims_on_masked_runs"].passed, True)
        self.assertEqual(got["solar_low_power_runs"].value, 1)

    def test_the_eight_without_panels_produce_no_split_at_all(self):
        rep = m.Report()
        plain = m.Household("09Y", WALLBOX, "timer", "wallbox, timer, 1", icp_id=8)
        m.measure_masking_split(rep, [plain], {}, {}, {}, {})
        self.assertEqual(rep.lines, [])
        self.assertEqual(rep.metrics, [])


class TestSessionsSection(unittest.TestCase):
    """§1 against the same row shape §1a uses.

    ⚠️ THE POINT OF THIS CLASS. A detected row grew a fourth field,
    masked_half_hours, when detect-v2 landed, and §1's filter went on
    destructuring three -- so `make evaluate-sprint4` died on the first solar
    household while all 27 unit tests passed, because nothing here had ever
    called measure_sessions. The section is cheap to run; not running it was
    the whole gap.
    """

    DAY = dt.date(2026, 7, 10)

    def setUp(self):
        d = self.DAY
        self.start = at(d, 23)
        self.h = m.Household("09Z", WALLBOX, "timer", "wallbox, timer, 1", icp_id=9,
                             sessions=[(self.start, self.start + 2 * m.HALF_HOUR_S,
                                        (3.6, 3.6))])
        self.detected = {9: [
            (self.start, self.start + 2 * m.HALF_HOUR_S, "high_power", 0),
        ]}

    def test_a_detected_row_carrying_its_masked_count_is_accepted(self):
        rep = m.Report()
        m.measure_sessions(rep, [self.h], self.detected, {9: {self.DAY}}, {}, {})
        got = {x.measure: x for x in rep.metrics}
        self.assertEqual(got["high_power_recall"].value, 1.0)
        self.assertEqual(got["high_power_precision"].value, 1.0)

    def test_the_filter_does_not_depend_on_the_row_width(self):
        # Widen the row again and the section must still run: §1 needs the span
        # and the class, and nothing about how many fields follow them.
        self.detected[9] = [(self.start, self.start + 2 * m.HALF_HOUR_S,
                             "high_power", 0, "a later column")]
        rep = m.Report()
        m.measure_sessions(rep, [self.h], self.detected, {9: {self.DAY}}, {}, {})
        self.assertEqual({x.measure: x for x in rep.metrics}["high_power_recall"].value, 1.0)


class TestUnlabelledGroups(unittest.TestCase):
    def test_a_planted_solar_household_leaves_the_solar_group(self):
        rep = m.Report()
        cohort = {1: "09A", 2: "09B", 3: "09C", 4: "09D"}
        m.measure_unlabelled(rep, cohort, planted_ids={1, 3}, solar_ids={3, 4},
                             detected={1: [(0, 0, "high_power", 0)],
                                       3: [(0, 0, "high_power", 0)],
                                       4: [(0, 0, "low_power", 0)]},
                             daily={i: {dt.date(2026, 7, 10): {"tn_hh": 0}} for i in cohort})
        got = {x.measure: x.value for x in rep.metrics}
        # 2 alone is unplanted and unpanelled; 4 alone is unplanted with panels.
        self.assertEqual(got["households_with_ev_sessions_no_solar"], 0)
        self.assertEqual(got["households_with_ev_sessions_solar"], 1)
