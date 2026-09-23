-- Grain: icp × half-hour inside a PUBLISHED run ('ev' or 'timed_night'), with
-- the half-hour's own bounds (design §15). Both detection marts sum these, so
-- the bounds are defined once:
--
--   low   max(import − the run's adjacent level, 0)
--   high  import − the floor of this half-hour's own date
--
-- low is capped at high: across midnight the level before a run belongs to the
-- previous date, whose floor can be lower. Where the run has no adjacent
-- level, the candidate threshold stands in (int_metering__import_runs).
--
-- Each half-hour keeps its own NZ date, so a run across midnight is split
-- between two days in mart_daily_detected_load.

select
    h.icp_id,
    h.interval_start,
    h.usage_date,
    h.is_assessed_day,
    h.is_tail,
    h.kwh,
    h.floor_kwh_per_hh,
    r.run_start,
    r.detected_class,
    r.charger_class,
    r.confidence,
    least(
        greatest(h.kwh - coalesce(r.adjacent_kwh,
                                  h.floor_kwh_per_hh + {{ var('detect_min_excess_kwh') }}), 0),
        h.kwh - h.floor_kwh_per_hh
    )                                                                as kwh_low,
    h.kwh - h.floor_kwh_per_hh                                       as kwh_high,
    r.method_version
from {{ ref('int_metering__import_half_hours') }} h
join {{ ref('int_metering__import_runs') }} r
  on r.icp_id = h.icp_id
 and r.run_start = h.run_start
where r.detected_class in ('ev', 'timed_night')
