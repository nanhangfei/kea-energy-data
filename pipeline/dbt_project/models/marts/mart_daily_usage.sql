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
-- A DAY'S COMPLETENESS COUNTS EACH IMPORT HALF-HOUR ONCE (Sprint 4). An
-- earlier version took count(*) over every register, so a solar household or
-- fixture 01 (import and export) read 96 on a 48-half-hour day: its days
-- never matched expected_intervals, the heating fit dropped all of them, and a
-- day with gaps still looked full to a `>=` reader. interval_count is now the
-- fewest distinct half-hours read on any of the ICP's import registers that
-- exist that day -- from its first_installed_on, or its first read if that is
-- earlier. A register with no reads that day counts 0, and so does an ICP
-- with no import register. Every ICP has one import register today (UN), so
-- this is simply its import half-hours read. estimated_count counts distinct
-- estimated import half-hours. Names and types are unchanged, so the export
-- and the backend read the corrected figures with no change.
--
-- BASE LOAD IS MEASURED IN THE DARK ON A GENERATING ICP. `import = max(0,
-- load - pv)`, so a solar household imports exactly 0 whenever its panels
-- cover its draw, and the day's smallest import half-hour measured the sun
-- rather than the house. For an ICP with an export register the minimum is
-- taken over 22:00-05:00 NZ local only. Non-generating ICPs are unchanged.
-- See the column for why the window stops there.
--
-- ⚠️ is_public_holiday IS COALESCED TO FALSE. The serving column is NOT NULL,
-- but dim_date has no holiday source and carries NULL. Coalescing is required
-- to load; it is also an assertion this pipeline cannot back, and it is
-- recorded rather than hidden behind a DEFAULT.

-- An ICP that exports has generation behind the meter. You cannot export
-- without an export register, so this is the same discriminator the solar
-- what-if uses (scripts/estimate_common.py, `has_export`).
with generating_icps as (
    select distinct icp_id
    from {{ ref('dim_register') }}
    where flow_direction = 'export'
),

reads as (
    select
        f.icp_id,
        convert_timezone('Pacific/Auckland', f.interval_start)       as local_start,
        convert_timezone('Pacific/Auckland', f.interval_start)::date as usage_date,
        f.register_id,
        f.interval_start,
        f.kwh,
        f.quality,
        r.flow_direction,
        r.is_controlled,
        g.icp_id is not null                                         as has_generation
    from {{ ref('fct_meter_read') }} f
    join {{ ref('dim_register') }} r on r.register_id = f.register_id
    left join generating_icps g on g.icp_id = f.icp_id
),

-- One row per import register per day it read, with its distinct half-hours.
import_register_days as (
    select
        icp_id,
        register_id,
        usage_date,
        count(distinct interval_start)                               as half_hours,
        count(distinct case when quality = 'estimated' then interval_start end)
                                                                     as estimated_half_hours
    from reads
    where flow_direction = 'import'
    group by 1, 2, 3
),

-- Each import register, from the day it exists: installed, or first read if
-- the reads start earlier than the meter record says.
import_registers as (
    select
        r.icp_id,
        r.register_id,
        least(
            coalesce(r.first_installed_on, date '9999-12-31'),
            coalesce(min(d.usage_date), date '9999-12-31')
        )                                                            as counts_from
    from {{ ref('dim_register') }} r
    left join import_register_days d on d.register_id = r.register_id
    where r.flow_direction = 'import'
    group by r.icp_id, r.register_id, r.first_installed_on
),

daily as (
    select
        icp_id,
        usage_date,
        sum(case when flow_direction = 'import' then kwh else 0 end) as import_kwh,
        sum(case when flow_direction = 'export' then kwh else 0 end) as export_kwh,
        sum(case when is_controlled then kwh else 0 end)             as controlled_kwh,
        sum(case when not is_controlled then kwh else 0 end)         as uncontrolled_kwh,
        count(distinct case when flow_direction = 'import' and quality = 'estimated'
                            then interval_start end)                 as estimated_count,
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
        --
        -- AND FOR A GENERATING ICP, DARK HALF-HOURS ONLY. Filtering to import
        -- was not enough. Behind the meter `import = max(0, load - pv)`, so a
        -- household whose panels cover its draw imports exactly 0 in that
        -- half-hour -- on an IMPORT register, with no export row involved. The
        -- day's minimum therefore measured the sun, not the house, which is the
        -- opposite of what §8.4 asks it for. Measured on
        -- release demo-20260918T113420: 5,807 of 5,978 complete ICP-days,
        -- 97.1%, across the 9 ICPs that carry an export register.
        --
        -- The definition is unchanged -- the smallest half-hour in which the
        -- WHOLE load is observable. For an ICP with no generation that is all
        -- 48. For one with panels it is the dark half-hours, 22:00-05:00 NZ
        -- local, the window in which no NZ roof generates on any day of the
        -- year: the southernmost region we carry is Christchurch (-43.5), which
        -- at midsummer sets 21:22 and rises 05:47 NZDT, clearing the window by
        -- 38 and 47 minutes. The generator's PV curve is narrower still -- its
        -- widest daylight is 14.8 hours centred on solar noon
        -- (gen_source/gen_metering.py, `pv_half_hour`), so 05:36 to 20:24.
        --
        -- ⚠️ NON-GENERATING ICPs ARE UNTOUCHED, deliberately. Narrowing their
        -- window too would match kea_serving_schema.sql's "overnight
        -- percentile" more closely, but it would move every published base
        -- load, every `delta_from_base_load` and every detected-load floor at
        -- once. That is a decision, not a bug fix; see §12.
        round(min(case when flow_direction = 'import'
                        and (not has_generation
                             or hour(local_start) >= 22
                             or hour(local_start) < 5)
                       then kwh end) * 48, 4)
                                                                     as base_load_kwh
    from reads
    group by 1, 2
),

-- See the header: the fewest half-hours on any import register that exists
-- that day, 0 when one of them read nothing or there is none.
completeness as (
    select
        u.icp_id,
        u.usage_date,
        coalesce(min(coalesce(rd.half_hours, 0)), 0)                 as interval_count
    from daily u
    left join import_registers ir
           on ir.icp_id = u.icp_id
          and ir.counts_from <= u.usage_date
    left join import_register_days rd
           on rd.register_id = ir.register_id
          and rd.usage_date = u.usage_date
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
    c.interval_count,
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
join completeness c            on c.icp_id = u.icp_id and c.usage_date = u.usage_date
join {{ ref('dim_date') }} d   on d.date_day = u.usage_date
join day_length dl             on dl.date_day = u.usage_date
join {{ ref('dim_icp') }} i    on i.icp_id = u.icp_id
left join {{ ref('fixture_weather_daily') }} fw
       on fw.icp_number = i.icp_number
      and fw.date_day = u.usage_date
left join {{ ref('fct_weather_daily') }} w
       on w.climate_region = i.weather_region
      and w.date_day = u.usage_date
