-- Payments are append-only today, but the RAW table is incremental and a
-- future source correction can redeliver a PAY_ID. Keep the latest extracted
-- representation rather than sending two attempts with one source identity to
-- serving. This is the same "latest version wins" boundary as meter reads,
-- expressed with the only version evidence this source currently carries.
--
-- ⚠️ PAID_TS IS NOT A TRUE UPDATE WATERMARK. A correction whose paid timestamp
-- remains unchanged will not enter a later extract window at all. The source
-- needs a changed_at/CDC field before that case can be recovered; this model
-- makes rows that DO reappear deterministic rather than pretending to solve
-- the source-side gap.

with ranked as (
    select
        p.*,
        row_number() over (
            partition by p.payment_id
            order by p._load_date desc, p._extracted_at desc, p._source_file desc
        ) as _version_rank
    from {{ ref('stg_billing__payments') }} p
)

select
    payment_id,
    account_id,
    invoice_id,
    paid_at,
    amount,
    payment_method,
    failed_flag,
    failure_reason,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from ranked
where _version_rank = 1
