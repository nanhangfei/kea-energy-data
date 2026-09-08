-- Stage 4's fourth checkbox: "a tariff_component.band_id references a tou_band
-- of the matching charge_group -- no SQL constraint can express it".
--
-- WHY POSTGRES CANNOT DO THIS. A foreign key checks that window_id EXISTS in
-- tou_windows. It cannot check that the referenced window belongs to the SAME
-- charge_group as the component pointing at it. Expressing that needs either a
-- composite key carrying charge_group redundantly into both tables, or a
-- trigger -- and kea_serving_schema.sql chooses neither, so the rule has to
-- live where a rule can be tested rather than declared.
--
-- WHAT GOING WRONG LOOKS LIKE, which is why it is worth a test at all: a
-- network component priced against an energy band. The FK holds, the plan
-- loads, every invoice still balances, and the household is billed peak
-- network rates during energy peak hours -- a window that does not correspond
-- to anything the network actually charges for. Nothing downstream can detect
-- it, because the arithmetic is internally consistent.
--
-- Components with no window_id are unbanded (a flat or daily charge) and are
-- correctly out of scope.
--
-- IT IS NOT PASSING VACUOUSLY, WHICH IS THE FIRST THING TO CHECK OF A TEST THAT
-- HAS NEVER FAILED. Measured 3 Sep 2026: 779 rate components, of which 400
-- carry a window_id and are therefore in scope; all 400 join and all 400 match.
-- And there are 4,985 (component, window) pairs within the same plan whose
-- charge groups DIFFER -- 4,985 distinct ways for a single mis-typed window_id
-- to produce a plan that still loads, still balances and is quietly wrong.

select
    rc.component_id,
    rc.plan_id,
    rc.charge_group        as component_charge_group,
    tw.charge_group        as band_charge_group,
    tw.band_label
from {{ ref('stg_catalogue__rate_components') }} rc
join {{ ref('stg_catalogue__tou_windows') }} tw
  on tw.window_id = rc.window_id
where rc.window_id is not null
  and rc.charge_group <> tw.charge_group
