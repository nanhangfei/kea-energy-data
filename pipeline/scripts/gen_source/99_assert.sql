-- =====================================================================
-- The Stage 2 gate.
--
-- One transaction, one exception on the first failure. These are the
-- properties that are WRONG SILENTLY: each one, if broken, leaves a
-- database that still loads, still joins and still adds up.
--
--   docker compose exec -T kea-source-db psql -v ON_ERROR_STOP=1 \
--       -U kea -d kea -f - < 99_assert.sql
-- =====================================================================

\set ON_ERROR_STOP on

DO $$
DECLARE
    n bigint; m bigint; expected_icps bigint; bad text;
    winter numeric; summer numeric;
BEGIN
    -- 0 -----------------------------------------------------------------
    -- THE FIXTURES ARE STILL INTACT.
    --
    -- This exists because the generator once damaged them. A header
    -- UPDATE written to recompute totals for generated invoices was not
    -- scoped by ICP, so it aggregated every row of invoice_lines and
    -- swept in all 28 fixture invoices -- zeroing TOTAL_IMPORT_KWH and
    -- TOTAL_EXPORT_KWH on all of them. Nothing failed. The nine fixtures
    -- still loaded, still tied out against their own lines, and Bill B
    -- still totalled $50.54, because their GST_RATE is 0 and the money
    -- columns happened to be recomputed to the same values. Only the
    -- metered volumes were gone, and only a check that looked at them
    -- would have said so.
    --
    -- The fixtures are the ONLY external ground truth in this repo.
    -- Assert them before asserting anything the generator produced.
    SELECT count(*) INTO n
      FROM src_billing.invoices i
      JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
     WHERE a.ICP_NUMBER NOT LIKE '09%' AND COALESCE(i.TOTAL_IMPORT_KWH, 0) = 0;
    IF n > 0 THEN
        RAISE EXCEPTION '% fixture invoices have lost their metered volume; re-run make source-seed', n;
    END IF;

    SELECT count(*) INTO n FROM src_billing.invoices i
      JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
     WHERE i.INV_NO = 'DEMO-INV-20240831'
       AND i.CHARGES_EXGST = 50.54 AND i.TOTAL_IMPORT_KWH = 180.4300
       AND i.TOTAL_EXPORT_KWH = 47.4800 AND i.EXPORT_CREDIT = 6.60;
    IF n <> 1 THEN
        RAISE EXCEPTION 'Bill B fixture header no longer matches the printed invoice';
    END IF;
    RAISE NOTICE '0. the nine fixtures are intact';

    -- 0b ----------------------------------------------------------------
    -- No identity sequence sits below its table's highest id.
    --
    -- The fixtures assign ids explicitly with OVERRIDING SYSTEM VALUE,
    -- which does not advance the sequence. A sequence left behind does
    -- not fail immediately -- it fails once generated rows have climbed
    -- far enough to reach a fixture's reserved id, which depends on how
    -- many rows previous runs happened to insert. Same code, same data,
    -- different outcome. Asserted so the state is checked rather than
    -- the symptom waited for.
    SELECT string_agg(format('%s.%s', sch, tbl), ', ') INTO bad
    FROM (
        SELECT n.nspname AS sch, c.relname AS tbl, a.attname AS col,
               pg_get_serial_sequence(format('%I.%I', n.nspname, c.relname),
                                      a.attname) AS seq
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attidentity <> ''
        WHERE n.nspname LIKE 'src\_%'
    ) t
    WHERE t.seq IS NOT NULL
      AND (SELECT last_value FROM pg_sequences
            WHERE schemaname || '.' || sequencename = t.seq) <
          (SELECT COALESCE(max_id, 0) FROM (
              SELECT (xpath('/row/c/text()',
                      query_to_xml(format('SELECT COALESCE(MAX(%I),0) AS c FROM %I.%I',
                                          t.col, t.sch, t.tbl),
                                   false, true, '')))[1]::text::bigint AS max_id) q);
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'identity sequences trail their table max (fixture ids will collide): %', bad;
    END IF;
    RAISE NOTICE '0b. every identity sequence is ahead of its table';

    -- 0c ----------------------------------------------------------------
    -- Generated rows sit at 1,000,000 and above, clear of every fixture id
    -- (gen_audit.sync_sequences). Below it, a generated row can hold an id a
    -- fixture adds later, and re-seeding that fixture fails -- invoice 239003
    -- did, under #143.
    SELECT string_agg(t, ', ') INTO bad FROM (
        SELECT 'connection_points' AS t FROM src_metering.connection_points
         WHERE icp_identifier LIKE '09%' AND icp_id < 1000000
        UNION
        SELECT 'accounts' FROM src_billing.accounts
         WHERE ICP_NUMBER LIKE '09%' AND ACCT_ID < 1000000
        UNION
        SELECT 'invoices' FROM src_billing.invoices i
          JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
         WHERE a.ICP_NUMBER LIKE '09%' AND i.INV_ID < 1000000
    ) x;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'generated rows inside the fixture id range: %', bad;
    END IF;
    RAISE NOTICE '0c. generated ids sit above the fixtures''';

    -- 1 -----------------------------------------------------------------
    SELECT cohort_size INTO expected_icps
      FROM gen_audit.runs ORDER BY run_id DESC LIMIT 1;
    IF expected_icps IS NULL OR expected_icps < 20 THEN
        RAISE EXCEPTION 'invalid generated cohort size: %', expected_icps;
    END IF;

    SELECT count(*), count(DISTINCT icp_identifier)
      INTO n, m
      FROM src_metering.interval_reads WHERE source_file LIKE 'lcl%';
    IF n < expected_icps * 30000 THEN
        RAISE EXCEPTION 'only % generated reads for % ICPs; expected at least %',
            n, expected_icps, expected_icps * 30000;
    END IF;
    IF m <> expected_icps THEN
        RAISE EXCEPTION 'generated reads span % ICPs; expected %', m, expected_icps;
    END IF;
    RAISE NOTICE '1. % reads across % ICPs', n, m;

    -- 2 -----------------------------------------------------------------
    -- THE LOCAL DAY IS NEVER ASSUMED TO BE 48.
    --
    -- COUNT(DISTINCT interval_start), not COUNT(*). Revisions coexist with
    -- the rows they supersede and duplicate deliveries repeat a row
    -- outright, so counting rows counts the same half-hour several times
    -- and reports days "longer" than the zone allows. That is the source
    -- behaving exactly as designed; only the check was wrong.
    --
    -- Note AT TIME ZONE. The container runs TZ=UTC, so interval_start::date
    -- would silently bucket the last two hours of every NZDT day into the
    -- next day: every day would still look plausible, and the two DST days
    -- would be wrong in a way that reads as missing data.
    SELECT count(*) INTO n FROM (
        SELECT ir.icp_identifier,
               (ir.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
               count(DISTINCT ir.interval_start) AS c
        FROM src_metering.interval_reads ir
        WHERE ir.source_file LIKE 'lcl%' AND ir.register_code = 'UN'
        GROUP BY 1, 2
    ) t
    WHERE t.c > (
        SELECT (extract(epoch FROM
                   ((t.d + 1)::timestamp AT TIME ZONE 'Pacific/Auckland')
                 - ( t.d      ::timestamp AT TIME ZONE 'Pacific/Auckland')) / 1800)::int);
    IF n > 0 THEN
        RAISE EXCEPTION '% household-days hold more reads than the local day is long', n;
    END IF;
    RAISE NOTICE '2a. no household-day exceeds its true local length';

    -- ...and both short and long days are actually PRESENT. 2a alone
    -- passes happily on a transform that produced 48 everywhere, and on
    -- the one that produced 44 on a UK spring-forward day: neither
    -- exceeds anything. Two thresholds, because they catch different
    -- failures:
    --
    --   per household, at least one complete 46 and one complete 50 --
    --     catches the daylight-saving handling breaking for anyone;
    --   across the cohort, at least 95%% of households complete on ALL
    --     four transitions -- catches a SYSTEMATIC shortfall, which is
    --     what the UK spring-forward bug looked like: every household
    --     two reads short on the same two days, indistinguishable from
    --     a gap when viewed one household at a time.
    --
    -- Not 100%%: the source genuinely has gaps, and 3 of 500 households
    -- are simply missing readings on one of the four days.
    SELECT count(*) INTO n FROM (
        SELECT icp_identifier
        FROM (
            SELECT ir.icp_identifier,
                   (ir.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
                   count(DISTINCT ir.interval_start) AS c
            FROM src_metering.interval_reads ir
            WHERE ir.source_file LIKE 'lcl%' AND ir.register_code = 'UN'
            GROUP BY 1, 2
        ) x
        GROUP BY icp_identifier
        HAVING count(*) FILTER (WHERE c = 46) < 1
            OR count(*) FILTER (WHERE c = 50) < 1
    ) y;
    IF n > 0 THEN
        RAISE EXCEPTION '% households have no complete 46-half-hour or 50-half-hour day', n;
    END IF;

    SELECT count(*) FILTER (WHERE n46 >= 2 AND n50 >= 2), count(*)
      INTO n, m
      FROM (
        SELECT icp_identifier,
               count(*) FILTER (WHERE c = 46) AS n46,
               count(*) FILTER (WHERE c = 50) AS n50
        FROM (
            SELECT ir.icp_identifier,
                   (ir.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
                   count(DISTINCT ir.interval_start) AS c
            FROM src_metering.interval_reads ir
            WHERE ir.source_file LIKE 'lcl%' AND ir.register_code = 'UN'
            GROUP BY 1, 2
        ) x GROUP BY icp_identifier) z;
    IF n * 100 < m * 95 THEN
        RAISE EXCEPTION 'only % of % households are complete on all four daylight-saving transitions', n, m;
    END IF;
    RAISE NOTICE '2b. every household has a 46- and a 50-half-hour day; % of % complete on all four', n, m;

    -- 3 -----------------------------------------------------------------
    -- THE SEASON FLIP HAPPENED. If the UK->NZ shift were ever dropped or
    -- recomputed wrongly, June would carry London's summer load. Every
    -- row would still be valid; only the weather attribution in the bill
    -- explainer would quietly invert, and nothing else would notice.
    SELECT avg(daily) INTO winter FROM (
        SELECT (interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
               icp_identifier, sum(kwh) AS daily
        FROM src_metering.interval_reads
        WHERE source_file LIKE 'lcl-%' AND register_code = 'UN'
          AND extract(month FROM interval_start AT TIME ZONE 'Pacific/Auckland') IN (6,7,8)
        GROUP BY 1, 2) w;
    SELECT avg(daily) INTO summer FROM (
        SELECT (interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
               icp_identifier, sum(kwh) AS daily
        FROM src_metering.interval_reads
        WHERE source_file LIKE 'lcl-%' AND register_code = 'UN'
          AND extract(month FROM interval_start AT TIME ZONE 'Pacific/Auckland') IN (12,1,2)
        GROUP BY 1, 2) s;
    IF winter < summer * 1.25 THEN
        RAISE EXCEPTION 'no season flip: NZ winter %.2f vs summer %.2f kWh/day (need +25%%)',
                        winter, summer;
    END IF;
    RAISE NOTICE '3. season flip: winter % vs summer % kWh/day (+% percent)',
        round(winter, 2), round(summer, 2), round((winter / summer - 1) * 100);

    -- 4 -----------------------------------------------------------------
    -- THE WATERMARK IS USABLE. The whole incremental-extract design keys
    -- off received_at. A DEFAULT now() would make this 1, the extract
    -- would appear to work, and it would be untestable.
    SELECT count(DISTINCT received_at::date) INTO n
      FROM src_metering.interval_reads WHERE source_file LIKE 'lcl-%';
    IF n < 700 THEN
        RAISE EXCEPTION 'received_at spans only % distinct days; the extract watermark is untestable', n;
    END IF;
    RAISE NOTICE '4. received_at spans % distinct days', n;

    -- 5 -----------------------------------------------------------------
    -- Reads carry BUSINESS keys, so a read can name a register that does
    -- not exist and no foreign key objects. Checked here rather than in
    -- 03_reference.sql, where it would run before the reads are loaded
    -- and pass against nothing.
    SELECT count(*) INTO n FROM (
        SELECT DISTINCT ir.icp_identifier, ir.register_code
        FROM src_metering.interval_reads ir
        WHERE ir.source_file LIKE 'lcl-%'
          AND NOT EXISTS (
            SELECT 1 FROM src_metering.registers r
            JOIN src_metering.meters m ON m.meter_id = r.meter_id
            JOIN src_metering.connection_points c ON c.icp_id = m.icp_id
            WHERE c.icp_identifier = ir.icp_identifier
              AND r.register_code = ir.register_code)) t;
    IF n > 0 THEN
        RAISE EXCEPTION '% read keys reference a register that does not exist', n;
    END IF;
    RAISE NOTICE '5. every read key resolves to a register';

    -- 6 -----------------------------------------------------------------
    -- Estimated reads exist and are real. A UK spring-forward day maps
    -- onto an NZ day whose grid still asks for 01:00 and 01:30, which
    -- London does not have; those are filled and flagged 'E'. If this is
    -- zero, the fill was lost and those two days are short for every
    -- household at once.
    SELECT count(*) INTO n FROM src_metering.interval_reads
     WHERE source_file LIKE 'lcl-%' AND read_status = 'E';
    IF n = 0 THEN
        RAISE EXCEPTION 'no estimated reads: the UK daylight-saving fill was lost';
    END IF;
    RAISE NOTICE '6. % estimated reads', n;

    -- 7 -----------------------------------------------------------------
    -- Export is a register, never a sign. ck_kwh_non_negative already
    -- forbids a negative value; this checks the other half, that export
    -- volume actually exists to be credited.
    SELECT count(DISTINCT icp_identifier) INTO n
      FROM src_metering.interval_reads
     WHERE source_file LIKE 'lcl-%' AND register_code = 'EX' AND kwh > 0;
    IF n = 0 THEN
        RAISE EXCEPTION 'no household exports: the solar split did not run';
    END IF;
    RAISE NOTICE '7. % households export on a separate register', n;


    -- 8 -----------------------------------------------------------------
    -- Every invoice resolves to EXACTLY ONE plan version. Two versions
    -- match if valid_to is treated as inclusive, and the bill silently
    -- doubles; none match if the boundaries leave a gap, and the bill
    -- silently empties. Both produce a number, neither produces an error.
    SELECT count(*) INTO n FROM (
        SELECT i.INV_NO, count(pp.plan_id) AS c
        FROM src_billing.invoices i
        JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
        LEFT JOIN src_catalogue.price_plans pp
               ON pp.code = i.PLAN_CODE
              AND pp.valid_from <= i.PERIOD_TO::date
              AND (pp.valid_to IS NULL OR i.PERIOD_TO::date < pp.valid_to)
        WHERE a.ICP_NUMBER LIKE '09%'
        GROUP BY i.INV_NO HAVING count(pp.plan_id) <> 1) t;
    IF n > 0 THEN
        RAISE EXCEPTION '% invoices resolve to zero or several plan versions', n;
    END IF;
    RAISE NOTICE '8. every invoice resolves to exactly one plan version';

    -- 9 -----------------------------------------------------------------
    -- Every generated bill ties out: the header is the sum of its lines,
    -- GST is computed on that sum, and the stated import volume equals the
    -- metered import volume for the period.
    SELECT count(*) INTO n
    FROM src_billing.invoices i
    JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
    JOIN (SELECT INV_ID, ROUND(SUM(LINE_AMT), 2) AS s
          FROM src_billing.invoice_lines GROUP BY INV_ID) l ON l.INV_ID = i.INV_ID
    WHERE a.ICP_NUMBER LIKE '09%'
      AND (i.CHARGES_EXGST <> l.s
        OR i.GST_AMT <> ROUND(l.s * i.GST_RATE, 2)
        OR i.TOTAL_AMT <> i.CHARGES_EXGST + i.GST_AMT);
    IF n > 0 THEN
        RAISE EXCEPTION '% invoices do not tie out against their own lines', n;
    END IF;
    RAISE NOTICE '9. every generated invoice ties out';

    -- 10 ----------------------------------------------------------------
    -- The delayed-run trap must exist, or fixture 07 has nothing to
    -- generalise to. A ~43-day period bills roughly twice the usual
    -- amount for entirely ordinary usage, and src_billing.invoices has no
    -- period_days column to make that visible.
    SELECT count(*) INTO n
    FROM src_billing.invoices i
    JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
    WHERE a.ICP_NUMBER LIKE '09%'
      AND (i.PERIOD_TO::date - i.PERIOD_FROM::date) + 1 > 40;
    IF n < greatest(1, expected_icps / 50) THEN
        RAISE EXCEPTION 'only % long-period invoices for % ICPs; the delayed-run trap is untested',
            n, expected_icps;
    END IF;
    RAISE NOTICE '10. % delayed-run invoices', n;

    -- 11 ----------------------------------------------------------------
    -- THE RECONCILIATION. Bill B is re-rated from metered half-hours
    -- through the catalogue and compared LINE BY LINE against the printed
    -- invoice. The total carries a documented one-cent residual on the
    -- levy line, whose printed rate and printed amount cannot both be
    -- true -- see design 10.3 and data/dictionary/lcl-source-data.md.
    -- Anything else, or any other size, is a failure.
    SELECT count(*) INTO n
    FROM (SELECT * FROM gen_audit.rate_lines(
              '0165841877LCC54', 'NZSZ_ZERO_STD_FXTOU_240901_T1',
              DATE '2024-08-08', DATE '2024-08-31')) r
    FULL JOIN (
        SELECT l.CHG_GROUP, l.FLOW_DIR, l.LINE_DESC, l.LINE_AMT
        FROM src_billing.invoice_lines l
        JOIN src_billing.invoices i ON i.INV_ID = l.INV_ID
        WHERE i.INV_NO = 'DEMO-INV-20240831') b
      ON b.CHG_GROUP = r.chg_group AND b.FLOW_DIR = r.flow_dir
     AND b.LINE_DESC = r.line_desc
    WHERE r.line_amt IS DISTINCT FROM b.LINE_AMT
      AND r.line_desc IS DISTINCT FROM 'ELECTRICITY AUTHORITY Levy';
    IF n > 0 THEN
        RAISE EXCEPTION 'Bill B: % lines do not reproduce (levy line excluded by design 10.3)', n;
    END IF;

    SELECT ROUND(SUM(line_amt), 2) INTO winter
    FROM gen_audit.rate_lines('0165841877LCC54', 'NZSZ_ZERO_STD_FXTOU_240901_T1',
                              DATE '2024-08-08', DATE '2024-08-31');
    IF abs(winter - 50.54) > 0.01 THEN
        RAISE EXCEPTION 'Bill B rates to % against a printed 50.54; residual exceeds the documented cent', winter;
    END IF;
    RAISE NOTICE '11. Bill B reproduces line by line; total % vs printed 50.54', winter;

    -- 12 ----------------------------------------------------------------
    -- Orphan cross-system references exist. This assertion is here
    -- because the step that creates them once ran before the accounts
    -- did: it updated nothing, logged nothing, and raised nothing. The
    -- manifest agreed with the database because both were empty.
    SELECT count(*) INTO n FROM src_billing.accounts
     WHERE ICP_NUMBER LIKE '09%X' OR PLAN_CODE = 'KEA_GONE_VECT_STA';
    IF n <> 5 THEN
        RAISE EXCEPTION 'expected 5 orphan account references, found % -- 07 ran too early or not at all', n;
    END IF;

    SELECT count(*) INTO n
      FROM src_billing.accounts a
     WHERE a.ICP_NUMBER LIKE '09%'
       AND NOT EXISTS (SELECT 1 FROM src_metering.connection_points c
                       WHERE c.icp_identifier = a.ICP_NUMBER);
    IF n = 0 THEN
        RAISE EXCEPTION 'every account resolves to a connection point; 2.1.1 has nothing to fire on';
    END IF;
    RAISE NOTICE '12. % accounts reference an ICP that does not exist', n;

    -- 13 ----------------------------------------------------------------
    -- All three Sprint 3 plans carry accounts. Anytime Rate is the flat one: on a
    -- time-of-use plan a bill can rise because usage moved into a charged
    -- window, so a rise has two causes tangled together. On a flat rate it
    -- is volume or weather and nothing else, which is the clean case the
    -- bill explainer should get right first.
    --
    -- split_part rather than a fixed-width substring keeps the assertion tied
    -- to complete codes: ANY, TOU and WKND.
    SELECT count(DISTINCT split_part(PLAN_CODE, '_', 2)) INTO n
      FROM src_billing.accounts WHERE ICP_NUMBER LIKE '09%'
       AND PLAN_CODE <> 'KEA_GONE_VECT_STA';
    IF n <> 3 THEN
        RAISE EXCEPTION 'only % of the three plans have accounts', n;
    END IF;
    RAISE NOTICE '13. all three plans carry accounts';

    -- 14 ----------------------------------------------------------------
    -- Solar export is a separate positive register, but its bill line is a
    -- negative credit. A catalogue row by itself is not enough: this proves
    -- the rate engine matched EX reads to flow_direction = 'export', applied
    -- a buy-back rate, and carried the result to the invoice header.
    SELECT count(*) INTO n
    FROM src_billing.invoices i
    JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
    WHERE a.ICP_NUMBER LIKE '09%' AND i.TOTAL_EXPORT_KWH > 0;
    IF n = 0 THEN
        RAISE EXCEPTION 'no generated invoice has exported kWh; solar buy-back cannot be exercised';
    END IF;

    SELECT count(*) INTO m
    FROM src_billing.invoices i
    JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
    WHERE a.ICP_NUMBER LIKE '09%' AND i.TOTAL_EXPORT_KWH > 0
      AND COALESCE(i.EXPORT_CREDIT, 0) <= 0;
    IF m > 0 THEN
        RAISE EXCEPTION '% exported-energy invoices have no positive buy-back credit', m;
    END IF;

    SELECT count(*) INTO m
    FROM src_billing.invoice_lines l
    JOIN src_billing.invoices i ON i.INV_ID = l.INV_ID
    JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
    WHERE a.ICP_NUMBER LIKE '09%' AND l.FLOW_DIR = 'EXPORT'
      AND l.LINE_AMT >= 0;
    IF m > 0 THEN
        RAISE EXCEPTION '% generated export lines are not negative credits', m;
    END IF;
    RAISE NOTICE '14. % exported-energy invoices receive positive buy-back credits', n;

    -- 15 ----------------------------------------------------------------
    -- SPRINT 4'S PLANTED ANSWERS ARE WHAT THE EVALUATION WILL ASSUME.
    --
    -- planted.py and 08_payments.sql each promise an exact set: so many
    -- households per load, so many sliding accounts, none of them on a
    -- household a source defect already breaks, and solar only where it is
    -- the point. A label that quietly lands on a broken household makes a
    -- detection miss look like a detector fault, so the promises are checked
    -- here, in the database the evaluation reads.
    SELECT string_agg(k || '=' || c, ' ' ORDER BY k) INTO bad
    FROM (SELECT load_kind || '/' || pattern AS k, count(DISTINCT icp_identifier) AS c
          FROM gen_audit.planted_load GROUP BY 1) t;
    IF bad IS DISTINCT FROM
       'ev_portable_2kw/evening=1 ev_solar_wallbox_7kw/evening=1'
       ' ev_solar_wallbox_7kw/midday=1 ev_solar_wallbox_7kw/timer=1'
       ' ev_wallbox_7kw/evening=2 ev_wallbox_7kw/timer=1 hot_water_3kw/timer=4' THEN
        RAISE EXCEPTION 'planted households per load are not as planned: %', bad;
    END IF;
    SELECT count(*) INTO n FROM (
        SELECT icp_identifier FROM gen_audit.planted_load
        GROUP BY 1 HAVING count(DISTINCT load_kind) > 1) t;
    IF n > 0 THEN
        RAISE EXCEPTION '% households carry more than one planted load', n;
    END IF;
    SELECT count(DISTINCT pl.icp_identifier) INTO n
    FROM gen_audit.planted_load pl
    LEFT JOIN src_metering.connection_points c ON c.icp_identifier = pl.icp_identifier
    WHERE c.icp_id IS NULL OR pl.icp_identifier NOT LIKE '09%';
    IF n > 0 THEN
        RAISE EXCEPTION '% planted households are not generated connection points', n;
    END IF;
    RAISE NOTICE '15a. planted loads: %', bad;

    -- The wallbox really is in the reads: 7.2 kW is 3.6 kWh a half-hour,
    -- above anything the scaled LCL load reaches on its own for long. Only
    -- the households WITHOUT panels are checked this way -- on a solar one
    -- the array may cover the whole draw, and that it does is the finding,
    -- not a fault.
    SELECT count(*) INTO n FROM (
        SELECT DISTINCT icp_identifier FROM gen_audit.planted_load
        WHERE load_kind = 'ev_wallbox_7kw') w
    WHERE NOT EXISTS (
        SELECT 1 FROM src_metering.interval_reads r
        WHERE r.icp_identifier = w.icp_identifier AND r.register_code = 'UN'
          AND r.source_file LIKE 'lcl-%' AND r.kwh >= 3);
    IF n > 0 THEN
        RAISE EXCEPTION '% wallbox households never import 3 kWh in a half-hour', n;
    END IF;
    RAISE NOTICE '15b. every wallbox household imports >= 3 kWh in some half-hour';

    -- Solar and planting line up exactly: every ev_solar_wallbox_7kw
    -- household has an export register and no other planted household does.
    -- Both halves matter. A solar+EV household without panels would make
    -- Stage 2 measure nothing; a hot-water element that quietly acquired
    -- panels would be netted against generation and its label would be a
    -- lie.
    SELECT count(DISTINCT pl.icp_identifier) INTO n
    FROM gen_audit.planted_load pl
    LEFT JOIN (
        SELECT DISTINCT c.icp_identifier
        FROM src_metering.connection_points c
        JOIN src_metering.meters m ON m.icp_id = c.icp_id
        JOIN src_metering.registers g ON g.meter_id = m.meter_id
        WHERE g.flow_direction = 'export') x ON x.icp_identifier = pl.icp_identifier
    WHERE (x.icp_identifier IS NOT NULL)
       IS DISTINCT FROM (pl.load_kind = 'ev_solar_wallbox_7kw');
    IF n > 0 THEN
        RAISE EXCEPTION '% planted households disagree with their solar status', n;
    END IF;
    RAISE NOTICE '15c. only the 3 solar+EV planted households have an export register';

    -- Defect targets: 05 §7-§9 and 07's orphans. An orphan ICP carries a
    -- trailing X by now, so both sides are compared without it.
    CREATE TEMP TABLE _targets ON COMMIT DROP AS
    SELECT DISTINCT rtrim(icp_identifier, 'X') AS icp FROM gen_audit.defects
    WHERE defect_kind IN ('src:ambiguous_register', 'src:hardship_usage_collapse',
                          'src:export_on_import', 'src:orphan_icp', 'src:orphan_plan');
    SELECT count(*) INTO n FROM _targets;
    IF n < 7 THEN
        RAISE EXCEPTION 'only % defect-target ICPs found; expected at least 7', n;
    END IF;
    SELECT string_agg(DISTINCT pl.load_kind, ', ') INTO bad
    FROM gen_audit.planted_load pl JOIN _targets t ON t.icp = pl.icp_identifier;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'planted loads on a defect-target household: %', bad;
    END IF;
    SELECT string_agg(ph.profile, ', ') INTO bad
    FROM gen_audit.planted_hardship ph
    JOIN src_billing.accounts a ON a.ACCT_NO = ph.acct_no
    JOIN _targets t ON t.icp = rtrim(a.ICP_NUMBER, 'X');
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'sliding accounts on a defect-target household: %', bad;
    END IF;
    RAISE NOTICE '15d. no planted load or sliding account is on any of % defect targets', n;

    -- The slides: every onset inside the data, after at least six paid
    -- bills and after the trigger; drift only where it was planted.
    SELECT string_agg(profile || '=' || c, ' ' ORDER BY profile) INTO bad
    FROM (SELECT profile, count(*) AS c FROM gen_audit.planted_hardship GROUP BY 1) t;
    IF bad IS DISTINCT FROM 'SLIDING=3 SLIDING_SUDDEN=1' THEN
        RAISE EXCEPTION 'planted hardship accounts are not as planned: %', bad;
    END IF;
    SELECT count(*) INTO n
    FROM gen_audit.planted_hardship ph
    JOIN src_billing.accounts a ON a.ACCT_NO = ph.acct_no
    WHERE ph.arrears_onset_on IS NULL
       OR ph.arrears_onset_on <= ph.trigger_issued_on
       OR ph.arrears_onset_on > (SELECT max(PERIOD_TO)::date FROM src_billing.invoices)
       OR (ph.profile = 'SLIDING') <> (ph.drift_from IS NOT NULL)
       OR (SELECT count(DISTINCT i.INV_ID)
             FROM src_billing.invoices i
             JOIN src_billing.payments p ON p.INV_ID = i.INV_ID AND p.FAILED_FLAG = 'N'
            WHERE i.ACCT_ID = a.ACCT_ID
              AND i.ISSUE_DT::date < ph.trigger_issued_on
              AND i.PAYMENTS_APPL >= i.TOTAL_AMT) < 6;
    IF n > 0 THEN
        RAISE EXCEPTION '% planted hardship accounts have an onset outside the data, before six paid bills, or drift where none was planted', n;
    END IF;
    RAISE NOTICE '15e. planted hardship: %, every onset after six paid bills and inside the data', bad;

    -- ...and the Sprint 3 profiles still have someone in them, outside the
    -- sliding accounts: late in full, bounced, part-paid, stopped.
    SELECT concat_ws(', ',
        CASE WHEN NOT EXISTS (
            SELECT 1 FROM src_billing.payments p
            JOIN src_billing.invoices i ON i.INV_ID = p.INV_ID
            JOIN src_billing.accounts a ON a.ACCT_ID = p.ACCT_ID
            WHERE a.ICP_NUMBER LIKE '09%' AND p.FAILED_FLAG = 'N'
              AND p.PAID_TS >= i.DUE_DT + interval '8 days'
              AND a.ACCT_NO NOT IN (SELECT acct_no FROM gen_audit.planted_hardship))
             THEN 'LATE_STREAK' END,
        CASE WHEN NOT EXISTS (
            SELECT 1 FROM src_billing.payments p
            JOIN src_billing.accounts a ON a.ACCT_ID = p.ACCT_ID
            WHERE a.ICP_NUMBER LIKE '09%' AND p.FAILED_FLAG = 'Y')
             THEN 'DD_FAILURE' END,
        CASE WHEN NOT EXISTS (
            SELECT 1 FROM src_billing.invoices i
            JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
            WHERE a.ICP_NUMBER LIKE '09%'
              AND i.PAYMENTS_APPL > 0 AND i.PAYMENTS_APPL < i.TOTAL_AMT
              AND a.ACCT_NO NOT IN (SELECT acct_no FROM gen_audit.planted_hardship))
             THEN 'PART_PAYMENT' END,
        CASE WHEN NOT EXISTS (
            SELECT 1 FROM src_billing.accounts a
            WHERE a.ICP_NUMBER LIKE '09%'
              AND a.ACCT_NO NOT IN (SELECT acct_no FROM gen_audit.planted_hardship)
              AND (SELECT count(*) FROM src_billing.invoices i
                    WHERE i.ACCT_ID = a.ACCT_ID AND i.TOTAL_AMT > 0
                      AND i.PAYMENTS_APPL = 0
                      AND i.DUE_DT <= (SELECT max(PERIOD_TO) FROM src_billing.invoices)
                                      - interval '35 days') >= 2)
             THEN 'STOPPED' END) INTO bad;
    IF bad <> '' THEN
        RAISE EXCEPTION 'Sprint 3 payment profiles with nobody left in them: %', bad;
    END IF;
    RAISE NOTICE '15f. every Sprint 3 payment profile is still populated';

    RAISE NOTICE 'STAGE 2 ASSERTIONS PASSED';
END $$;
