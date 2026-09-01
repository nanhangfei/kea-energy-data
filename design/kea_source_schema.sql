-- =====================================================================
-- Kea Energy — SOURCE SCHEMA (targets PostgreSQL 17; developed against 16)
-- File 1 of 3. See kea-data-platform-design.md for rationale.
--
--   src_billing     the billing system
--   src_metering    the half-hourly meter-data feed
--   src_catalogue   the plan catalogue
--
-- Three schemas in one database for development convenience; the
-- pipeline must still treat them as separate systems. No cross-system
-- foreign keys, local surrogate keys, inconsistent naming — all
-- deliberate. Scoped to Sprint 2; src_billing.payments is commented out.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS src_billing;
CREATE SCHEMA IF NOT EXISTS src_metering;
CREATE SCHEMA IF NOT EXISTS src_catalogue;


-- =====================================================================
-- SYSTEM 1 — BILLING
--
-- The oldest of the three. Uppercase legacy column names, VARCHAR
-- everywhere, dates as timestamps. Holds the commercial relationship
-- and produces bills; knows nothing about interval data.
-- =====================================================================

CREATE TABLE src_billing.customers (
    CUST_ID         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The business key. This is what appears on the bill (196501) and
    -- the only customer identifier any other system could reference.
    CUST_NO         varchar(20)  NOT NULL UNIQUE,
    CUST_NAME       varchar(200) NOT NULL,
    EMAIL_ADDR      varchar(200),
    PHONE_NO        varchar(50),
    -- Address lives in billing, not metering, in this system.
    ADDR_LINE1      varchar(200),
    ADDR_LINE2      varchar(200),
    ADDR_SUBURB     varchar(100),
    ADDR_CITY       varchar(100),
    ADDR_POSTCODE   varchar(10),
    -- Consumer Care flags. Y/N rather than boolean — legacy system.
    MED_DEPENDENT   char(1)      NOT NULL DEFAULT 'N',
    VULNERABLE_FLAG char(1)      NOT NULL DEFAULT 'N',
    CREATED_TS      timestamp    NOT NULL DEFAULT now(),
    LAST_MOD_TS     timestamp    NOT NULL DEFAULT now(),
    CONSTRAINT ck_med_dep CHECK (MED_DEPENDENT IN ('Y','N')),
    CONSTRAINT ck_vuln    CHECK (VULNERABLE_FLAG IN ('Y','N'))
);

CREATE TABLE src_billing.accounts (
    ACCT_ID         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ACCT_NO         varchar(20)  NOT NULL UNIQUE,
    -- Within-system FK: allowed, same database.
    CUST_ID         integer      NOT NULL REFERENCES src_billing.customers(CUST_ID),

    -- ===== CROSS-SYSTEM REFERENCES — TEXT, NO FK POSSIBLE =====
    -- These are the seams the warehouse has to stitch. The metering
    -- database's icp_id and the catalogue's plan_id do not exist here,
    -- so billing stores the business keys and hopes they resolve.
    -- They may not: see the orphan handling in the pipeline design.
    ICP_NUMBER      varchar(20)  NOT NULL,
    PLAN_CODE       varchar(60)  NOT NULL,
    -- ==========================================================

    PAY_METHOD      varchar(20)  NOT NULL DEFAULT 'DD',   -- DD | BT | CARD | OTHER
    BILL_CYCLE_DAY  smallint     NOT NULL,
    START_DT        timestamp    NOT NULL,
    END_DT          timestamp,
    LAST_MOD_TS     timestamp    NOT NULL DEFAULT now(),
    CONSTRAINT ck_cycle_day CHECK (BILL_CYCLE_DAY BETWEEN 1 AND 28)
);

CREATE INDEX ix_acct_cust ON src_billing.accounts(CUST_ID);
CREATE INDEX ix_acct_icp  ON src_billing.accounts(ICP_NUMBER);

CREATE TABLE src_billing.invoices (
    INV_ID          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    INV_NO          varchar(20)  NOT NULL UNIQUE,           -- '0000776126'
    ACCT_ID         integer      NOT NULL REFERENCES src_billing.accounts(ACCT_ID),
    -- Which plan was actually rated. Text again — catalogue is elsewhere.
    PLAN_CODE       varchar(60)  NOT NULL,
    ISSUE_DT        timestamp    NOT NULL,
    PERIOD_FROM     timestamp    NOT NULL,
    PERIOD_TO       timestamp    NOT NULL,
    -- Note: no period_days column. The source system does not compute
    -- it; the warehouse derives it. This is the 43-day move-in bill
    -- trap, and it is invisible in the source.
    OPEN_BAL        numeric(12,2) NOT NULL DEFAULT 0,
    PAYMENTS_APPL   numeric(12,2) NOT NULL DEFAULT 0,
    BAL_BROUGHT_FWD numeric(12,2) NOT NULL DEFAULT 0,
    CHARGES_EXGST   numeric(12,2) NOT NULL,
    GST_RATE        numeric(5,4)  NOT NULL DEFAULT 0.15,
    GST_AMT         numeric(12,2) NOT NULL,
    TOTAL_AMT       numeric(12,2) NOT NULL,
    DUE_DT          timestamp    NOT NULL,
    LATE_FEE        numeric(12,2) NOT NULL DEFAULT 0,
    TOTAL_IF_LATE   numeric(12,2),
    PPD_AMT         numeric(12,2) NOT NULL DEFAULT 0,       -- prompt payment discount
    TOTAL_IMPORT_KWH numeric(12,4),
    TOTAL_EXPORT_KWH numeric(12,4) NOT NULL DEFAULT 0,
    EXPORT_CREDIT   numeric(12,2) NOT NULL DEFAULT 0,
    INV_STATUS      varchar(20)  NOT NULL DEFAULT 'ISSUED',
    LAST_MOD_TS     timestamp    NOT NULL DEFAULT now()
);

CREATE INDEX ix_inv_acct   ON src_billing.invoices(ACCT_ID, PERIOD_TO DESC);
CREATE INDEX ix_inv_issue  ON src_billing.invoices(ISSUE_DT);  -- extract watermark

CREATE TABLE src_billing.invoice_lines (
    LINE_ID         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    INV_ID          integer      NOT NULL REFERENCES src_billing.invoices(INV_ID),
    -- The charge group as a code, not an enum. 'NET' | 'ENE' | 'OTH'.
    CHG_GROUP       varchar(10)  NOT NULL,
    FLOW_DIR        varchar(10)  NOT NULL DEFAULT 'IMPORT',  -- IMPORT | EXPORT
    LINE_DESC       varchar(200) NOT NULL,      -- 'Peak Usage', as printed
    TOU_BAND        varchar(30),                -- free text: 'PEAK','SHOULDER','OFFPEAK'
    QTY             numeric(12,4) NOT NULL,     -- metered volume, or days
    UOM             varchar(10)  NOT NULL,      -- 'KWH' | 'DAY'
    UNIT_RATE       numeric(12,6) NOT NULL,
    LOSS_QTY        numeric(12,4),              -- line losses, where applicable
    LINE_AMT        numeric(12,2) NOT NULL,     -- signed: negative for export
    SORT_SEQ        smallint     NOT NULL DEFAULT 0,
    CONSTRAINT ck_chg_group CHECK (CHG_GROUP IN ('NET','ENE','OTH')),
    CONSTRAINT ck_flow_dir  CHECK (FLOW_DIR IN ('IMPORT','EXPORT')),
    CONSTRAINT ck_uom       CHECK (UOM IN ('KWH','DAY'))
);

CREATE INDEX ix_line_inv ON src_billing.invoice_lines(INV_ID, SORT_SEQ);

-- DEFERRED — src_billing.payments
--
-- The billing system holds individual payment transactions, but nothing
-- in Sprint 2 reads them. The bill explainer needs only the single
-- "Payment Received" figure, which is already on invoices.PAYMENTS_APPL.
--
-- Transaction detail is what hardship triage runs on: late-payment
-- streaks, failed direct debits, part payments, arrears trajectory. Add
-- this table when building that (Sprint 4), together with
-- billing.payment in kea_deferred_schema.sql.
--
-- CREATE TABLE src_billing.payments (
--     PAY_ID          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
--     ACCT_ID         integer      NOT NULL REFERENCES src_billing.accounts(ACCT_ID),
--     INV_ID          integer      REFERENCES src_billing.invoices(INV_ID),
--     PAID_TS         timestamp    NOT NULL,
--     PAY_AMT         numeric(12,2) NOT NULL,
--     PAY_METHOD      varchar(20)  NOT NULL,
--     FAILED_FLAG     char(1)      NOT NULL DEFAULT 'N',
--     FAIL_REASON     varchar(200),
--     CONSTRAINT ck_pay_failed CHECK (FAILED_FLAG IN ('Y','N'))
-- );
--
-- CREATE INDEX ix_pay_acct ON src_billing.payments(ACCT_ID, PAID_TS DESC);


-- =====================================================================
-- SYSTEM 2 — METERING (half-hourly meter-data feed)
--
-- Newer than billing. snake_case, proper timestamps. Knows about
-- physical connections and readings; knows nothing about customers,
-- accounts or money.
--
-- Note it has no customer_id anywhere. The link between a person and a
-- connection exists ONLY in the billing system, via ICP_NUMBER. That
-- asymmetry is the integration problem in one sentence.
-- =====================================================================

CREATE TABLE src_metering.networks (
    network_id      integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    network_code    text NOT NULL UNIQUE,       -- 'VECT'
    network_name    text NOT NULL,
    region          text
);

CREATE TABLE src_metering.connection_points (
    -- LOCAL surrogate key. Unrelated to anything in billing.
    icp_id              integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The business key, and the ONLY thing billing can reference.
    icp_identifier      text NOT NULL UNIQUE,   -- '0160647002LCFCF'
    network_id          integer NOT NULL REFERENCES src_metering.networks(network_id),
    -- Address again, in a different shape from billing's. The two may
    -- disagree; the registry (this system) is authoritative for NZ.
    address_full        text,
    suburb              text,
    city                text,
    postcode            text,
    -- Climate region for the weather join. Present here, absent in
    -- billing, which is why the warehouse sources it from metering.
    climate_region      text,
    latitude            numeric(9,6),
    longitude           numeric(9,6),
    connection_date     date,
    disconnection_date  date,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_cp_network ON src_metering.connection_points(network_id);

CREATE TABLE src_metering.meters (
    meter_id        integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    icp_id          integer NOT NULL REFERENCES src_metering.connection_points(icp_id),
    serial_number   text    NOT NULL,
    is_smart        boolean NOT NULL DEFAULT true,
    installed_on    date,
    removed_on      date
);

CREATE TABLE src_metering.registers (
    register_id     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    meter_id        integer NOT NULL REFERENCES src_metering.meters(meter_id),
    -- Register code is unique per meter, not globally.
    register_code   text    NOT NULL,           -- 'UN' | 'CN'
    description     text,
    is_controlled   boolean NOT NULL DEFAULT false,
    flow_direction  text    NOT NULL DEFAULT 'import',
    UNIQUE (meter_id, register_code),
    CONSTRAINT ck_flow CHECK (flow_direction IN ('import','export'))
);

CREATE INDEX ix_reg_meter ON src_metering.registers(meter_id);

-- The feed itself. Reads arrive keyed by BUSINESS keys, not by the
-- register_id above — because the feed originates outside this database
-- (from metering equipment providers) and cannot know local ids.
-- Resolving these to register_id is work the extract must do.
CREATE TABLE src_metering.interval_reads (
    read_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    icp_identifier  text        NOT NULL,       -- business key, not icp_id
    register_code   text        NOT NULL,       -- business key, not register_id
    interval_start  timestamptz NOT NULL,
    kwh             numeric(10,4) NOT NULL,
    -- Read status, EIEP-style single characters.
    read_status     char(1)     NOT NULL DEFAULT 'A',  -- A actual, E estimated, F final
    -- Revisions: the same interval can be re-issued. The extract's
    -- lookback window exists because of this column.
    version_no      integer     NOT NULL DEFAULT 1,
    received_at     timestamptz NOT NULL DEFAULT now(),
    source_file     text,
    CONSTRAINT ck_read_status CHECK (read_status IN ('A','E','F')),
    -- Deliberately NOT unique on (icp, register, interval): revisions
    -- coexist, and picking the latest version is the warehouse's job.
    CONSTRAINT ck_kwh_non_negative CHECK (kwh >= 0)
);

CREATE INDEX ix_reads_lookup   ON src_metering.interval_reads(icp_identifier, register_code, interval_start);
CREATE INDEX ix_reads_received ON src_metering.interval_reads(received_at);  -- extract watermark


-- =====================================================================
-- SYSTEM 3 — PLAN CATALOGUE
--
-- The smallest and simplest. Product/pricing owns it. Versioned by
-- effective date, because a rate change must never overwrite history —
-- doing so would make every historical bill unreproducible.
-- =====================================================================

CREATE TABLE src_catalogue.price_plans (
    plan_id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Business key. What billing stores as PLAN_CODE.
    code            text    NOT NULL,           -- 'VECT_OFPK_STD_140520'
    name            text    NOT NULL,
    -- Network as a CODE, not an id: the catalogue does not have
    -- metering's network_id either. Another seam.
    network_code    text    NOT NULL,           -- 'VECT'
    user_category   text    NOT NULL,           -- 'LOW' | 'STANDARD'
    is_tou          boolean NOT NULL DEFAULT false,
    ppd_percent     numeric(5,4) NOT NULL DEFAULT 0,
    valid_from      date    NOT NULL,
    valid_to        date,
    active          boolean NOT NULL DEFAULT true,
    UNIQUE (code, valid_from),
    CONSTRAINT ck_user_cat CHECK (user_category IN ('LOW','STANDARD'))
);

-- TOU windows. Note charge_scope: bands differ between network and
-- energy charges within one plan — verified against a real bill, where
-- network split consumption into 2 bands and energy into 3.
CREATE TABLE src_catalogue.tou_windows (
    window_id       integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plan_id         integer NOT NULL REFERENCES src_catalogue.price_plans(plan_id),
    charge_scope    text    NOT NULL,           -- 'NET' | 'ENE' | 'OTH'
    band_label      text    NOT NULL,           -- 'PEAK' | 'SHOULDER' | 'OFFPEAK'
    -- Days as a comma-separated string, not an array. Legacy shape the
    -- staging layer has to parse. This is representative: source
    -- systems rarely hand you clean types.
    days_csv        text    NOT NULL,           -- '1,2,3,4,5'
    time_from       time    NOT NULL,
    time_to         time    NOT NULL,
    months_csv      text,                       -- NULL = all year
    CONSTRAINT ck_scope CHECK (charge_scope IN ('NET','ENE','OTH'))
);

CREATE INDEX ix_tou_plan ON src_catalogue.tou_windows(plan_id, charge_scope);

CREATE TABLE src_catalogue.rate_components (
    component_id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plan_id         integer NOT NULL REFERENCES src_catalogue.price_plans(plan_id),
    charge_scope    text    NOT NULL,           -- 'NET' | 'ENE' | 'OTH'
    component_desc  text    NOT NULL,           -- 'Peak Usage', as printed
    rate_uom        text    NOT NULL,           -- 'KWH' | 'DAY'
    window_id       integer REFERENCES src_catalogue.tou_windows(window_id),
    flow_direction  text    NOT NULL DEFAULT 'import',
    unit_rate       numeric(12,6) NOT NULL,
    controlled_only boolean NOT NULL DEFAULT false,
    valid_from      date    NOT NULL,
    valid_to        date,
    CONSTRAINT ck_rate_scope CHECK (charge_scope IN ('NET','ENE','OTH')),
    CONSTRAINT ck_rate_uom   CHECK (rate_uom IN ('KWH','DAY')),
    CONSTRAINT ck_rate_flow  CHECK (flow_direction IN ('import','export'))
);

CREATE INDEX ix_rate_plan ON src_catalogue.rate_components(plan_id, valid_from DESC);


-- =====================================================================
-- EXTRACT ACCESS
--
-- One read-only role per system. In production these are three
-- credentials against three servers; here they are three grants. The
-- pipeline should use them separately, so that a mistake which joins
-- across systems at extract time fails rather than silently working.
-- =====================================================================

-- Guarded so the file is re-runnable: CREATE ROLE has no IF NOT EXISTS.
-- Created without a password; set it out of band from the secret store
--   ALTER ROLE kea_extract_billing PASSWORD '...';
-- so no credential ever lands in this repo.
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['kea_extract_billing',
                             'kea_extract_metering',
                             'kea_extract_catalogue']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I LOGIN', r);
        END IF;
    END LOOP;
END
$$;

GRANT USAGE ON SCHEMA src_billing   TO kea_extract_billing;
GRANT USAGE ON SCHEMA src_metering  TO kea_extract_metering;
GRANT USAGE ON SCHEMA src_catalogue TO kea_extract_catalogue;

GRANT SELECT ON ALL TABLES IN SCHEMA src_billing   TO kea_extract_billing;
GRANT SELECT ON ALL TABLES IN SCHEMA src_metering  TO kea_extract_metering;
GRANT SELECT ON ALL TABLES IN SCHEMA src_catalogue TO kea_extract_catalogue;

-- ON ALL TABLES covers only what exists right now. Without these, any table
-- added later is invisible to the extract role until someone re-runs the
-- grant -- which shows up as an empty extract, not as an error.
ALTER DEFAULT PRIVILEGES IN SCHEMA src_billing   GRANT SELECT ON TABLES TO kea_extract_billing;
ALTER DEFAULT PRIVILEGES IN SCHEMA src_metering  GRANT SELECT ON TABLES TO kea_extract_metering;
ALTER DEFAULT PRIVILEGES IN SCHEMA src_catalogue GRANT SELECT ON TABLES TO kea_extract_catalogue;
