-- kea.mart_charging_session  <-  mart_charging_session
--
-- Column-for-column (Sprint 4, V005; masked_half_hours and a nullable
-- charger_class added by V006). session_start and session_end are UTC
-- instants, like metering.meter_read_hh.interval_start. built_at is left to
-- the serving default.
select
    icp_id, session_start, session_end, half_hours,
    kwh_low, kwh_high, charger_class, confidence, masked_half_hours,
    method_version
from KEA.KEA_MARTS.MART_CHARGING_SESSION
