"""Tests for the solar sizing in cohort.py. No dataset: cohort.json and arithmetic.

The array a household gets is the one place where the generator's solar and its
load are coupled. Two things must stay true, and neither is visible in a diff:
the coupling runs one way only (size depends on load; membership does not), and
the NZ figure the size is derived from is the same one gen_metering later scales
to.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import cohort as m  # noqa: E402
import gen_metering as g  # noqa: E402

SPEC = json.loads((HERE / "cohort.json").read_text())
COHORT = SPEC["households"]


class TestConstantsAgree(unittest.TestCase):
    """cohort.py restates two of gen_metering's constants, because it runs
    before scale.json exists and cannot import a measured number. Restating is
    fine; drifting is not."""

    def test_the_nz_target_matches_gen_metering(self):
        self.assertEqual(m.NZ_TARGET_MEAN_KWH_DAY, g.NZ_TARGET_MEAN_KWH_DAY)

    def test_the_yield_matches_gen_metering(self):
        self.assertEqual(m.NZ_YIELD_KWH_PER_KWP, g.NZ_YIELD_KWH_PER_KWP)


class TestSizing(unittest.TestCase):
    def test_a_size_is_always_a_real_product(self):
        for kwh in range(0, 40_000, 137):
            self.assertIn(m.pv_kwp(kwh), m.PV_SIZES_KWP)

    def test_nothing_falls_below_the_floor(self):
        self.assertEqual(m.pv_kwp(0.0), min(m.PV_SIZES_KWP))
        self.assertEqual(m.pv_kwp(1.0), min(m.PV_SIZES_KWP))

    def test_it_is_monotonic_in_load(self):
        sizes = [m.pv_kwp(kwh) for kwh in range(0, 40_000, 250)]
        self.assertEqual(sizes, sorted(sizes))

    def test_it_rounds_to_the_nearest_size_not_down(self):
        # 2.91 kWp of need is a 3 kWp roof, not a 2 kWp one. Rounding down was
        # the first attempt and under-sized four of the eight households.
        need = 2.91
        self.assertEqual(m.pv_kwp(need * m.NZ_YIELD_KWH_PER_KWP / m.PV_LOAD_FRACTION), 3.0)

    def test_a_tie_takes_the_smaller_size(self):
        need = 2.5   # exactly between 2 and 3
        self.assertEqual(m.pv_kwp(need * m.NZ_YIELD_KWH_PER_KWP / m.PV_LOAD_FRACTION), 2.0)


class TestTheCommittedCohort(unittest.TestCase):
    """cohort.json IS the selection, so these assert the file itself."""

    def test_every_solar_household_has_an_array(self):
        for h in COHORT:
            if h["has_solar"]:
                self.assertIn(h["pv_kwp"], m.PV_SIZES_KWP, h["lclid"])

    def test_a_household_without_panels_has_no_array(self):
        for h in COHORT:
            if not h["has_solar"]:
                self.assertIsNone(h["pv_kwp"], h["lclid"])

    def test_the_arrays_are_not_all_the_same(self):
        # The defect this work fixed: eight households, one 4 kWp roof between
        # them, regardless of whether they used 6.6 or 47 kWh a day.
        sizes = {h["pv_kwp"] for h in COHORT if h["has_solar"]}
        self.assertGreater(len(sizes), 2, "arrays are no longer sized to load")

    def test_who_has_solar_is_still_only_a_hash(self):
        # ⚠️ The coupling that must NOT exist. planted.candidates() draws from
        # the households WITHOUT solar, so making membership depend on load
        # would move a household between the two pools and reassign every
        # planted load in Sprint 4.
        chosen = [{"LCLid": h["lclid"], "Acorn_grouped": h["acorn_grouped"] or "ACORN-U"}
                  for h in COHORT]
        expected = m.solar_households(chosen)
        self.assertEqual({h["lclid"] for h in COHORT if h["has_solar"]}, expected)

    def test_the_solar_count_is_the_fixed_share(self):
        self.assertEqual(sum(h["has_solar"] for h in COHORT), round(m.SOLAR_SHARE * len(COHORT)))

    def test_every_array_is_what_its_own_load_asks_for(self):
        # pv_kwp is written beside the load that produced it, so the file can
        # be checked without re-scanning 115 blocks of LCL readings. This is
        # the roof BEFORE any planted car; planted.SOLAR_PLAN re-sizes three
        # of them, and gen_metering, not this file, resolves the two.
        for h in COHORT:
            if h["has_solar"]:
                self.assertEqual(h["pv_kwp"], m.pv_kwp(h["nz_annual_kwh"]), h["lclid"])

    def test_the_cohort_lands_on_the_nz_target(self):
        # The per-household loads are the global scale factor's arithmetic one
        # step earlier, so their mean must be the target it aims at.
        loads = [h["nz_annual_kwh"] for h in COHORT]
        self.assertTrue(all(x > 0 for x in loads))
        self.assertAlmostEqual(sum(loads) / len(loads) / 365.25,
                               m.NZ_TARGET_MEAN_KWH_DAY, delta=2.0)

    def test_the_solar_pool_offers_more_households_than_places(self):
        # SOLAR_PLAN has three places and the screen rejects most of the pool
        # on its own load -- at 50 households, four of the eight fit. The margin is
        # thin enough that a re-run of cohort.py which moved a household out
        # of solar could empty it, and select() would then raise rather than
        # quietly plant nothing; fail here instead, where the cause is clear.
        import planted as p  # noqa: PLC0415
        self.assertGreater(len(p.solar_candidates(COHORT)), len(p.SOLAR_PLAN))

    def test_the_planted_arrays_are_bigger_than_the_cohort_s(self):
        # SOLAR_PLAN sizes a roof for a household that has an EV; cohort.py
        # sizes it for one that does not. If a declared array ever came out
        # no larger than what the pool's own loads ask for, declaring it
        # would be pointless and the override should be deleted rather than
        # left as a second source of truth.
        import planted as p  # noqa: PLC0415
        pool = p.solar_candidates(COHORT)
        self.assertGreater(max(p.SOLAR_ARRAY_KWP.values()),
                           max(h["pv_kwp"] for h in pool[1:]))

    def test_a_declared_array_is_still_a_real_product_size(self):
        import planted as p  # noqa: PLC0415
        for pattern, kwp in p.SOLAR_ARRAY_KWP.items():
            self.assertIn(kwp, m.PV_SIZES_KWP, pattern)


if __name__ == "__main__":
    unittest.main()
