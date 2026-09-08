"""The customer-facing terms of a plan recommendation stay explicit.

These tests do not need a warehouse: the SQL is integration-tested by the
pipeline, while the important product rules are pure arithmetic and should be
cheap to protect here.
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_plan_recommendations as m  # noqa: E402


class _Cursor:
    """Enough of a DB-API cursor to pin the term-offer query's contract."""

    def __init__(self, rows):
        self.rows = rows
        self.called_with = None

    def execute(self, sql, params):
        self.called_with = (sql, params)

    def fetchall(self):
        return self.rows


class TestCustomerAmounts(unittest.TestCase):
    def test_rates_are_grossed_up_before_they_meet_advertised_credit(self):
        assert m.customer_amount(Decimal("100.00")) == Decimal("115.0000")

    def test_term_credit_is_the_term_total_not_an_annualised_amount(self):
        assert m.term_credit(0, Decimal("0")) == Decimal("0")
        assert m.term_credit(6, Decimal("10")) == Decimal("60")
        assert m.term_credit(12, Decimal("25")) == Decimal("300")

    def test_credit_can_rank_an_offer_but_cannot_make_a_switch_recommendation(self):
        # A $300 term credit can make a $1,050 tariff rank ahead of a $900
        # flexible tariff. It is still not a genuine switch saving if the
        # household pays $50 more for its ordinary usage.
        assert m.ranked_cost(Decimal("1050"), Decimal("300")) == Decimal("750")
        assert not m.is_switch_recommendation(
            rank=1, candidate_plan_id=2, current_plan_id=1,
            usage_saving=Decimal("-50"))

    def test_only_a_cheaper_different_top_offer_is_recommended(self):
        assert m.is_switch_recommendation(
            rank=1, candidate_plan_id=2, current_plan_id=1,
            usage_saving=Decimal("0.01"))
        assert not m.is_switch_recommendation(
            rank=1, candidate_plan_id=1, current_plan_id=1,
            usage_saving=Decimal("20"))
        assert not m.is_switch_recommendation(
            rank=2, candidate_plan_id=2, current_plan_id=1,
            usage_saving=Decimal("20"))

    def test_bill_saving_is_derived_from_the_displayed_cent_amounts(self):
        # Decimal's half-even rounding puts these on opposite sides of the
        # classic one-cent mismatch if high-precision totals are subtracted.
        current = round(Decimal("98.114"), 2)
        candidate = round(Decimal("92.205"), 2)
        assert current == Decimal("98.11")
        assert candidate == Decimal("92.20")
        assert current - candidate == Decimal("5.91")


class TestTermOptions(unittest.TestCase):
    def test_flexible_is_added_to_the_current_catalogue_offers(self):
        cur = _Cursor([(6, Decimal("10.00")), (12, Decimal("25.00"))])
        asof = dt.date(2027, 8, 27)

        assert m.load_term_options(cur, asof) == [
            (0, Decimal("0")), (6, Decimal("10.00")), (12, Decimal("25.00")),
        ]
        assert cur.called_with[1] == {"asof": asof}

    def test_invalid_or_duplicate_offers_fail_closed(self):
        with self.assertRaisesRegex(SystemExit, "invalid term-credit"):
            m.load_term_options(_Cursor([(3, Decimal("10"))]), dt.date.today())
        with self.assertRaisesRegex(SystemExit, "more than one active"):
            m.load_term_options(_Cursor([(6, Decimal("15")), (6, Decimal("16"))]),
                                dt.date.today())
        with self.assertRaisesRegex(SystemExit, "both 6- and 12-month"):
            m.load_term_options(_Cursor([(6, Decimal("15"))]), dt.date.today())


class TestCandidateEligibility(unittest.TestCase):
    def test_candidates_are_separated_by_network_and_user_type(self):
        plans = {
            1: {"plan_code": "KEA_ANY_VECT_STA", "network_id": 10,
                "user_type": "STANDARD"},
            2: {"plan_code": "KEA_TOU_VECT_STA", "network_id": 10,
                "user_type": "STANDARD"},
            3: {"plan_code": "KEA_ANY_VECT_LOW", "network_id": 10,
                "user_type": "LOW"},
            4: {"plan_code": "KEA_ANY_ORON_STA", "network_id": 20,
                "user_type": "STANDARD"},
            5: {"plan_code": "FIXTURE_PLAN", "network_id": 10,
                "user_type": "STANDARD"},
        }

        assert m.candidate_sets(plans) == {
            (10, "STANDARD"): [1, 2],
            (10, "LOW"): [3],
            (20, "STANDARD"): [4],
        }


class TestExportLabels(unittest.TestCase):
    def test_one_export_component_covers_every_window_with_its_label(self):
        # Components point to one representative window, but a tariff label
        # may name more than one disconnected period. This generic shape keeps
        # that assembler guarantee even though the three Kea plans share one
        # flat buy-back rate.
        bands = [
            {"charge_group": "energy", "band_name": "OTHER", "days_of_week": [1, 2, 3, 4, 5, 6, 7],
             "start_time": dt.time(0), "end_time": dt.time(7), "direction_is_import": True},
            {"charge_group": "energy", "band_name": "PEAK", "days_of_week": [1, 2, 3, 4, 5],
             "start_time": dt.time(7), "end_time": dt.time(9), "direction_is_import": True},
            {"charge_group": "energy", "band_name": "OTHER", "days_of_week": [1, 2, 3, 4, 5],
             "start_time": dt.time(9), "end_time": dt.time(18), "direction_is_import": True},
            {"charge_group": "energy", "band_name": "PEAK", "days_of_week": [1, 2, 3, 4, 5],
             "start_time": dt.time(18), "end_time": dt.time(21), "direction_is_import": True},
            {"charge_group": "energy", "band_name": "OTHER", "days_of_week": [1, 2, 3, 4, 5],
             "start_time": dt.time(21), "end_time": dt.time(0), "direction_is_import": True},
            {"charge_group": "energy", "band_name": "OTHER", "days_of_week": [6, 7],
             "start_time": dt.time(7), "end_time": dt.time(0), "direction_is_import": True},
        ]
        comps = [
            {"charge_group": "energy", "component_name": "ordinary import", "basis": "per_kwh",
             "direction": "import", "rate": Decimal("0.10"), "band_name": "OTHER",
             "days": [1], "start_time": dt.time(0), "end_time": dt.time(7)},
            {"charge_group": "energy", "component_name": "peak import", "basis": "per_kwh",
             "direction": "import", "rate": Decimal("0.20"), "band_name": "PEAK",
             "days": [1], "start_time": dt.time(7), "end_time": dt.time(9)},
            # This mimics the source component's single representative band.
            {"charge_group": "energy", "component_name": "buy-back", "basis": "per_kwh",
             "direction": "export", "rate": Decimal("0.25"), "band_name": "PEAK",
             "days": [1], "start_time": dt.time(7), "end_time": dt.time(9)},
        ]
        tariff = m.build_tariff("test", bands, comps)
        reads = [
            m.Reading(dt.datetime(2024, 1, 1, 7), Decimal("1"), exported=True),
            m.Reading(dt.datetime(2024, 1, 1, 18), Decimal("1"), exported=True),
            m.Reading(dt.datetime(2024, 1, 1, 10), Decimal("1"), exported=True),
            # The shared PEAK label must not let the 25c export component
            # overwrite the 20c import component.
            m.Reading(dt.datetime(2024, 1, 1, 7), Decimal("1")),
        ]

        got = m.cost_of(reads, tariff, days=1)
        assert got.by_group["energy"] == Decimal("-0.30")
        assert got.kwh_by_band[("energy", "export:PEAK")] == Decimal("2")
        assert got.kwh_by_band[("energy", "PEAK")] == Decimal("1")


if __name__ == "__main__":
    unittest.main()
