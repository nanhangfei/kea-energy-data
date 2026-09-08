"""Sprint 3 source-to-serving plumbing that is checkable without a database.

The warehouse build exercises the SQL against real data. These tests pin the
static contract between its layers so a later edit cannot quietly stop one of
the three Sprint 3 entities before that expensive build is run.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from kea_pipeline import avro_schema, serving, spec  # noqa: E402


class TestSprint3Entities(unittest.TestCase):
    def test_payment_is_an_nz_wall_clock_incremental(self):
        payment = spec.get("src_billing.payments")
        assert payment.mode == spec.INCREMENTAL
        assert payment.watermark == "paid_ts"
        assert payment.naive_watermark is True

    def test_history_and_term_credits_are_snapshots(self):
        assert spec.get("src_billing.account_plan_history").mode == spec.SNAPSHOT
        assert spec.get("src_catalogue.term_credits").mode == spec.SNAPSHOT

    def test_account_contract_carries_the_term_fields(self):
        fields = avro_schema.fields(
            avro_schema.load(avro_schema.contract_for("src_billing", "accounts"))
        )
        by_name = {field.name: field for field in fields}
        assert by_name["term_months"].kind == "int"
        assert by_name["term_start_dt"].kind == "timestamp-micros"
        assert by_name["term_start_dt"].nullable is True

    def test_new_contracts_match_the_source_shapes(self):
        expected = {
            ("src_billing", "payments"): [
                "pay_id", "acct_id", "inv_id", "paid_ts", "pay_amt",
                "pay_method", "failed_flag", "fail_reason",
            ],
            ("src_billing", "account_plan_history"): [
                "hist_id", "acct_id", "plan_code", "term_months",
                "valid_from", "valid_to", "change_rsn",
            ],
            ("src_catalogue", "term_credits"): [
                "term_months", "monthly_credit", "valid_from", "valid_to",
            ],
        }
        for entity, names in expected.items():
            got = avro_schema.fields(avro_schema.load(avro_schema.contract_for(*entity)))
            assert [field.name for field in got] == names


class TestSprint3ServingOrder(unittest.TestCase):
    def test_new_tables_are_loaded_after_all_of_their_foreign_keys(self):
        names = [table.name for table in serving.TABLES]
        assert names.index("catalogue.plan") < names.index("catalogue.term_credit")
        assert names.index("catalogue.plan") < names.index("billing.account_plan_history")
        assert names.index("billing.account") < names.index("billing.account_plan_history")
        assert names.index("billing.bill") < names.index("billing.payment")

    def test_append_only_runtime_tables_stay_out_of_replica_loads(self):
        names = {table.name for table in serving.TABLES}
        assert "kea.alert" not in names
        assert "kea.plan_recommendation" not in names

    def test_each_new_serving_table_has_its_named_export_query(self):
        root = Path(__file__).resolve().parents[1] / "export"
        for name in ("catalogue.term_credit", "billing.account_plan_history", "billing.payment"):
            assert (root / f"{name}.sql").is_file()


if __name__ == "__main__":
    unittest.main()
