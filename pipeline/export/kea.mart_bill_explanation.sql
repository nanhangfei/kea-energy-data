-- kea.mart_bill_explanation  <-  mart_bill_explanation
--
-- Column-for-column, which is what design §6.1 means by "each one maps 1:1
-- onto a kea.mart_* in the serving schema so Stage 5 exports without
-- reshaping". This file exists to be that claim's evidence, not to transform.
--
-- ONE EXCEPTION, AND IT IS A COMPROMISE WORTH NAMING. estimated_read_pct is
-- NOT NULL DEFAULT 0 in serving and null on 4,959 of 11,528 warehouse rows,
-- so it is coalesced to 0. That is not the same statement -- "none of these
-- reads were estimated" is a claim, and "we could not work it out" is not --
-- and 4,954 of those rows do have kWh, so the null is a missing denominator
-- rather than an empty period.
--
-- What makes it safe rather than merely convenient: every one of the 4,959 is
-- already is_explainable = false, which is the flag the agent gates on. It is
-- told not to trust these bills before it ever reads the percentage. If that
-- correlation is ever broken, this coalesce becomes a lie and the column
-- should be made nullable in kea_serving_schema.sql instead.
select
    bill_id, account_id, icp_id, period_start, period_end, period_days,
    total_amount, total_kwh, prior_bill_id, prior_period_days,
    prior_total_amount, prior_total_kwh, amount_delta, amount_delta_pct,
    kwh_delta, delta_from_period_length, delta_from_weather,
    delta_from_base_load, delta_from_tariff, delta_unexplained,
    hdd_this_period, hdd_prior_period, temp_mean_this, temp_mean_prior,
    base_load_kwh_day_this, base_load_kwh_day_prior,
    heating_sensitivity_kwh_per_hdd, coalesce(estimated_read_pct, 0) as estimated_read_pct,
    missing_interval_count, is_explainable,
    -- Sprint 4 (V005): the detected-load driver and its inputs.
    detected_load_kwh_day_this, detected_load_kwh_day_prior,
    delta_from_detected_loads
from KEA.KEA_MARTS.MART_BILL_EXPLANATION
-- 69 rows carry no icp_id and serving declares it NOT NULL. These are design
-- §6.3's orphan references -- a bill whose account points at a connection
-- point metering has never heard of, which Stage 2 plants deliberately and
-- silver quarantines. All 69 are already is_explainable = false. They are
-- dropped rather than given a placeholder ICP, because every serving consumer
-- of this mart reaches it through a household, and there is no household here.
where icp_id is not null
