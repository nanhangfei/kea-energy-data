"""Hardship outreach is traceable, idempotent, never sent by the job, and never
created by a warning sign on its own."""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "decisions"))

import create_hardship_alerts as m  # noqa: E402


class TestEligibility(unittest.TestCase):
    def candidate(self, **overrides) -> m.Candidate:
        values = dict(
            assessment_id=11, account_id=22, assessed_on=dt.date(2027, 8, 31),
            codes=frozenset({"late_payment_streak"}), already_alerted=False,
            has_open_outreach=False,
        )
        values.update(overrides)
        values["codes"] = frozenset(values["codes"])
        return m.Candidate(**values)

    def test_only_a_new_flagged_assessment_without_open_outreach_is_eligible(self):
        assert self.candidate().eligible
        assert not self.candidate(codes=()).eligible
        assert not self.candidate(already_alerted=True).eligible
        assert not self.candidate(has_open_outreach=True).eligible

    def test_every_v1_code_stays_eligible_on_its_own(self):
        for code in sorted(m.V1_CODES):
            with self.subTest(code=code):
                row = self.candidate(codes={code})
                assert row.eligible and not row.is_early

    def test_a_warning_sign_on_its_own_creates_no_outreach(self):
        for code in sorted(m.WARNING_SIGN_CODES):
            with self.subTest(code=code):
                row = self.candidate(codes={code})
                assert not row.warrants_outreach and not row.eligible

    def test_drift_and_a_shortfall_without_a_large_bill_create_no_outreach(self):
        assert not self.candidate(codes={"payment_drift", "first_shortfall"}).eligible

    def test_a_large_bill_with_drift_or_a_shortfall_is_an_early_outreach(self):
        for other in ({"payment_drift"}, {"first_shortfall"},
                      {"payment_drift", "first_shortfall"}):
            with self.subTest(codes=other):
                row = self.candidate(codes={"bill_above_usual_payment"} | other)
                assert row.eligible and row.is_early
        assert not self.candidate(codes={"bill_above_usual_payment", "payment_drift"},
                                  has_open_outreach=True).eligible

    def test_a_v1_code_with_warning_signs_is_an_ordinary_outreach(self):
        row = self.candidate(codes={"late_payment_streak", "bill_above_usual_payment",
                                    "payment_drift"})
        assert row.eligible and not row.is_early
        row = self.candidate(codes={"direct_debit_failed", "bill_above_usual_payment"})
        assert row.eligible and not row.is_early

    def test_the_code_sets_match_the_assessment_job(self):
        import os
        api = Path(os.environ.get("KEA_API_DIR", Path(__file__).resolve().parents[3]
                                  / "services" / "api"))
        if not (api / "app" / "analytics" / "engine.py").exists():
            self.skipTest("the assessment job needs services/api")
        import build_hardship_assessments as assessments
        assert m.V1_CODES == frozenset(assessments.V1_CODES)
        assert m.WARNING_SIGN_CODES == frozenset(assessments.WARNING_SIGN_CODES)
        assert m.EARLY_WITH == assessments.OUTLOOK_TRIGGERS

    def test_the_dashboards_use_the_same_rule(self):
        import importlib.util
        folder = Path(__file__).resolve().parents[2] / "analysis" / "metabase"

        def load(name):
            spec = importlib.util.spec_from_file_location(name, folder / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        plans = load("build_plans_dashboard")
        assert frozenset(plans.HARDSHIP_V1_CODES) == m.V1_CODES
        assert plans.HARDSHIP_EARLY_SPIKE == m.EARLY_SPIKE
        assert frozenset(plans.HARDSHIP_EARLY_WITH) == m.EARLY_WITH
        assert frozenset(load("build_story_dashboard").V1_HARDSHIP_CODES) == m.V1_CODES

        sprint4 = load("build_sprint4_dashboard")
        assert frozenset(sprint4.V1_CODES) == m.V1_CODES
        assert sprint4.EARLY_SPIKE == m.EARLY_SPIKE
        assert frozenset(sprint4.EARLY_WITH) == m.EARLY_WITH

    def test_eligibility_reads_the_codes_not_the_count(self):
        sql = " ".join(m.ASSESSMENTS_SQL.split())
        assert "array(select s.signal_code::text from kea.hardship_signal s" in sql

    def test_customer_copy_contains_no_internal_reason_or_amount(self):
        assert "$" not in m.BODY
        assert "failed" not in m.BODY.lower()
        assert "arrears" not in m.BODY.lower()
        assert "private conversation" in m.BODY


class TestSchemaGuards(unittest.TestCase):
    def test_fresh_schema_and_migration_both_carry_idempotency_guards(self):
        data = Path(__file__).resolve().parents[2]
        schema = (data / "design" / "kea_serving_schema.sql").read_text()
        migration = (data / "pipeline" / "migrations" /
                     "2026-09-07_sprint3_alert_idempotency.sql").read_text()
        for sql in (schema, migration):
            assert "alert_hardship_has_assessment" in sql
            assert "uq_alert_hardship_assessment" in sql
            assert "uq_alert_hardship_open_account" in sql


if __name__ == "__main__":
    unittest.main()
