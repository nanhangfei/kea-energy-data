-- 1:1 with src_billing.invoices. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- Incremental on issue_dt. RAW accumulates every load date because the
-- extract windows are disjoint by construction (spec.window_predicate).
--
-- THESE TIMESTAMPS ARE ALREADY UTC INSTANTS. The billing system stores naive
-- `timestamp`, and design §5 has parse read it as Pacific/Auckland wall clock
-- and convert -- which is why the contract types them timestamp-micros rather
-- than something naive. Nothing NZ-specific belongs in this model; it was
-- already done, upstream, once.

select
    inv_id             as invoice_id,
    inv_no             as invoice_number,
    acct_id            as account_id,
    plan_code,
    issue_dt           as issued_at,
    period_from,
    period_to,
    open_bal           as opening_balance,
    payments_appl      as payments_applied,
    bal_brought_fwd    as balance_brought_forward,
    charges_exgst      as charges_excl_gst,
    gst_rate,
    gst_amt            as gst_amount,
    total_amt          as total_amount,
    due_dt             as due_at,
    late_fee,
    total_if_late,
    ppd_amt            as prompt_payment_discount,
    total_import_kwh,
    total_export_kwh,
    export_credit,
    inv_status         as invoice_status,
    last_mod_ts        as last_modified_at,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('billing', 'invoices') }}
