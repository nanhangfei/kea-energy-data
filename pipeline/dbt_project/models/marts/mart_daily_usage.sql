-- Grain: icp × NZ day. Serving target kea.mart_daily_usage
-- (kea_serving_schema.sql:584), PK (icp_id, usage_date).
--
-- THIS IS WHERE THE WEATHER FINALLY JOINS. Design §6.4 wanted HDD available
-- without a join at query time; fct_weather_daily holds it at (region, day)
-- and this is the model that denormalises it onto the household-day, via
-- dim_icp.weather_region: the ICP's own region where it has a station, the
-- nearest station's otherwise. A station is not a region -- that assumption is
-- recorded in dictionary/niwa-weather-source.md, not implied here.
--
-- A FIXTURE'S OWN WEATHER COMES FIRST. Fixtures 02-09 were written against
-- synthetic temperatures, and their expected bill explanations are computed
-- from them; explained against NIWA they would describe a different winter
-- (issue #143). fixture_weather_daily, generated from sample/*.sql, carries
-- those temperatures, and where it has the ICP's day it wins. HDD is recomputed
-- from it on fct_weather_daily's base -- one formula, two inputs.
--
-- THE DAY IS AN NZ CALENDAR DAY, AND IT IS NOT 24 HOURS. interval_start is a
-- UTC instant; grouping on it directly would put NZ evening readings on the
-- previous day. And expected_intervals is 46 or 50 across a DST transition,
-- not 48 -- computed from the actual NZ day length rather than assumed, which
-- is the same rule kea_pipeline.window enforces for load dates. Hard-coding 48
-- would make two days a year look 4% short and 4% over.
--
-- ⚠️ is_public_holiday IS COALESCED TO FALSE. The serving column is NOT NULL,
-- but dim_date has no holiday source and carries NULL. Coalescing is required
-- to load; it is also an assertion this pipeline cannot back, and it is
-- recorded rather than hidden behind a DEFAULT.

with reads as (
    select
        f.icp_id,
        convert_timezone('Pacific/Auckland', f.interval_start)::date as usage_date,
        f.kwh,
        f.quality,
        r.flow_direction,
        r.is_controlled
    from {{ ref('fct_meter_read') }} f
    join {{ ref('dim_register') }} r on r.register_id = f.register_id
),

daily as (
    select
        icp_id,
        usage_date,
        sum(case when flow_direction = 'import' then kwh else 0 end) as import_kwh,
        sum(case when flow_direction = 'export' then kwh else 0 end) as export_kwh,
        sum(case when is_controlled then kwh else 0 end)             as controlled_kwh,
        sum(case when not is_controlled then kwh else 0 end)         as uncontrolled_kwh,
        count(*)                                                     as interval_count,
        count_if(quality = 'estimated')                              as estimated_count,
        -- Design §8.4: ROUND(MIN(x.kwh) * 48, 4) -- the smallest half-hour of
        -- the day, annualised to a day. It is a floor, not a mean: whatever the
        -- house draws when nobody is doing anything. A heater raises the mean
        -- and leaves this alone; a failing fridge or a forgotten element draws
        -- the same at 3am as at 3pm, so this steps up and stays up. That is
        -- what makes it separate a fault from a cold snap (§8.7).
        --
        -- IMPORT ONLY. Export half-hours would drag the minimum toward zero on
        -- every solar household and make base load meaningless exactly where
        -- the fault detection matters.
        round(min(case when flow_direction = 'import' then kwh end) * 48, 4)
                                                                     as base_load_kwh
    from reads
    group by 1, 2
),

-- 46 / 48 / 50. Derived from the NZ day's real length, not assumed.
day_length as (
    select
        d.date_day,
        datediff(
            'minute',
            convert_timezone('Pacific/Auckland', 'UTC', d.date_day::timestamp_ntz),
            convert_timezone('Pacific/Auckland', 'UTC', dateadd(day, 1, d.date_day)::timestamp_ntz)
        ) / 30 as expected_intervals
    from {{ ref('dim_date') }} d
)

select
    u.icp_id,
    u.usage_date,
    round(u.import_kwh, 4)       as import_kwh,
    round(u.export_kwh, 4)       as export_kwh,
    round(u.controlled_kwh, 4)   as controlled_kwh,
    round(u.uncontrolled_kwh, 4) as uncontrolled_kwh,
    u.interval_count,
    u.base_load_kwh,
    dl.expected_intervals,
    u.estimated_count,
    coalesce(fw.temp_mean_c, w.temp_mean_c) as temp_mean_c,
    case when fw.temp_mean_c is not null
         then cast(round(greatest({{ var('hdd_base_c') }} - fw.temp_mean_c, 0), 2) as number(6,2))
         else w.heating_degree_days
    end                          as heating_degree_days,
    d.is_weekend,
    -- See the header: required by the serving NOT NULL, unbacked by any source.
    coalesce(d.is_public_holiday, false) as is_public_holiday
from daily u
join {{ ref('dim_date') }} d   on d.date_day = u.usage_date
join day_length dl             on dl.date_day = u.usage_date
join {{ ref('dim_icp') }} i    on i.icp_id = u.icp_id
left join {{ ref('fixture_weather_daily') }} fw
       on fw.icp_number = i.icp_number
      and fw.date_day = u.usage_date
left join {{ ref('fct_weather_daily') }} w
       on w.climate_region = i.weather_region
      and w.date_day = u.usage_date
