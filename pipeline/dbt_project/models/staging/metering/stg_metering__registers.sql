-- 1:1 with src_metering.registers. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- Snapshot. UNIQUE(meter_id, register_code) at source, while interval_reads
-- arrives keyed on (icp, register_code) -- the join is silver's problem.

select
    register_id,
    meter_id,
    register_code,
    description,
    is_controlled,
    flow_direction,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('metering', 'registers') }}
