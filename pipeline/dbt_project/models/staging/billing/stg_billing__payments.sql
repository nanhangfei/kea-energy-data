-- 1:1 with src_billing.payments. Staging keeps the source method and Y/N
-- failure flag intact; the serving enum/boolean translations are deliberate
-- export-boundary decisions, not a hidden parse-side interpretation.
--
-- Incremental on PAID_TS. Like invoices, the billing source stored this as a
-- naive NZ timestamp and parse has already made it a UTC instant.

select
    pay_id             as payment_id,
    acct_id            as account_id,
    inv_id             as invoice_id,
    paid_ts            as paid_at,
    pay_amt            as amount,
    pay_method         as payment_method,
    failed_flag,
    fail_reason        as failure_reason,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('billing', 'payments') }}
