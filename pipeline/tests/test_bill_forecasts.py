"""The Stage 6 forecast formula and its warehouse-owned readiness gate."""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_bill_forecasts as m  # noqa: E402


def forecast(**overrides) -> m.ForecastProjection:
    values = dict(
        days_elapsed=10,
        days_total=30,
        kwh_to_date=Decimal("100"),
        cost_to_date=Decimal("50"),
        baseline_daily_kwh=Decimal("9"),
        hdd_expected_remaining=Decimal("20"),
        heating_sensitivity_kwh_per_hdd=Decimal("0.5"),
        fixed_charges_period=Decimal("30"),
        baseline_amount=Decimal("120"),
    )
    values.update(overrides)
    return m.project_forecast(**values)


class TestProjection(unittest.TestCase):
    def test_uses_baseline_weather_and_uncertain_remaining_cost_only(self):
        result = forecast()

        self.assertEqual(result.projected_kwh, Decimal("290.0000"))
        self.assertEqual(result.projected_amount, Decimal("146.00"))
        self.assertEqual(result.projected_low, Decimal("134.60"))
        self.assertEqual(result.projected_high, Decimal("157.40"))
        self.assertEqual(result.variance_pct, Decimal("21.67"))
        self.assertTrue(result.is_anomalous)

    def test_band_closes_when_the_cycle_is_complete(self):
        result = forecast(
            days_elapsed=30,
            kwh_to_date=Decimal("300"),
            cost_to_date=Decimal("150"),
            hdd_expected_remaining=Decimal("0"),
        )

        self.assertEqual(result.projected_kwh, Decimal("300.0000"))
        self.assertEqual(result.projected_low, result.projected_amount)
        self.assertEqual(result.projected_amount, result.projected_high)

    def test_missing_comparison_anchor_does_not_invent_a_variance(self):
        result = forecast(baseline_amount=None)
        self.assertIsNone(result.variance_pct)
        self.assertFalse(result.is_anomalous)

    def test_unsafe_inputs_fail_loudly(self):
        for overrides in (
            {"days_elapsed": 0},
            {"days_elapsed": 31},
            {"kwh_to_date": Decimal("0")},
            {"baseline_daily_kwh": Decimal("-1")},
            {"cost_to_date": Decimal("1")},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                forecast(**overrides)


class TestContracts(unittest.TestCase):
    def test_schema_and_export_both_carry_the_authoritative_gate(self):
        data = Path(__file__).resolve().parents[2]
        schema = (data / "design" / "kea_serving_schema.sql").read_text()
        export = (
            data / "pipeline" / "export" / "kea.mart_forecast_input.sql"
        ).read_text()
        migration = (
            data
            / "pipeline"
            / "migrations"
            / "2026-09-07_bill_forecast_input_gate.sql"
        ).read_text()
        self.assertIn("is_forecastable", schema)
        self.assertIn("boolean     NOT NULL", schema)
        self.assertIn("fixed_charges_period, is_forecastable", export)
        self.assertIn("ADD COLUMN IF NOT EXISTS is_forecastable", migration)
        self.assertIn("ALTER COLUMN is_forecastable DROP DEFAULT", migration)
        self.assertIn("JOIN billing.account a USING (account_id)", m.INPUT_SQL)


if __name__ == "__main__":
    unittest.main()
