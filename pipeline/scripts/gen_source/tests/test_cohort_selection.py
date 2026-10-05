"""Tests for cohort.select() and allocate(). No dataset: synthetic households.

The rule is design/kea-cohort-selection.md §4: places by each group's share of
ALL households, at least one per group, capped at what is eligible, an exact
total, and within a group a sample spread along usage.
"""

from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cohort as m  # noqa: E402


def households(spec: dict[tuple[str, str], tuple[int, int]]) -> tuple[list[dict], set[str]]:
    """{(tariff, acorn): (in the population, of whom eligible)} -> rows and the eligible ids."""
    rows, eligible = [], set()
    for (tariff, acorn), (total, ok) in spec.items():
        for i in range(total):
            lcl = f"{tariff}-{acorn}-{i:04d}"
            rows.append({"LCLid": lcl, "stdorToU": tariff, "Acorn_grouped": acorn})
            if i < ok:
                eligible.add(lcl)
    return rows, eligible


# The shape of the real problem: the eligible pool over-represents Affluent.
SPEC = {("Std", "Affluent"): (300, 120), ("Std", "Adversity"): (300, 40),
        ("ToU", "Comfortable"): (100, 20), ("Std", "ACORN-U"): (10, 2)}
ROWS, ELIGIBLE = households(SPEC)
MEAN = {r["LCLid"]: float(int(r["LCLid"][-4:])) for r in ROWS}   # usage rises with the index


class TestAllocate(unittest.TestCase):
    def test_the_total_is_exact(self):
        share = {"a": 0.5, "b": 0.3, "c": 0.2}
        caps = {"a": 100, "b": 100, "c": 100}
        for n in range(3, 120):
            self.assertEqual(sum(m.allocate(n, share, caps).values()), n)

    def test_quotas_follow_the_population_not_the_caps(self):
        take = m.allocate(100, {"a": 0.5, "b": 0.5}, {"a": 400, "b": 60})
        self.assertEqual(take, {"a": 50, "b": 50})

    def test_every_group_gets_a_place_however_small(self):
        take = m.allocate(20, {"a": 0.99, "b": 0.01}, {"a": 50, "b": 3})
        self.assertGreaterEqual(take["b"], 1)

    def test_a_group_that_runs_out_passes_its_quota_on(self):
        take = m.allocate(100, {"a": 0.5, "b": 0.5}, {"a": 400, "b": 10})
        self.assertEqual(take, {"a": 90, "b": 10})

    def test_an_empty_group_gets_nothing(self):
        take = m.allocate(10, {"a": 0.5, "b": 0.5}, {"a": 50, "b": 0})
        self.assertEqual(take, {"a": 10})

    def test_too_many_raises_rather_than_shrinking(self):
        with self.assertRaises(ValueError):
            m.allocate(61, {"a": 0.5, "b": 0.5}, {"a": 30, "b": 30})


class TestSelect(unittest.TestCase):
    def setUp(self):
        self.chosen, self.take = m.select(ROWS, ELIGIBLE, MEAN, 60)

    def test_the_size_is_exact_and_every_pick_eligible_and_distinct(self):
        ids = [h["LCLid"] for h in self.chosen]
        self.assertEqual(len(ids), 60)
        self.assertEqual(len(set(ids)), 60)
        self.assertTrue(set(ids) <= ELIGIBLE)

    def test_the_mix_is_the_population_s_not_the_pool_s(self):
        # Affluent is 120 of 182 eligible (66%) but 300 of 710 households (42%).
        got = Counter(h["Acorn_grouped"] for h in self.chosen)
        self.assertLess(got["Affluent"] / 60, 0.50)
        self.assertGreater(got["Adversity"], 60 * 40 / 182)

    def test_the_unclassified_group_is_present(self):
        self.assertIn(("Std", "ACORN-U"), self.take)
        self.assertGreaterEqual(self.take[("Std", "ACORN-U")], 1)

    def test_a_group_s_sample_spans_its_usage(self):
        # 120 eligible Std/Affluent, usage 0..119; whatever it takes must reach
        # both the lightest and the heaviest sixth of them.
        picks = sorted(MEAN[h["LCLid"]] for h in self.chosen
                       if h["stdorToU"] == "Std" and h["Acorn_grouped"] == "Affluent")
        self.assertLess(picks[0], 20)
        self.assertGreater(picks[-1], 100)

    def test_it_is_deterministic(self):
        again, _ = m.select(ROWS, ELIGIBLE, MEAN, 60)
        self.assertEqual([h["LCLid"] for h in again], [h["LCLid"] for h in self.chosen])

    def test_the_final_order_is_the_seed_hash(self):
        # The ICP's last two digits are the position in this list.
        keys = [m._h(h["LCLid"], m.SEED) for h in self.chosen]
        self.assertEqual(keys, sorted(keys))


class TestSolar(unittest.TestCase):
    def setUp(self):
        self.chosen, _ = m.select(ROWS, ELIGIBLE, MEAN, 60)

    def test_the_count_is_the_fixed_share(self):
        self.assertEqual(len(m.solar_households(self.chosen)), round(m.SOLAR_SHARE * 60))

    def test_membership_reads_only_the_id_and_the_acorn_group(self):
        # Nothing about load reaches it: the same households, in any order and
        # carrying any other field, get the same panels.
        noisy = [dict(h, mean_kwh=i) for i, h in enumerate(reversed(self.chosen))]
        self.assertEqual(m.solar_households(noisy), m.solar_households(self.chosen))


if __name__ == "__main__":
    unittest.main()
