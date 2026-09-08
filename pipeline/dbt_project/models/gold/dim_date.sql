-- Design §6.4's date dimension. ONE ROW PER NZ CALENDAR DAY.
--
-- ⚠️ TWO DEVIATIONS FROM §6.4, BOTH DELIBERATE AND BOTH VISIBLE HERE.
--
-- 1. HEATING DEGREE DAYS ARE NOT ON THIS TABLE. §6.4 says dim_date "carries
--    the NZ holiday calendar and heating degree days joined from NIWA, so the
--    explainer's weather comparison needs no join at query time". HDD is
--    REGIONAL -- there are four climate regions and four different values for
--    any given day -- so putting it here would either force this dimension to
--    a (date, region) grain, which stops it being a conformed date dimension,
--    or silently pick one region. It lives in fct_weather_daily at its true
--    grain; §6.4's intent is met by mart_daily_usage denormalising it, which
--    is where "no join at query time" actually matters.
--
-- 2. is_public_holiday IS NULL, NOT FALSE. There is no NZ holiday calendar
--    anywhere in this repo -- it is named in three design documents and backed
--    by no data. NULL says "unknown"; false would assert that Waitangi Day is
--    an ordinary Tuesday, which is worse than admitting the gap. Filling it
--    needs a source, the same way the weather column did before NIWA landed.
--
-- The spine is generated rather than derived from observed data, so a date with
-- no readings still has a row -- otherwise a gap in metering would silently
-- become a gap in the calendar, and "no usage that day" would be
-- indistinguishable from "that day is missing".

with spine as (
    select dateadd(day, seq4(), date '2023-01-01') as date_day
    from table(generator(rowcount => 2192))   -- 2023-01-01 .. 2028-12-31
)

select
    date_day,
    year(date_day)                                   as year,
    month(date_day)                                  as month,
    day(date_day)                                    as day_of_month,
    dayofweekiso(date_day)                           as day_of_week,
    dayname(date_day)                                as day_name,
    dayofweekiso(date_day) in (6, 7)                 as is_weekend,
    weekofyear(date_day)                             as week_of_year,
    quarter(date_day)                                as quarter,
    date_trunc('month', date_day)::date              as month_start,
    last_day(date_day)                               as month_end,
    -- See deviation 2 above. Typed so a source can fill it without a migration.
    cast(null as boolean)                            as is_public_holiday,
    cast(null as varchar)                            as public_holiday_name
from spine
