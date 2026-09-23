-- Grain: one run with an EV's charging pattern. Serving target
-- kea.mart_charging_session (V005), PK (icp_id, session_start).
--
-- For the shift-to-night scenario (build_ev_scenarios.py) and for staff. The
-- whole run is here, including half-hours on days that were not read in
-- full: a session is a fact about the run, and mart_daily_detected_load is the
-- one that only counts assessed days.
--
-- session_end is exclusive and exactly half_hours × 30 minutes after the
-- start, across a daylight-saving change too: a run is consecutive half-hours
-- on the continuous timeline. Runs never overlap, so neither do sessions
-- (assert_charging_sessions_do_not_overlap).
--
-- ⚠️ charger_class MAY BE NULL (detect-v2, V006). masked_half_hours counts
-- the half-hours of this session the panels hid; above zero, a shaved
-- wallbox and a portable charger read the same, so no class is asserted.
-- NULL means "we cannot tell the two apart", NOT "no EV found" -- the
-- session is published either way. A reader that assumed non-null needs a
-- branch (assert_masked_runs_have_no_charger_class holds the rule).

select
    r.icp_id,
    r.run_start                                                      as session_start,
    r.run_end                                                        as session_end,
    r.half_hours,
    round(sum(d.kwh_low), 4)                                         as kwh_low,
    round(sum(d.kwh_high), 4)                                        as kwh_high,
    r.charger_class,
    r.confidence,
    r.masked_half_hours,
    r.method_version
from {{ ref('int_metering__import_runs') }} r
join {{ ref('int_metering__detected_half_hours') }} d
  on d.icp_id = r.icp_id
 and d.run_start = r.run_start
where r.detected_class = 'ev'
group by
    r.icp_id, r.run_start, r.run_end, r.half_hours,
    r.charger_class, r.confidence, r.masked_half_hours, r.method_version
