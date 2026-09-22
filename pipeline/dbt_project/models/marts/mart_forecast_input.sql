-- Grain: account × cycle × day. Serving target kea.mart_forecast_input
-- (kea_serving_schema.sql:740), PK (account_id, period_start, as_at_date).
--
-- IT IS AN INSERT, NOT AN UPDATE (§8.6). as_at_date is in the key, so one cycle
-- accumulates a row per elapsed day and a past forecast can be replayed exactly
-- as it was made. A mart that overwrote today's row would make every historical
-- forecast unauditable -- you could never show why the projection said what it
-- said on the 10th.
--
-- THE INVARIANT §8.6 ASSERTS: hdd_to_date + hdd_expected_remaining is CONSTANT
-- across a cycle -- "fact displacing forecast, day by day, over a fixed total".
-- That is what fixes the shape of the calculation. The expected TOTAL is
-- decided once for the period from climate normals; the remaining is then
-- derived as total − to_date, not forecast independently. Forecasting the
-- remaining days directly would let the sum drift as actuals came in, and the
-- fixture's assertion would fail.
--
-- ⚠️ THE CLAMP IS WHERE THE INVARIANT STOPS HOLDING, deliberately. A colder
-- period than normal can push hdd_to_date past the expected total, which would
-- make the remainder negative and forecast a household using less energy for
-- being cold. greatest(..., 0) prevents that, and where it engages the sum is no
-- longer constant -- correctly, because the forecast has been overtaken by fact.
-- hdd_expected_total is published so a reader can see when that happened rather
-- than inferring it.

with cycles as (
    -- One cycle per invoice: the invoice defines the period, and the account it
    -- belongs to. NZ dates, for the reason int_billing__plan_resolution gives --
    -- comparing a TIMESTAMP_TZ to a DATE resolves in the SESSION timezone.
    select distinct
        l.source_account_id                                                  as account_id,
        l.icp_id,
        convert_timezone('Pacific/Auckland', l.period_from)::date            as period_start,
        convert_timezone('Pacific/Auckland', l.period_to)::date              as period_end,
        l.invoice_id
    from {{ ref('fct_bill_line') }} l
),

cycle_totals as (
    select
        c.account_id, c.icp_id, c.period_start, c.period_end, c.invoice_id,
        datediff('day', c.period_start, c.period_end) + 1                    as days_total,
        -- Daily charges are certain on day one of the cycle (§8.6): only the
        -- variable part needs forecasting at all.
        round(sum(case when l.unit_of_measure = 'DAY' then l.line_amount else 0 end), 2)
                                                                             as fixed_charges_period,
        round(sum(case when l.charge_group = 'ENE' and l.flow_direction = 'import'
                       then l.quantity else 0 end), 4)                       as period_kwh,
        round(sum(l.line_amount), 2)                                         as period_amount
    from cycles c
    join {{ ref('fct_bill_line') }} l on l.invoice_id = c.invoice_id
    group by 1,2,3,4,5
),

-- Every elapsed day of every cycle. dim_date is the spine so a day with no
-- reading still produces a snapshot -- otherwise a metering gap would look like
-- a day on which no forecast was made.
snapshots as (
    select
        t.*,
        d.date_day                                          as as_at_date,
        datediff('day', t.period_start, d.date_day) + 1     as days_elapsed
    from cycle_totals t
    join {{ ref('dim_date') }} d
      on d.date_day between t.period_start and t.period_end
),

-- Actuals to date, from the daily mart.
to_date as (
    select
        s.account_id, s.period_start, s.as_at_date,
        sum(u.import_kwh)                                   as kwh_to_date,
        sum(u.heating_degree_days)                          as hdd_to_date,
        count_if(u.heating_degree_days is null)             as days_without_weather,
        -- COUNTS DAYS THAT EXIST, which days_without_weather cannot. That column
        -- only sees rows that ARE in the daily mart and finds their HDD null; a
        -- day with no row at all is invisible to it. Without this, a cycle whose
        -- metering covers 2 of its 31 days passed the hedge with hdd_to_date
        -- frozen at whatever those two days summed to -- a forecast built on a
        -- fifteenth of the period, presented as complete.
        count(*)                                            as days_observed
    from snapshots s
    join {{ ref('mart_daily_usage') }} u
      on u.icp_id = s.icp_id
     and u.usage_date between s.period_start and s.as_at_date
    group by 1,2,3
),

-- The expected total for the WHOLE period, decided once. This is what makes the
-- invariant hold.
expected as (
    select
        s.account_id, s.period_start,
        round(sum(n.normal_hdd), 2)                         as hdd_expected_total
    from (select distinct account_id, icp_id, period_start, period_end from snapshots) s
    join {{ ref('dim_icp') }} i        on i.icp_id = s.icp_id
    join {{ ref('dim_date') }} d       on d.date_day between s.period_start and s.period_end
    join {{ ref('int_weather__climate_normals') }} n
      on n.climate_region = i.weather_region
     and n.month_of_year  = month(d.date_day)
     and n.day_of_month   = day(d.date_day)
    group by 1,2
),

-- This household a year ago, over the same calendar window. §8.6's second
-- honest anchor: the household's own recent daily rate is the first.
last_year as (
    select
        s.account_id, s.period_start,
        round(sum(u.import_kwh), 4)                         as same_period_last_year_kwh
    from (select distinct account_id, icp_id, period_start, period_end from snapshots) s
    join {{ ref('mart_daily_usage') }} u
      on u.icp_id = s.icp_id
     and u.usage_date between dateadd(year, -1, s.period_start)
                          and dateadd(year, -1, s.period_end)
    group by 1,2
),

prior_bill as (
    -- The previous closed bill for this account, for the variable rate and the
    -- year-ago amount. Ordered by period end, not issue date, so a re-issued
    -- invoice cannot become its own predecessor.
    select
        account_id, period_start, period_amount, period_kwh, fixed_charges_period,
        lag(period_amount)        over (partition by account_id order by period_end) as prior_amount,
        lag(period_kwh)           over (partition by account_id order by period_end) as prior_kwh,
        lag(fixed_charges_period) over (partition by account_id order by period_end) as prior_fixed
    from cycle_totals
)

select
    s.account_id,
    s.period_start,
    s.as_at_date,
    s.days_elapsed,
    s.days_total,
    round(coalesce(t.kwh_to_date, 0), 4)                    as kwh_to_date,

    -- ⚠️ AN ESTIMATE AT THE PRIOR PERIOD'S RATE, NOT A RATING. A true
    -- cost_to_date needs the tariff applied to a partial period -- tou_windows,
    -- rate_components, band scoping -- which is the same rating engine
    -- delta_from_tariff is missing in §8.5. Priced at the prior bill's own
    -- average variable rate, which is the method §8.5 already uses to turn kWh
    -- into dollars, so the two marts at least agree with each other. Null where
    -- there is no prior bill to take a rate from, rather than 0.
    case when p.prior_kwh > 0 then
        round(s.fixed_charges_period * s.days_elapsed / nullif(s.days_total, 0)
              + coalesce(t.kwh_to_date, 0)
                * ((p.prior_amount - p.prior_fixed) / p.prior_kwh), 2)
    end                                                     as cost_to_date,

    sens.baseline_daily_kwh,
    ly.same_period_last_year_kwh,
    -- The amount a year ago is the PRIOR-YEAR BILL's total where one exists.
    -- Not re-derived from kWh: the tariff a year ago is not this year's.
    round(ly_amt.period_amount, 2)                          as same_period_last_year_amount,

    coalesce(t.days_observed, 0)                            as days_observed,
    round(coalesce(t.hdd_to_date, 0), 2)                    as hdd_to_date,
    round(greatest(e.hdd_expected_total - coalesce(t.hdd_to_date, 0), 0), 2)
                                                            as hdd_expected_remaining,
    -- Published so the invariant is checkable and the clamp visible.
    e.hdd_expected_total,

    sens.heating_sensitivity_kwh_per_hdd,
    s.fixed_charges_period,

    -- The same hedge mart_bill_explanation carries. A forecast without a usable
    -- sensitivity, or over a period the weather does not cover, is a confident
    -- guess rather than a projection.
    (sens.heating_sensitivity_kwh_per_hdd is not null
     and sens.heating_sensitivity_kwh_per_hdd >= 0
     -- A regression can have a good fit and still extrapolate to a negative
     -- zero-HDD intercept. That is not physically usable baseline demand.
     and sens.baseline_daily_kwh is not null
     and sens.baseline_daily_kwh >= 0
     and sens.fit_r2 is not null
     and sens.fit_r2 >= {{ var('min_heating_fit_r2') }}
     and coalesce(t.kwh_to_date, 0) > 0
     -- cost_to_date is inferred from the prior bill's variable rate. Export
     -- credit can make that rate negative; such a bill is not a safe price
     -- anchor for an import forecast.
     and p.prior_kwh is not null
     and p.prior_kwh > 0
     and p.prior_amount is not null
     and p.prior_fixed is not null
     and p.prior_amount >= p.prior_fixed
     and coalesce(s.fixed_charges_period, -1) >= 0
     and coalesce(t.days_without_weather, 1) = 0
     -- Every elapsed day must be present, not merely non-null where present.
     and coalesce(t.days_observed, 0) = s.days_elapsed
     and e.hdd_expected_total is not null)                  as is_forecastable
from snapshots s
left join to_date  t on t.account_id = s.account_id and t.period_start = s.period_start
                    and t.as_at_date = s.as_at_date
left join expected e on e.account_id = s.account_id and e.period_start = s.period_start
left join last_year ly on ly.account_id = s.account_id and ly.period_start = s.period_start
left join prior_bill p on p.account_id = s.account_id and p.period_start = s.period_start
left join cycle_totals ly_amt
       on ly_amt.account_id = s.account_id
      and ly_amt.period_end = dateadd(year, -1, s.period_end)
left join {{ ref('int_metering__heating_sensitivity') }} sens on sens.icp_id = s.icp_id
