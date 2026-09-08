-- 1:1 with src_billing.accounts. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- Snapshot. `icp_number` is the metering system's icp_identifier under
-- another name -- design §8.3 maps the two, and silver resolves them. Kept as
-- the source spells it so the rename is visible where it happens, not here.

select
    acct_id            as account_id,
    acct_no            as account_number,
    cust_id            as customer_id,
    icp_number,
    plan_code,
    pay_method         as payment_method,
    bill_cycle_day     as billing_cycle_day,
    start_dt           as started_on,
    end_dt             as ended_on,
    term_months,
    term_start_dt,
    last_mod_ts        as last_modified_at,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('billing', 'accounts') }}
