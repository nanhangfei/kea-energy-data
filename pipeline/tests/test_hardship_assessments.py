"""The auditable hardship rules and the arrears outlook, without a database or a score."""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "decisions"))

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


START = dt.date(2027, 1, 1)
USUAL = Decimal("100.00")


def recent(number: int, due: dt.date, amount: str, paid: str,
           paid_on: dt.date | None = None, *, issued: dt.date | None = None,
           ) -> m.RecentBill:
    issued = issued or due - dt.timedelta(days=21)
    return m.RecentBill(
        bill_id=number,
        issued_on=issued,
        period_end=issued - dt.timedelta(days=2),
        due_on=due,
        amount_due=Decimal(amount),
        paid=Decimal(paid),
        latest_paid_on=paid_on,
    )


def paid_bills(delays: list[int], *, amounts: list[str] | None = None,
               last_due: dt.date = dt.date(2027, 8, 1)) -> list[m.RecentBill]:
    """Monthly bills paid in full, the last due on ``last_due``."""
    amounts = amounts or ["100"] * len(delays)
    out = []
    for i, (delay, amount) in enumerate(zip(delays, amounts)):
        due = last_due - dt.timedelta(days=30 * (len(delays) - 1 - i))
        out.append(recent(i + 1, due, amount, amount, due + dt.timedelta(days=delay)))
    return out


def codes(found) -> list[str]:
    return [signal.code for signal in found]


def warning(**overrides) -> tuple[m.Signal, ...]:
    return signals(**overrides)


def slide(k: int, *, as_of: dt.date, sudden: bool = False) -> list[m.RecentBill]:
    """One account as 08_payments.sql plants SLIDING (drift over k bills) or
    SLIDING_SUDDEN, seen on ``as_of``: six bills of $100, a $160 trigger bill
    (seq 7), then $120 bills; issued 2 days after the period, due 21 later."""
    trigger = 7
    out = []
    for seq in range(1, 11):
        period_end = START + dt.timedelta(days=30 * seq)
        issued = period_end + dt.timedelta(days=2)
        if issued > as_of:
            break
        due = issued + dt.timedelta(days=21)
        amount = Decimal("160") if seq == trigger else Decimal("120" if seq > trigger else "100")
        if sudden and seq >= trigger:
            delay = None
        elif sudden:
            delay = -((seq * 3) % 4)
        elif seq > trigger:
            delay = 7
        elif seq >= trigger - k:
            delay = (3 if k == 2 else 5) * (seq - (trigger - k) + 1) - 3
        elif seq == trigger - k - 1:
            delay = -3
        else:
            delay = -((seq * 3) % 4)
        pays = amount if sudden or seq < trigger else min(USUAL, round(amount * Decimal("0.92"), 2))
        paid_on = None if delay is None else due + dt.timedelta(days=delay)
        known = paid_on is not None and paid_on <= as_of
        out.append(m.RecentBill(seq, issued, period_end, due, amount,
                                pays if known else Decimal("0"),
                                paid_on if known else None))
    return out


def trigger_bill(k: int = 2) -> m.RecentBill:
    return slide(k, as_of=dt.date(2028, 1, 1))[6]


class TestPaymentDrift(unittest.TestCase):
    def test_three_bills_each_three_days_later_ending_late(self):
        found = warning(recent_bills=paid_bills([-3, 0, 3]))
        assert codes(found) == ["payment_drift"]
        assert found[0].evidence_count == 3
        assert found[0].value == ("Last 3 bills paid 3 days early, then on the due date, "
                                  "then 3 days late")
        assert found[0].last_seen_on == dt.date(2027, 8, 4)

    def test_a_step_under_three_days_is_not_drift(self):
        assert codes(warning(recent_bills=paid_bills([-3, -1, 2]))) == []

    def test_more_than_a_week_late_is_the_late_streaks_case(self):
        assert codes(warning(recent_bills=paid_bills([2, 5, 8]))) == []
        assert codes(warning(recent_bills=paid_bills([1, 4, 7]))) == ["payment_drift"]

    def test_drifting_but_still_on_time_is_not_drift(self):
        assert codes(warning(recent_bills=paid_bills([-6, -3, 0]))) == []

    def test_only_the_last_three_paid_bills_count(self):
        # An unpaid newer bill does not break the trend; an older early one is ignored.
        bills = paid_bills([5, -3, 0, 3]) + [recent(9, dt.date(2027, 8, 31), "100", "0")]
        assert codes(warning(recent_bills=bills)) == ["payment_drift"]

    def test_the_v1_bill_view_is_not_read(self):
        late = [bill(1, dt.date(2027, 6, 1), "100", "100", dt.date(2027, 5, 29)),
                bill(2, dt.date(2027, 7, 1), "100", "100", dt.date(2027, 7, 1)),
                bill(3, dt.date(2027, 8, 1), "100", "100", dt.date(2027, 8, 4))]
        assert "payment_drift" not in codes(warning(bills=late))


class TestBillAboveUsualPayment(unittest.TestCase):
    def history(self, latest: str, *, paid: str = "0") -> list[m.RecentBill]:
        return paid_bills([0] * 6, amounts=["90", "100", "100", "110", "100", "100"],
                          last_due=dt.date(2027, 7, 15)) + [
            recent(7, dt.date(2027, 8, 15), latest, paid)]

    def test_at_130_percent_of_the_median_paid(self):
        found = warning(recent_bills=self.history("130.00"))
        assert codes(found) == ["bill_above_usual_payment"]
        assert found[0].value == ("Latest bill $130.00 is 130% of the usual $100.00 paid "
                                  "per bill (median of the previous 6)")
        assert found[0].first_seen_on == dt.date(2027, 7, 25)

    def test_just_under_130_percent(self):
        assert codes(warning(recent_bills=self.history("129.99"))) == []

    def test_usual_needs_three_paid_bills(self):
        bills = self.history("200")
        assert codes(warning(recent_bills=bills[-3:])) == []
        assert codes(warning(recent_bills=bills[-4:])) == ["bill_above_usual_payment"]

    def test_unpaid_bills_are_not_part_of_usual(self):
        usual = m.usual_payment(
            paid_bills([0, 0, 0], amounts=["80", "100", "120"])
            + [recent(9, dt.date(2027, 8, 20), "500", "0")], before=None)
        assert usual == m.UsualPayment(Decimal("100.00"), 3)

    def test_the_forecast_speaks_when_the_latest_bill_does_not(self):
        bills = self.history("100", paid="100")
        forecast = m.OpenForecast(dt.date(2027, 8, 20), dt.date(2027, 8, 31),
                                  Decimal("130.00"))
        found = warning(recent_bills=bills, open_forecast=forecast)
        assert codes(found) == ["bill_above_usual_payment"]
        assert found[0].value.startswith("Forecast bill $130.00 incl. GST, for the cycle "
                                         "ending 2027-08-31, is 130%")
        assert found[0].first_seen_on == dt.date(2027, 8, 20)
        low = m.OpenForecast(dt.date(2027, 8, 20), dt.date(2027, 8, 31), Decimal("129.99"))
        assert codes(warning(recent_bills=bills, open_forecast=low)) == []

    def test_a_large_bill_still_open_counts_after_the_next_one_arrives(self):
        k1 = slide(1, as_of=trigger_bill(1).due_on + dt.timedelta(days=12))
        assert k1[-1].bill_id == 8 and k1[-1].amount_due == Decimal("120")
        found = warning(recent_bills=k1, as_of=k1[-1].issued_on + dt.timedelta(days=1))
        above = next(s for s in found if s.code == "bill_above_usual_payment")
        assert above.value.startswith(
            f"Bill due {k1[6].due_on}, not paid in full, $160.00 is 160%")

    def test_a_large_bill_paid_in_full_stops_counting_once_it_is_not_the_latest(self):
        bills = self.history("160", paid="160") + [recent(8, dt.date(2027, 9, 15), "100", "0")]
        assert codes(warning(recent_bills=bills, as_of=dt.date(2027, 8, 31))) == []


class TestFirstShortfall(unittest.TestCase):
    def bills(self, paid: str, *, before=("100", "100", "100")) -> list[m.RecentBill]:
        out = [recent(i + 1, dt.date(2027, 5, 1) + dt.timedelta(days=30 * i), "100", p,
                      dt.date(2027, 5, 1) + dt.timedelta(days=30 * i))
               for i, p in enumerate(before)]
        return out + [recent(9, dt.date(2027, 8, 20), "100", paid,
                             dt.date(2027, 8, 25) if paid != "0" else None)]

    def test_part_paid_under_95_percent_after_three_paid_in_full(self):
        found = warning(recent_bills=self.bills("94.99"))
        assert codes(found) == ["first_shortfall"]
        assert found[0].value == ("Latest bill past due 95% paid ($94.99 of $100.00) "
                                  "after 3 bills paid in full")
        assert (found[0].first_seen_on, found[0].last_seen_on) == (
            dt.date(2027, 8, 20), dt.date(2027, 8, 25))

    def test_95_percent_is_not_a_shortfall(self):
        assert codes(warning(recent_bills=self.bills("95.00"))) == []

    def test_nothing_paid_is_not_a_shortfall(self):
        assert codes(warning(recent_bills=self.bills("0"))) == []

    def test_needs_three_bills_paid_in_full_before_it(self):
        assert codes(warning(recent_bills=self.bills("50", before=("100", "100")))) == []
        assert codes(warning(recent_bills=self.bills("50", before=("99", "100", "100", "100")))
                     ) == ["first_shortfall"]
        assert codes(warning(recent_bills=self.bills("50", before=("100", "100", "99")))) == []

    def test_a_bill_due_today_is_not_past_due(self):
        assert codes(warning(recent_bills=self.bills("50"), as_of=dt.date(2027, 8, 20))) == []


class TestSlidingShapes(unittest.TestCase):
    """The generator's planted slides, as the job sees them week by week."""

    def assess(self, bills, as_of, forecast=None):
        found = warning(recent_bills=bills, as_of=as_of, open_forecast=forecast)
        outlook = m.project_outlook(as_of=as_of, bills=bills, forecast=forecast, signals=found)
        return codes(found), outlook

    def test_k2_warns_when_the_trigger_bill_is_issued(self):
        t = trigger_bill(2)
        found, outlook = self.assess(slide(2, as_of=t.issued_on), t.issued_on)
        assert found == ["payment_drift", "bill_above_usual_payment"]
        assert outlook.projected_arrears_on == t.due_on + dt.timedelta(days=30)
        assert outlook.projected_overdue_nzd == Decimal("60.00")
        assert (outlook.projected_arrears_on - t.issued_on).days == 51

    def test_k2_a_forecast_warns_before_the_bill_exists(self):
        t = trigger_bill(2)
        as_of = t.issued_on - dt.timedelta(days=5)
        bills = slide(2, as_of=as_of)
        assert bills[-1].bill_id == 6 and bills[-1].latest_paid_on is not None
        found, outlook = self.assess(bills, as_of)
        assert found == ["payment_drift"] and outlook is None
        forecast = m.OpenForecast(as_of - dt.timedelta(days=3), t.period_end, Decimal("160.00"))
        found, outlook = self.assess(bills, as_of, forecast)
        assert found == ["payment_drift", "bill_above_usual_payment"]
        assert outlook.projected_arrears_on == t.due_on + dt.timedelta(days=30)
        assert outlook.projected_overdue_nzd == Decimal("60.00")
        assert f"forecast bill for the cycle ending {t.period_end}" in outlook.basis

    def test_k1_shows_nothing_until_the_trigger_bill_is_part_paid(self):
        t = trigger_bill(1)
        found, _ = self.assess(slide(1, as_of=t.due_on - dt.timedelta(days=1)),
                               t.due_on - dt.timedelta(days=1))
        assert "payment_drift" not in found
        as_of = t.due_on + dt.timedelta(days=7)
        found, outlook = self.assess(slide(1, as_of=as_of), as_of)
        assert found == ["payment_drift", "bill_above_usual_payment", "first_shortfall"]
        assert outlook.projected_arrears_on == t.due_on + dt.timedelta(days=30)
        assert outlook.projected_overdue_nzd == Decimal("60.00")
        assert "$100.00 paid so far, taken as final" in outlook.basis

    def test_no_outlook_once_the_account_is_in_arrears(self):
        t = trigger_bill(1)
        as_of = t.due_on + dt.timedelta(days=30)
        found, outlook = self.assess(slide(1, as_of=as_of), as_of)
        assert "first_shortfall" in found or "payment_drift" in found
        assert outlook is None

    def test_the_sudden_stop_is_an_expected_miss(self):
        t = trigger_bill()
        for days in range(-20, 31, 7):
            as_of = t.due_on + dt.timedelta(days=days)
            found, outlook = self.assess(slide(0, as_of=as_of, sudden=True), as_of)
            assert "payment_drift" not in found and "first_shortfall" not in found
            assert outlook is None


class TestOutlook(unittest.TestCase):
    drift = (m.Signal("payment_drift", "", 3, AS_OF, AS_OF, ""),)

    def test_a_bill_spike_alone_projects_nothing(self):
        t = trigger_bill(2)
        bills = slide(2, as_of=t.issued_on)
        spike = (m.Signal("bill_above_usual_payment", "", 1, AS_OF, AS_OF, ""),)
        assert m.project_outlook(as_of=t.issued_on, bills=bills, forecast=None,
                                 signals=spike) is None

    def test_no_outlook_when_usual_payments_cover_the_bills(self):
        bills = paid_bills([-3, 0, 3]) + [recent(9, dt.date(2027, 9, 1), "100", "0")]
        assert m.project_outlook(as_of=AS_OF, bills=bills, forecast=None,
                                 signals=self.drift) is None

    def test_a_forecast_due_date_follows_the_accounts_own_billing_lag(self):
        bills = paid_bills([-3, 0, 3])
        forecast = m.OpenForecast(dt.date(2027, 8, 25), dt.date(2027, 8, 31), Decimal("200"))
        outlook = m.project_outlook(as_of=AS_OF, bills=bills, forecast=forecast,
                                    signals=self.drift)
        # The bills fall due 23 days after their period ends.
        assert outlook.projected_arrears_on == dt.date(2027, 9, 23) + dt.timedelta(days=30)
        assert outlook.projected_overdue_nzd == Decimal("100.00")

    def test_a_forecast_already_past_its_arrears_date_is_ignored(self):
        bills = paid_bills([-3, 0, 3])
        stale = m.OpenForecast(dt.date(2027, 6, 1), dt.date(2027, 6, 30), Decimal("200"))
        assert m.project_outlook(as_of=AS_OF, bills=bills, forecast=stale,
                                 signals=self.drift) is None

    def test_the_overdue_figure_adds_every_bill_up_to_the_one_in_arrears(self):
        bills = paid_bills([-3, 0, 3], last_due=dt.date(2027, 7, 1)) + [
            recent(8, dt.date(2027, 8, 10), "100", "96", dt.date(2027, 8, 12)),
            recent(9, dt.date(2027, 9, 1), "150", "0")]
        outlook = m.project_outlook(as_of=AS_OF, bills=bills, forecast=None,
                                    signals=self.drift)
        assert outlook.projected_arrears_on == dt.date(2027, 10, 1)
        assert outlook.projected_overdue_nzd == Decimal("54.00")
        assert "under the 95% arrears line" in outlook.basis

    def test_an_outlook_must_look_forward(self):
        account = m.Account(1, "A1", False, False)
        outlook = m.Outlook(AS_OF, Decimal("10"), "basis")
        stale = m.Assessment(account, AS_OF, AS_OF, AS_OF, (), outlook)
        with self.assertRaises(AssertionError):
            m.outlook_rows([stale], {1: 42})
        fresh = m.Assessment(account, AS_OF - dt.timedelta(days=1), AS_OF, AS_OF, (), outlook)
        assert m.outlook_rows([fresh], {1: 42}) == [(42, AS_OF, Decimal("10"), "basis")]
        assert m.outlook_rows([fresh], {}) == []

    def test_forecasts_are_read_as_at_the_assessment_date_and_grossed_up(self):
        sql = " ".join(m.OPEN_FORECAST_SQL.split())
        assert "f.forecast_date <= %(as_of)s::date" in sql
        assert "f.period_start > b.billed_to" in sql
        assert "f.projected_amount * (1 + b.gst_rate)" in sql
        assert "b.issue_date <= %(as_of)s::date" in sql
        recent_sql = " ".join(m.RECENT_BILL_SQL.split())
        assert recent_sql.count("p.paid_at < ((%(as_of)s::date + 1)") == 2


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
        assert got[0].outlook is None

    def test_warning_signs_and_outlook_reach_the_parent(self):
        t = trigger_bill(2)
        account = m.Account(1, "A1", False, False)
        got = m.build_assessments(
            accounts=[account], as_of=t.issued_on,
            observed_from=t.issued_on - dt.timedelta(days=179),
            bills_by_account={}, failed_dd_by_account={}, history_by_account={},
            daily_usage_by_account={}, usage_to=None,
            recent_bills_by_account={1: slide(2, as_of=t.issued_on)},
        )
        assert codes(got[0].signals) == ["payment_drift", "bill_above_usual_payment"]
        assert got[0].outlook is not None
        assert not m.has_v1_signal(got[0])


class TestCodes(unittest.TestCase):
    def test_the_eight_codes_match_the_schema_enum(self):
        root = Path(__file__).resolve().parents[2]
        v005 = (root / "migrations" / "serving" / "V005__sprint4_stretch.sql").read_text()
        reference = (root / "design" / "kea_serving_schema.sql").read_text()
        for code in m.WARNING_SIGN_CODES:
            assert f"ADD VALUE IF NOT EXISTS '{code}'" in v005, code
        for code in m.V1_CODES + m.WARNING_SIGN_CODES:
            assert f"'{code}'" in reference, code
        assert m.OUTLOOK_TRIGGERS < set(m.WARNING_SIGN_CODES)
        assert m.RULESET_VERSION == "hardship-identification-v2"


if __name__ == "__main__":
    unittest.main()
