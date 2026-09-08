-- kWh added per heating degree day, fitted per ICP. Design §8.4:
--
--     daily kwh  ≈  baseline_daily_kwh  +  heating_sensitivity_kwh_per_hdd × HDD
--
-- ONE NUMBER, TWO MARTS, DELIBERATELY. §8.5 runs it backwards across two closed
-- periods (delta_from_weather) and §8.6 runs it forwards across the unfinished
-- one (hdd_expected_remaining × sensitivity). Fitting it twice would let the
-- explanation and the forecast disagree about the same household.
--
-- ORDINARY LEAST SQUARES, which Snowflake gives directly as regr_slope. The
-- magnitude is itself a finding (§8.4): ~0.2 is a house that barely responds to
-- cold -- wood or gas heat, off-meter, or empty; ~0.9 a typical NZ home on
-- electric heating; ~2.5 a poorly insulated house on resistive heaters, which
-- is a heat-pump conversation reached from meter data without a site visit.
--
-- ⚠️ NULL RATHER THAN ZERO WHEN IT CANNOT BE FITTED. A new connection with a
-- fortnight of data has no defensible slope, and zero would assert the house
-- does not respond to cold -- which then silently attributes a real cold snap
-- to base load instead. §8.4 says both this and baseline_daily_kwh are
-- nullable for exactly that reason. MIN_DAYS is the floor; days with no HDD
-- reading cannot contribute either, so they are excluded rather than treated
-- as zero-degree days.

-- ⚠️ FITTED AT MONTHLY GRAIN, AND THAT IS THE WHOLE CORRECTNESS ARGUMENT.
--
-- An earlier version fitted daily and produced noise: median R² 0.01, slopes
-- from -3.98 to 8.44, and a negative slope asserts a house uses more heating
-- when it is warmer. The cause is in the source data, not the arithmetic. The
-- cohort is LCL -- real LONDON metering date-shifted so UK winter lands in NZ
-- winter (dictionary/lcl-source-data.md). The shift aligns SEASONS, not
-- WEATHER: a household's day-to-day variation responds to London's weather that
-- day, while the HDD it is regressed against is Auckland's. The seasonal
-- envelope matches; the daily wiggle inside it cannot.
--
-- Fitting monthly measures exactly the component that does align, and it is
-- also the grain the slope is USED at -- §8.5 applies it across billing
-- periods, not across days. Measured on 12 months, same data, same households:
--
--     daily    499 icps   median slope 0.45   median R² 0.069
--     monthly  446 icps   median slope 0.74   median R² 0.467
--
-- 0.74 against §8.4's ~0.9 for a typical NZ home on electric heating, and 290
-- of 446 clear the 0.30 fit floor. Fit an estimator at the grain it is spent at.

{% set min_days_in_month = 20 %}
{% set min_months = 6 %}

with monthly as (
    -- Near-complete months only. A month with four observed days has a mean
    -- that is not a monthly mean, and it would carry the same weight as a full
    -- one in the regression.
    select
        icp_id,
        date_trunc('month', usage_date)::date as month_start,
        avg(heating_degree_days)              as hdd,
        avg(import_kwh)                       as kwh,
        count(*)                              as days_observed
    from {{ ref('mart_daily_usage') }}
    where heating_degree_days is not null
      and import_kwh is not null
      -- Complete days only: a partial day understates consumption and would
      -- drag the month's mean down independently of the weather.
      and interval_count = expected_intervals
    group by 1, 2
    having count(*) >= {{ min_days_in_month }}
)

select
    icp_id,
    count(*)                                              as fitted_on_months,
    sum(days_observed)                                    as fitted_on_days,
    case when count(*) >= {{ min_months }}
         then round(regr_slope(kwh, hdd), 4) end          as heating_sensitivity_kwh_per_hdd,
    case when count(*) >= {{ min_months }}
         then round(regr_intercept(kwh, hdd), 4) end      as baseline_daily_kwh,
    -- Carried because §8.4 treats the magnitude as a finding, and a finding
    -- needs a fit quality. mart_bill_explanation gates on it: a slope with no
    -- explanatory power must not price a weather driver.
    case when count(*) >= {{ min_months }}
         then round(regr_r2(kwh, hdd), 4) end             as fit_r2
from monthly
group by icp_id
