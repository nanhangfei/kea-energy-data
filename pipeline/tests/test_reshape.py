"""The design-5 reshapes. No database, no lake, no Spark, no dataset.

Each test names the wrong answer it exists to prevent, because every one of
these functions has a plausible-looking alternative that would corrupt a bill
without failing anything.
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from kea_pipeline import reshape as m  # noqa: E402

UTC = dt.timezone.utc


class TestQuality(unittest.TestCase):
    def test_the_two_the_design_lists(self):
        assert m.quality("A") == "actual"
        assert m.quality("E") == "estimated"

    def test_final_is_carried_not_dropped(self):
        """The source produces 1,010 'F' rows and design 5's table omits them.

        Mapping F onto 'actual' would be parse deciding that a final read at
        account close is an ordinary one -- a judgement, not a rewriting.
        """
        assert m.quality("F") == "final"

    def test_an_unknown_status_is_quarantined_not_guessed(self):
        with self.assertRaises(m.RejectRow) as caught:
            m.quality("X")
        assert caught.exception.reason == "parse.enum_unknown"

    def test_a_null_status_is_its_own_reason(self):
        with self.assertRaises(m.RejectRow) as caught:
            m.quality(None)
        assert caught.exception.reason == "parse.null_in_required_field"


class TestFlag(unittest.TestCase):
    def test_y_and_n(self):
        assert m.flag("Y") is True
        assert m.flag("N") is False

    def test_null_stays_null_and_does_not_become_false(self):
        """MED_DEPENDENT decides whether the agent may answer at all. An
        unanswered question is not a No, and collapsing the two would silently
        clear a medical-dependency flag."""
        assert m.flag(None) is None
        assert m.flag("") is None


class TestDaysOfWeek(unittest.TestCase):
    def test_a_weekday_window(self):
        assert m.days_of_week("1,2,3,4,5") == [1, 2, 3, 4, 5]

    def test_all_week(self):
        assert m.days_of_week("1,2,3,4,5,6,7") == [1, 2, 3, 4, 5, 6, 7]

    def test_split_not_substring_search(self):
        """The TOU banding trap: `position('1' in '1,2,3,4,5')` also matches
        the '1' inside '11', so a weekday-only window quietly applies on a
        Sunday and the bill still balances. Splitting cannot do that."""
        assert m.days_of_week("1") == [1]
        assert 1 not in m.days_of_week("7")

    def test_a_day_outside_one_to_seven_is_refused(self):
        for bad in ("0,1,2", "1,2,8", "1,2,11"):
            with self.assertRaises(m.RejectRow):
                m.days_of_week(bad)

    def test_a_repeated_day_is_refused(self):
        with self.assertRaises(m.RejectRow):
            m.days_of_week("1,1,2")


class TestChargeGroup(unittest.TestCase):
    def test_the_three_the_source_permits(self):
        assert m.charge_group("NET") == "network"
        assert m.charge_group("ENE") == "energy"
        assert m.charge_group("OTH") == "other"

    def test_an_unknown_group_is_quarantined(self):
        with self.assertRaises(m.RejectRow):
            m.charge_group("ZZZ")


class TestToInstant(unittest.TestCase):
    """Billing stores naive `timestamp`; the conformed schema is UTC."""

    def test_a_naive_billing_timestamp_is_read_as_nz_local(self):
        got = m.to_instant(dt.datetime(2026, 8, 25, 9, 15))
        assert got == dt.datetime(2026, 8, 24, 21, 15, tzinfo=UTC)

    def test_reading_it_as_utc_would_move_the_date(self):
        """13 hours is enough to push a period end across a plan-version
        boundary and rate the bill on the wrong tariff -- design 2.1.2's
        silent failure."""
        naive = dt.datetime(2026, 4, 1, 0, 30)
        assert m.to_instant(naive).astimezone(m.NZ).date() == dt.date(2026, 4, 1)
        assert naive.replace(tzinfo=UTC).astimezone(m.NZ).date() == dt.date(2026, 4, 1)
        # ... but the instants are 13 hours apart, and only one of them is the
        # moment the retailer's clock showed.
        assert m.to_instant(naive) != naive.replace(tzinfo=UTC)

    def test_an_aware_value_passes_through_and_is_not_relocalised(self):
        aware = dt.datetime(2026, 8, 25, 9, 15, tzinfo=ZoneInfo("Pacific/Auckland"))
        assert m.to_instant(aware) == aware
        assert m.to_instant(m.to_instant(aware)) == aware  # idempotent

    def test_null_survives(self):
        assert m.to_instant(None) is None


class TestKwh(unittest.TestCase):
    def test_a_negative_read_is_quarantined_not_absolute_valued(self):
        """ck_kwh_non_negative means a negative value cannot be a source row,
        so meeting one means reading a file. Taking its magnitude would invent
        a direction the data does not carry (design 8.2.3)."""
        with self.assertRaises(m.RejectRow) as caught:
            m.kwh(-0.5)
        assert caught.exception.reason == "parse.negative_read"

    def test_zero_is_fine(self):
        assert m.kwh(0) == 0

    def test_a_non_numeric_value_is_a_type_reject(self):
        with self.assertRaises(m.RejectRow) as caught:
            m.kwh("not a number")
        assert caught.exception.reason == "parse.type_unparseable"


class TestTheVocabularyIsTheContract(unittest.TestCase):
    def test_every_reason_raised_is_declared_in_the_file(self):
        """RejectRow refuses an undeclared code, so the JSON file is the
        vocabulary rather than a description of one that can drift from it."""
        with self.assertRaises(KeyError):
            m.RejectRow("parse.invented_on_the_spot")

    def test_the_parse_group_is_the_five_decidable_from_one_row(self):
        assert set(m.parse_reasons()) == {
            "parse.type_unparseable",
            "parse.timestamp_unparseable",
            "parse.enum_unknown",
            "parse.null_in_required_field",
            "parse.negative_read",
        }

    def test_all_three_writer_groups_are_declared(self):
        """Design 6.3 wants one vocabulary across three writers, so all three
        groups exist from the beginning even before they have members.

        THIS TEST USED TO ASSERT integrity == {} AND CORRECTLY BROKE. Stage 4's
        silver layer populated it, which is the point -- the file is the
        vocabulary, so growing it is a change the tests notice. `validation`
        is still empty: dbt's own validation rejections are not built yet."""
        vocab = m._vocabulary()
        assert set(vocab) >= {"parse", "validation", "integrity"}
        assert vocab["validation"] == {}

    def test_the_integrity_group_is_the_cross_system_classes(self):
        """Design 6.3's table has four failure classes; a fifth was planted by
        05_defects.sql for Stage 4 to find, and this pins all five.

        Each needs a SECOND TABLE or a SECOND SYSTEM to decide, which is why
        none of them may appear in the parse group -- parse sees one row of one
        file. ambiguous_register is the clearest case: it cannot be decided
        without joining reads to meters to registers.
        """
        vocab = m._vocabulary()
        assert set(vocab["integrity"]) == {
            "integrity.orphan_icp",
            "integrity.timing_skew",
            "integrity.unknown_plan_code",
            "integrity.no_plan_version",
            "integrity.ambiguous_register",
        }

    def test_no_reason_needs_a_second_row_or_table(self):
        """The scope boundary, as an assertion. A duplicate delivery needs a
        second row; an orphan ICP needs a second table. Neither may appear
        here, or parse has started doing silver's job."""
        for code in m.parse_reasons():
            assert "duplicate" not in code
            assert "orphan" not in code


if __name__ == "__main__":
    unittest.main()
