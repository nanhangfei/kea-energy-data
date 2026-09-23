-- =====================================================================
-- gen_audit.planted_load -- the sessions gen_metering.py planted, from
-- staging, stamped with this load's run (05_defects.sql inserts it).
--
-- Replaced on every run, as 05 replaces the src:% defects. The sessions
-- themselves are already inside the reads; this is only the answer key.
-- =====================================================================

BEGIN;

DELETE FROM gen_audit.planted_load;

INSERT INTO gen_audit.planted_load
    (run_id, icp_identifier, load_kind, pattern, session_start, session_end,
     half_hours, kwh)
SELECT (SELECT max(run_id) FROM gen_audit.runs),
       icp_identifier, load_kind, pattern, session_start, session_end,
       half_hours, kwh
FROM gen_audit.stg_planted_load;

DO $$
DECLARE r record; n int;
BEGIN
    SELECT count(*) INTO n FROM gen_audit.planted_load;
    IF n = 0 THEN
        RAISE EXCEPTION 'no planted sessions staged: planted_loads.csv.gz was empty or not loaded';
    END IF;
    FOR r IN SELECT load_kind, pattern, count(DISTINCT icp_identifier) AS icps,
                    count(*) AS sessions, round(sum(kwh)) AS kwh
             FROM gen_audit.planted_load GROUP BY 1, 2 ORDER BY 1, 2
    LOOP
        RAISE NOTICE 'planted % %: % households, % sessions, % kWh',
            r.load_kind, r.pattern, r.icps, r.sessions, r.kwh;
    END LOOP;
END $$;

COMMIT;
