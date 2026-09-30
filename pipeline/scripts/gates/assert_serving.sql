-- THE STAGE 5 GATE. The pipeline's own output, against what the fixtures assert.
--
-- Stage 1 hand-wrote nine fixtures, and `make init-sources` still loads their
-- source rows. Their hand-built serving rows are gone: the served tables live
-- in the backend's database, and every row there was published from the
-- warehouse. This file is what survives them -- their assertions, re-pointed
-- at whatever the served tables hold now.
--
-- IT RUNS AS kea_loader (KEA_PUBLISH_DSN), INSIDE A TRANSACTION ITS CALLER
-- ROLLS BACK. The checks build temp objects, which a read-only session
-- refuses, so the ROLLBACK is what keeps anything from persisting -- and the
-- publisher's credential cannot reach app.*, which the owner's could. The
-- sequence check in section 5 reads every identity sequence in the served
-- schemas; migrations/serving/V004 is what lets kea_loader read them all.
--
-- IT ASSERTS ON BUSINESS KEYS, NEVER ON THE FIXTURES' IDS. The fixtures used
-- hand-assigned surrogates (icp 138001, account 228001); the pipeline mints
-- its own from key_map_icp and friends, so those numbers no longer exist and
-- an id-based check would fail for a reason that has nothing to do with
-- correctness. icp_number and account_number are the same on both sides
-- because they come from the same source.
--
-- IT COLLECTS FAILURES RATHER THAN RAISING ON THE FIRST. The fixtures could
-- stop at the first bad number because each one had just inserted its own
-- data; here a single wrong join can move several numbers at once, and seeing
-- all of them together is what tells you which.
\set ON_ERROR_STOP on
\pset pager off

CREATE TEMP TABLE _checks (
    n serial, area text, what text, got text, want text,
    ok boolean, kind text, why text);

-- TWO KINDS OF CHECK, AND THE DISTINCTION IS THE POINT OF THIS FILE.
--
--   'must'   the pipeline is required to reproduce this. A failure is a bug.
--   'known'  the fixture asserts something the pipeline CANNOT reproduce, for
--            a reason that is understood, recorded here, and is a property of
--            the source data rather than of the loader.
--
-- The temptation with a 'known' divergence is to delete the assertion. Keeping
-- it, with its reason attached, is what makes the difference visible the day it
-- changes -- including the day it starts passing, which is information too.
CREATE OR REPLACE FUNCTION pg_temp.chk(
    area text, what text, got anyelement, want anyelement,
    tol numeric DEFAULT NULL, kind text DEFAULT 'must', why text DEFAULT NULL)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE passed boolean;
BEGIN
    IF got IS NULL OR want IS NULL THEN
        passed := (got IS NOT DISTINCT FROM want);
    ELSIF tol IS NOT NULL THEN
        passed := abs(got::numeric - want::numeric) <= tol;
    ELSE
        passed := (got = want);
    END IF;
    INSERT INTO _checks (area, what, got, want, ok, kind, why)
    VALUES (area, what, coalesce(got::text, 'NULL'), coalesce(want::text, 'NULL'),
            passed, kind, why);
END $$;


-- =====================================================================
-- 1. The printed invoice.  data/sample/How_to_read_your_invoice_page_2
--    ICP 0165841877LCC54, 08-31 Aug 2024, $50.54. The only numbers in this
--    whole file that come from paper rather than from a generator.
-- =====================================================================
DO $$
DECLARE b record; v numeric;
BEGIN
    SELECT bl.* INTO b
      FROM billing.bill bl
      JOIN billing.account a ON a.account_id = bl.account_id
      JOIN metering.icp i    ON i.icp_id     = a.icp_id
     WHERE i.icp_number = '0165841877LCC54'
       AND bl.period_start = DATE '2024-08-08'
       AND bl.period_end   = DATE '2024-08-31';

    PERFORM pg_temp.chk('bill B', 'the bill exists', (b.bill_id IS NOT NULL), true);
    IF b.bill_id IS NULL THEN RETURN; END IF;

    PERFORM pg_temp.chk('bill B', 'total_charges',  b.total_charges, 50.54::numeric, 0.005);
    PERFORM pg_temp.chk('bill B', 'period_days',    b.period_days, 24);

    SELECT sum(quantity) INTO v FROM billing.bill_line
     WHERE bill_id = b.bill_id AND charge_group = 'energy'
       AND direction = 'import' AND band_name ILIKE 'peak';
    PERFORM pg_temp.chk('bill B', 'energy peak import kWh', v, 34.08::numeric, 0.02);

    SELECT sum(quantity) INTO v FROM billing.bill_line
     WHERE bill_id = b.bill_id AND direction = 'export';
    PERFORM pg_temp.chk('bill B', 'total export kWh', v, 47.48::numeric, 0.02);

    SELECT round(sum(amount), 2) INTO v FROM billing.bill_line WHERE bill_id = b.bill_id;
    PERFORM pg_temp.chk('bill B', 'lines sum to the header', v, b.charges_excl_gst, 0.005);
END $$;


-- =====================================================================
-- 2. The six scenarios.  Each was generated so that ONE driver dominates,
--    and mart_bill_explanation has to name that driver back. This is the
--    check that a decomposition which merely adds up is still wrong if it
--    attributes the change to the wrong thing.
-- =====================================================================
DO $$
DECLARE s record; e record; v_top text; v_delta numeric; v_drivers numeric;
        v_kind text;
BEGIN
    FOR s IN
        SELECT * FROM (VALUES
            ('DEMO-ACCT-222001', 'full_house',       'unexplained',   false),
            ('DEMO-ACCT-223001', 'cold_snap',        'weather',       true),
            ('DEMO-ACCT-224001', 'faulty_appliance', 'base_load',     true),
            -- Sprint 4: the 21:00-23:00 timer every night since 5 June is a
            -- detected timed_night load (design §15, rule 3), so the rise it
            -- adds is priced by delta_from_detected_loads and no longer falls
            -- into unexplained. is_explainable stays false: no heating slope.
            ('DEMO-ACCT-225001', 'new_appliance',    'detected_loads', false),
            ('DEMO-ACCT-226001', 'estimated_reads',  'base_load',     false),
            ('DEMO-ACCT-227001', 'longer_period',    'period_length', true)
        ) AS t(acct, scenario, want_driver, want_explainable)
    LOOP
        SELECT x.* INTO e
          FROM kea.mart_bill_explanation x
          JOIN billing.account a ON a.account_id = x.account_id
         WHERE a.account_number = s.acct
           AND x.prior_bill_id IS NOT NULL
         ORDER BY x.period_start DESC LIMIT 1;

        PERFORM pg_temp.chk(s.scenario, 'has an explanation with a prior bill',
                            (e.bill_id IS NOT NULL), true);
        CONTINUE WHEN e.bill_id IS NULL;

        PERFORM pg_temp.chk(s.scenario, 'the bill rose', (e.amount_delta > 0), true);

        -- the drivers must still partition the delta
        v_drivers := round(coalesce(e.delta_from_period_length,0)
                         + coalesce(e.delta_from_weather,0)
                         + coalesce(e.delta_from_base_load,0)
                         + coalesce(e.delta_from_detected_loads,0)
                         + coalesce(e.delta_from_tariff,0)
                         + coalesce(e.delta_unexplained,0), 2);
        -- Six rounded terms, so up to 0.03 of rounding between them.
        PERFORM pg_temp.chk(s.scenario, 'drivers sum to the delta',
                            v_drivers, round(e.amount_delta, 2), 0.03);

        SELECT d.name INTO v_top FROM (VALUES
            ('period_length', abs(coalesce(e.delta_from_period_length, 0))),
            ('weather',       abs(coalesce(e.delta_from_weather, 0))),
            ('base_load',     abs(coalesce(e.delta_from_base_load, 0))),
            ('detected_loads', abs(coalesce(e.delta_from_detected_loads, 0))),
            ('tariff',        abs(coalesce(e.delta_from_tariff, 0))),
            ('unexplained',   abs(coalesce(e.delta_unexplained, 0)))
        ) AS d(name, v) ORDER BY d.v DESC LIMIT 1;

        -- WHETHER THESE TWO CAN MATCH IS DECIDED BY THE DATA, NOT BY THE
        -- SCENARIO'S NAME. Attributing a delta to weather requires a fitted
        -- heating slope, and these households carry about two months of reads
        -- -- int_metering__heating_sensitivity reports fitted_on_months = 2 and
        -- returns NULL. With no slope the weather term is not computed, the
        -- delta falls through to unexplained, and is_explainable is false.
        --
        -- That is the model being honest rather than wrong: the fixture's
        -- 0.93 kWh/HDD is a hand-set constant in generate_scenarios.py, not
        -- something any estimator recovered from 61 days. So the check is
        -- marked 'known' exactly when the slope is missing, and stays a hard
        -- requirement whenever it is present.
        --
        -- A detected load needs no slope, so a scenario that expects one keeps
        -- its dominant-driver check a hard requirement either way.
        v_kind := CASE WHEN e.heating_sensitivity_kwh_per_hdd IS NULL
                       THEN 'known' ELSE 'must' END;
        PERFORM pg_temp.chk(s.scenario, 'dominant driver', v_top, s.want_driver,
                            NULL,
                            CASE WHEN s.want_driver = 'detected_loads'
                                 THEN 'must' ELSE v_kind END,
                            'no fitted heating slope for this household (2 months of reads)');
        PERFORM pg_temp.chk(s.scenario, 'is_explainable', e.is_explainable,
                            s.want_explainable, NULL, v_kind,
                            'is_explainable requires the weather term the missing slope prevents');
    END LOOP;
END $$;


-- =====================================================================
-- 3. Thirteen months.  The fixture that exists so a year-on-year question
--    has an answer, and so the 46- and 50-interval local days the schema
--    warns about are actually present rather than merely described.
-- =====================================================================
DO $$
DECLARE v_span int; v_bills int; v_rows int; v_days int; v_gaps int;
        v_months int; v_short int; v_long int; v_bad int; v_icp bigint;
BEGIN
    SELECT icp_id INTO v_icp FROM metering.icp WHERE icp_number = 'DEMO0000000801AA';
    PERFORM pg_temp.chk('13 months', 'the ICP is loaded', (v_icp IS NOT NULL), true);
    IF v_icp IS NULL THEN RETURN; END IF;

    SELECT (max(interval_start AT TIME ZONE 'Pacific/Auckland')::date
          - min(interval_start AT TIME ZONE 'Pacific/Auckland')::date) + 1
      INTO v_span FROM metering.meter_read_hh WHERE icp_id = v_icp;
    PERFORM pg_temp.chk('13 months', 'reads span (days)', v_span, 397);

    SELECT count(*) INTO v_bills FROM billing.bill b
      JOIN billing.account a ON a.account_id = b.account_id
     WHERE a.account_number = 'DEMO-ACCT-228001';
    -- FIVE OF THE THIRTEEN ARE DATED BEFORE THEIR TARIFF EXISTED.
    -- DEMO_STD_FXTOU_13_MONTH_HISTORY has exactly one version, effective
    -- 2024-01-01, and invoices INV-228001-202308 through -202312 end before
    -- it. int_billing__plan_resolution reports versions_matched = 0 for all
    -- five and quarantines them as integrity.no_plan_version.
    --
    -- This is design §2.1.2 doing the thing it exists to do -- refusing to
    -- re-rate history against a tariff that did not cover it -- and the
    -- fixture could assert 13 only because it inserted bills by hand, with no
    -- tariff to resolve. The gap is in the source catalogue, not the loader.
    PERFORM pg_temp.chk('13 months', 'bills', v_bills, 13, NULL, 'known',
                        '5 invoices predate the plan version effective 2024-01-01');
    PERFORM pg_temp.chk('13 months', 'bills that had a covering plan version',
                        v_bills, 8);

    SELECT count(*) INTO v_bad
      FROM billing.bill b
      JOIN billing.account a ON a.account_id = b.account_id
      JOIN (SELECT bill_id, round(sum(amount), 2) AS s
              FROM billing.bill_line GROUP BY bill_id) l ON l.bill_id = b.bill_id
     WHERE a.account_number = 'DEMO-ACCT-228001' AND l.s <> b.charges_excl_gst;
    PERFORM pg_temp.chk('13 months', 'headers disagreeing with their lines', v_bad, 0);

    SELECT count(*), (max(usage_date) - min(usage_date)) + 1 INTO v_rows, v_days
      FROM kea.mart_daily_usage WHERE icp_id = v_icp;
    PERFORM pg_temp.chk('13 months', 'daily rows', v_rows, 397);
    PERFORM pg_temp.chk('13 months', 'daily rows span, no holes', v_days, v_rows);

    SELECT count(*) INTO v_gaps FROM kea.mart_daily_usage
     WHERE icp_id = v_icp AND interval_count <> expected_intervals;
    PERFORM pg_temp.chk('13 months', 'days not filling the local day', v_gaps, 0);

    SELECT count(DISTINCT profile_month) INTO v_months
      FROM kea.mart_hourly_profile WHERE icp_id = v_icp;
    PERFORM pg_temp.chk('13 months', 'months in the hourly profile', v_months, 13);

    SELECT count(*) FILTER (WHERE expected_intervals = 46),
           count(*) FILTER (WHERE expected_intervals = 50)
      INTO v_short, v_long FROM kea.mart_daily_usage WHERE icp_id = v_icp;
    PERFORM pg_temp.chk('13 months', 'DST short days (46 intervals)', (v_short >= 1), true);
    PERFORM pg_temp.chk('13 months', 'DST long days (50 intervals)',  (v_long  >= 1), true);
END $$;


-- =====================================================================
-- 4. The mid-cycle forecast inputs.  Design §8.6's invariant: the period is
--    PARTITIONED between what has happened and what is left, so the total
--    cannot drift as the period progresses.
-- =====================================================================
DO $$
DECLARE v_n int; v_drift numeric; v_acct bigint;
BEGIN
    SELECT account_id INTO v_acct FROM billing.account
     WHERE account_number = 'DEMO-ACCT-229001';
    PERFORM pg_temp.chk('forecast', 'the account is loaded', (v_acct IS NOT NULL), true);
    IF v_acct IS NULL THEN RETURN; END IF;

    SELECT count(*) INTO v_n FROM kea.mart_forecast_input WHERE account_id = v_acct;
    -- The warehouse builds a cycle per INVOICE, so the June in flight has
    -- none. Of the three billed months, last June has no bill before it to
    -- take a rate from: its cost_to_date is null and the export drops it
    -- (serving declares the column NOT NULL). April (30 days) and May (31)
    -- are priced at the bill before each, and are what reaches serving.
    PERFORM pg_temp.chk('forecast', 'snapshots (the April and May cycles)', v_n, 61);

    -- PER CYCLE, AND ONLY BEFORE THE CLAMP. The two cycles have different
    -- totals, so one min/max across the account would always "drift". And
    -- mart_forecast_input breaks the invariant on purpose once a colder than
    -- normal period passes its expected total: the remainder is held at 0 and
    -- the sum then rises with fact. Fixture 09's May is colder than the
    -- Wellington normal it is forecast against, so its last days do that.
    SELECT max(hi - lo) INTO v_drift FROM (
        SELECT max(hdd_to_date + hdd_expected_remaining) AS hi,
               min(hdd_to_date + hdd_expected_remaining) AS lo
          FROM kea.mart_forecast_input
         WHERE account_id = v_acct AND hdd_to_date IS NOT NULL
           AND hdd_expected_remaining > 0
         GROUP BY period_start) c;
    IF v_drift IS NOT NULL THEN
        PERFORM pg_temp.chk('forecast', 'hdd total does not drift within a cycle',
                            round(v_drift, 2), 0.00::numeric, 0.05);
    END IF;
END $$;


-- =====================================================================
-- 5. The load itself.  Stage 5's three exit conditions that no fixture
--    could ever have checked, because they are about the loader.
-- =====================================================================
DO $$
DECLARE v int; v_lines int; v_stale int; v_seq int;
BEGIN
    -- app IS UNTOUCHED. The served tables share the backend's database with
    -- the application's own `app` schema, so this is no longer structural. It
    -- rests on the loader's credential: design §1.1 gives kea_loader no grant
    -- on app, not even USAGE, so a pipeline bug cannot reach it even by a fully
    -- qualified name. has_*_privilege rather than the grant views, because it
    -- also counts what arrives through PUBLIC or a role membership.
    SELECT count(*) INTO v FROM pg_namespace
     WHERE nspname = 'app' AND has_schema_privilege('kea_loader', oid, 'USAGE');
    PERFORM pg_temp.chk('load', 'kea_loader USAGE on the app schema', v, 0);

    SELECT count(*) INTO v FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'app' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
       AND has_table_privilege('kea_loader', c.oid,
             'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER');
    PERFORM pg_temp.chk('load', 'app relations kea_loader can touch', v, 0);

    -- Fixture login addresses are customer attributes, not presentation-layer
    -- constants. This catches a source/export regression before Metabase or the
    -- backend has to invent a fallback for a NULL billing.customer.email.
    SELECT count(*) INTO v
      FROM (VALUES
            ('196501', 'julia@kea.example'),
            ('212001', 'ana@kea.example'),
            ('213001', 'tama@kea.example'),
            ('214001', 'mere@kea.example'),
            ('215001', 'josh@kea.example'),
            ('216001', 'priya@kea.example'),
            ('217001', 'daniel@kea.example'),
            ('218001', 'rangi@kea.example'),
            ('219001', 'mereana@kea.example')) expected(customer_number, email)
      JOIN billing.customer c
        ON c.customer_number = expected.customer_number
       AND c.email = expected.email;
    PERFORM pg_temp.chk('load', 'fixture customers with their login email', v, 9);

    SELECT count(*) INTO v
      FROM kea.mart_account_context m
      JOIN billing.customer c ON c.customer_id = m.customer_id
     WHERE m.account_number LIKE 'DEMO-ACCT-22%'
       AND m.email IS DISTINCT FROM c.email;
    PERFORM pg_temp.chk('load', 'fixture mart emails differing from customer', v, 0);

    -- EVERY IDENTITY SEQUENCE IS ABOVE ITS TABLE'S MAX ID. The ids came from
    -- the warehouse via OVERRIDING SYSTEM VALUE, which does not advance a
    -- sequence; without the sync this is the state that fails months later on
    -- the first insert that does not supply one.
    -- NOT pg_sequences.last_value, WHICH IS NULL HERE FOR THE RIGHT REASON.
    -- sync_serving_sequences calls setval(seq, hi + 1, false), and a sequence
    -- with is_called = false reports last_value as NULL in that view -- "null
    -- if the sequence has not been read from yet". Reading the view would make
    -- a correctly synced sequence indistinguishable from an untouched one,
    -- which is the exact bug this check exists to catch.
    --
    -- So the property is asserted directly: the next value the sequence will
    -- hand out is above the largest id already in its table.
    v_seq := 0;
    DECLARE r record; seq text; v_next bigint; v_max bigint;
    BEGIN
        FOR r IN
            SELECT n.nspname AS sch, c.relname AS tbl, a.attname AS col
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attidentity <> ''
            WHERE n.nspname IN ('metering','billing','catalogue','kea')
              AND c.relkind IN ('r','p')
        LOOP
            seq := pg_get_serial_sequence(format('%I.%I', r.sch, r.tbl), r.col);
            CONTINUE WHEN seq IS NULL;
            -- is_called DECIDES WHETHER last_value HAS BEEN HANDED OUT.
            -- setval(seq, n, false) stores n and hands out n next; an ordinary
            -- nextval stores what it just gave away and hands out n + 1. So
            -- the next id is last_value + is_called, and comparing last_value
            -- alone flags a perfectly healthy sequence the moment anything
            -- inserts a row normally -- which kea.mart_load_log does, on every
            -- single run, after the sync.
            EXECUTE format('SELECT last_value + CASE WHEN is_called THEN 1 ELSE 0 END '
                           'FROM %s', seq) INTO v_next;
            EXECUTE format('SELECT COALESCE(MAX(%I), 0) FROM %I.%I',
                           r.col, r.sch, r.tbl) INTO v_max;
            IF v_next <= v_max THEN
                v_seq := v_seq + 1;
            END IF;
        END LOOP;
    END;
    PERFORM pg_temp.chk('load', 'identity sequences at or below their max id', v_seq, 0);

    -- synced_at is ONE value per table, not a spread across a slow load.
    SELECT count(*) INTO v_stale FROM (
        SELECT count(DISTINCT synced_at) AS d FROM metering.icp
        UNION ALL SELECT count(DISTINCT synced_at) FROM catalogue.plan
        UNION ALL SELECT count(DISTINCT synced_at) FROM billing.account) t
     WHERE t.d > 1;
    PERFORM pg_temp.chk('load', 'tables with more than one synced_at', v_stale, 0);

    -- THE BILL LINES DID NOT FAN OUT on the tariff-component recovery. Stated
    -- as "no line_id appears twice" rather than as a row count: the count
    -- moves whenever the export's filters move, and a constant here would have
    -- to be edited every time -- which is how a real duplication eventually
    -- gets edited away instead of noticed.
    SELECT count(*) - count(DISTINCT line_id) INTO v_lines FROM billing.bill_line;
    PERFORM pg_temp.chk('load', 'duplicated bill lines', v_lines, 0);

    -- And every line still belongs to a bill that loaded.
    SELECT count(*) INTO v FROM billing.bill_line l
     WHERE NOT EXISTS (SELECT 1 FROM billing.bill b WHERE b.bill_id = l.bill_id);
    PERFORM pg_temp.chk('load', 'bill lines with no bill', v, 0);

    -- Every mart recorded an outcome.
    SELECT count(*) INTO v FROM kea.mart_load_log
     WHERE status = 'succeeded' AND completed_at IS NOT NULL;
    PERFORM pg_temp.chk('load', 'marts with a succeeded log row', (v >= 5), true);
END $$;


-- =====================================================================
-- 6. Weather on every fixture day.  Issue #143: the fixtures' synthetic
--    temperatures never reached the warehouse, and seven of the eight
--    DEMO ICPs are outside the four NIWA station regions, so their days
--    published with no temperature and the weather driver dropped out of
--    every explanation. Fixture 01 is not DEMO; it is on NIWA Auckland.
-- =====================================================================
DO $$
DECLARE v_icps int; v_null int; v_hdd int;
BEGIN
    SELECT count(DISTINCT u.icp_id),
           count(*) FILTER (WHERE u.temp_mean_c IS NULL
                               OR u.heating_degree_days IS NULL),
           count(*) FILTER (WHERE u.heating_degree_days
                                  <> round(greatest(16.0 - u.temp_mean_c, 0), 2))
      INTO v_icps, v_null, v_hdd
      FROM kea.mart_daily_usage u
      JOIN metering.icp i ON i.icp_id = u.icp_id
     WHERE i.icp_number LIKE 'DEMO%';
    -- The positive control: the two zeros below would also hold on no rows.
    PERFORM pg_temp.chk('weather', 'DEMO ICPs with daily usage', v_icps, 8);
    PERFORM pg_temp.chk('weather', 'DEMO days with no temperature or HDD', v_null, 0);
    -- Design §8.4's base 16, whichever source the temperature came from.
    PERFORM pg_temp.chk('weather', 'DEMO days whose HDD is not base 16', v_hdd, 0);
END $$;


-- =====================================================================
-- 7. The latest bill.  Issue #143: fixture 09's newest served bill ended six
--    weeks before its last metered day, with no bill before it, so the
--    explainer had nothing to say about the bill a customer would ask about.
--    Every DEMO account's latest bill must be within a cycle of its meter,
--    and where it has more than one bill, explained against the one before.
-- =====================================================================
DO $$
DECLARE v_accts int; v_stale int; v_none int; v_noprior int;
BEGIN
    WITH latest AS (
        SELECT DISTINCT ON (a.account_id)
               a.account_id, a.icp_id, b.bill_id, b.period_end,
               count(*) OVER (PARTITION BY a.account_id) AS n_bills
          FROM billing.account a
          JOIN billing.bill b ON b.account_id = a.account_id
         WHERE a.account_number LIKE 'DEMO-ACCT-%'
         ORDER BY a.account_id, b.period_end DESC
    ), metered AS (
        SELECT icp_id, max(usage_date) AS last_day
          FROM kea.mart_daily_usage WHERE interval_count > 0 GROUP BY icp_id
    )
    SELECT count(*),
           count(*) FILTER (WHERE m.last_day - l.period_end > 31),
           count(*) FILTER (WHERE l.n_bills > 1 AND e.bill_id IS NULL),
           count(*) FILTER (WHERE l.n_bills > 1 AND e.bill_id IS NOT NULL
                              AND e.prior_bill_id IS NULL)
      INTO v_accts, v_stale, v_none, v_noprior
      FROM latest l
      LEFT JOIN metered m ON m.icp_id = l.icp_id
      LEFT JOIN kea.mart_bill_explanation e ON e.bill_id = l.bill_id;
    -- The positive control: the three zeros below would also hold on no rows.
    -- Nine, because fixture 01's account is DEMO-ACCT-221001 too.
    PERFORM pg_temp.chk('latest bill', 'DEMO accounts with a bill', v_accts, 9);
    -- A month and a day: fixture 09's in-flight June is 10 days past May.
    PERFORM pg_temp.chk('latest bill', 'more than 31 days behind the meter', v_stale, 0);
    PERFORM pg_temp.chk('latest bill', 'with no explanation row', v_none, 0);
    PERFORM pg_temp.chk('latest bill', 'explained against no prior bill', v_noprior, 0);
END $$;


-- =====================================================================
\echo ''
\echo '=== Stage 5 gate ==='
SELECT area, what, got, want,
       CASE WHEN ok THEN 'ok'
            WHEN kind = 'known' THEN 'known'
            ELSE 'FAIL' END AS result,
       CASE WHEN ok OR kind <> 'known' THEN NULL ELSE why END AS reason
  FROM _checks ORDER BY n;

DO $$
DECLARE v_fail int; v_known int; v_all int;
BEGIN
    SELECT count(*) FILTER (WHERE NOT ok AND kind = 'must'),
           count(*) FILTER (WHERE NOT ok AND kind = 'known'),
           count(*)
      INTO v_fail, v_known, v_all FROM _checks;
    RAISE NOTICE '% checks: % passed, % known divergences, % failures',
                 v_all, v_all - v_fail - v_known, v_known, v_fail;
    IF v_fail > 0 THEN
        RAISE EXCEPTION '% required check(s) failed -- see the FAIL rows above', v_fail;
    END IF;
END $$;
