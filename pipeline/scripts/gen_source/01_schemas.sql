-- =====================================================================
-- gen_audit -- everything the generator needs to record about itself.
--
-- Deliberately NOT a src_* schema. kea_source_schema.sql ends with
--
--     ALTER DEFAULT PRIVILEGES IN SCHEMA src_metering ... GRANT SELECT
--
-- so any table created in a src_* schema becomes readable by the extract
-- roles automatically. A defect manifest sitting there would be pulled
-- into bronze on the next run and the pipeline would be marking its own
-- homework -- it could "detect" every injected defect by reading the list.
--
-- Staging tables live here too, for the same reason: they hold the same
-- rows as src_metering and would otherwise be extracted twice.
--
-- No role is granted anything on this schema.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS gen_audit;

REVOKE ALL ON SCHEMA gen_audit FROM PUBLIC;

DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['kea_extract_billing',
                             'kea_extract_metering',
                             'kea_extract_catalogue']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('REVOKE ALL ON SCHEMA gen_audit FROM %I', r);
        END IF;
    END LOOP;
END $$;

-- What ran, when, and against which inputs. One row per generator run,
-- so a surprising number in the warehouse can be traced to the run that
-- produced it rather than guessed at.
CREATE TABLE IF NOT EXISTS gen_audit.runs (
    run_id       integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at   timestamptz NOT NULL DEFAULT now(),
    cohort_seed  text,
    cohort_size  integer,
    shift_days   integer,
    nz_span_from date,
    nz_span_to   date,
    scale_factor numeric(8,6),
    notes        text
);

-- Ground truth for the injected defects. The pipeline must find these
-- WITHOUT reading this table; it exists so a test can ask "did you find
-- what was actually there", not so the pipeline can look up the answer.
CREATE TABLE IF NOT EXISTS gen_audit.defects (
    defect_id      integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id         integer REFERENCES gen_audit.runs(run_id),
    defect_kind    text NOT NULL,
    icp_identifier text,
    register_code  text,
    nz_date        date,
    interval_start timestamptz,
    detail         jsonb
);

CREATE INDEX IF NOT EXISTS ix_defects_kind ON gen_audit.defects(defect_kind);

-- Staging. Business keys only: the source schema's ids are all
-- GENERATED ALWAYS AS IDENTITY, and resolving a business key to a local
-- id is exactly the work the extract has to do downstream.
CREATE TABLE IF NOT EXISTS gen_audit.stg_networks (
    network_code text, network_name text, region text
);

CREATE TABLE IF NOT EXISTS gen_audit.stg_connection_points (
    icp_identifier text, network_code text, address_full text, suburb text,
    city text, postcode text, climate_region text,
    latitude numeric(9,6), longitude numeric(9,6), connection_date date
);

CREATE TABLE IF NOT EXISTS gen_audit.stg_meters (
    icp_identifier text, serial_number text, is_smart boolean, installed_on date
);

CREATE TABLE IF NOT EXISTS gen_audit.stg_registers (
    icp_identifier text, serial_number text, register_code text,
    description text, is_controlled boolean, flow_direction text
);

CREATE TABLE IF NOT EXISTS gen_audit.stg_customers (
    cust_no text, cust_name text, email_addr text, phone_no text,
    addr_line1 text, addr_line2 text, addr_suburb text, addr_city text,
    addr_postcode text, med_dependent text, vulnerable_flag text
);

CREATE TABLE IF NOT EXISTS gen_audit.stg_accounts (
    acct_no text, cust_no text, icp_number text, plan_code text,
    pay_method text, bill_cycle_day smallint, start_dt date
);

CREATE TABLE IF NOT EXISTS gen_audit.stg_invoice_periods (
    inv_no text, acct_no text, plan_code text,
    period_from date, period_to date, issue_dt date, due_dt date
);


-- ---------------------------------------------------------------------
-- Identity sequences vs. the fixtures' hand-assigned ids.
--
-- Every id in kea_source_schema.sql is GENERATED ALWAYS AS IDENTITY, and
-- all nine fixtures insert explicit values with OVERRIDING SYSTEM VALUE
-- (network 111001, connection point 131001, invoice line 241001, plan
-- 311001, and so on). OVERRIDING SYSTEM VALUE does NOT advance the
-- sequence. So the sequence still sits near 1 while rows exist far above
-- it, and any generated INSERT eventually climbs into a fixture's id and
-- dies on the primary key.
--
-- This is a TIME BOMB, not a constant failure. The first generator run
-- inserted 116,144 invoice lines without complaint -- it left the
-- sequence around 116,000, below the fixtures' 241,001. Deleting those
-- rows, re-seeding the fixtures and running again put the sequence on a
-- collision course, and the second run failed on line_id 241001. The
-- same code, the same data, a different outcome depending on history.
--
-- Called before every generated insert.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION gen_audit.sync_sequences()
RETURNS void LANGUAGE plpgsql AS $$
DECLARE r record; seq text; hi bigint;
BEGIN
    FOR r IN
        SELECT n.nspname AS sch, c.relname AS tbl, a.attname AS col
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attidentity <> ''
        WHERE n.nspname LIKE 'src\_%'
    LOOP
        seq := pg_get_serial_sequence(format('%I.%I', r.sch, r.tbl), r.col);
        CONTINUE WHEN seq IS NULL;
        EXECUTE format('SELECT COALESCE(MAX(%I), 0) FROM %I.%I', r.col, r.sch, r.tbl)
           INTO hi;
        -- is_called = false, so the NEXT value handed out is hi + 1.
        PERFORM setval(seq, hi + 1, false);
    END LOOP;
END $$;


-- ---------------------------------------------------------------------
-- Deterministic per-row draw, replacing random().
--
-- setseed() fixes the random STREAM, not which row draws from it.
-- random() is evaluated per row in scan order, and Postgres may
-- parallelise a sequential scan over 19 M rows -- each worker then draws
-- from its own state. Two runs of 05_defects.sql on identical input
-- produced 359,160 and 359,377 revised rows: statistically the same,
-- never the same twice.
--
-- Hashing the row's OWN BUSINESS KEY makes the draw a property of the
-- row rather than of the plan. Scan order, parallelism and worker count
-- stop mattering. This is the technique cohort.py already uses to pick
-- households; this is its SQL equivalent.
--
-- Returns [0, 1). IMMUTABLE and PARALLEL SAFE, so it can be used freely
-- in a WHERE clause without forcing a serial plan.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION gen_audit.h(k text)
RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT ('x' || substr(md5(k), 1, 7))::bit(28)::int / 268435456.0 $$;
