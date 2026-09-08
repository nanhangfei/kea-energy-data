-- The weather fact dim_date cannot carry, at its true grain: one row per
-- (climate_region, day). See dim_date's header for why it is not a column
-- there.
--
-- HEATING DEGREE DAYS ARE COMPUTED HERE, ONCE. Design §8.4 fixes the
-- definition -- GREATEST(16.0 - temp_c, 0), base 16 -- and this is the only
-- place in the warehouse that applies it. The API carried an 18 with an
-- unsourced docstring calling it "the NZ convention"; that was corrected in
-- 71f9126, and having one definition here is what stops the two drifting again.
--
-- NULL, NOT ZERO, WHERE THERE IS NO TEMPERATURE. greatest(16 - null, 0)
-- returns 0.0 in Snowflake because greatest skips nulls -- indistinguishable
-- from a warm day, and it would sum straight into a monthly total. A day no
-- station covers must leave heating_degree_days null so
-- mart_bill_explanation.is_explainable can be false for any period containing
-- one (dictionary/niwa-weather-source.md).
--
-- ⚠️ THE NEAREST-STATION FILL RULE IS NOT IMPLEMENTED. The dictionary
-- specifies it -- fill a missing day from the nearest of the other three
-- stations, mark the row as filled, and leave HDD null only where NO station
-- covers the day. This model reports what NIWA actually published, so a
-- regional gap is currently a null rather than a substitution. Recorded rather
-- than silently approximated; the marts are where the fill belongs, because
-- that is where "which household-day" is known.

with obs as (
    select
        s.climate_region,
        o.obs_date,
        o.temp_mean_c,
        o.temp_max_c,
        o.temp_min_c,
        o.agent_no,
        s.station_name
    from {{ ref('stg_weather__daily_observations') }} o
    join {{ ref('stg_weather__stations') }} s
      on s.agent_no = o.agent_no
)

select
    climate_region,
    obs_date                        as date_day,
    station_name,
    agent_no,
    temp_mean_c,
    temp_max_c,
    temp_min_c,
    case
        when temp_mean_c is null then cast(null as number(6,2))
        else round(greatest({{ var('hdd_base_c') }} - temp_mean_c, 0), 2)
    end                             as heating_degree_days,
    -- False everywhere for now; the column exists so the fill rule above can
    -- be added without every consumer changing shape.
    false                           as is_filled_from_other_station
from obs
