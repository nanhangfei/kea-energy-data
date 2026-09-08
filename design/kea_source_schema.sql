-- =====================================================================
-- Kea Energy — SOURCE SCHEMA (targets PostgreSQL 17; developed against 16)
-- File 1 of 3. See kea-data-platform-design.md for rationale.
--
--   src_billing     the billing system
--   src_metering    the half-hourly meter-data feed
--   src_catalogue   the plan catalogue
--   src_weather     NIWA's climate database -- a THIRD PARTY's system,
--                   retrieved as files, and an input to the marts only
--
-- Four schemas in one database for development convenience; the
-- pipeline must still treat them as separate systems. No cross-system
-- foreign keys, local surrogate keys, inconsistent naming — all
-- deliberate. Scoped to Sprint 2; src_billing.payments is commented out.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS src_billing;
CREATE SCHEMA IF NOT EXISTS src_metering;
CREATE SCHEMA IF NOT EXISTS src_catalogue;
CREATE SCHEMA IF NOT EXISTS src_weather;


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

    -- ===== TERM COMMITMENT (Sprint 3) =====
    -- 0 = flexible, no commitment and no credit. 6 or 12 months carry a
    -- monthly bill credit, and leaving early repays what was received --
    -- there is no break fee. docs/sprints/sprint-3/plans-and-terms.md has
    -- the amounts and the NZ retailers they are calibrated against.
    --
    -- The credit is NOT stored here. It is a property of the term, not of
    -- the account, so it lives in the catalogue with the plans and reaches
    -- the bill as a line like every other charge. An account that carried
    -- its own credit amount could disagree with the catalogue, and the bill
    -- would have no way to say which was right.
    TERM_MONTHS     smallint     NOT NULL DEFAULT 0,
    TERM_START_DT   timestamp,

    LAST_MOD_TS     timestamp    NOT NULL DEFAULT now(),
    CONSTRAINT ck_cycle_day CHECK (BILL_CYCLE_DAY BETWEEN 1 AND 28),
    CONSTRAINT ck_term      CHECK (TERM_MONTHS IN (0, 6, 12)),
    -- A term without a start date cannot be measured, and a start date
    -- without a term is a commitment to nothing.
    CONSTRAINT ck_term_start CHECK ((TERM_MONTHS = 0) = (TERM_START_DT IS NULL))
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
    -- CRD is the term credit. Its own group, and not for tidiness: a plan
    -- comparison re-rates usage against a candidate plan, and the credit is
    -- a flat monthly amount that must NOT be re-banded when it does. Its own
    -- charge group makes that structural instead of something the re-rating
    -- code has to remember not to do.
    CONSTRAINT ck_chg_group CHECK (CHG_GROUP IN ('NET','ENE','OTH','CRD')),
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
-- streaks, failed direct debits, part payments, arrears trajectory.
--
-- BUILT AT SPRINT 3, NOT SPRINT 4, and the reason is a gap between the PRD
-- and the regulation it cites. Sprint 3's hardship feature triggers on
-- LANGUAGE -- the customer says something that sounds like difficulty. The
-- Consumer Care Obligations it quotes require the retailer to IDENTIFY
-- customers who may be in payment difficulty and engage with them. Those
-- are different duties, and the households at highest risk are frequently
-- the ones who say nothing at all.
--
-- Detection needs no data. Identification needs this table.
--
-- It also closes a hole that has nothing to do with hardship: before this,
-- every invoice carried PAYMENTS_APPL = 0, so the serving store's balance
-- was always the full amount and "how much do I owe" had no true answer.
--
-- A FAILED PAYMENT IS A ROW, NOT AN ABSENCE. A direct debit that bounces is
-- the single strongest early signal here, and it is invisible if failure is
-- modelled by there being no payment: that looks identical to a customer who
-- simply has not paid yet.
CREATE TABLE src_billing.payments (
    PAY_ID          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ACCT_ID         integer      NOT NULL REFERENCES src_billing.accounts(ACCT_ID),
    -- Nullable: a payment does not always map to one invoice once part
    -- payments and payment arrangements exist.
    INV_ID          integer      REFERENCES src_billing.invoices(INV_ID),
    PAID_TS         timestamp    NOT NULL,
    PAY_AMT         numeric(12,2) NOT NULL,
    PAY_METHOD      varchar(20)  NOT NULL,
    FAILED_FLAG     char(1)      NOT NULL DEFAULT 'N',
    FAIL_REASON     varchar(200),
    CONSTRAINT ck_pay_failed CHECK (FAILED_FLAG IN ('Y','N')),
    CONSTRAINT ck_pay_amount CHECK (PAY_AMT > 0)
);

CREATE INDEX ix_pay_acct ON src_billing.payments(ACCT_ID, PAID_TS DESC);
CREATE INDEX ix_pay_inv  ON src_billing.payments(INV_ID);


-- ---------------------------------------------------------------------
-- Account plan history (Sprint 3)
--
-- Which plan and term an account has been on, and since when. One row per
-- account today: these plans are new, so nobody has switched yet.
--
-- ⚠️ ONE ROW IS NOT A DEGENERATE CASE, it is the starting state. Outcome 2
-- RECOMMENDS a switch; it does not require that switches already happened.
-- A second row appears the first time somebody acts on a recommendation,
-- and that is precisely what makes this table the measure of whether the
-- feature works.
--
-- Generating historical switches instead would mean re-rating every invoice
-- after each one, since a bill must be priced on the plan in force at the
-- time -- the most delicate code in the source, changed for a row nothing
-- reads yet.
-- ---------------------------------------------------------------------
CREATE TABLE src_billing.account_plan_history (
    HIST_ID         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ACCT_ID         integer      NOT NULL REFERENCES src_billing.accounts(ACCT_ID),
    PLAN_CODE       varchar(60)  NOT NULL,
    TERM_MONTHS     smallint     NOT NULL DEFAULT 0,
    VALID_FROM      date         NOT NULL,
    VALID_TO        date,
    CHANGE_RSN      varchar(200),
    CONSTRAINT ck_aph_dates CHECK (VALID_TO IS NULL OR VALID_TO > VALID_FROM),
    CONSTRAINT ck_aph_term  CHECK (TERM_MONTHS IN (0, 6, 12))
);

CREATE INDEX ix_aph_acct ON src_billing.account_plan_history(ACCT_ID, VALID_FROM DESC);


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
    -- Revisions: the same interval can be re-issued. There is NO lookback
    -- window -- design §4.1 withdrew the 7-day trailing re-extract an earlier
    -- draft specified, because it was a workaround for watermarking on the
    -- wrong column. Watermarking on received_at catches a revision whatever
    -- period it corrects; a lookback both missed older ones and re-read a
    -- week of data every night for nothing.
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
-- ---------------------------------------------------------------------
-- Term credits (Sprint 3)
--
-- The monthly bill credit that comes with a 6- or 12-month commitment.
-- HERE, NOT ON THE ACCOUNT, because it is a price: prices live in the
-- catalogue where the pipeline can be tested against them, and an amount
-- copied onto every account could disagree with the offer it came from.
--
-- Amounts are GST-INCLUSIVE, which is how they are advertised and how
-- Mercury states its own figures. "$25 off" has to mean the customer pays
-- $25 less, so the line written to a bill is the ex-GST equivalent and the
-- total moves by the advertised number.
-- ---------------------------------------------------------------------
CREATE TABLE src_catalogue.term_credits (
    TERM_MONTHS     smallint     NOT NULL,
    MONTHLY_CREDIT  numeric(12,2) NOT NULL,   -- GST-inclusive
    VALID_FROM      date         NOT NULL,
    VALID_TO        date,
    PRIMARY KEY (TERM_MONTHS, VALID_FROM),
    CONSTRAINT ck_tc_term   CHECK (TERM_MONTHS IN (6, 12)),
    CONSTRAINT ck_tc_amount CHECK (MONTHLY_CREDIT > 0)
);


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
-- SYSTEM 4 — WEATHER
--
-- NIWA's National Climate Database, via DataHub. Not Kea's system at
-- all: it is a third party's, retrieved as files on a monthly cycle,
-- which is why it is a SNAPSHOT and carries no watermark. A published
-- correction to a historical observation arrives as a changed row in the
-- next full file, and only a snapshot sees that -- an incremental read
-- keyed on anything would keep the superseded value forever.
--
-- The weather is an INPUT to the marts, never exported to serving:
-- design doc §6.4. What the agent needs is already denormalised into
-- mart_daily_usage.heating_degree_days and the period sums in
-- mart_bill_explanation.
--
-- Provenance, the four stations, and the station-is-not-a-region
-- assumption are recorded in data/dictionary/niwa-weather-source.md.
-- =====================================================================

CREATE TABLE src_weather.stations (
    station_id      integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- NIWA's own station number. The business key: stable across
    -- renames, and what the CSV filenames are named for.
    agent_no        integer NOT NULL UNIQUE,
    station_name    text    NOT NULL,
    -- The join to metering.connection_points.climate_region. THIS IS AN
    -- ASSUMPTION, not a measurement: one station stands for a whole
    -- region, and a household in the Waitakere hills does not experience
    -- Mangere's temperature. It buys a defensible regional signal, not a
    -- per-household one.
    climate_region  text    NOT NULL,
    latitude        numeric(9,6),
    longitude       numeric(9,6),
    elevation_m     integer
);

CREATE TABLE src_weather.daily_observations (
    obs_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent_no        integer NOT NULL,           -- business key, not station_id
    -- THE NEW ZEALAND DATE, DERIVED -- and the one thing in this table
    -- easy to get silently wrong. NIWA stamps each row with the instant
    -- of the 9am observation in UTC: '2026-08-16T21:00:00Z' is 09:00 NZST
    -- on the 17th, and DataHub itself labels that row 17 Aug 2026.
    -- Reading the UTC date instead shifts every temperature back a day,
    -- which no constraint would catch and which would quietly move every
    -- cold snap in the bill explanations.
    obs_date        date    NOT NULL,
    -- Kept so the derivation above is auditable rather than trusted.
    observed_at_utc timestamptz NOT NULL,
    temp_max_c      numeric(5,2),
    temp_min_c      numeric(5,2),
    -- NIWA's own mean. Verified to be (max+min)/2 across 69,422 rows,
    -- agreeing within 0.05 C on all but one. That is the meteorological
    -- convention for degree days, not an approximation to be improved on
    -- with hourly data -- and it is what the fixtures' 0.93 kWh/HDD was
    -- fitted against.
    temp_mean_c     numeric(5,2),
    grass_min_c     numeric(5,2),
    rel_humidity_pct smallint,
    source_file     text,
    received_at     timestamptz NOT NULL DEFAULT now(),
    -- A day is observed once. Two rows for one station-day means the
    -- file was loaded twice or the date derivation moved.
    CONSTRAINT uq_obs_station_day UNIQUE (agent_no, obs_date),
    -- Physically possible NZ temperatures. The record range is roughly
    -- -25 to +43; this is deliberately wider, because the job of the
    -- CHECK is to catch a unit error or a parse fault, not to referee
    -- the weather.
    CONSTRAINT ck_temp_plausible CHECK (
        temp_max_c  IS NULL OR temp_max_c  BETWEEN -40 AND 55),
    CONSTRAINT ck_mean_within_range CHECK (
        temp_mean_c IS NULL OR temp_max_c IS NULL OR temp_min_c IS NULL
        OR temp_mean_c BETWEEN temp_min_c - 0.05 AND temp_max_c + 0.05)
);

CREATE INDEX ix_obs_region_date ON src_weather.daily_observations(agent_no, obs_date);
CREATE INDEX ix_obs_date        ON src_weather.daily_observations(obs_date);


-- =====================================================================
-- EXTRACT ACCESS
--
-- One read-only role per system. In production these are four
-- credentials against four servers; here they are four grants. The
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
                             'kea_extract_catalogue',
                             'kea_extract_weather']
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
GRANT USAGE ON SCHEMA src_weather   TO kea_extract_weather;

GRANT SELECT ON ALL TABLES IN SCHEMA src_billing   TO kea_extract_billing;
GRANT SELECT ON ALL TABLES IN SCHEMA src_metering  TO kea_extract_metering;
GRANT SELECT ON ALL TABLES IN SCHEMA src_catalogue TO kea_extract_catalogue;
GRANT SELECT ON ALL TABLES IN SCHEMA src_weather   TO kea_extract_weather;

-- ON ALL TABLES covers only what exists right now. Without these, any table
-- added later is invisible to the extract role until someone re-runs the
-- grant -- which shows up as an empty extract, not as an error.
ALTER DEFAULT PRIVILEGES IN SCHEMA src_billing   GRANT SELECT ON TABLES TO kea_extract_billing;
ALTER DEFAULT PRIVILEGES IN SCHEMA src_metering  GRANT SELECT ON TABLES TO kea_extract_metering;
ALTER DEFAULT PRIVILEGES IN SCHEMA src_catalogue GRANT SELECT ON TABLES TO kea_extract_catalogue;
ALTER DEFAULT PRIVILEGES IN SCHEMA src_weather   GRANT SELECT ON TABLES TO kea_extract_weather;
