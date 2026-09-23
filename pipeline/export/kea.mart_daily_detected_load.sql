-- kea.mart_daily_detected_load  <-  mart_daily_detected_load
--
-- Column-for-column (Sprint 4, V005; masked_intervals added by V006). Every
-- ICP and every assessed day, like
-- kea.mart_daily_usage: no 13-month window, because a billing period's
-- detected-load driver needs its prior period's days too. built_at is left to
-- the serving default.
select
    icp_id, usage_date, floor_kwh_per_hh,
    ev_kwh_low, ev_kwh_high, ev_half_hours, ev_charger_class, ev_confidence,
    timed_night_kwh_low, timed_night_kwh_high, timed_night_half_hours,
    timed_night_confidence, masked_intervals, method_version
from KEA.KEA_MARTS.MART_DAILY_DETECTED_LOAD
