-- =====================================================================
-- The defect profile.
--
-- LCL is clean -- 98.7% of its household-days are complete -- so nothing
-- in it exercises a data-quality rule. Design §10.2 is explicit that the
-- rules get proven against injected faults, and this is that injection.
--
-- INJECTED INTO THE GENERATED COHORT ONLY. The nine fixtures assert their
-- own totals and are the only external ground truth here; corrupting them
-- would destroy the thing everything else is measured against.
--
-- The manifest goes to gen_audit.defects, NOT to a src_* schema.
-- kea_source_schema.sql ends with ALTER DEFAULT PRIVILEGES ... GRANT
-- SELECT, so a table created under src_metering becomes readable by the
-- extract roles automatically -- the pipeline could then "detect" every
-- defect by reading the list of them.
--
-- NOT IDEMPOTENT AGAINST ITSELF. This file DELETES reads (gaps) and
-- UPDATES kwh (spikes, stuck meters). Running it twice cuts twice as many
-- gaps and multiplies the spikes again. It must run against a FRESH load
-- of reads.csv.gz -- which is what load_source.sh does, in order.
--
-- DETERMINISTIC WITHOUT random(). Every selection below is a hash of the
-- row's own business key, via gen_audit.h(). setseed() was not enough:
-- it fixes the random stream, not which row draws from it, and Postgres
-- may parallelise a scan over 19 M rows so each worker draws from its
-- own state. Two runs on identical input gave 359,160 and 359,377
-- revised rows -- close enough to look right, never the same twice.
-- =====================================================================

BEGIN;

-- No setseed(): nothing here draws from the random stream any more.
-- Every selection below is a hash of the row's own business key.

INSERT INTO gen_audit.runs (cohort_seed, cohort_size, notes)
SELECT 'kea-stage2-v1', count(DISTINCT icp_identifier), 'defect injection'
FROM src_metering.interval_reads
WHERE source_file LIKE 'lcl-%'
RETURNING run_id \gset

DELETE FROM gen_audit.defects WHERE defect_kind LIKE 'src:%';

-- ---------------------------------------------------------------------
-- 1. Interval gaps, in RUNS rather than scattered singletons.
--
-- A comms outage drops consecutive intervals; it does not remove every
-- two-hundredth reading independently. Scattered gaps are trivially
-- detectable and unrealistic -- the run length is what makes
-- interval_count < expected_intervals meaningful.
-- ---------------------------------------------------------------------
-- ⚠️ EVERY TEMP TABLE HERE IS ANALYZEd. A fresh temp table has no
-- statistics, so the planner guessed and built its hash join on the 8 M-row
-- reads table instead of the few-thousand-row temp one: at 200 households the
-- gap DELETE alone spilled ~9 GB to disk and ran for over 13 minutes.
-- ANALYZE changes the plan, never the rows.
CREATE TEMP TABLE _gap_start ON COMMIT DROP AS
SELECT icp_identifier, interval_start,
       (2 + floor(gen_audit.h(icp_identifier || interval_start::text || 'len') * 94))::int AS run_len
FROM src_metering.interval_reads
WHERE source_file LIKE 'lcl-%' AND register_code = 'UN'
  AND gen_audit.h(icp_identifier || interval_start::text || 'gap') < 0.00008;
ANALYZE _gap_start;

CREATE TEMP TABLE _gap ON COMMIT DROP AS
SELECT g.icp_identifier,
       g.interval_start + (i * interval '30 min') AS interval_start
FROM _gap_start g, generate_series(0, 95) AS i
WHERE i < g.run_len;
ANALYZE _gap;

INSERT INTO gen_audit.defects (run_id, defect_kind, icp_identifier, register_code, interval_start)
SELECT :run_id, 'src:gap', icp_identifier, 'UN', interval_start FROM _gap;

DELETE FROM src_metering.interval_reads r
 USING _gap g
 WHERE r.icp_identifier = g.icp_identifier
   AND r.interval_start = g.interval_start
   AND r.source_file LIKE 'lcl-%';

-- ---------------------------------------------------------------------
-- 2. Estimated reads, as whole household-days and after every gap.
--
-- A meter that could not be read is estimated for the DAY, not for
-- scattered half-hours, and the reading after a comms outage is
-- estimated because the register was catching up. Both are fixture 06's
-- scenario generalised.
-- ---------------------------------------------------------------------
-- Selected at DAY level, so the whole day is chosen or none of it is.
-- The old per-row draw picked a day if any of its 48 intervals cleared
-- the threshold, which is the same thing only on average.
CREATE TEMP TABLE _est_day ON COMMIT DROP AS
SELECT icp_identifier, nz_date FROM (
    SELECT DISTINCT icp_identifier,
           (interval_start AT TIME ZONE 'Pacific/Auckland')::date AS nz_date
    FROM src_metering.interval_reads
    WHERE source_file LIKE 'lcl-%' AND register_code = 'UN') d
WHERE gen_audit.h(icp_identifier || nz_date::text || 'est') < 0.035;
ANALYZE _est_day;

UPDATE src_metering.interval_reads r
   SET read_status = 'E'
  FROM _est_day e
 WHERE r.icp_identifier = e.icp_identifier
   AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date = e.nz_date
   AND r.source_file LIKE 'lcl-%';

INSERT INTO gen_audit.defects (run_id, defect_kind, icp_identifier, nz_date)
SELECT :run_id, 'src:estimated_day', icp_identifier, nz_date FROM _est_day;

-- The first four readings after each gap.
UPDATE src_metering.interval_reads r
   SET read_status = 'E'
  FROM _gap_start g
 WHERE r.icp_identifier = g.icp_identifier
   AND r.interval_start >= g.interval_start + (g.run_len * interval '30 min')
   AND r.interval_start <  g.interval_start + ((g.run_len + 4) * interval '30 min')
   AND r.source_file LIKE 'lcl-%';

-- ---------------------------------------------------------------------
-- 3. Final reads at account close. read_status='F' is a CHECK value
--    nothing in the repo has ever produced.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _final ON COMMIT DROP AS
SELECT icp_identifier, max(interval_start) AS last_read
FROM src_metering.interval_reads
WHERE source_file LIKE 'lcl-%' AND register_code = 'UN'
  -- Sample the DISTINCT households. Applying random() < 0.002 to the
  -- read rows instead selects every household: with ~37,000 reads each,
  -- the chance of a household never clearing the threshold is nil. The
  -- first run of this file closed all 500 accounts.
  AND icp_identifier IN (
      SELECT icp_identifier FROM (
          SELECT DISTINCT icp_identifier FROM src_metering.interval_reads
          WHERE source_file LIKE 'lcl-%') h
      WHERE gen_audit.h(icp_identifier || 'final') < 0.05)
GROUP BY icp_identifier;
ANALYZE _final;

UPDATE src_metering.interval_reads r
   SET read_status = 'F'
  FROM _final f
 WHERE r.icp_identifier = f.icp_identifier
   AND r.interval_start > f.last_read - interval '1 day'
   AND r.source_file LIKE 'lcl-%';

INSERT INTO gen_audit.defects (run_id, defect_kind, icp_identifier, interval_start)
SELECT :run_id, 'src:final', icp_identifier, last_read FROM _final;

-- ---------------------------------------------------------------------
-- 4. Implausible spikes. A range check should catch these trivially --
--    they exist so that it is proven to, not because they are subtle.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _spike ON COMMIT DROP AS
SELECT read_id, kwh
FROM src_metering.interval_reads
WHERE source_file LIKE 'lcl-%' AND register_code = 'UN'
  AND gen_audit.h(icp_identifier || interval_start::text || 'spike') < 0.0002;
ANALYZE _spike;

UPDATE src_metering.interval_reads r
   SET kwh = ROUND(r.kwh * (40 + gen_audit.h(r.icp_identifier || r.interval_start::text || 'mag') * 60)::numeric, 4)
  FROM _spike s WHERE r.read_id = s.read_id;

INSERT INTO gen_audit.defects (run_id, defect_kind, icp_identifier, interval_start, detail)
SELECT :run_id, 'src:spike', r.icp_identifier, r.interval_start,
       jsonb_build_object('was', s.kwh, 'now', r.kwh)
FROM src_metering.interval_reads r JOIN _spike s ON s.read_id = r.read_id;

-- ---------------------------------------------------------------------
-- 5. Stuck meter -- a whole day of zeros. The nastiest of the set.
--
-- It passes ck_kwh_non_negative, it passes completeness (every interval
-- is present), and it passes any range check. ONLY a distribution rule
-- finds it: a household that used 20 kWh yesterday and 0.0000 today,
-- every half hour, did not stop existing.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _stuck ON COMMIT DROP AS
SELECT icp_identifier, nz_date FROM (
    SELECT DISTINCT icp_identifier,
           (interval_start AT TIME ZONE 'Pacific/Auckland')::date AS nz_date
    FROM src_metering.interval_reads
    WHERE source_file LIKE 'lcl-%' AND register_code = 'UN') d
WHERE gen_audit.h(icp_identifier || nz_date::text || 'stuck') < 0.001;
ANALYZE _stuck;

UPDATE src_metering.interval_reads r
   SET kwh = 0
  FROM _stuck s
 WHERE r.icp_identifier = s.icp_identifier
   AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date = s.nz_date
   AND r.register_code = 'UN' AND r.source_file LIKE 'lcl-%';

INSERT INTO gen_audit.defects (run_id, defect_kind, icp_identifier, nz_date)
SELECT :run_id, 'src:stuck_meter', icp_identifier, nz_date FROM _stuck;

-- ---------------------------------------------------------------------
-- 6. Duplicate delivery -- the SAME row twice, different received_at.
--
-- Not a revision: version_no is unchanged. Dedup logic written as
-- "keep the highest version_no" silently keeps both of these, because
-- both are version 1. It has to key on the interval as well.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _dup ON COMMIT DROP AS
SELECT read_id FROM src_metering.interval_reads
WHERE source_file LIKE 'lcl-%'
  AND gen_audit.h(icp_identifier || register_code || interval_start::text || 'dup') < 0.0005;
ANALYZE _dup;

INSERT INTO src_metering.interval_reads
    (icp_identifier, register_code, interval_start, kwh, read_status,
     version_no, received_at, source_file)
SELECT r.icp_identifier, r.register_code, r.interval_start, r.kwh, r.read_status,
       r.version_no, r.received_at + interval '4 hours', r.source_file
FROM src_metering.interval_reads r JOIN _dup d ON d.read_id = r.read_id;

INSERT INTO gen_audit.defects (run_id, defect_kind, icp_identifier, register_code, interval_start)
SELECT :run_id, 'src:duplicate', r.icp_identifier, r.register_code, r.interval_start
FROM src_metering.interval_reads r JOIN _dup d ON d.read_id = r.read_id;

-- ---------------------------------------------------------------------
-- 7. Export delivered on the IMPORT register.
--
-- This replaces "negative reads", which the source schema forbids by
-- design: ck_kwh_non_negative is the enforcement of §8.2.3, that export
-- is modelled by direction and never by sign. A literal negative value
-- belongs at the FILE boundary in Stage 3 -- which is exactly what the
-- CHECK exists to stop reaching this table.
--
-- So the source-side equivalent is export volume landing on 'UN' as a
-- positive number. It passes every constraint, inflates the customer's
-- import, and is findable only by noticing that a solar household's
-- midday import rises on sunny days instead of falling.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _misdeliver ON COMMIT DROP AS
SELECT icp_identifier FROM (
    SELECT DISTINCT icp_identifier FROM src_metering.interval_reads
    WHERE source_file LIKE 'lcl-%' AND register_code = 'EX'
    ORDER BY icp_identifier LIMIT 2) t;
ANALYZE _misdeliver;

UPDATE src_metering.interval_reads r
   SET kwh = r.kwh + COALESCE((
       SELECT e.kwh FROM src_metering.interval_reads e
        WHERE e.icp_identifier = r.icp_identifier AND e.register_code = 'EX'
          AND e.interval_start = r.interval_start AND e.version_no = r.version_no
        LIMIT 1), 0)
  FROM _misdeliver m
 WHERE r.icp_identifier = m.icp_identifier
   AND r.register_code = 'UN' AND r.source_file LIKE 'lcl-%'
   AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date
       BETWEEN DATE '2025-11-01' AND DATE '2025-11-30';

INSERT INTO gen_audit.defects (run_id, defect_kind, icp_identifier, register_code, detail)
SELECT :run_id, 'src:export_on_import', icp_identifier, 'UN',
       jsonb_build_object('month', '2025-11')
FROM _misdeliver;

-- ---------------------------------------------------------------------
-- 8. Ambiguous register: one ICP, two meters, both carrying 'UN'.
--
-- registers is UNIQUE(meter_id, register_code) but interval_reads is
-- keyed on (icp_identifier, register_code). An ICP with two meters
-- sharing a register code therefore has no unique resolution, and no
-- rule is documented for picking one. This makes that hazard concrete
-- instead of theoretical; Stage 4 needs the rule.
-- ---------------------------------------------------------------------
WITH pick AS (
    SELECT c.icp_id, c.icp_identifier FROM src_metering.connection_points c
    WHERE c.icp_identifier LIKE '09%' ORDER BY c.icp_identifier LIMIT 1
), m AS (
    INSERT INTO src_metering.meters (icp_id, serial_number, is_smart, installed_on)
    SELECT icp_id, 'SM-SECOND-01', true, DATE '2025-06-01' FROM pick
    RETURNING meter_id, icp_id
)
INSERT INTO src_metering.registers
    (meter_id, register_code, description, is_controlled, flow_direction)
SELECT meter_id, 'UN', 'Grid import (second meter)', false, 'import' FROM m;

INSERT INTO gen_audit.defects (run_id, defect_kind, icp_identifier, register_code, detail)
SELECT :run_id, 'src:ambiguous_register', c.icp_identifier, 'UN',
       jsonb_build_object('meters', count(*))
FROM src_metering.connection_points c
JOIN src_metering.meters m ON m.icp_id = c.icp_id
WHERE c.icp_identifier LIKE '09%'
GROUP BY c.icp_identifier HAVING count(*) > 1;

-- ---------------------------------------------------------------------
-- 9. Demo care scenario: sustained recent consumption collapse.
--
-- This is not a missing-data trick. Every interval and every revision stays
-- present; one non-quarantined generated household genuinely uses 70% less in
-- the final eight local days. Eight rather than seven makes the scenario
-- survive the compact demo extract, whose latest source day normally arrives
-- one day beyond its range. Sprint 3 can therefore demonstrate all five
-- explainable hardship signals even with a 50-household cohort.
-- ---------------------------------------------------------------------
WITH bounds AS (
    SELECT max((interval_start AT TIME ZONE 'Pacific/Auckland')::date) AS last_day
    FROM src_metering.interval_reads
    WHERE source_file LIKE 'lcl-%'
), pick AS (
    -- OFFSET 1 avoids the first generated ICP, which is deliberately made
    -- ambiguous above and is correctly quarantined downstream.
    SELECT DISTINCT icp_identifier
    FROM src_metering.interval_reads
    WHERE source_file LIKE 'lcl-%' AND register_code = 'UN'
    ORDER BY icp_identifier
    OFFSET 1 LIMIT 1
)
UPDATE src_metering.interval_reads r
   SET kwh = ROUND(r.kwh * 0.30, 4)
  FROM bounds b, pick p
 WHERE r.icp_identifier = p.icp_identifier
   AND r.register_code = 'UN'
   AND r.source_file LIKE 'lcl-%'
   AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date
       BETWEEN b.last_day - 7 AND b.last_day;

INSERT INTO gen_audit.defects
    (run_id, defect_kind, icp_identifier, register_code, detail)
SELECT :run_id, 'src:hardship_usage_collapse', icp_identifier, 'UN',
       jsonb_build_object('recent_multiplier', 0.30, 'days', 8)
FROM (
    SELECT DISTINCT icp_identifier
    FROM src_metering.interval_reads
    WHERE source_file LIKE 'lcl-%' AND register_code = 'UN'
    ORDER BY icp_identifier
    OFFSET 1 LIMIT 1
) p;

DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT defect_kind, count(*) AS c FROM gen_audit.defects
             WHERE defect_kind LIKE 'src:%' GROUP BY 1 ORDER BY 1
    LOOP
        RAISE NOTICE '  % : %', rpad(r.defect_kind, 26), r.c;
    END LOOP;
END $$;

COMMIT;
