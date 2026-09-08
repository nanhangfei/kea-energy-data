-- billing.bill  <-  stg_billing__invoices + int_billing__plan_resolution
--
-- PLAN_ID COMES FROM TEMPORAL RESOLUTION, NEVER FROM THE INVOICE'S PLAN CODE.
-- Design §2.1.2: 73 versions across 41 codes, and a code-only lookup re-rates
-- history against current tariffs while never failing. Measured on this
-- catalogue, it mis-rates 2,878 of 11,534 invoices.
--
-- The join is an inner join, so an invoice no version covers does not land.
-- Those are quarantined as integrity.no_plan_version; a bill with a null plan
-- cannot be explained or re-rated, so a row here would be worse than absent.
--
-- PERIOD_DAYS IS NOT EXPORTED. Serving computes it as a GENERATED column, and
-- supplying one is an error rather than a redundancy -- "cannot insert a
-- non-DEFAULT value into column period_days". The definition there already
-- counts inclusively, which is what makes 08-31 August 24 days and matches the
-- 24 daily fixed charges the printed invoice bills.
--
-- THE DATE CASTS CONVERT TO NZ FIRST, AND THAT IS NOT OPTIONAL. Snowflake
-- resolves a TIMESTAMP_TZ against the SESSION timezone, and every one of these
-- periods ends at midnight NZ -- which is the previous DAY in the account
-- default of America/Los_Angeles. Measured once already: all 41 loaded
-- invoices resolved a day early, which on a period end that sits on a
-- plan-version boundary silently picks the wrong tariff.
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
)
select
    i.invoice_id                as bill_id,
    i.account_id,
    i.invoice_number,
    r.plan_id,
    convert_timezone('Pacific/Auckland', i.issued_at)::date    as issue_date,
    convert_timezone('Pacific/Auckland', i.period_from)::date  as period_start,
    convert_timezone('Pacific/Auckland', i.period_to)::date    as period_end,
    i.opening_balance,
    i.payments_applied,
    i.balance_brought_forward   as balance_brought_fwd,
    i.charges_excl_gst,
    i.gst_rate,
    i.gst_amount,
    i.total_amount              as total_charges,
    convert_timezone('Pacific/Auckland', i.due_at)::date       as due_date,
    i.late_fee                  as late_payment_fee,
    i.total_if_late             as total_if_paid_late,
    i.prompt_payment_discount,
    i.total_import_kwh,
    i.total_export_kwh,
    i.export_credit,
    lower(i.invoice_status)     as status
from KEA.KEA_STAGING.STG_BILLING__INVOICES i
join KEA.KEA_SILVER.INT_BILLING__PLAN_RESOLUTION r
  on r.invoice_id = i.invoice_id
 and r.plan_id is not null
join eligible_accounts ea on ea.account_id = i.account_id
order by bill_id
