-- metering.meter_register  <-  dim_register
--
-- flow_direction is already lowercase 'import'/'export' in gold and matches
-- common.flow_direction as-is. It was NOT always: stg_billing__invoice_lines
-- had to lowercase its copy, and every bill totalled 0 kWh until it did.
select
    register_id,
    icp_id,
    register_code,
    description,
    is_controlled,
    flow_direction as direction
from KEA.KEA_GOLD.DIM_REGISTER
order by register_id
