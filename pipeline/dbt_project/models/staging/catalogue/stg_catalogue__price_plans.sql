-- 1:1 with src_catalogue.price_plans. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- Snapshot. (code, valid_from) is the real key, not code alone: one plan
-- code has many versions and a bill must be rated against the version in force
-- at its period end. Design §2.1.2 calls resolving by code alone the most
-- dangerous step in the pipeline, because it fails silently.

select
    plan_id,
    code               as plan_code,
    name               as plan_name,
    network_code,
    user_category,
    is_tou,
    ppd_percent        as prompt_payment_discount_pct,
    valid_from,
    valid_to,
    active             as is_active,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('catalogue', 'price_plans') }}
