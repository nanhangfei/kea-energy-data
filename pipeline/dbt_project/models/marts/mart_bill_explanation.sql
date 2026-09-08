-- Grain: bill. Serving target kea.mart_bill_explanation
-- (kea_serving_schema.sql:673) -- drivers PRE-DECOMPOSED in attribution order,
-- so the agent reads a decomposition rather than computing one.
--
-- ORDER MATTERS AND IT IS NOT A PREFERENCE (design §8.5). period_length is
-- computed first and REMOVED before anything else is attributed. A 43-day
-- move-in bill looks like a 40% consumption increase and is not one, and
-- attributing that to cold weather would be confidently wrong -- which is the
-- specific failure this whole mart exists to prevent. Each driver is measured
-- after the ones above it are taken out, so nothing is counted twice.
--
-- EVERYTHING IS PRICED AT THE PRIOR PERIOD'S OWN AVERAGE VARIABLE RATE, not a
-- headline tariff, so a driver measured in kWh becomes a figure the customer
-- can check against a bill they already hold:
--
--     rate = (prior_amount − daily_rate × prior_days) / prior_kwh
--
-- Subtracting the fixed daily charges first is what makes it a VARIABLE rate.
-- Leaving them in inflates every per-kWh driver by the fixed component.

with bills as (
    select
        l.invoice_id,
        max(l.invoice_number)                          as invoice_number,
        max(l.source_account_id)                       as account_id,
        max(l.icp_id)                                  as icp_id,
        max(l.plan_id)                                 as plan_id,
        convert_timezone('Pacific/Auckland', max(l.period_from))::date as period_start,
        convert_timezone('Pacific/Auckland', max(l.period_to))::date   as period_end,
        round(sum(l.line_amount), 2)                   as total_amount,
        -- Import energy only: export lines are credits and negative quantities
        -- would net against consumption and understate the driver.
        round(sum(case when l.charge_group = 'ENE' and l.flow_direction = 'import'
                       then l.quantity else 0 end), 4) as total_kwh,
        -- The fixed component, in dollars per day, so it can be removed before
        -- the variable rate is derived.
        round(sum(case when l.unit_of_measure = 'DAY' then l.line_amount else 0 end), 2)
                                                       as fixed_amount
    from {{ ref('fct_bill_line') }} l
    group by l.invoice_id
),

periods as (
    select
        b.*,
        datediff('day', b.period_start, b.period_end) + 1 as period_days,
        -- The PRIOR bill is the previous period for the same ACCOUNT, ordered
        -- by period end. Not by issue date: a re-issued or late invoice would
        -- otherwise become its own predecessor.
        lag(b.invoice_id)    over (partition by b.account_id order by b.period_end) as prior_bill_id,
        lag(b.total_amount)  over (partition by b.account_id order by b.period_end) as prior_total_amount,
        lag(b.total_kwh)     over (partition by b.account_id order by b.period_end) as prior_total_kwh,
        lag(b.period_start)  over (partition by b.account_id order by b.period_end) as prior_period_start,
        lag(b.period_end)    over (partition by b.account_id order by b.period_end) as prior_period_end,
        lag(b.fixed_amount)  over (partition by b.account_id order by b.period_end) as prior_fixed_amount,
        lag(b.plan_id)       over (partition by b.account_id order by b.period_end) as prior_plan_id
    from bills b
),

-- Weather and base load over each period, from the daily mart. A period with
-- ANY uncovered day cannot be explained on weather, which is what
-- is_explainable exists to say.
period_daily as (
    select
        p.invoice_id,
        sum(d.heating_degree_days)                     as hdd,
        avg(d.temp_mean_c)                             as temp_mean,
        avg(d.base_load_kwh)                           as base_load_kwh_day,
        sum(d.estimated_count)                         as estimated_intervals,
        sum(d.interval_count)                          as observed_intervals,
        sum(d.expected_intervals)                      as expected_intervals,
        count_if(d.heating_degree_days is null)        as days_without_weather
    from periods p
    join {{ ref('mart_daily_usage') }} d
      on d.icp_id = p.icp_id
     and d.usage_date between p.period_start and p.period_end
    group by p.invoice_id
),

prior_daily as (
    select
        p.invoice_id,
        sum(d.heating_degree_days)                     as prior_hdd,
        avg(d.temp_mean_c)                             as prior_temp_mean,
        avg(d.base_load_kwh)                           as prior_base_load_kwh_day
    from periods p
    join {{ ref('mart_daily_usage') }} d
      on d.icp_id = p.icp_id
     and d.usage_date between p.prior_period_start and p.prior_period_end
    group by p.invoice_id
),

j as (
    select
        p.*,
        pd.hdd, pd.temp_mean, pd.base_load_kwh_day,
        pd.estimated_intervals, pd.observed_intervals, pd.expected_intervals,
        pd.days_without_weather,
        qd.prior_hdd, qd.prior_temp_mean, qd.prior_base_load_kwh_day,
        s.heating_sensitivity_kwh_per_hdd              as sens,
        s.fit_r2,
        datediff('day', p.prior_period_start, p.prior_period_end) + 1 as prior_period_days
    from periods p
    left join period_daily pd on pd.invoice_id = p.invoice_id
    left join prior_daily  qd on qd.invoice_id = p.invoice_id
    left join {{ ref('int_metering__heating_sensitivity') }} s on s.icp_id = p.icp_id
),

rated as (
    select
        j.*,
        -- The prior period's own fixed charge per day, and the variable rate
        -- left once it is removed. Both null when there is no prior bill --
        -- the first bill on an account has nothing to be explained against.
        case when prior_period_days > 0
             then prior_fixed_amount / prior_period_days end             as daily_rate,
        case when prior_total_kwh > 0 and prior_period_days > 0
             then (prior_total_amount - prior_fixed_amount) / prior_total_kwh
        end                                                              as rate
    from j
)

select
    invoice_id                                       as bill_id,
    account_id,
    icp_id,
    period_start,
    period_end,
    period_days,
    total_amount,
    total_kwh,
    prior_bill_id,
    prior_period_days,
    prior_total_amount,
    prior_total_kwh,
    round(total_amount - prior_total_amount, 2)      as amount_delta,
    case when prior_total_amount > 0
         then round((total_amount - prior_total_amount) / prior_total_amount * 100, 2)
    end                                              as amount_delta_pct,
    round(total_kwh - prior_total_kwh, 4)            as kwh_delta,

    -- ---- the four drivers, in attribution order (design §8.5) -------------
    -- An extra day costs both the energy an average day uses AND that day's
    -- fixed charge, which is why this one alone carries + daily_rate.
    round((period_days - prior_period_days)
          * (prior_total_kwh / nullif(prior_period_days, 0) * rate + daily_rate), 2)
                                                     as delta_from_period_length,
    round(sens * (hdd - prior_hdd) * rate, 2)        as delta_from_weather,
    round((base_load_kwh_day - prior_base_load_kwh_day) * period_days * rate, 2)
                                                     as delta_from_base_load,
    -- ⚠️ NOT RE-RATED. §8.5 defines this as re-rating this period's kWh under
    -- both plan versions with usage held constant, which needs the rating
    -- engine expressed in SQL -- tou_windows, rate_components and band
    -- scoping. Where the plan version did not change between the two periods
    -- the answer is definitionally 0 and that is asserted here; where it did,
    -- it is NULL rather than 0, because 0 would silently push a real tariff
    -- move into delta_unexplained and make the decomposition look complete.
    case when prior_plan_id is null then null
         when plan_id = prior_plan_id then 0
    end                                              as delta_from_tariff,

    round(
        (total_amount - prior_total_amount)
        - coalesce((period_days - prior_period_days)
              * (prior_total_kwh / nullif(prior_period_days, 0) * rate + daily_rate), 0)
        - coalesce(sens * (hdd - prior_hdd) * rate, 0)
        - coalesce((base_load_kwh_day - prior_base_load_kwh_day) * period_days * rate, 0)
        - coalesce(case when plan_id = prior_plan_id then 0 end, 0)
    , 2)                                             as delta_unexplained,

    round(hdd, 2)                                    as hdd_this_period,
    round(prior_hdd, 2)                              as hdd_prior_period,
    round(temp_mean, 2)                              as temp_mean_this,
    round(prior_temp_mean, 2)                        as temp_mean_prior,
    round(base_load_kwh_day, 4)                      as base_load_kwh_day_this,
    round(prior_base_load_kwh_day, 4)                as base_load_kwh_day_prior,
    sens                                             as heating_sensitivity_kwh_per_hdd,

    round(coalesce(estimated_intervals, 0)
          / nullif(observed_intervals, 0) * 100, 2)  as estimated_read_pct,
    greatest(coalesce(expected_intervals, 0) - coalesce(observed_intervals, 0), 0)
                                                     as missing_interval_count,

    -- ⚠️ THE HEDGE, AND THE POINT OF THE WHOLE MART (design §6.3, §8.5). False
    -- means the agent must not present this decomposition as an explanation.
    -- Each condition is a way the arithmetic above can be confidently wrong:
    --   no prior bill        -- nothing to compare against
    --   no sensitivity       -- weather cannot be priced, so its share leaks
    --                           into unexplained and looks like base load
    --   a day with no weather-- the HDD sum is short by an unknown amount
    --   a tariff change      -- delta_from_tariff is unknown, see above
    --   missing intervals    -- the kWh totals are not the whole period
    (prior_bill_id is not null
     and sens is not null
     and fit_r2 >= {{ var('min_heating_fit_r2') }}
     and coalesce(days_without_weather, 1) = 0
     -- THE PRIOR PERIOD MUST BE COVERED TOO. An earlier version checked only
     -- this period's weather and reported 1,269 bills as explainable whose
     -- delta_from_weather was NULL -- the prior period had no daily rows, so
     -- the driver could not be computed at all. "Explainable" has to mean the
     -- decomposition exists, not that half of its inputs do.
     and prior_hdd is not null
     and prior_base_load_kwh_day is not null
     and rate is not null
     and (prior_plan_id is null or plan_id = prior_plan_id)
     and coalesce(expected_intervals, 0) = coalesce(observed_intervals, -1)
    )                                                as is_explainable
from rated
