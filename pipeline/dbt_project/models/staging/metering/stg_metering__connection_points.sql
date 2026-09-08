-- 1:1 with src_metering.connection_points. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- Snapshot. icp_identifier is the business key the billing system also
-- stores; resolving the two to one surrogate is silver's job (design §6.2).

select
    icp_id,
    icp_identifier,
    network_id,
    address_full,
    suburb,
    city,
    postcode,
    climate_region,
    latitude,
    longitude,
    connection_date,
    disconnection_date,
    updated_at,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('metering', 'connection_points') }}
