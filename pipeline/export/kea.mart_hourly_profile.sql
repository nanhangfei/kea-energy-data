-- kea.mart_hourly_profile  <-  mart_hourly_profile
select
    icp_id, profile_month, hour_of_day, is_weekday,
    avg_import_kwh, avg_export_kwh, sample_days
from KEA.KEA_MARTS.MART_HOURLY_PROFILE
