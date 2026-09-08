"""Hardship outreach is traceable, idempotent and never sent by the job."""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import create_hardship_alerts as m  # noqa: E402


class TestEligibility(unittest.TestCase):
    def candidate(self, **overrides) -> m.Candidate:
        values = dict(
            assessment_id=11, account_id=22, assessed_on=dt.date(2027, 8, 31),
            signal_count=1, already_alerted=False, has_open_outreach=False,
        )
        values.update(overrides)
        return m.Candidate(**values)

    def test_only_a_new_flagged_assessment_without_open_outreach_is_eligible(self):
        assert self.candidate().eligible
        assert not self.candidate(signal_count=0).eligible
        assert not self.candidate(already_alerted=True).eligible
        assert not self.candidate(has_open_outreach=True).eligible

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
