-- 1:1 with src_catalogue.rate_components. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- Snapshot. band_id -> tou_band must match on charge_group, which no SQL
-- constraint can express -- Stage 4's fourth checkbox makes that a dbt test,
-- and it belongs in silver where both sides are resolved.

select
    component_id,
    plan_id,
    charge_group,
    component_desc     as component_description,
    rate_uom           as rate_unit_of_measure,
    window_id,
    flow_direction,
    unit_rate,
    controlled_only    as is_controlled_only,
    valid_from,
    valid_to,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('catalogue', 'rate_components') }}
