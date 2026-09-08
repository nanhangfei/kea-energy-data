-- catalogue.tariff_component  <-  stg_catalogue__rate_components
--
-- Same reach past gold as tou_band, and the same reason.
--
-- band_id is nullable here and must stay nullable: a daily fixed charge has no
-- time band. The join to the window is therefore a LEFT join, and a component
-- whose window_id does not resolve keeps a null rather than being dropped --
-- dropping it would silently remove a charge from every bill that uses it.
select
    c.component_id,
    p.plan_id,
    c.charge_group,
    c.component_description                      as component_name,
    case c.rate_unit_of_measure when 'DAY' then 'per_day' else 'per_kwh' end as basis,
    w.window_id                                  as band_id,
    c.flow_direction                             as direction,
    c.unit_rate                                  as rate,
    c.is_controlled_only                         as controlled_only,
    c.valid_from                                 as effective_from,
    c.valid_to                                   as effective_to
from KEA.KEA_STAGING.STG_CATALOGUE__RATE_COMPONENTS c
join KEA.KEA_GOLD.DIM_PLAN p
  on p.source_plan_id = c.plan_id
left join KEA.KEA_STAGING.STG_CATALOGUE__TOU_WINDOWS w
  on w.window_id = c.window_id
order by c.component_id
