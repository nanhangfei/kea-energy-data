-- billing.account  <-  stg_billing__accounts (+ the three key maps)
--
-- There is no dim_account and no key_map_account: an account is not a
-- conformed dimension in this warehouse, it is the grain the bills hang off.
-- Its source id is stable and already the id the marts use, so it crosses
-- unchanged rather than being given a fourth surrogate to reconcile.
--
-- PLAN_ID IS THE ACTIVE VERSION, NOT THE TEMPORALLY RESOLVED ONE. An account
-- is on a plan now; a BILL is rated against whichever version covered its
-- period, and that resolution lives in billing.bill. Using the historical
-- resolution here would say an account is on a version that expired years ago.
--
-- The account is dropped if its ICP or customer has no key -- design §6.3's
-- orphan reference. Those are quarantined upstream and must not be resurrected
-- here by an outer join that fills the FK with a null.
select
    a.account_id,
    a.account_number,
    c.customer_id,
    i.icp_id,
    p.plan_id,
    case a.payment_method
        when 'DD'   then 'direct_debit'
        when 'BT'   then 'bank_transfer'
        when 'CARD' then 'card'
        else 'other'
    end                     as payment_method,
    a.billing_cycle_day,
    a.started_on            as start_date,
    a.ended_on              as end_date,
    a.term_months,
    -- TERM_START_DT began as a naive NZ billing timestamp. Parse made it a
    -- UTC instant, so cast only after returning it to NZ or midnight local
    -- turns into the previous calendar date in Snowflake's session timezone.
    convert_timezone('Pacific/Auckland', a.term_start_dt)::date as term_start
from KEA.KEA_STAGING.STG_BILLING__ACCOUNTS a
join KEA.KEA_SILVER.KEY_MAP_ICP        i  on i.icp_number      = a.icp_number
-- TWO HOPS TO THE CUSTOMER, NOT ONE. accounts.customer_id is the SOURCE's
-- numeric id (211001); the key map is keyed on the customer NUMBER ('196501'),
-- and only stg_billing__customers holds both. Joining the map straight onto
-- accounts compares a text key to a number and fails on the first row --
-- "Numeric value 'C0008725' is not recognized" -- which is a better outcome
-- than the silent empty join it would have been had the ids looked alike.
join KEA.KEA_STAGING.STG_BILLING__CUSTOMERS sc on sc.customer_id  = a.customer_id
join KEA.KEA_SILVER.KEY_MAP_CUSTOMER  c  on c.customer_number = sc.customer_number
join KEA.KEA_GOLD.DIM_PLAN            p  on p.plan_code       = a.plan_code
                                        and p.is_active
order by a.account_id
