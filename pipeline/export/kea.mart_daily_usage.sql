-- kea.mart_daily_usage  <-  mart_daily_usage
--
-- §7.1: a plain table loaded from the warehouse, NOT a materialized view
-- computed in Postgres. Postgres holds only a 13-month subset for the demo
-- cohort, so anything computed locally could see only that subset; the
-- warehouse sees every read.
--
-- peak_kwh and off_peak_kwh exist in the serving table and not in the mart.
-- They stay null rather than being filled with a split this warehouse does not
-- compute -- a plausible-looking number here would be indistinguishable from a
-- real one downstream.
select
    icp_id, usage_date, import_kwh, export_kwh, controlled_kwh, uncontrolled_kwh,
    base_load_kwh, interval_count, expected_intervals, estimated_count,
    temp_mean_c, heating_degree_days, is_weekend, is_public_holiday
from KEA.KEA_MARTS.MART_DAILY_USAGE
