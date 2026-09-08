-- 1:1 with src_metering.meters. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- Snapshot.

select
    meter_id,
    icp_id,
    serial_number,
    is_smart,
    installed_on,
    removed_on,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('metering', 'meters') }}
