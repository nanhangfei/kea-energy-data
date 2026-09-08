-- Determinism cannot be tested in a single run -- you need two builds and a
-- content hash. What CAN be tested in one run is the property determinism
-- depends on: that the ordering key used to break ties is TOTAL.
--
-- int_metering__reads_deduped ranks on (version_no desc, read_id desc). If
-- (icp, register, interval_start, version_no, read_id) is unique at source,
-- that ordering has exactly one row at rank 1 and the choice is stable across
-- builds. If it ever stops being unique, row_number() starts breaking ties
-- arbitrarily again and the warehouse quietly stops being reproducible.
--
-- THAT IS NOT HYPOTHETICAL. The model previously ranked on version_no alone and
-- asserted in a comment that ties were impossible. There are 4,775 tie groups --
-- Stage 2 injects 9,263 duplicate deliveries, and a duplicate is the same
-- reading at the SAME version_no, not a revision. A different read_id survived
-- every build, "a second build is a no-op" was false, and the uniqueness test on
-- the triple passed throughout, because exactly one row survives either way.
--
-- This test is what that bug should have run into.

select
    icp_identifier,
    register_code,
    interval_start,
    version_no,
    read_id,
    count(*) as rows_sharing_the_full_ordering_key
from {{ ref('stg_metering__interval_reads') }}
group by 1, 2, 3, 4, 5
having count(*) > 1
