"""The solar estimate's arithmetic: sun, netting, pricing and payback.

No database: the SQL is exercised by a publication, and the rules that decide
what a customer is told are pure functions over half-hours and a tariff.
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "decisions"))

import build_plan_recommendations as plans  # noqa: E402
import build_solar_estimates as m  # noqa: E402
import estimate_common as common  # noqa: E402

AUCKLAND = (-37.008, 174.789)
WEEK = frozenset(range(1, 8))
NO_BATTERY = (Decimal("0"),)


def flat_tariff(rate="0.30", buy_back="0.08"):
    return plans.Tariff(
        name="flat",
        bands=(plans.Band("energy", "ANY", WEEK, 0, 1440, Decimal(rate)),),
        flat=(plans.FlatCharge("other", "daily", Decimal("1.00"), "day"),),
        export_bands=(plans.Band("energy", "ANY", WEEK, 0, 1440, Decimal(buy_back)),))


def day_of_half_hours(day: dt.date, kwh: float, offset=780) -> list[common.HalfHour]:
    start = dt.datetime.combine(day, dt.time())
    return [common.HalfHour(at=start + dt.timedelta(minutes=30 * k - offset),
                            loc=start + dt.timedelta(minutes=30 * k),
                            utc_offset_min=offset, import_kwh=kwh, export_kwh=0.0)
            for k in range(48)]


class TestSun(unittest.TestCase):
    def test_auckland_solstice_day_lengths_are_plausible(self):
        # About 14 h 25 min in December and 9 h 40 min in June.
        rise, sets = m.sun_times(dt.date(2025, 12, 21), *AUCKLAND, 780)
        self.assertTrue(14 * 60 < sets - rise < 14 * 60 + 45)
        self.assertTrue(5 * 60 + 45 < rise < 6 * 60 + 15)       # NZDT
        rise, sets = m.sun_times(dt.date(2025, 6, 21), *AUCKLAND, 720)
        self.assertTrue(9 * 60 + 20 < sets - rise < 10 * 60)
        self.assertTrue(7 * 60 + 15 < rise < 7 * 60 + 45)       # NZST

    def test_a_days_shares_add_up_to_one_and_are_zero_at_night(self):
        rise, sets = m.sun_times(dt.date(2025, 3, 1), *AUCKLAND, 780)
        shares = [m.daylight_share(rise, sets, 30 * k) for k in range(48)]
        self.assertAlmostEqual(sum(shares), 1.0, places=9)
        self.assertEqual(shares[0], 0.0)
        self.assertEqual(shares[47], 0.0)
        noon = max(range(48), key=shares.__getitem__)
        self.assertIn(noon, (26, 27))                           # 13:00-14:00 NZDT


class TestNetting(unittest.TestCase):
    def test_panels_cover_import_first_and_never_make_it_negative(self):
        self.assertEqual(m.net_half_hour(1.0, 0.0, 3.0), (0.0, 2.0, 1.0))
        self.assertEqual(m.net_half_hour(1.0, 0.0, 0.25), (0.75, 0.0, 0.25))

    def test_netting_each_half_hour_differs_from_netting_the_profile(self):
        # Two Mondays at noon: one draws 2 kWh with no sun, one draws nothing
        # in full sun. Netted per half-hour the house imports 2 and exports 2;
        # netted after grouping the two would cancel to nothing.
        monday, next_monday = dt.datetime(2025, 3, 3, 12), dt.datetime(2025, 3, 10, 12)
        p = common.Profile()
        for loc, imp, gen in ((monday, 2.0, 0.0), (next_monday, 0.0, 2.0)):
            i, e, _ = m.net_half_hour(imp, 0.0, gen)
            p.add(loc, i, exported=False)
            p.add(loc, e, exported=True)
        self.assertEqual(p.total(exported=False), 2.0)
        self.assertEqual(p.total(exported=True), 2.0)
        cells = [(r.at.isoweekday(), r.at.hour, r.exported, r.kwh) for r in p.readings()]
        self.assertEqual(cells, [(1, 12, False, Decimal("2.0")),
                                 (1, 12, True, Decimal("2.0"))])

    def test_a_negative_cell_is_refused(self):
        with self.assertRaises(ValueError):
            common.Profile().add(dt.datetime(2025, 3, 3), -0.1, exported=False)


class TestEstimate(unittest.TestCase):
    region_zero = {"low": dict.fromkeys(range(1, 13), 0.0),
                   "high": dict.fromkeys(range(1, 13), 0.0), "lat": AUCKLAND[0],
                   "lon": AUCKLAND[1]}

    def test_zero_yield_saves_nothing_and_has_no_payback(self):
        hh = day_of_half_hours(dt.date(2025, 1, 15), 0.5)
        (e,) = m.estimate_account(hh, flat_tariff(), self.region_zero,
                                  system_kws=(Decimal("5"),), battery_kwhs=NO_BATTERY)
        self.assertEqual((e["saving_low"], e["saving_high"]), (0, 0))
        self.assertEqual((e["payback_low"], e["payback_high"]), (None, None))
        self.assertEqual(e["generation_high"], 0)
        self.assertEqual((e["cost_low"], e["cost_high"]),
                         (Decimal("10000.00"), Decimal("15000.00")))

    def test_saving_is_self_use_at_the_import_rate_plus_export_at_buy_back(self):
        # 4 kWh/kW/day x 1 kW on a house drawing 0.5 kWh every half-hour.
        region = {"low": dict.fromkeys(range(1, 13), 4.0),
                  "high": dict.fromkeys(range(1, 13), 4.0),
                  "lat": AUCKLAND[0], "lon": AUCKLAND[1]}
        hh = day_of_half_hours(dt.date(2025, 1, 15), 0.5)
        (e,) = m.estimate_account(hh, flat_tariff(), region, system_kws=(Decimal("1"),),
                                  battery_kwhs=NO_BATTERY)
        shares = m.solar_shares(hh, *AUCKLAND)
        used = sum(min(0.5, 4.0 * s) for s in shares)
        exported = 4.0 - used
        expected = round(Decimal(str(used)) * Decimal("0.30") * Decimal("1.15")
                         + Decimal(str(exported)) * Decimal("0.08") * Decimal("1.15"), 2)
        self.assertAlmostEqual(float(e["saving_low"]), float(expected), delta=0.011)
        self.assertEqual(e["generation_low"], Decimal("4.0"))
        self.assertEqual(e["self_used_low"], round(Decimal(str(used / 4.0 * 100)), 2))

    def test_payback_best_case_is_never_above_the_worst_case(self):
        best, worst = m.payback_range(Decimal("6000"), Decimal("9000"),
                                      Decimal("300"), Decimal("500"))
        self.assertEqual((best, worst), (Decimal("12.0"), Decimal("30.0")))
        self.assertLessEqual(best, worst)
        # Only the best case pays back: the worst case has nothing to state.
        self.assertEqual(m.payback_range(Decimal("6000"), Decimal("9000"),
                                         Decimal("0"), Decimal("500")),
                         (Decimal("12.0"), None))
        self.assertEqual(m.payback_range(Decimal("6000"), Decimal("9000"),
                                         Decimal("-5"), Decimal("0")), (None, None))


class TestBattery(unittest.TestCase):
    # 4 kWh/kW/day all year, so a 5 kW array makes 20 kWh a day.
    region = {"low": dict.fromkeys(range(1, 13), 4.0),
              "high": dict.fromkeys(range(1, 13), 4.0),
              "lat": AUCKLAND[0], "lon": AUCKLAND[1]}

    def run_days(self, hh, battery_kwh, system_kw=5.0):
        shares = m.solar_shares(hh, *AUCKLAND)
        return m.simulate(hh, shares, self.region["low"], system_kw, battery_kwh)

    def test_surplus_charges_up_to_power_and_capacity_with_the_loss_on_the_way_in(self):
        # 2.5 kWh is the most 5 kW moves in half an hour; 90% of it is kept.
        imp, surplus, stored, out = m.battery_half_hour(0.0, 4.0, 0.0, 13.5)
        self.assertEqual((imp, surplus, out), (0.0, 1.5, 0.0))
        self.assertAlmostEqual(stored, 2.25)
        # Nearly full: only what fits is taken from the surplus.
        imp, surplus, stored, out = m.battery_half_hour(0.0, 4.0, 13.05, 13.5)
        self.assertAlmostEqual(surplus, 3.5)
        self.assertAlmostEqual(stored, 13.5)
        # Full: all of the surplus is exported.
        self.assertEqual(m.battery_half_hour(0.0, 4.0, 13.5, 13.5),
                         (0.0, 4.0, 13.5, 0.0))

    def test_import_is_met_up_to_power_and_what_is_stored(self):
        imp, surplus, stored, out = m.battery_half_hour(4.0, 0.0, 10.0, 13.5)
        self.assertEqual((imp, surplus, stored, out), (1.5, 0.0, 7.5, 2.5))
        imp, surplus, stored, out = m.battery_half_hour(1.0, 0.0, 0.4, 13.5)
        self.assertAlmostEqual(imp, 0.6)
        self.assertEqual((stored, out), (0.0, 0.4))

    def test_no_battery_is_the_estimate_as_it_was(self):
        hh = day_of_half_hours(dt.date(2025, 1, 15), 0.5)
        (plain,) = m.estimate_account(hh, flat_tariff(), self.region,
                                      system_kws=(Decimal("5"),), battery_kwhs=NO_BATTERY)
        both = m.estimate_account(hh, flat_tariff(), self.region, system_kws=(Decimal("5"),))
        self.assertEqual([e["battery_kwh"] for e in both], [Decimal("0"), Decimal("13.5")])
        self.assertEqual(both[0], plain)
        profile, generated, used = self.run_days(hh, 0.0)
        shares = m.solar_shares(hh, *AUCKLAND)
        self.assertAlmostEqual(used, sum(min(0.5, 20.0 * s) for s in shares))
        self.assertAlmostEqual(profile.total(exported=True), generated - used)

    def test_a_battery_moves_surplus_to_the_evening_and_loses_a_tenth(self):
        hh = (day_of_half_hours(dt.date(2025, 1, 15), 0.5)
              + day_of_half_hours(dt.date(2025, 1, 16), 0.5))
        plain, generated, used_plain = self.run_days(hh, 0.0)
        stored, generated_b, used_b = self.run_days(hh, 13.5)
        self.assertEqual(generated_b, generated)
        less_import = plain.total(exported=False) - stored.total(exported=False)
        less_export = plain.total(exported=True) - stored.total(exported=True)
        self.assertGreater(less_import, 0)
        # Every kWh the home drew from the battery is a kWh it did not import,
        # and is counted as self-used.
        self.assertAlmostEqual(used_b - used_plain, less_import)
        # What came out is never more than 90% of what went in; the difference
        # is what the battery still held when the readings ended.
        self.assertLessEqual(less_import, less_export * 0.9 + 1e-9)
        self.assertLessEqual(less_export * 0.9 - less_import, 13.5 + 1e-9)

    def test_a_gap_in_the_readings_empties_the_battery(self):
        day = day_of_half_hours(dt.date(2025, 1, 15), 0.5)
        # 20:00 to midnight, on its own it starts with an empty battery.
        late = day_of_half_hours(dt.date(2025, 1, 17), 0.5)[40:]
        alone, _, _ = self.run_days(late, 13.5)
        whole, _, _ = self.run_days(day, 13.5)
        until_eight, _, _ = self.run_days(day[:40], 13.5)
        # Straight after a sunny day, those four hours run off the battery ...
        self.assertLess(whole.total(exported=False) - until_eight.total(exported=False),
                        alone.total(exported=False))
        # ... and after a day nobody metered, they import as if it were empty.
        gapped, _, _ = self.run_days(day + late, 13.5)
        self.assertAlmostEqual(
            gapped.total(exported=False) - whole.total(exported=False),
            alone.total(exported=False))

    def test_a_battery_row_costs_panels_plus_battery(self):
        self.assertEqual(m.install_cost(Decimal("5"), Decimal("0")),
                         (Decimal("10000"), Decimal("15000")))
        self.assertEqual(m.install_cost(Decimal("5"), Decimal("13.5")),
                         (Decimal("22000"), Decimal("33000")))

    def test_on_a_flat_plan_the_battery_saves_more_and_still_pays_back_later(self):
        hh = day_of_half_hours(dt.date(2025, 1, 15), 0.5)
        plain, stored = m.estimate_account(hh, flat_tariff(), self.region,
                                           system_kws=(Decimal("5"),))
        # 0.9 kWh back at $0.30 beats 1 kWh exported at $0.08.
        self.assertGreater(stored["saving_low"], plain["saving_low"])
        self.assertGreater(stored["self_used_low"], plain["self_used_low"])
        self.assertGreater(stored["payback_low"], plain["payback_low"])
        self.assertEqual(stored["generation_low"], plain["generation_low"])

    def test_a_buy_back_near_the_import_rate_makes_the_battery_the_worse_row(self):
        # Storing loses a tenth; exporting at $0.29 loses a thirtieth.
        hh = day_of_half_hours(dt.date(2025, 1, 15), 0.5)
        plain, stored = m.estimate_account(hh, flat_tariff(buy_back="0.29"), self.region,
                                           system_kws=(Decimal("5"),))
        self.assertLess(stored["saving_low"], plain["saving_low"])


class TestCoverage(unittest.TestCase):
    def test_a_year_needs_most_of_its_half_hours(self):
        lo = dt.datetime(2024, 9, 28, 12, tzinfo=dt.UTC)
        window = common.Window(dt.date(2024, 9, 29), dt.date(2025, 9, 28),
                               lo, lo + dt.timedelta(days=365))
        day = day_of_half_hours(dt.date(2025, 1, 15), 0.5)
        self.assertTrue(common.covers_window(day * 330, window))    # 90.4%
        self.assertFalse(common.covers_window(day * 328, window))   # 89.9%
        self.assertFalse(common.covers_window([], window))


class TestInputs(unittest.TestCase):
    def test_the_yield_table_covers_every_region_and_month(self):
        yields = m.load_yields()
        self.assertEqual(sorted(yields),
                         ["Auckland", "Christchurch", "Waikato", "Wellington"])
        for region in yields.values():
            # Summer out-produces winter in every region.
            self.assertGreater(region["low"][1], region["high"][6])

    def test_a_region_maps_as_dim_icp_maps_it(self):
        yields = m.load_yields()
        seed = m.load_region_map()
        self.assertEqual(m.weather_region("Northland", seed, yields), "Auckland")
        self.assertEqual(m.weather_region("Wellington", seed, yields), "Wellington")
        self.assertIsNone(m.weather_region("Chatham Islands", seed, yields))
        self.assertIsNone(m.weather_region(None, seed, yields))


if __name__ == "__main__":
    unittest.main()
