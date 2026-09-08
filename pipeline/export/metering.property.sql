-- metering.property  <-  stg_metering__connection_points
--
-- THE SOURCE HAS NO PROPERTY TABLE. src_metering carries the address inline on
-- the connection point, so a property here is 1:1 with an ICP and takes the
-- ICP's surrogate as its own id. That is a faithful statement of what the
-- source knows, not a modelling shortcut: nothing upstream can distinguish two
-- ICPs at one address from two addresses, so inventing a shared property row
-- would be asserting something no data supports.
--
-- address_full is split back into two lines on the first comma. The source
-- stores one string; serving declares address_line_1 NOT NULL and line 2
-- nullable, so the split has to put the whole address somewhere even when
-- there is no comma -- hence the coalesce on the first part rather than the
-- second.
select
    m.icp_id                                   as property_id,
    coalesce(nullif(trim(split_part(c.address_full, ',', 1)), ''),
             c.address_full)                   as address_line_1,
    nullif(trim(split_part(c.address_full, ',', 2)), '') as address_line_2,
    c.suburb,
    c.city,
    c.postcode,
    c.climate_region,
    c.latitude,
    c.longitude
from KEA.KEA_STAGING.STG_METERING__CONNECTION_POINTS c
join KEA.KEA_SILVER.KEY_MAP_ICP m
  on m.icp_number = c.icp_identifier
order by property_id
