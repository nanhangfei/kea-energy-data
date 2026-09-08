-- 1:1 with src_metering.networks. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- Snapshot: the loader truncates and reloads, so there is only ever one
-- day's rows here and this model needs no latest-load_date filter.

select
    network_id,
    network_code,
    network_name,
    region,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('metering', 'networks') }}
