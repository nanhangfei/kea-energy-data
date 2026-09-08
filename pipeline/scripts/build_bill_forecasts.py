"""Build the mid-cycle bill forecast output from its serving mart input.

The warehouse owns feature preparation and the ``is_forecastable`` safety
gate.  This job owns only the deterministic projection and its persisted
confidence band.  Re-running it is safe: the natural key is upserted and rows
previously written by this model are removed when their input is no longer
eligible.
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from load_export_to_serving import _connect as connect_serving


MODEL_VERSION = "hdd-linear-v2"
UNCERTAINTY_PCT = Decimal("0.15")
ANOMALY_PCT = Decimal("15")
ZERO = Decimal("0")


def _round(value: Decimal, quantum: str) -> Decimal:
    return value.quantize(Decimal(quantum), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ForecastProjection:
    projected_kwh: Decimal
    projected_amount: Decimal
    projected_low: Decimal
    projected_high: Decimal
    variance_pct: Decimal | None
    is_anomalous: bool


def project_forecast(
    *,
    days_elapsed: int,
    days_total: int,
    kwh_to_date: Decimal,
    cost_to_date: Decimal,
    baseline_daily_kwh: Decimal,
    hdd_expected_remaining: Decimal,
    heating_sensitivity_kwh_per_hdd: Decimal,
    fixed_charges_period: Decimal,
    baseline_amount: Decimal | None,
) -> ForecastProjection:
    """Apply the forecast formula documented by fixture 09.

    Only the unobserved variable-cost portion receives a confidence band.
    Metered consumption and the cycle's fixed charge are already known.
    """
    if not 1 <= days_elapsed <= days_total:
        raise ValueError("days_elapsed must be between 1 and days_total")
    if kwh_to_date <= ZERO:
        raise ValueError("kwh_to_date must be positive")
    non_negative = {
        "cost_to_date": cost_to_date,
        "baseline_daily_kwh": baseline_daily_kwh,
        "hdd_expected_remaining": hdd_expected_remaining,
        "heating_sensitivity_kwh_per_hdd": heating_sensitivity_kwh_per_hdd,
        "fixed_charges_period": fixed_charges_period,
    }
    for name, value in non_negative.items():
        if value < ZERO:
            raise ValueError(f"{name} must not be negative")

    fixed_so_far = _round(
        fixed_charges_period * Decimal(days_elapsed) / Decimal(days_total), "0.01"
    )
    variable_cost_to_date = cost_to_date - fixed_so_far
    if variable_cost_to_date < ZERO:
        raise ValueError("cost_to_date is below the elapsed fixed charge")
    rate_per_kwh = _round(variable_cost_to_date / kwh_to_date, "0.000001")

    remaining_days = Decimal(days_total - days_elapsed)
    remaining_base_kwh = _round(remaining_days * baseline_daily_kwh, "0.0001")
    remaining_heat_kwh = _round(
        hdd_expected_remaining * heating_sensitivity_kwh_per_hdd, "0.0001"
    )
    remaining_kwh = remaining_base_kwh + remaining_heat_kwh
    projected_kwh = _round(kwh_to_date + remaining_kwh, "0.0001")
    remaining_cost = _round(remaining_kwh * rate_per_kwh, "0.01")
    projected_amount = _round(
        fixed_charges_period + projected_kwh * rate_per_kwh, "0.01"
    )
    half_band = _round(remaining_cost * UNCERTAINTY_PCT, "0.01")
    projected_low = max(ZERO, projected_amount - half_band)
    projected_high = projected_amount + half_band

    variance_pct = None
    if baseline_amount is not None and baseline_amount != ZERO:
        variance_pct = _round(
            (projected_amount - baseline_amount) / baseline_amount * Decimal("100"),
            "0.01",
        )

    return ForecastProjection(
        projected_kwh=projected_kwh,
        projected_amount=projected_amount,
        projected_low=projected_low,
        projected_high=projected_high,
        variance_pct=variance_pct,
        is_anomalous=variance_pct is not None and abs(variance_pct) >= ANOMALY_PCT,
    )


INPUT_SQL = """
SELECT account_id, period_start, as_at_date, days_elapsed, days_total,
       kwh_to_date, cost_to_date, baseline_daily_kwh,
       same_period_last_year_amount, hdd_expected_remaining,
       heating_sensitivity_kwh_per_hdd, fixed_charges_period
FROM kea.mart_forecast_input i
JOIN billing.account a USING (account_id)
JOIN metering.icp m ON m.icp_id = a.icp_id
WHERE i.is_forecastable
  AND m.icp_number LIKE '09%%'
  AND i.kwh_to_date > 0
  AND i.baseline_daily_kwh IS NOT NULL
  AND i.hdd_expected_remaining IS NOT NULL
  AND i.heating_sensitivity_kwh_per_hdd IS NOT NULL
  AND (%s IS NULL OR i.account_id = %s)
ORDER BY i.account_id, i.period_start, i.as_at_date
"""


UPSERT_SQL = """
INSERT INTO kea.bill_forecast
    (account_id, period_start, period_end, forecast_date, days_elapsed,
     kwh_to_date, projected_kwh, projected_amount, projected_low, projected_high,
     baseline_amount, variance_pct, is_anomalous, model_version)
VALUES %s
ON CONFLICT (account_id, period_start, forecast_date) DO UPDATE SET
    period_end = EXCLUDED.period_end,
    days_elapsed = EXCLUDED.days_elapsed,
    kwh_to_date = EXCLUDED.kwh_to_date,
    projected_kwh = EXCLUDED.projected_kwh,
    projected_amount = EXCLUDED.projected_amount,
    projected_low = EXCLUDED.projected_low,
    projected_high = EXCLUDED.projected_high,
    baseline_amount = EXCLUDED.baseline_amount,
    variance_pct = EXCLUDED.variance_pct,
    is_anomalous = EXCLUDED.is_anomalous,
    model_version = EXCLUDED.model_version
"""


def build_rows(source_rows: list[tuple]) -> list[tuple]:
    rows = []
    for (
        account_id, period_start, forecast_date, days_elapsed, days_total,
        kwh_to_date, cost_to_date, baseline_daily_kwh, baseline_amount,
        hdd_expected_remaining, sensitivity, fixed_charges_period,
    ) in source_rows:
        projection = project_forecast(
            days_elapsed=days_elapsed,
            days_total=days_total,
            kwh_to_date=kwh_to_date,
            cost_to_date=cost_to_date,
            baseline_daily_kwh=baseline_daily_kwh,
            hdd_expected_remaining=hdd_expected_remaining,
            heating_sensitivity_kwh_per_hdd=sensitivity,
            fixed_charges_period=fixed_charges_period,
            baseline_amount=baseline_amount,
        )
        period_end = period_start + dt.timedelta(days=days_total - 1)
        rows.append((
            account_id, period_start, period_end, forecast_date, days_elapsed,
            kwh_to_date, projection.projected_kwh, projection.projected_amount,
            projection.projected_low, projection.projected_high, baseline_amount,
            projection.variance_pct, projection.is_anomalous, MODEL_VERSION,
        ))
    return rows


def run(
    account_id: int | None = None, dry_run: bool = False
) -> tuple[int, int, int, int]:
    from psycopg2.extras import execute_values

    with connect_serving() as conn:
        with conn.cursor() as cur:
            cur.execute(INPUT_SQL, (account_id, account_id))
            rows = build_rows(cur.fetchall())
            if not rows:
                raise SystemExit(
                    "no forecastable inputs: rebuild the full weather-overlapping "
                    "meter span and inspect mart_forecast_input.is_forecastable"
                )

            accounts = len({row[0] for row in rows})
            anomalies = sum(bool(row[12]) for row in rows)
            if dry_run:
                conn.rollback()
                return len(rows), accounts, anomalies, 0

            execute_values(cur, UPSERT_SQL, rows, page_size=1000)

            # Keep this model's output exactly aligned with the authoritative
            # input gate. The optional account scope never touches other users.
            cur.execute(
                """
                DELETE FROM kea.bill_forecast f
                 WHERE f.model_version = %s
                   AND (%s IS NULL OR f.account_id = %s)
                   AND NOT EXISTS (
                       SELECT 1
                         FROM kea.mart_forecast_input i
                        WHERE i.account_id = f.account_id
                          AND i.period_start = f.period_start
                          AND i.as_at_date = f.forecast_date
                          AND i.is_forecastable
                   )
                """,
                (MODEL_VERSION, account_id, account_id),
            )
            removed = cur.rowcount
            cur.execute(
                """
                INSERT INTO kea.mart_load_log
                    (mart_name, started_at, completed_at, row_count,
                     warehouse_run_id, status)
                VALUES ('bill_forecast', now(), now(), %s, %s, 'succeeded')
                """,
                (len(rows), MODEL_VERSION),
            )
        conn.commit()
    return len(rows), accounts, anomalies, removed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    rows, accounts, anomalies, removed = run(args.account_id, args.dry_run)
    mode = "would write" if args.dry_run else "upserted"
    print(f"bill forecasts: {mode} {rows:,} rows for {accounts:,} accounts; "
          f"{anomalies:,} anomalous; {removed:,} stale rows removed")


if __name__ == "__main__":
    main()
