-- Design §6.4. One row per connection point.
--
-- climate_region is carried here because it is the join key to the NIWA series
-- (dictionary/niwa-weather-source.md), and a station is NOT a region -- that
-- assumption is recorded there rather than implied by this column's existence.
--
-- INNER JOIN TO key_map_icp IS DELIBERATE. The map is seeded from metering, so
-- every connection point has a surrogate by construction; an ICP that appeared
-- here without one would mean the map had been built from something else, and
-- failing to produce a row is better than inventing an id.

select
    k.icp_id,
    c.icp_identifier         as icp_number,
    n.network_code,
    n.network_name,
    n.region                 as network_region,
    c.climate_region,
    c.address_full,
    c.suburb,
    c.city,
    c.postcode,
    c.latitude,
    c.longitude,
    c.connection_date,
    c.disconnection_date,
    c.updated_at
from {{ ref('stg_metering__connection_points') }} c
join {{ ref('key_map_icp') }} k
  on k.icp_number = c.icp_identifier
left join {{ ref('stg_metering__networks') }} n
  on n.network_id = c.network_id
