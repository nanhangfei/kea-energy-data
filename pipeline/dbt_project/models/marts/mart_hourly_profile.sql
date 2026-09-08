-- Grain: icp × month × hour × weekday. Serving target kea.mart_hourly_profile
-- (kea_serving_schema.sql:627).
--
-- HOUR OF DAY IS THE NZ HOUR. interval_start is a UTC instant; taking hour()
-- from it directly would rotate every household's load shape by 12 or 13 hours
-- and put the evening peak at breakfast. The DST offset means the rotation is
-- not even constant across the year, so the error would look like a seasonal
-- effect.
--
-- AVERAGED OVER SAMPLE DAYS, NOT SUMMED. Two half-hours land in each hour, so
-- the hourly total is summed within a day and then averaged across the days in
-- the month -- otherwise a month with more observed days looks like a
-- higher-consuming household.
--
-- ⚠️ is_weekday CARRIES LITTLE SIGNAL IN THE FIXTURES and that is a known
-- property of the generator, not of this model: generate_scenarios.py applies
-- one hourly shape to every day, so the two curves sit on top of each other.
-- The Stage 2 cohort does not have this problem -- LCL is real metering. Stack
-- doc §11 records the measurement.

with hourly as (
    select
        f.icp_id,
        date_trunc('month', convert_timezone('Pacific/Auckland', f.interval_start))::date as profile_month,
        hour(convert_timezone('Pacific/Auckland', f.interval_start))                       as hour_of_day,
        dayofweekiso(convert_timezone('Pacific/Auckland', f.interval_start)) between 1 and 5 as is_weekday,
        convert_timezone('Pacific/Auckland', f.interval_start)::date                       as usage_date,
        sum(case when r.flow_direction = 'import' then f.kwh else 0 end) as import_kwh,
        sum(case when r.flow_direction = 'export' then f.kwh else 0 end) as export_kwh
    from {{ ref('fct_meter_read') }} f
    join {{ ref('dim_register') }} r on r.register_id = f.register_id
    group by 1, 2, 3, 4, 5
)

select
    icp_id,
    profile_month,
    hour_of_day,
    is_weekday,
    round(avg(import_kwh), 4)      as avg_import_kwh,
    round(avg(export_kwh), 4)      as avg_export_kwh,
    count(distinct usage_date)     as sample_days
from hourly
group by 1, 2, 3, 4
