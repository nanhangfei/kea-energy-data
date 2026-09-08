"""Small boundary tests for the bill-reproduction acceptance gate."""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import verify_bill_reproduction as m  # noqa: E402


class TestTermBoundary(unittest.TestCase):
    def test_calendar_months_match_a_term_not_a_fixed_day_count(self):
        # PostgreSQL's `date + interval '1 month'` turns 31 January into the
        # last day of February. A six-month credit needs that same calendar
        # rule, not an inaccurate 180-day shortcut.
        assert m.add_months(dt.date(2025, 1, 31), 1) == dt.date(2025, 2, 28)
        assert m.add_months(dt.date(2024, 1, 31), 1) == dt.date(2024, 2, 29)

    def test_term_end_is_exclusive_like_the_source_biller(self):
        start = dt.date(2026, 4, 1)
        assert m.term_is_active(6, start, dt.date(2026, 9, 30))
        assert not m.term_is_active(6, start, dt.date(2026, 10, 1))
        assert not m.term_is_active(0, start, dt.date(2026, 5, 1))
        assert not m.term_is_active(12, None, dt.date(2026, 5, 1))


class TestTolerance(unittest.TestCase):
    def test_one_dollar_is_allowed_and_one_cent_over_is_not(self):
        assert m.within_tolerance(Decimal("1.00"))
        assert m.within_tolerance(Decimal("-1.00"))
        assert not m.within_tolerance(Decimal("1.01"))


if __name__ == "__main__":
    unittest.main()
