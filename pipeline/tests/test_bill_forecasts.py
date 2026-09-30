"""The Stage 6 forecast formula and its warehouse-owned readiness gate."""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "decisions"))

import build_bill_forecasts as m  # noqa: E402


def forecast(**overrides) -> m.ForecastProjection:
    values = dict(
        days_elapsed=10,
        days_total=30,
        kwh_to_date=Decimal("100"),
        cost_to_date=Decimal("50"),
        baseline_daily_kwh=Decimal("9"),
        detected_load_kwh_day=Decimal("0"),
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

    def test_no_detected_load_gives_the_v2_projection(self):
        # v2 had no detected-load term: remaining days x baseline + HDD x slope.
        # 100 + 20 x 9 + 20 x 0.5 = 290, and the same band as before.
        result = forecast(detected_load_kwh_day=Decimal("0"))
        self.assertEqual(result.projected_kwh, Decimal("290.0000"))
        self.assertEqual(result.projected_amount, Decimal("146.00"))
        self.assertEqual(result.projected_low, Decimal("134.60"))
        self.assertEqual(result.projected_high, Decimal("157.40"))

    def test_detected_load_adds_remaining_days_times_its_kwh(self):
        without = forecast()
        result = forecast(detected_load_kwh_day=Decimal("4.5"))

        # 20 remaining days x 4.5 kWh = 90 kWh, at the cycle's rate of
        # (50 - 10) / 100 = $0.40: $36 more, and the band widens by 15% of it.
        self.assertEqual(result.projected_kwh - without.projected_kwh, Decimal("90.0000"))
        self.assertEqual(result.projected_amount - without.projected_amount, Decimal("36.00"))
        self.assertEqual(result.projected_high - result.projected_amount, Decimal("16.80"))

    def test_detected_load_is_not_added_to_elapsed_days(self):
        result = forecast(
            days_elapsed=30,
            kwh_to_date=Decimal("300"),
            cost_to_date=Decimal("150"),
            hdd_expected_remaining=Decimal("0"),
            detected_load_kwh_day=Decimal("4.5"),
        )
        self.assertEqual(result.projected_kwh, Decimal("300.0000"))

    def test_unsafe_inputs_fail_loudly(self):
        for overrides in (
            {"days_elapsed": 0},
            {"days_elapsed": 31},
            {"kwh_to_date": Decimal("0")},
            {"baseline_daily_kwh": Decimal("-1")},
            {"detected_load_kwh_day": Decimal("-0.1")},
            {"cost_to_date": Decimal("1")},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                forecast(**overrides)


class TestRows(unittest.TestCase):
    def test_rows_carry_v3_and_read_the_detected_load(self):
        source = [(
            7, dt.date(2026, 6, 1), dt.date(2026, 6, 10), 10, 30,
            Decimal("100"), Decimal("50"), Decimal("9"), Decimal("4.5"),
            Decimal("120"), Decimal("20"), Decimal("0.5"), Decimal("30"),
        )]
        (row,) = m.build_rows(source)
        self.assertEqual(row[-1], "hdd-linear-v3")
        self.assertEqual(row[2], dt.date(2026, 6, 30))
        self.assertEqual(row[6], Decimal("380.0000"))
        self.assertIn("detected_load_kwh_day IS NOT NULL", m.INPUT_SQL)


class _Cursor:
    """Records statements; returns one forecastable input row."""

    def __init__(self, source):
        self.source = source
        self.statements = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        self.rowcount = 1 if sql.lstrip().startswith("DELETE") else 0

    def fetchall(self):
        return self.source


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class TestStaleRows(unittest.TestCase):
    def test_delete_removes_every_model_version(self):
        # A v2 row whose snapshot is no longer forecastable is not overwritten
        # by the upsert, so the delete must match the family, not v3 alone.
        source = [(
            7, dt.date(2026, 6, 1), dt.date(2026, 6, 10), 10, 30,
            Decimal("100"), Decimal("50"), Decimal("9"), Decimal("0"),
            None, Decimal("20"), Decimal("0.5"), Decimal("30"),
        )]
        cur = _Cursor(source)
        extras = mock.MagicMock()
        with mock.patch.dict(sys.modules, {
            "psycopg2": mock.MagicMock(extras=extras),
            "psycopg2.extras": extras,
        }):
            rows, accounts, _, removed = m.run(connection=_Connection(cur))

        self.assertEqual((rows, accounts, removed), (1, 1, 1))
        (delete_sql, delete_params), = [
            (sql, params) for sql, params in cur.statements
            if sql.lstrip().startswith("DELETE")
        ]
        self.assertIn("model_version LIKE %s", delete_sql)
        self.assertEqual(delete_params[0], "hdd-linear-%")
        # The pattern matches both the stale version and the current one.
        import fnmatch
        pattern = delete_params[0].replace("%", "*")
        self.assertTrue(fnmatch.fnmatch("hdd-linear-v2", pattern))
        self.assertTrue(fnmatch.fnmatch(m.MODEL_VERSION, pattern))


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
