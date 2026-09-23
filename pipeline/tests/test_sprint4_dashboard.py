"""The Sprint 4 dashboard builder: the promises its page makes about itself.

No database and no Metabase. Every check here is one a reader of the page
would have to trust otherwise:

  * no card can put an identifier in a cell, and the one counted exemption
    stays exactly that -- a count;
  * every append-only table is pinned to its latest generation, so a page that
    is rebuilt tomorrow does not quietly double;
  * no card reads both databases, because Metabase cannot join them;
  * the month lists restated in SQL still match the dbt vars they came from.

verify.py covers what only a live Metabase can: that each card runs, returns
rows and uses every column it selects.
"""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

DATA = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, DATA / "analysis" / "metabase" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("build_sprint4_dashboard")
CARDS = [it for _, items in m.SECTIONS for it in items if it["kind"] == "card"]


class TestCards(unittest.TestCase):
    def test_card_names_are_unique(self):
        # The name is the upsert key: two cards sharing one would take turns
        # overwriting each other on every build, and neither would be wrong
        # enough to notice.
        names = [c["name"] for c in CARDS]
        assert len(names) == len(set(names)), sorted(
            {n for n in names if names.count(n) > 1})

    def test_every_card_names_a_connected_database(self):
        for c in CARDS:
            with self.subTest(card=c["name"]):
                assert c["db"] in (m.SERVING_DB, m.PIPELINE_DB)

    def test_no_card_reads_both_databases(self):
        # A native question is one statement on one connection. A card that
        # named tables from both would not error -- it would fail to find the
        # other database's table, at query time, on the page.
        for c in CARDS:
            sql = re.sub(r"--[^\n]*", "", c["sql"]).lower()
            with self.subTest(card=c["name"]):
                if c["db"] == m.SERVING_DB:
                    assert not re.search(r"\b(gen_audit|lake_audit|src_\w+)\.", sql)
                else:
                    # The source systems live in src_* on the sandbox, so the
                    # boundary is a word one: src_billing is not billing.
                    assert not re.search(r"(?<![a-z_])(kea|billing|catalogue|metering)\.", sql)

    def test_chart_cards_declare_their_axes(self):
        # Metabase drops a column the chart was not told about, without an
        # error. verify.py catches it against live results; this catches it
        # before anything is published.
        charts = ("bar", "row", "line", "area", "scatter", "combo", "waterfall")
        for c in CARDS:
            if c["display"] not in charts:
                continue
            with self.subTest(card=c["name"]):
                assert c["viz"].get("graph.dimensions"), "no dimensions"
                assert c["viz"].get("graph.metrics"), "no metrics"

    def test_nothing_is_wider_than_the_grid(self):
        for it in [i for _, items in m.SECTIONS for i in items]:
            with self.subTest(item=it.get("name", "text")):
                assert 1 <= it["w"] <= 24


class TestSolarMaskedLabels(unittest.TestCase):
    """A withheld charger class must stay unknown on every affected card."""

    def sql(self, name: str) -> str:
        return next(c["sql"] for c in CARDS if c["name"] == name)

    def test_session_charts_use_explicit_high_low_and_null_labels(self):
        for name in ("When a charging run starts", "How long a charging run lasts"):
            sql = self.sql(name)
            with self.subTest(card=name):
                self.assertIn("WHEN s.charger_class = 'high_power'", sql)
                self.assertIn("WHEN s.charger_class = 'low_power'", sql)
                self.assertIn("WHEN s.charger_class IS NULL", sql)
                self.assertIn("charger class unknown (solar-masked)", sql)
                self.assertNotIn("ELSE 'portable-shaped", sql)

    def test_confidence_chart_keeps_masked_charging_apart_from_timers(self):
        sql = self.sql("How sure the detector is")
        self.assertIn(
            "WHEN d.ev_charger_class IS NULL AND d.ev_half_hours > 0", sql)
        self.assertIn("charging, class unknown (solar-masked)", sql)
        self.assertIn(
            "WHEN d.timed_night_half_hours > 0 THEN 'night block, timer-shaped'", sql)


class TestIdentifierGuard(unittest.TestCase):
    def test_a_card_that_selects_an_identifier_is_refused(self):
        for sql in ("SELECT icp_number FROM metering.icp",
                    "SELECT a.customer_name FROM billing.account a",
                    "SELECT count(*) FROM gen_audit.planted_load GROUP BY icp_identifier"):
            with self.subTest(sql=sql), self.assertRaises(SystemExit):
                m.guard("a card", sql)

    def test_counting_households_is_allowed(self):
        # The exemption, and the whole of it: a number of households reaches a
        # cell, a household never does.
        m.guard("a card", "SELECT count(DISTINCT icp_identifier) FROM gen_audit.planted_load")
        m.guard("a card", "SELECT count( distinct p.icp_identifier ) FROM gen_audit.planted_load p")

    def test_the_exemption_does_not_cover_the_rest_of_the_query(self):
        with self.assertRaises(SystemExit):
            m.guard("a card", "SELECT count(DISTINCT icp_identifier), icp_identifier "
                              "FROM gen_audit.planted_load GROUP BY 2")

    def test_a_comment_may_say_the_word(self):
        # The guard reads what reaches a cell, not what the file says about it.
        m.guard("a card", "-- one row per icp_number\nSELECT count(*) FROM metering.icp")

    def test_every_built_card_passes_its_own_guard(self):
        for c in CARDS:
            with self.subTest(card=c["name"]):
                m.guard(c["name"], c["sql"])


class TestAppendOnlyTablesArePinned(unittest.TestCase):
    """Every generation is kept, so a query without a pin returns all of them.

    It does not error and it does not look wrong: the counts are simply
    multiplied by however many times the job has run.
    """

    PINS = {
        "kea.solar_estimate": m.LATEST_SOLAR,
        "kea.ev_scenario": m.LATEST_EV,
        "kea.hardship_assessment": m.LATEST_HARDSHIP,
        "gen_audit.planted_load": m.LATEST_PLANT,
    }

    def test_each_card_pins_every_append_only_table_it_reads(self):
        for c in CARDS:
            sql = re.sub(r"--[^\n]*", "", c["sql"])
            flat = " ".join(sql.split()).lower()
            for table, pin in self.PINS.items():
                if table not in flat:
                    continue
                with self.subTest(card=c["name"], table=table):
                    assert " ".join(pin.split()).lower() in flat, "not pinned"

    def test_hardship_outlook_is_pinned_through_its_assessment(self):
        # The outlook table has no computed_at of its own: it is one row per
        # assessment, so the pin has to arrive over the join.
        for c in CARDS:
            flat = " ".join(re.sub(r"--[^\n]*", "", c["sql"]).split()).lower()
            if "kea.hardship_outlook" not in flat:
                continue
            with self.subTest(card=c["name"]):
                assert "kea.hardship_assessment" in flat
                assert " ".join(m.LATEST_HARDSHIP.split()).lower() in flat


class TestHouseholdSelectorsAreDeterministic(unittest.TestCase):
    """Section 2 picks two households by rule and calls them A and B; section 7
    picks three more and calls them C, D and E. Every one of those rules has to
    end its ORDER BY on an id, so that two builds of the same data land on the
    same households -- and a rebuilt cohort deterministically picks its own.
    Without the tiebreak Postgres may return either of two households with the
    same count, and the page would quietly change who it is about."""

    def one_row_ctes(self):
        """Every `<name> AS ( ... LIMIT 1 )` in every card, by balanced parens.

        Comments and string literals go first: a literal like '(the baseline)'
        is balanced today, but one that was not would silently end a body early
        and make the assertions below vacuous rather than failing."""
        for c in CARDS:
            sql = re.sub(r"'[^']*'", "''", re.sub(r"--[^\n]*", "", c["sql"]))
            for head in re.finditer(r"\b(\w+) AS \(", sql):
                depth, i = 1, head.end()
                while depth and i < len(sql):
                    depth += {"(": 1, ")": -1}.get(sql[i], 0)
                    i += 1
                body = " ".join(sql[head.end():i - 1].split())
                if body.upper().endswith("LIMIT 1"):
                    yield c["name"], head.group(1), body

    def test_there_are_selector_ctes_to_check(self):
        # A rename that emptied the match would make every assertion below
        # vacuously true, which is the one way this test can rot silently.
        names = {name for _, name, _ in self.one_row_ctes()}
        assert {"household_a", "household_b", "household_c",
                "household_d", "household_e"} <= names, names

    def test_every_one_row_cte_orders_before_it_limits(self):
        # LIMIT without ORDER BY is whichever row the planner reached first.
        for card, name, body in self.one_row_ctes():
            with self.subTest(card=card, cte=name):
                assert " ORDER BY " in body.upper(), "picks one row unordered"

    def test_every_household_selector_breaks_its_tie_on_an_id(self):
        for card, name, body in self.one_row_ctes():
            if not name.startswith("household_"):
                continue
            with self.subTest(card=card, cte=name):
                order = body.upper().rsplit(" ORDER BY ", 1)[1]
                last = order.rsplit(" LIMIT ", 1)[0].split(",")[-1].strip()
                assert last.endswith(".ICP_ID") or last == "ICP_ID", last


class TestRestatedConstants(unittest.TestCase):
    """A panel cannot import a dbt var, so the ones it needs are restated. The
    cost of restating is drift, and this is what pays it."""

    def vars_(self) -> str:
        return (DATA / "pipeline" / "dbt_project" / "dbt_project.yml").read_text()

    def months(self, name: str) -> tuple[int, ...]:
        # A regex, not PyYAML: the host tests run on the standard library.
        hit = re.search(rf"^\s*{name}:\s*\[([0-9,\s]+)\]\s*$", self.vars_(), re.MULTILINE)
        assert hit, f"{name} is not in dbt_project.yml"
        return tuple(int(x) for x in hit.group(1).split(","))

    def test_the_month_lists_match_dbt(self):
        assert m.COLD_MONTHS == self.months("detect_cold_months")
        assert m.WARM_MONTHS == self.months("detect_warm_months")

    def test_the_cohort_boundary_matches_the_story_page(self):
        # Both pages count "the generated households still on supply". A page
        # that drifted from the other would make the same sentence mean two
        # different populations in one demo.
        assert m.FIRST_GENERATED == load("build_story_dashboard").FIRST_GENERATED


if __name__ == "__main__":
    unittest.main()
