-- How many physical registers a read's (icp, register_code) could mean.
--
-- Reads carry no meter_id (KEA-047), so resolving one to a register means
-- going icp -> meters -> registers and matching the code. Where an ICP has had
-- more than one meter, that can return several.
--
-- LIVE AT THE READING'S INSTANT IS THE FIRST FILTER, and it is the one that
-- resolves the ordinary case: a meter replaced in June is not a candidate for
-- a reading in March. installed_on/removed_on is exactly the temporal bracket
-- int_billing__plan_resolution applies to tariffs, and for the same reason --
-- resolving on identity alone re-attributes history.
--
-- This model reports the candidate COUNT rather than picking. Picking is
-- fct_meter_read's job where the count is 1, and nobody's where it is more:
-- int_integrity__rejects quarantines those, because a join that picks one
-- multiplies that ICP's readings by the number of candidates and the total
-- still balances.

with reads as (
    select distinct icp_identifier, register_code
    from {{ ref('int_metering__reads_deduped') }}
),

resolved as (
    select
        rd.icp_identifier as icp_number,
        rd.register_code,
        rg.register_id    as source_register_id,
        m.meter_id        as source_meter_id,
        m.installed_on,
        m.removed_on
    from reads rd
    join {{ ref('stg_metering__connection_points') }} c
      on c.icp_identifier = rd.icp_identifier
    join {{ ref('stg_metering__meters') }} m
      on m.icp_id = c.icp_id
    join {{ ref('stg_metering__registers') }} rg
      on rg.meter_id = m.meter_id
     and rg.register_code = rd.register_code
)

select
    icp_number,
    register_code,
    count(*)                                   as candidates,
    min(source_register_id)                    as source_register_id,
    -- Carried so a reader can see WHY it is ambiguous without re-deriving it:
    -- five meters installed the same day is a different problem from one
    -- replaced and one new.
    count(distinct source_meter_id)            as meters,
    min(installed_on)                          as first_installed_on,
    max(coalesce(removed_on, date '9999-12-31')) as last_removed_on
from resolved
group by 1, 2
