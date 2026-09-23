-- kea.mart_forecast_input  <-  mart_forecast_input
--
-- days_observed and hdd_expected_total are warehouse-side
-- working columns with no serving equivalent. hdd_expected_total in particular
-- must NOT be reconstructed downstream: design §8.6's invariant is that the
-- total is decided once, so that hdd_to_date + hdd_expected_remaining never
-- drifts as the period progresses. Serving carries the remainder, which is the
-- half a forecast actually consumes.
select
    account_id, period_start, as_at_date, days_elapsed, days_total,
    kwh_to_date, cost_to_date, baseline_daily_kwh,
    same_period_last_year_kwh, same_period_last_year_amount,
    hdd_to_date, hdd_expected_remaining, heating_sensitivity_kwh_per_hdd,
    fixed_charges_period, is_forecastable,
    -- Sprint 4 (V005): added back by forecast hdd-linear-v3.
    detected_load_kwh_day
from KEA.KEA_MARTS.MART_FORECAST_INPUT
-- Some early snapshots have no cost_to_date, and serving declares it NOT NULL.
-- They are dropped rather than zero-filled: a cost of 0 would feed straight
-- into a projected bill. The warehouse gate makes every such row
-- is_forecastable = false, so nothing a forecast can use is lost.
where cost_to_date is not null
