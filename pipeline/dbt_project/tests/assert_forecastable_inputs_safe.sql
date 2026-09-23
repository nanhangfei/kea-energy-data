-- A true readiness gate is permission to publish a monetary projection. Every
-- term consumed by build_bill_forecasts.py must therefore already be present
-- and physically meaningful; the output job does not silently clamp bad model
-- features into plausible-looking customer amounts.
select account_id, period_start, as_at_date
from {{ ref('mart_forecast_input') }}
where is_forecastable
  and (
      kwh_to_date <= 0
      or cost_to_date is null
      or baseline_daily_kwh is null
      or baseline_daily_kwh < 0
      or hdd_expected_remaining is null
      or hdd_expected_remaining < 0
      or heating_sensitivity_kwh_per_hdd is null
      or heating_sensitivity_kwh_per_hdd < 0
      or fixed_charges_period is null
      or fixed_charges_period < 0
      or detected_load_kwh_day is null
      or detected_load_kwh_day < 0
  )
