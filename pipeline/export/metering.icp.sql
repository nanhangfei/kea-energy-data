-- metering.icp  <-  dim_icp (+ the source's network id, via staging)
--
-- dim_icp carries network_code, not network_id -- serving's FK needs the id, so
-- the connection point is re-read from staging for that one column. Joining on
-- the business key (icp_number = icp_identifier) rather than on either side's
-- own icp_id, because those two ids are different things: dim_icp's is the
-- key_map surrogate and staging's is the source's own.
select
    i.icp_id,
    i.icp_number,
    i.icp_id            as property_id,   -- 1:1, see metering.property.sql
    c.network_id,
    i.connection_date,
    i.disconnection_date
from KEA.KEA_GOLD.DIM_ICP i
join KEA.KEA_STAGING.STG_METERING__CONNECTION_POINTS c
  on c.icp_identifier = i.icp_number
order by i.icp_id
