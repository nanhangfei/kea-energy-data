-- metering.meter_read_hh  <-  fct_meter_read
--
-- THE RETENTION WINDOW IS A PER-HOUSEHOLD RULE, NOT A GLOBAL ONE.
--
--   cohort (icp_number like '09%')  the last 13 months
--   the nine fixture households     their entire span
--
-- §7.2 budgets "13 months of half-hourly for the demo cohort" -- thirteen so a
-- year-over-year comparison works at every point in the year. Applied globally
-- it would also delete every fixture read, because those are 2023-06 -> 2024-08
-- and fall outside any 13-month window ending today. The nine households are
-- the acceptance target for this whole stage; a window that excludes them
-- makes the stage unprovable. Anyone simplifying this back to one global
-- window silently removes them again.
--
-- THE ANCHOR IS THE DATA, NOT current_date. "Runs twice with identical row
-- counts" is one of Stage 5's three exit conditions, and a wall-clock anchor
-- breaks it the first time the two runs straddle a month boundary.
--
-- quality: 'final' -> 'validated'. KEA-047 carries A/E/F and common.read_quality
-- has no 'final'; design §12 left the choice open and it is settled here.
-- Both words mean "confirmed, not an estimate"; only the source's spelling is
-- lost, and the dictionary records that.
with anchor as (
    select max(interval_start) as latest
    from KEA.KEA_GOLD.FCT_METER_READ
    where icp_number like '09%'
)
select
    r.icp_id,
    r.register_id,
    r.interval_start,
    r.kwh,
    case r.quality when 'final' then 'validated' else r.quality end as quality,
    r.source_file
from KEA.KEA_GOLD.FCT_METER_READ r
cross join anchor a
where r.icp_number not like '09%'
   or r.interval_start >= dateadd(month, -13, a.latest)
