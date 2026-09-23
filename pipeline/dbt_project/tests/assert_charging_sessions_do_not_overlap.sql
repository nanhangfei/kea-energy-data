-- No two charging sessions of one ICP overlap. A run is a set of consecutive
-- half-hours and each half-hour belongs to at most one, so an overlap means
-- the gaps-and-islands step in int_metering__import_half_hours broke. V005
-- cannot check this: a CHECK sees one row.
--
-- Returns offending rows; dbt fails the test if any come back.

select
    icp_id,
    session_start,
    session_end,
    next_start
from (
    select
        icp_id,
        session_start,
        session_end,
        lead(session_start) over (partition by icp_id order by session_start) as next_start
    from {{ ref('mart_charging_session') }}
)
where next_start < session_end
