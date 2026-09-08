-- The dimension fct_meter_read's register_id points at. Missing from design
-- §6.4's diagram, which shows four dimensions -- but the fact is grained on
-- register and the serving PK includes register_id, so a register dimension
-- exists whether or not it was drawn.
--
-- ONLY UNAMBIGUOUS REGISTERS ARE HERE, which is what makes the join to
-- src_metering.registers safe. Where a (icp, register_code) has several
-- candidates there is no single flow_direction or is_controlled to report, and
-- inventing one would put a made-up import/export split into every mart
-- downstream. Those subjects are quarantined; fct_meter_read excludes their
-- readings, so this dimension covers the fact exactly.

select
    kr.register_id,
    ki.icp_id,
    rc.icp_number,
    rc.register_code,
    r.description,
    r.is_controlled,
    r.flow_direction,
    rc.source_register_id,
    rc.first_installed_on
from {{ ref('int_metering__register_candidates') }} rc
join {{ ref('key_map_register') }} kr
  on kr.icp_number = rc.icp_number
 and kr.register_code = rc.register_code
join {{ ref('key_map_icp') }} ki
  on ki.icp_number = rc.icp_number
join {{ ref('stg_metering__registers') }} r
  on r.register_id = rc.source_register_id
where rc.candidates = 1
