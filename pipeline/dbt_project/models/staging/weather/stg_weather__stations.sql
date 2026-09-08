-- 1:1 with src_weather.stations. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- Snapshot, four rows. agent_no is NIWA's own station number and the
-- business key -- stable across renames, and what the CSV filenames are named
-- for. station_id is a local identity column and means nothing to NIWA.

select
    station_id,
    agent_no,
    station_name,
    climate_region,
    latitude,
    longitude,
    elevation_m,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('weather', 'stations') }}
