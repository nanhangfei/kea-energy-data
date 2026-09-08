-- metering.network  <-  stg_metering__networks
--
-- There is no dim_network. Design §6.1 folds the network's code, name and
-- region into dim_icp, because every mart that needs a network reaches it
-- through an ICP. Serving needs the table on its own account -- catalogue.plan
-- carries a foreign key to it, which is §7.4's "the two are not siblings" --
-- so it comes from staging, the layer that is still 1:1 with the source and
-- where a network is still a row rather than three columns on something else.
select
    network_id,
    network_code,
    network_name,
    region
from KEA.KEA_STAGING.STG_METERING__NETWORKS
order by network_id
