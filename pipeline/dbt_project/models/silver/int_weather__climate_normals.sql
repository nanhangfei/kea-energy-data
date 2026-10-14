-- Expected heating degree days for a calendar day, per climate region.
--
-- WHY THIS EXISTS. §8.6 requires `hdd_expected_remaining` and never says where
-- the expectation comes from -- the column is specified, its source is not. A
-- weather forecast API would answer it for the next fortnight and nothing for
-- the rest of a billing cycle, and we do not have one. Climate normals answer
-- the whole horizon and come from data already in the warehouse:
-- fct_weather_daily holds 69,689 NIWA observations spanning 1954-2026.
--
-- THIRTY YEARS, NOT SEVENTY-THREE. A normal is a climate baseline, and the
-- meteorological convention is a 30-year window precisely because a longer one
-- averages in a climate that no longer applies. Christchurch reaches back to
-- 1954; including the 1950s would bias every forecast toward a colder past.
--
-- KEYED ON (month, day), NOT day-of-year. Day-of-year shifts by one after
-- February in a leap year, so 1 March would average against 29 February's
-- neighbours in three years out of four. (month, day) is stable; 29 February
-- simply has a quarter of the samples, which `sample_years` makes visible
-- rather than hiding.

{% set normal_years = 30 %}

with history as (
    select
        climate_region,
        month(date_day)              as month_of_year,
        day(date_day)                as day_of_month,
        heating_degree_days
    from {{ ref('fct_weather_daily') }}
    where heating_degree_days is not null
      and date_day >= dateadd(year, -{{ normal_years }}, current_date())
)

select
    climate_region,
    month_of_year,
    day_of_month,
    round(avg(heating_degree_days), 2)  as normal_hdd,
    -- Published so a consumer can tell a normal built from 30 observations from
    -- one built from 7. A thin normal is still better than no forecast, but the
    -- reader should be able to see which they have.
    count(*)                            as sample_years,
    round(stddev(heating_degree_days), 2) as hdd_stddev
from history
group by 1, 2, 3
