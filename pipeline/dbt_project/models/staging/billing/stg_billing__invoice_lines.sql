-- 1:1 with src_billing.invoice_lines. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- Snapshot, and it deviates from design §4.1's table on purpose: the
-- source has no timestamp column at all, so there is nothing to watermark on.
-- Recorded in spec.py as one of the two deliberate deviations.

select
    line_id            as invoice_line_id,
    inv_id             as invoice_id,
    chg_group          as charge_group,
    -- LOWERCASED TO MATCH METERING. src_billing spells it 'IMPORT'/'EXPORT'
    -- and src_metering.registers spells it 'import'/'export' -- two systems
    -- built by different teams, which is exactly the mechanical difference
    -- design §5 says staging exists to reconcile. Left alone it is invisible:
    -- a filter on one spelling silently matches nothing and the sum comes back
    -- 0 rather than erroring. It did -- every bill in mart_bill_explanation had
    -- total_kwh = 0, which made the variable rate null and every weather driver
    -- with it.
    lower(flow_dir)    as flow_direction,
    line_desc          as line_description,
    tou_band,
    qty                as quantity,
    uom                as unit_of_measure,
    unit_rate,
    loss_qty           as loss_quantity,
    line_amt           as line_amount,
    sort_seq           as sort_sequence,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('billing', 'invoice_lines') }}
