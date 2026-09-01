-- =====================================================================
-- Revisions.
--
-- src_metering.interval_reads is DELIBERATELY not unique on
-- (icp, register, interval): a correction arrives as a NEW ROW with a
-- higher version_no, and picking the latest is the warehouse's job. The
-- v1 row is never updated and never deleted -- that is the whole point,
-- and it is why the extract needs a lookback window at all.
--
-- The single most important row in this file is the PLANTED LATE
-- REVISION at the bottom. Stage 3's exit criterion is literally "a
-- three-week-old correction is picked up by an ordinary nightly run".
-- Without a revision whose reading is old but whose received_at is
-- recent, that criterion has nothing to test and passes vacuously.
--
-- setseed() first, as in 05_defects.sql.
-- =====================================================================

BEGIN;

-- No setseed(): every selection here hashes the row's business key.
-- See gen_audit.h() in 01_schemas.sql for why random() was not enough.

-- Corrections cover a whole household-day. A metering equipment provider
-- re-issues a day, not a scattering of half-hours.
CREATE TEMP TABLE _rev_day ON COMMIT DROP AS
SELECT icp_identifier, nz_date,
       -- The correction itself. Re-reads land near the original, not
       -- wildly away from it; a large delta would be caught by any
       -- range check and would not test "highest version wins".
       (0.90 + gen_audit.h(icp_identifier || nz_date::text || 'factor') * 0.25)::numeric AS factor
FROM (
    SELECT DISTINCT icp_identifier,
           (interval_start AT TIME ZONE 'Pacific/Auckland')::date AS nz_date
    FROM src_metering.interval_reads
    WHERE source_file LIKE 'lcl-%' AND register_code = 'UN') d
WHERE gen_audit.h(icp_identifier || nz_date::text || 'rev') < 0.021;

-- version_no = 2. Note read_status: an estimate that has been re-read is
-- now actual, which is the commonest reason a revision exists at all.
INSERT INTO src_metering.interval_reads
    (icp_identifier, register_code, interval_start, kwh, read_status,
     version_no, received_at, source_file)
SELECT r.icp_identifier, r.register_code, r.interval_start,
       ROUND(r.kwh * d.factor, 4),
       CASE WHEN r.read_status = 'E' THEN 'A' ELSE r.read_status END,
       2,
       r.received_at + ((3 + gen_audit.h(r.icp_identifier || r.interval_start::text || 'lag') * 42) * interval '1 day'),
       'lcl-rev-' || to_char(d.nz_date, 'YYYYMMDD') || '.csv'
FROM src_metering.interval_reads r
JOIN _rev_day d ON d.icp_identifier = r.icp_identifier
               AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date = d.nz_date
WHERE r.source_file LIKE 'lcl-%' AND r.version_no = 1;

-- A handful reach version 3, so that "the latest version" cannot be
-- satisfied by accident with "version_no = 2" or "max = 2".
CREATE TEMP TABLE _rev3 ON COMMIT DROP AS
SELECT icp_identifier, register_code, interval_start, kwh, received_at
FROM src_metering.interval_reads
WHERE version_no = 2 AND source_file LIKE 'lcl-rev-%'
ORDER BY icp_identifier, interval_start
LIMIT 20;

INSERT INTO src_metering.interval_reads
    (icp_identifier, register_code, interval_start, kwh, read_status,
     version_no, received_at, source_file)
SELECT icp_identifier, register_code, interval_start,
       ROUND(kwh * 1.04, 4), 'A', 3,
       received_at + interval '9 days', 'lcl-rev3.csv'
FROM _rev3;

-- ---------------------------------------------------------------------
-- THE PLANTED LATE REVISION.
--
-- A reading from five weeks back, corrected and delivered three days ago.
-- An extract that filters on the READING time will never see it. Only one
-- that watermarks on received_at will -- which is precisely why §4.1
-- watermarks on received_at and not on interval_start, and precisely what
-- Stage 3 has to demonstrate.
--
-- Pinned to explicit dates rather than now(), so it does not drift.
-- ---------------------------------------------------------------------
INSERT INTO src_metering.interval_reads
    (icp_identifier, register_code, interval_start, kwh, read_status,
     version_no, received_at, source_file)
SELECT r.icp_identifier, r.register_code, r.interval_start,
       ROUND(r.kwh * 1.11, 4), 'A', 2,
       TIMESTAMPTZ '2026-08-25 09:15:00+12', 'lcl-rev-late.csv'
FROM src_metering.interval_reads r
WHERE r.source_file LIKE 'lcl-%'
  AND r.version_no = 1
  AND r.register_code = 'UN'
  AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date = DATE '2026-07-20'
  AND r.icp_identifier = (
      SELECT min(icp_identifier) FROM src_metering.interval_reads
      WHERE source_file LIKE 'lcl-%');

INSERT INTO gen_audit.defects (defect_kind, icp_identifier, nz_date, detail)
SELECT 'src:late_revision', icp_identifier, DATE '2026-07-20',
       jsonb_build_object('received_at', '2026-08-25',
                          'age_days', DATE '2026-08-25' - DATE '2026-07-20')
FROM src_metering.interval_reads
WHERE source_file = 'lcl-rev-late.csv' GROUP BY icp_identifier;

DO $$
DECLARE n2 bigint; n3 bigint; late bigint; v1 bigint;
BEGIN
    SELECT count(*) INTO n2 FROM src_metering.interval_reads WHERE version_no = 2;
    SELECT count(*) INTO n3 FROM src_metering.interval_reads WHERE version_no = 3;
    SELECT count(*) INTO late FROM src_metering.interval_reads
     WHERE source_file = 'lcl-rev-late.csv';

    -- The v1 rows must still be there, unmodified. If a "revision" ever
    -- becomes an UPDATE, the extract's whole lookback design is pointless
    -- and nothing else would notice.
    SELECT count(*) INTO v1 FROM src_metering.interval_reads r
     WHERE r.version_no = 1 AND r.source_file LIKE 'lcl-%'
       AND EXISTS (SELECT 1 FROM src_metering.interval_reads x
                   WHERE x.icp_identifier = r.icp_identifier
                     AND x.register_code = r.register_code
                     AND x.interval_start = r.interval_start
                     AND x.version_no > 1);
    IF v1 = 0 THEN
        RAISE EXCEPTION 'no superseded v1 rows survive: revisions were applied as updates';
    END IF;
    IF late = 0 THEN
        RAISE EXCEPTION 'the planted late revision was not created; Stage 3 has nothing to find';
    END IF;
    RAISE NOTICE 'revisions: % at v2, % at v3, % late, % superseded v1 rows intact',
                 n2, n3, late, v1;
END $$;

COMMIT;
