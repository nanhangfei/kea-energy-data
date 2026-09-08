-- Design §4.1: "silver dedups on (icp, register, interval_start) keeping the
-- highest version_no". This is that, and it is the first model in the whole
-- pipeline permitted to throw a source row away.
--
-- WHY IT COULD NOT HAPPEN EARLIER. A revision arrives as a NEW row at a higher
-- version_no, not as an update -- so bronze holds every version and staging
-- carries them all through unresolved. Stage 3's replay proof compares row
-- multisets over whole partitions; deduping upstream of that would have made
-- the proof compare something other than what the source sent.
--
-- SUPERSEDED ROWS ARE NOT DELETED, THEY ARE NOT SELECTED. The lake still holds
-- them and always will. "The correction that arrived three weeks late" is
-- answerable because both versions survive somewhere -- this model just says
-- which one is current.
--
-- ⚠️ TIES ARE NOT IMPOSSIBLE, AND THIS COMMENT USED TO SAY THEY WERE.
--
-- The earlier version asserted that (icp, register, interval_start, version_no)
-- is unique at source, so row_number() over that partition has exactly one row
-- at rank 1. There are 4,775 groups where it is not: Stage 2 injects 9,263
-- DUPLICATE DELIVERIES, and a duplicate is the same reading at the SAME
-- version_no, not a revision.
--
-- The consequence was not a wrong number -- the tied rows are identical in kwh
-- and quality, measured: 0 of the 4,775 groups differ in either -- but a
-- NON-DETERMINISTIC one. row_number() broke the tie arbitrarily, so a different
-- read_id survived on every build, and "a second build is a no-op" was false.
-- The uniqueness test never caught it, because exactly one row survives either
-- way; only hashing the content across two builds did.
--
-- read_id is the tie-break: it is unique at source, so the ordering is total and
-- the choice is stable. Which of two identical rows wins does not matter; that
-- the same one wins every time does.

with versions as (
    -- Distinct VERSIONS per reading, which is not the same as the number of
    -- ROWS. A reading delivered twice at v1 has two rows and one version, and
    -- reporting 2 there would tell a bill explanation the reading had been
    -- corrected when nothing of the sort happened.
    select
        icp_identifier,
        register_code,
        interval_start,
        count(distinct version_no) as version_count
    from {{ ref('stg_metering__interval_reads') }}
    group by 1, 2, 3
),

ranked as (
    select
        r.*,
        row_number() over (
            partition by r.icp_identifier, r.register_code, r.interval_start
            order by r.version_no desc, r.read_id desc
        ) as _version_rank
    from {{ ref('stg_metering__interval_reads') }} r
)

select
    r.read_id,
    r.icp_identifier,
    r.register_code,
    r.interval_start,
    r.kwh,
    r.quality,
    r.version_no,
    r.received_at,
    r.source_file,
    -- 1 means nothing ever superseded this reading. Counts VERSIONS, not
    -- deliveries -- see the CTE above.
    v.version_count,
    r._extracted_at,
    r._source_system,
    r._source_file,
    r._load_date
from ranked r
join versions v
  on v.icp_identifier = r.icp_identifier
 and v.register_code  = r.register_code
 and v.interval_start = r.interval_start
where r._version_rank = 1
