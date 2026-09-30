"""The forecast backtest dashboard builder: what its page promises about itself.

No database and no Metabase. The page is generated from the committed results
in analysis/forecast_backtest/results/, so every check here is about that
translation, and none pins a figure -- a re-run of the backtest changes the
numbers, not these promises:

  * every card is built from the results and reads no table at all;
  * a report whose table has moved or changed shape stops the build instead of
    drawing something else;
  * text cannot break out of a SQL literal;
  * the Stage 2 panels cover every days-elapsed bucket, in cycle order;
  * kNN and v2 keep one colour each on every chart.

verify.py covers what only a live Metabase can: that each card runs, returns
rows and uses every column it selects.
"""

from __future__ import annotations

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

DATA = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, DATA / "analysis" / "metabase" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("build_backtest_dashboard")
SECTIONS, LAYOUT = m.sections()
CARDS = {name: (display, sql) for _, panels in SECTIONS for name, display, _w, sql in panels}


def rows_of(sql: str) -> list[str]:
    body = sql.split("FROM (VALUES ", 1)[1].split("\n     AS t(", 1)[0]
    return [r.strip().rstrip(",") for r in body.split("\n")]


class BuiltFromTheResults(unittest.TestCase):

    def test_names_are_unique(self):
        names = [name for _, panels in SECTIONS for name, *_ in panels]
        self.assertEqual(len(names), len(set(names)))

    def test_no_card_reads_a_table(self):
        for name, (_display, sql) in CARDS.items():
            with self.subTest(card=name):
                self.assertIn("FROM (VALUES", sql)
                self.assertNotRegex(sql, r"\b(src_\w+|kea|billing|metering|lake_audit)\.")

    def test_every_card_names_its_source(self):
        for name, (_display, sql) in CARDS.items():
            with self.subTest(card=name):
                self.assertRegex(sql.splitlines()[0], r"^-- From analysis/forecast_backtest/results/\S+")

    def test_layout_only_names_cards_that_are_built(self):
        self.assertEqual([n for n in LAYOUT if n not in CARDS], [])

    def test_check_mode_passes(self):
        m.check()


class Stage2Panels(unittest.TestCase):

    def test_each_chart_covers_every_bucket_in_cycle_order(self):
        for name, (display, sql) in CARDS.items():
            if display in ("bar", "line") and "days elapsed" in sql:
                with self.subTest(card=name):
                    buckets = [re.match(r"\(\d+, '([^']+)'", r).group(1) for r in rows_of(sql)]
                    self.assertEqual(tuple(buckets), m.DAYS)

    def test_the_gate_has_one_row_per_criterion_with_a_verdict_in_each_cell(self):
        rows = rows_of(CARDS["The gate, for the paper's setting and the seasonal variant"][1])
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertEqual(len(re.findall(r"— (PASS|FAIL)'", r)), 2, r)


class ReadingTheReports(unittest.TestCase):

    def test_a_table_that_has_moved_stops_the_build(self):
        with self.assertRaises(ValueError):
            m.md_table("stage3_extras.md", "No such column")
        with self.assertRaises(ValueError):
            m.md_table("stage3_extras.md", "Criterion", after="No such heading")

    def test_the_table_after_a_heading_is_the_one_read(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "r.md").write_text(
                "## First\n\n| Criterion | Result |\n|---|---|\n| a | 1 |\n\n"
                "## Second\n\n| Criterion | Result |\n|---|---|\n| b | **2** |\n", encoding="utf-8")
            saved, m.RESULTS = m.RESULTS, Path(d)
            try:
                self.assertEqual(m.md_table("r.md", "Criterion", after="Second"),
                                 [{"Criterion": "b", "Result": "2"}])
            finally:
                m.RESULTS = saved

    def test_numbers_are_read_as_the_reports_print_them(self):
        self.assertEqual(m.num("+0.9%"), 0.9)
        self.assertEqual(m.num("1.84 ×"), 1.84)
        self.assertEqual(m.num("27,882"), 27882)
        self.assertEqual(m.num("-4.7% to +6.6%"), -4.7)


class SqlLiterals(unittest.TestCase):

    def test_a_quote_cannot_end_a_literal(self):
        self.assertEqual(m.literal("v2's own"), "'v2''s own'")

    def test_the_sort_key_is_not_selected(self):
        sql = m.values("x.csv", ["a", "b"], [["one", 1], ["two", 2.5]])
        self.assertIn('SELECT "a", "b"\n', sql)
        self.assertIn('AS t(ord, "a", "b")', sql)
        self.assertTrue(sql.endswith("ORDER BY ord"))

    def test_no_rows_is_an_error(self):
        with self.assertRaises(ValueError):
            m.values("x.csv", ["a"], [])


class Colours(unittest.TestCase):

    def test_knn_and_v2_keep_one_colour_each(self):
        for name, spec in LAYOUT.items():
            for series, setting in spec.get("viz", {}).get("series_settings", {}).items():
                with self.subTest(card=name, series=series):
                    if series.startswith("kNN") or series == "here (%)" or "whole cohort" in series:
                        self.assertEqual(setting["color"], m.KNN)
                    elif series.startswith("v2"):
                        self.assertEqual(setting["color"], m.V2)


if __name__ == "__main__":
    unittest.main()
