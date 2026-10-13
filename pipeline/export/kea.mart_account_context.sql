-- kea.mart_account_context  <-  mart_account_context, enriched
--
-- THE ONE MART THAT IS NOT 1:1. The serving table is the agent's whole picture
-- of a customer, and it declares eighteen columns the warehouse mart does not
-- carry -- five of them NOT NULL. They are recovered from the dimensions the
-- mart was built from rather than added to the mart itself, because nothing in
-- the warehouse needs them: they exist for the agent's first prompt, and
-- widening a mart to serve a consumer in another system is how a mart stops
-- being purpose-built.
--
-- has_solar and has_controlled_load are properties of the REGISTERS, so they
-- are aggregated from dim_register rather than guessed from the plan's name.
-- A plan called ecoSOLAR does not prove an export register exists.
--
-- daily_fixed_charge sums the per-day import components of the account's
-- current plan version: the plan's daily charge, ex GST. The catalogue and
-- its provenance are in dictionary/ecotricity-tariff-source.md.
--
-- WHAT IS DELIBERATELY LEFT NULL: current_period_start, current_period_end and
-- days_into_period. Those describe a billing period in progress, and this
-- pipeline has no notion of "now" -- it has invoices that closed. Filling them
-- from the last closed bill would state that a period is open when the data
-- says it ended. They are nullable, and the agent can tell null from wrong.
with registers as (
    select icp_id,
           boolor_agg(flow_direction = 'export') as has_solar,
           boolor_agg(is_controlled)             as has_controlled_load
    from KEA.KEA_GOLD.DIM_REGISTER
    group by icp_id
),
reads as (
    select icp_id,
           max(interval_start) as last_read_at
    from KEA.KEA_GOLD.FCT_METER_READ
    group by icp_id
),
estimated as (
    select icp_id,
           case when sum(interval_count) = 0 then 0
                else round(100.0 * sum(estimated_count) / sum(interval_count), 2)
           end as estimated_read_pct
    from KEA.KEA_MARTS.MART_DAILY_USAGE
    group by icp_id
),
daily_fixed as (
    select p.plan_id,
           sum(rc.unit_rate) as daily_fixed_charge
    from KEA.KEA_STAGING.STG_CATALOGUE__RATE_COMPONENTS rc
    join KEA.KEA_GOLD.DIM_PLAN p on p.source_plan_id = rc.plan_id
    where rc.rate_unit_of_measure = 'DAY'
      and rc.flow_direction = 'import'
    group by p.plan_id
)
select
    m.account_id,
    m.account_number,
    m.customer_id,
    c.customer_number,
    m.customer_name,
    c.email_address                     as email,
    c.phone_number                      as phone,
    m.is_medically_dependent,
    m.is_vulnerable,
    m.icp_id,
    m.icp_number,
    i.address_full                      as address_display,
    m.climate_region,
    i.network_code,
    m.network_name,
    coalesce(r.has_solar, false)            as has_solar,
    coalesce(r.has_controlled_load, false)  as has_controlled_load,
    p.plan_id,
    m.plan_code,
    p.plan_name,
    case p.user_category when 'LOW' then 'low_user' else 'standard' end as user_type,
    p.is_tou,
    f.daily_fixed_charge,
    case m.payment_method
        when 'DD'   then 'direct_debit'
        when 'BT'   then 'bank_transfer'
        when 'CARD' then 'card'
        else 'other'
    end                                 as payment_method,
    m.billing_cycle_day,
    cast(null as date)                  as current_period_start,
    cast(null as date)                  as current_period_end,
    cast(null as integer)               as days_into_period,
    coalesce(e.estimated_read_pct, 0)   as estimated_read_pct,
    rd.last_read_at,
    m.is_quarantined,
    m.reject_reasons                    as quarantine_reason
from KEA.KEA_MARTS.MART_ACCOUNT_CONTEXT m
join KEA.KEA_GOLD.DIM_CUSTOMER c on c.customer_id = m.customer_id
join KEA.KEA_GOLD.DIM_ICP      i on i.icp_id      = m.icp_id
join KEA.KEA_GOLD.DIM_PLAN     p on p.plan_code   = m.plan_code and p.is_active
left join registers   r  on r.icp_id  = m.icp_id
left join reads       rd on rd.icp_id = m.icp_id
left join estimated   e  on e.icp_id  = m.icp_id
left join daily_fixed f  on f.plan_id = p.plan_id
-- The same three orphan accounts as mart_bill_explanation, excluded for the
-- same reason: serving's icp_id is NOT NULL, and the agent's whole context for
-- a customer is meaningless without the connection point it describes. They
-- are visible as quarantined in the warehouse, which is where that fact
-- belongs.
where m.icp_id is not null
order by m.account_id
