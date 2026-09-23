-- A day cannot read more half-hours than it has. interval_count once took
-- count(*) over every register, so a solar household read 96 on a
-- 48-half-hour day: its days never counted as complete, and a reader testing
-- `>=` passed a day with gaps. It now counts each import half-hour once
-- (mart_daily_usage's header), and this keeps it that way.
--
-- Returns offending rows; dbt fails the test if any come back.

select
    icp_id,
    usage_date,
    interval_count,
    estimated_count,
    expected_intervals
from {{ ref('mart_daily_usage') }}
where interval_count > expected_intervals
   or estimated_count > expected_intervals
