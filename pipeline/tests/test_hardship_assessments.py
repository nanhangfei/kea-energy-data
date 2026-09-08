"""The five auditable hardship rules, without a database or a score."""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_hardship_assessments as m  # noqa: E402


AS_OF = dt.date(2027, 8, 31)


def bill(number: int, due: dt.date, amount: str, paid: str,
         paid_on: dt.date | None = None) -> m.BillEvidence:
    return m.BillEvidence(
        bill_id=number,
        due_on=due,
        amount_due=Decimal(amount),
        payments_applied=Decimal(paid),
        latest_successful_payment_on=paid_on,
    )


def signals(**overrides) -> tuple[m.Signal, ...]:
    args = dict(
        as_of=AS_OF,
        bills=(),
        failed_direct_debits=None,
        payment_history=None,
        daily_usage=(),
        usage_to=None,
    )
    args.update(overrides)
    return m.evaluate_signals(**args)


class TestPaymentSignals(unittest.TestCase):
    def test_failed_direct_debit_is_an_event_with_dates_and_count(self):
        found = signals(failed_direct_debits=m.DateSummary(
            3, dt.date(2027, 5, 2), dt.date(2027, 8, 2)))
        assert [signal.code for signal in found] == ["direct_debit_failed"]
        assert found[0].evidence_count == 3
        assert found[0].first_seen_on == dt.date(2027, 5, 2)

    def test_two_consecutive_fully_paid_late_bills_form_a_streak(self):
        found = signals(bills=[
            bill(1, dt.date(2027, 6, 1), "100", "100", dt.date(2027, 6, 9)),
            bill(2, dt.date(2027, 7, 1), "110", "110", dt.date(2027, 7, 12)),
        ])
        assert [signal.code for signal in found] == ["late_payment_streak"]
        assert found[0].evidence_count == 2

    def test_one_late_bill_does_not_flag_and_an_on_time_bill_resets_the_streak(self):
        found = signals(bills=[
            bill(1, dt.date(2027, 5, 1), "100", "100", dt.date(2027, 5, 9)),
            bill(2, dt.date(2027, 6, 1), "100", "100", dt.date(2027, 6, 1)),
            bill(3, dt.date(2027, 7, 1), "100", "100", dt.date(2027, 7, 9)),
        ])
        assert all(signal.code != "late_payment_streak" for signal in found)

    def test_repeated_part_payments_report_compounding_arrears(self):
        found = signals(bills=[
            bill(1, dt.date(2027, 6, 1), "100", "60"),
            bill(2, dt.date(2027, 7, 1), "120", "50"),
        ])
        part = next(signal for signal in found if signal.code == "part_payment_arrears")
        assert part.evidence_count == 2
        assert "$110.00" in part.value

    def test_stopped_means_an_established_payer_plus_two_mature_unpaid_bills(self):
        found = signals(
            bills=[
                bill(1, dt.date(2027, 7, 15), "100", "0"),
                bill(2, dt.date(2027, 8, 15), "120", "0"),
            ],
            payment_history=m.PaymentHistory(
                dt.date(2026, 1, 1), dt.date(2027, 6, 1)),
        )
        stopped = next(signal for signal in found if signal.code == "payments_stopped")
        assert stopped.evidence_count == 2

    def test_never_paid_is_not_mislabelled_as_stopped(self):
        found = signals(bills=[
            bill(1, dt.date(2027, 7, 15), "100", "0"),
            bill(2, dt.date(2027, 8, 15), "120", "0"),
        ])
        assert all(signal.code != "payments_stopped" for signal in found)


class TestUsageCollapse(unittest.TestCase):
    def _daily(self, *, omit: int | None = None) -> list[m.DailyUsage]:
        start = AS_OF - dt.timedelta(days=27)
        return [
            m.DailyUsage(
                usage_on=start + dt.timedelta(days=offset),
                kwh=Decimal("10") if offset < 21 else Decimal("5"),
                read_count=48,
            )
            for offset in range(28) if offset != omit
        ]

    def test_complete_50_percent_drop_uses_the_shared_detector(self):
        signal = m.usage_collapse_signal(self._daily(), usage_to=AS_OF)
        assert signal is not None
        assert signal.code == "usage_collapse"
        assert "50%" in signal.value

    def test_a_missing_day_cannot_masquerade_as_self_disconnection(self):
        assert m.usage_collapse_signal(self._daily(omit=25), usage_to=AS_OF) is None


class TestAssessmentParent(unittest.TestCase):
    def test_clean_account_still_gets_a_parent_and_care_flags_are_not_signals(self):
        account = m.Account(1, "A1", True, True)
        got = m.build_assessments(
            accounts=[account], as_of=AS_OF,
            observed_from=AS_OF - dt.timedelta(days=179),
            bills_by_account={}, failed_dd_by_account={}, history_by_account={},
            daily_usage_by_account={}, usage_to=None,
        )
        assert len(got) == 1
        assert got[0].signals == ()
        assert got[0].account.is_medically_dependent
        assert got[0].account.is_vulnerable


if __name__ == "__main__":
    unittest.main()
