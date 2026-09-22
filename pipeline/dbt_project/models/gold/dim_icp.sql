-- Design §6.4. One row per connection point.
--
-- climate_region is where the ICP is. weather_region is which NIWA series its
-- weather comes from: the same region where a station exists, otherwise the
-- nearest of the four, from the climate_region_weather_region seed. Every
-- weather join uses weather_region; climate_region stays for display. A
-- station is NOT a region -- that assumption is recorded in
-- dictionary/niwa-weather-source.md rather than implied by either column.
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
    coalesce(m.weather_region, c.climate_region) as weather_region,
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
left join {{ ref('climate_region_weather_region') }} m
  on m.climate_region = c.climate_region
