-- billing.bill_line  <-  fct_bill_line (+ tariff_component, for the FK)
--
-- §7.4: this depends on catalogue.tariff_component, not just on billing.bill.
-- The whole catalogue chain must be current before bill lines land.
--
-- COMPONENT_ID IS RECOVERED BY MATCHING THE TARIFF, because gold does not
-- carry it: the fact records what was charged, and the component that priced
-- it is reachable from (plan version, charge group, direction, basis, rate).
-- The match must include the RATE and the BAND. Rate alone fans out by 3,057
-- rows: a plan version carries two 'Anytime Usage' components at one price,
-- identical in charge group, direction, unit and rate, differing only in which
-- TOU window they attach to. The window's label is what separates them, and
-- the fact records it as tou_band.
--
-- THE LABELS DO NOT AGREE ON SPELLING. Windows say OFFPEAK; bill lines say
-- both OFFPEAK and OFF_PEAK. Underscores are stripped from both sides rather
-- than one spelling being declared correct, because the source uses both and
-- neither is wrong here.
--
-- A LINE WHOSE BAND MATCHES NOTHING KEEPS A NULL component_id, and that is the
-- honest answer rather than a defect: an ALLDAY line spans a plan's PEAK and
-- OFFPEAK windows at once, so no single component priced it. Gold does not
-- carry the component id, so this is recovery, not lookup, and it is allowed
-- to fail on rows where the fact genuinely does not say.
--
-- The candidate set is a CTE rather than a correlated subquery in the join
-- condition, which Snowflake does not support.
--
-- The join stays LEFT and unguarded: if it ever fans out again the row count
-- grows, and assert_serving.sql compares this table's count against
-- fct_bill_line's for exactly that reason. A silent tie-break would hide a
-- real catalogue defect instead of reporting it.
--
-- billed_quantity stays null. The printed invoice shows a metered quantity and
-- a line loss, and computing a third number from them would be inventing a
-- figure the source never states.
with -- THE ACCOUNTS THAT SURVIVE THE EXPORT, repeated from billing.account.sql
-- because a bill for an account that is not loaded is a foreign key violation
-- at the end of the transaction, not a missing row. billing.account drops an
-- account whose ICP is an orphan, whose customer has no key, or whose
-- plan_code has no active version -- 5 of 509 -- and every invoice belonging
-- to one of those has to go with it.
--
-- The predicate is duplicated rather than factored into a view: these files
-- are the reviewable statement of what crosses into serving, and a reader
-- checking why a bill is missing should not have to open a second file.
eligible_accounts as (
    select a.account_id
    from KEA.KEA_STAGING.STG_BILLING__ACCOUNTS a
    join KEA.KEA_SILVER.KEY_MAP_ICP             i  on i.icp_number      = a.icp_number
    join KEA.KEA_STAGING.STG_BILLING__CUSTOMERS sc on sc.customer_id    = a.customer_id
    join KEA.KEA_SILVER.KEY_MAP_CUSTOMER        c  on c.customer_number = sc.customer_number
    join KEA.KEA_GOLD.DIM_PLAN                  p  on p.plan_code       = a.plan_code
                                                  and p.is_active
),
-- The same set, applied through the parent: a line whose bill did not load
-- violates bill_line -> bill just as surely.
lines as (
    select
        l.*,
        -- ⚠️ 'CRD' MUST MAP, NOT FALL THROUGH. kea_serving_schema.sql:40 says
        -- "the export maps NET/ENE/OTH/CRD to these four names", and while this
        -- CASE had no CRD arm the term credit arrived labelled 'other' --
        -- a -$21.74 line sitting among the metering and admin charges, where
        -- nothing distinguishes it from a fee. analytics/tariff.py keeps the
        -- credit in its own group precisely so a re-rating pass cannot re-band
        -- it; collapsing it into 'other' here undoes that one layer down.
        case l.charge_group when 'NET' then 'network'
                            when 'ENE' then 'energy'
                            when 'CRD' then 'credit'
                            else 'other' end                as charge_group_srv,
        case l.unit_of_measure when 'DAY' then 'per_day'
                               else 'per_kwh' end           as unit_srv
    from KEA.KEA_GOLD.FCT_BILL_LINE l
    join eligible_accounts ea on ea.account_id = l.source_account_id
    join KEA.KEA_SILVER.INT_BILLING__PLAN_RESOLUTION r
      on r.invoice_id = l.invoice_id and r.plan_id is not null
),
components as (
    select
        rc.component_id,
        p.plan_id,
        rc.charge_group,
        rc.flow_direction,
        rc.rate_unit_of_measure,
        rc.unit_rate,
        replace(upper(w.band_label), '_', '') as band_key
    from KEA.KEA_STAGING.STG_CATALOGUE__RATE_COMPONENTS rc
    join KEA.KEA_GOLD.DIM_PLAN p
      on p.source_plan_id = rc.plan_id
    left join KEA.KEA_STAGING.STG_CATALOGUE__TOU_WINDOWS w
      on w.window_id = rc.window_id
)
select
    l.invoice_line_id  as line_id,
    l.invoice_id       as bill_id,
    c.component_id,
    l.charge_group_srv as charge_group,
    l.flow_direction   as direction,
    l.line_description as line_label,
    l.tou_band         as band_name,
    l.quantity,
    l.unit_srv         as unit,
    l.unit_rate        as rate,
    l.loss_quantity    as loss_kwh,
    l.line_amount      as amount,
    l.sort_sequence    as sort_order
from lines l
left join components c
       on c.plan_id             = l.plan_id
      and c.charge_group        = l.charge_group_srv
      and c.flow_direction      = l.flow_direction
      and c.rate_unit_of_measure = l.unit_of_measure
      and c.unit_rate           = l.unit_rate
      and coalesce(c.band_key, '~none~')
        = coalesce(replace(upper(l.tou_band), '_', ''), '~none~')
order by line_id
