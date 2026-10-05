-- =====================================================================
-- Kea Energy — SERVING SCHEMA (targets PostgreSQL 17; developed against 16)
-- File 2 of 3. See kea-data-platform-design.md for rationale.
--
-- Written by the pipeline (kea_loader); read-only to the application.
-- Nothing here is computed in Postgres.
--
--   common     shared enum types
--   metering   replica of the meter-data feed        source-shaped
--   billing    replica of the billing system         source-shaped
--   catalogue  replica of the plan catalogue         source-shaped
--   kea        derived marts and forecasts           query-shaped
--
-- Sign convention: kwh and rate are non-negative magnitudes; direction
-- lives on the register and the tariff component. Only bill_line.amount
-- is signed.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS common;
CREATE SCHEMA IF NOT EXISTS metering;
CREATE SCHEMA IF NOT EXISTS billing;
CREATE SCHEMA IF NOT EXISTS catalogue;
CREATE SCHEMA IF NOT EXISTS kea;

SET search_path TO kea, billing, metering, catalogue, common, public;

-- ---------------------------------------------------------------------
-- Enumerations
-- ---------------------------------------------------------------------

-- Mirrors the three charge groups printed on the bill.
-- 'credit' is the term bill credit. Its own group, and not for tidiness: a
-- plan comparison re-rates usage against a candidate plan, and a flat monthly
-- credit must NOT be re-banded when it does. Its own charge group makes that
-- structural rather than something the re-rating code has to remember.
--
-- ⚠️ ADDING A VALUE HERE IS NOT LIKE RELAXING A CHECK. The source system uses
-- CHG_GROUP CHECK ('NET','ENE','OTH','CRD'); this is an ENUM, so an existing
-- database needs ALTER TYPE ... ADD VALUE, which cannot run inside a
-- transaction block. The export maps NET/ENE/OTH/CRD to these four names.
CREATE TYPE common.charge_group AS ENUM ('network', 'energy', 'other', 'credit');

-- per_kwh scales with consumption; per_day is fixed regardless of usage
-- (the "low user paying high fixed charges" problem lives entirely in
-- per_day rows).
CREATE TYPE common.rate_basis AS ENUM ('per_kwh', 'per_day');

-- NZ regulated user categories, central to plan comparison.
CREATE TYPE common.user_type AS ENUM ('low_user', 'standard');

-- 'estimated' matters to the product: the explainer must hedge its
-- language when a period contains estimates.
CREATE TYPE common.read_quality AS ENUM ('actual', 'estimated', 'validated', 'missing');

CREATE TYPE common.bill_status AS ENUM ('draft', 'issued', 'paid', 'overdue', 'cancelled');

CREATE TYPE common.payment_method AS ENUM ('direct_debit', 'bank_transfer', 'card', 'other');

-- Import = drawing from the grid. Export = surplus solar sent back.
CREATE TYPE common.flow_direction AS ENUM ('import', 'export');

-- ---------------------------------------------------------------------
-- Network / distributor
-- The bill shows "Network Charges [Vector]" — network charges are set by
-- the lines company, not the retailer, so they are modelled separately.
-- ---------------------------------------------------------------------
CREATE TABLE metering.network (
    network_id      integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    network_code    text        NOT NULL UNIQUE,   -- e.g. 'VECT'
    network_name    text        NOT NULL,          -- e.g. 'Vector'
    region          text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    -- Replica freshness. Mirrored from the source system; a stale
    -- value is how the agent can tell it may be answering from old data.
    synced_at       timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Customer and property
-- ---------------------------------------------------------------------
CREATE TABLE billing.customer (
    customer_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_number   text        NOT NULL UNIQUE, -- printed on the bill, e.g. '196501'
    full_name         text        NOT NULL,
    email             text,
    phone             text,
    -- Consumer Care: flags that change how the agent must behave.
    is_medically_dependent  boolean NOT NULL DEFAULT false,
    is_vulnerable           boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    -- Replica freshness. Mirrored from the source system; a stale
    -- value is how the agent can tell it may be answering from old data.
    synced_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE metering.property (
    property_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    address_line_1  text NOT NULL,
    address_line_2  text,
    suburb          text,
    city            text,
    postcode        text,
    -- Region drives the weather join (NIWA) used by the heating-
    -- sensitivity model in the bill explainer.
    climate_region  text,
    latitude        numeric(9,6),
    longitude       numeric(9,6),
    -- Replica freshness. Mirrored from the source system; a stale
    -- value is how the agent can tell it may be answering from old data.
    synced_at       timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- ICP — Installation Control Point, the NZ identifier for a physical
-- electricity connection. One property may have more than one.
-- ---------------------------------------------------------------------
CREATE TABLE metering.icp (
    icp_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    icp_number      text        NOT NULL UNIQUE,   -- e.g. '0160647002LCFCF'
    property_id     bigint      NOT NULL REFERENCES metering.property(property_id),
    network_id      integer     NOT NULL REFERENCES metering.network(network_id),
    connection_date date,
    disconnection_date date,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT icp_dates_ordered
        CHECK (disconnection_date IS NULL OR disconnection_date >= connection_date),
    -- Replica freshness. Mirrored from the source system; a stale
    -- value is how the agent can tell it may be answering from old data.
    synced_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_icp_property ON metering.icp(property_id);
CREATE INDEX idx_icp_network  ON metering.icp(network_id);

-- ---------------------------------------------------------------------
-- Meter registers
--
-- What the tariff actually prices. A connection has one or more
-- registers: typically uncontrolled (general household load) and
-- controlled (ripple-switched hot water). On a TOU plan the register
-- split is how peak / off-peak consumption is separated.
--
-- DEFERRED: a `meter` table (serial number, install and removal dates)
-- sits logically between icp and register, but neither Sprint 2 feature
-- reads it. Registers reference icp directly for now. Adding `meter`
-- later is one small table plus a nullable meter_id column here — cheap,
-- because it does not touch meter_read_hh.
--
-- NOT DEFERRED, deliberately: this table itself. meter_read_hh keys on
-- register_id, so removing it would mean changing the primary key of a
-- 2.45B-row partitioned table later. Decisions that touch the big table
-- are made now; standalone tables are added when the feature is built.
-- ---------------------------------------------------------------------
CREATE TABLE metering.meter_register (
    register_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    icp_id          bigint         NOT NULL REFERENCES metering.icp(icp_id),
    register_code   text           NOT NULL,   -- e.g. 'UN' uncontrolled, 'CN' controlled
    description     text,
    -- Controlled load = ripple-controlled circuits (typically hot water),
    -- switched by the network. Distinguishing this is what makes hot-water
    -- separation tractable at 30-minute resolution.
    is_controlled   boolean        NOT NULL DEFAULT false,
    -- Export is metered on its own register. A solar household therefore
    -- has at least two.
    direction       flow_direction NOT NULL DEFAULT 'import',
    UNIQUE (icp_id, register_code),
    -- Replica freshness. Mirrored from the source system; a stale
    -- value is how the agent can tell it may be answering from old data.
    synced_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_register_icp ON metering.meter_register(icp_id);


-- ---------------------------------------------------------------------
-- Plan and tariff structure
--
-- The bill's plan code (e.g. 'VECT_OFPK_STD_140520') decomposes into
-- network + tariff shape + user type + effective date. Modelled
-- explicitly rather than parsed from the code string.
-- ---------------------------------------------------------------------
CREATE TABLE catalogue.plan (
    plan_id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plan_code       text        NOT NULL,           -- 'VECT_OFPK_STD_140520'
    plan_name       text        NOT NULL,
    network_id      integer     NOT NULL REFERENCES metering.network(network_id),
    user_type       user_type   NOT NULL,
    is_tou          boolean     NOT NULL DEFAULT false,
    -- Conditional discount applied when the bill is paid by its due date.
    prompt_payment_discount_pct numeric(5,4) NOT NULL DEFAULT 0,
    effective_from  date        NOT NULL,
    effective_to    date,
    is_active       boolean     NOT NULL DEFAULT true,
    CONSTRAINT plan_dates_ordered
        CHECK (effective_to IS NULL OR effective_to > effective_from),
    UNIQUE (plan_code, effective_from),
    -- Replica freshness. Mirrored from the source system; a stale
    -- value is how the agent can tell it may be answering from old data.
    synced_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_plan_network_active ON catalogue.plan(network_id) WHERE is_active;

-- Time-of-use bands.
--
-- IMPORTANT: band windows are a property of the PLAN, not a global
-- constant. The same half-hour can be peak under one plan and shoulder
-- under another, which is precisely what makes plan comparison
-- non-trivial. Never hard-code these.
-- ---------------------------------------------------------------------
-- Term credits
--
-- The monthly bill credit that comes with a 6- or 12-month commitment.
-- HERE rather than on the account, because it is a price: an amount copied
-- onto every account could disagree with the offer it came from, and the
-- bill would have no way to say which was right.
--
-- GST-INCLUSIVE, which is how it is advertised and how NZ retailers state
-- these figures. "$25 off" has to mean the customer pays $25 less, so the
-- line written to a bill is the ex-GST equivalent and the total moves by the
-- advertised number. Storing the ex-GST amount here would make the offer and
-- the bill disagree by 15%.
-- ---------------------------------------------------------------------
CREATE TABLE catalogue.term_credit (
    term_months     smallint      PRIMARY KEY,
    monthly_credit  numeric(12,2) NOT NULL,      -- GST-inclusive
    effective_from  date          NOT NULL,
    effective_to    date,
    synced_at       timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT term_credit_term   CHECK (term_months IN (6, 12)),
    CONSTRAINT term_credit_amount CHECK (monthly_credit > 0)
);


CREATE TABLE catalogue.tou_band (
    band_id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plan_id         integer NOT NULL REFERENCES catalogue.plan(plan_id) ON DELETE CASCADE,
    -- Bands are scoped to a charge group, NOT just to the plan.
    -- Confirmed against a real bill: on plan NZSZ_ZERO_STD_FXTOU_240901_T1
    -- the network charges split the same consumption into 2 bands
    -- (38.22 peak / 142.22 off-peak) while the energy charges split it
    -- into 3 (34.08 / 74.96 / 71.39 shoulder). Same kWh, same period,
    -- different windows. Keying bands to the plan alone cannot express
    -- that, and the rating engine would mis-band every TOU network line.
    charge_group    charge_group NOT NULL,
    band_name       text    NOT NULL,      -- 'peak' | 'shoulder' | 'off_peak'
    -- ISO day numbers the window applies to: 1 = Monday .. 7 = Sunday.
    days_of_week    smallint[] NOT NULL,
    start_time      time    NOT NULL,
    end_time        time    NOT NULL,
    -- Months the band applies to, for seasonal plans. NULL = all year.
    months          smallint[],
    CONSTRAINT tou_band_days_valid
        CHECK (days_of_week <@ ARRAY[1,2,3,4,5,6,7]::smallint[]),
    UNIQUE (plan_id, charge_group, band_name, start_time, days_of_week),
    -- Replica freshness. Mirrored from the source system; a stale
    -- value is how the agent can tell it may be answering from old data.
    synced_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_tou_band_plan ON catalogue.tou_band(plan_id, charge_group);

-- One row per priced line that can appear on a bill.
--
-- Every line item on the sample bills maps to a row here:
--   network / per_kwh  / peak      -> 'Peak Usage'         0.1004 $/kWh
--   network / per_day  / NULL      -> 'VECT + Daily'       1.12   $/day
--   energy  / per_kwh  / off_peak  -> 'Off Peak Usage'     0.1056 $/kWh
--   other   / per_kwh  / NULL      -> 'EA Levy'            0.0013 $/kWh
--   other   / per_day  / NULL      -> 'Metering daily'     0.30   $/day
CREATE TABLE catalogue.tariff_component (
    component_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plan_id         integer        NOT NULL REFERENCES catalogue.plan(plan_id) ON DELETE CASCADE,
    charge_group    charge_group   NOT NULL,
    component_name  text           NOT NULL,   -- as printed on the bill
    basis           rate_basis     NOT NULL,
    -- NULL band = applies to all consumption regardless of time of use
    -- (e.g. the EA levy, transmission charges, any per_day component).
    -- When set, the band's charge_group MUST equal this component's
    -- charge_group. Postgres cannot express that as a CHECK across
    -- tables, so it is enforced in the loader and covered by a dbt test.
    band_id         integer        REFERENCES catalogue.tou_band(band_id),
    direction       flow_direction NOT NULL DEFAULT 'import',
    -- Six decimal places: the EA levy is $0.0013/kWh, and rounding at
    -- four would introduce error across a 2.45B-row re-rate.
    rate            numeric(12,6)  NOT NULL,
    -- Applies only to controlled-load registers when true.
    controlled_only boolean        NOT NULL DEFAULT false,
    effective_from  date           NOT NULL,
    effective_to    date,
    -- Rate is a magnitude, never signed. For direction = 'export' this is
    -- the buyback rate; the credit sign is applied in bill_line.amount.
    CONSTRAINT tariff_rate_non_negative CHECK (rate >= 0),
    CONSTRAINT tariff_band_only_for_kwh
        CHECK (band_id IS NULL OR basis = 'per_kwh'),
    -- Export is only ever priced per kWh — there is no daily charge on
    -- exported energy, and no network or levy component applies to it.
    CONSTRAINT tariff_export_is_per_kwh_energy
        CHECK (direction = 'import'
               OR (basis = 'per_kwh' AND charge_group = 'energy')),
    CONSTRAINT tariff_dates_ordered
        CHECK (effective_to IS NULL OR effective_to > effective_from),
    -- Replica freshness. Mirrored from the source system; a stale
    -- value is how the agent can tell it may be answering from old data.
    synced_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_tariff_plan_effective
    ON catalogue.tariff_component(plan_id, effective_from DESC);

-- ---------------------------------------------------------------------
-- Account — the customer's supply agreement at an ICP on a given plan.
-- Separate from customer and icp because all three change independently:
-- customers move, plans change, ICPs outlive both.
-- ---------------------------------------------------------------------
CREATE TABLE billing.account (
    account_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_number    text        NOT NULL UNIQUE,
    customer_id       bigint      NOT NULL REFERENCES billing.customer(customer_id),
    icp_id            bigint      NOT NULL REFERENCES metering.icp(icp_id),
    plan_id           integer     NOT NULL REFERENCES catalogue.plan(plan_id),
    payment_method    payment_method NOT NULL DEFAULT 'direct_debit',
    -- Day of month the meter is read and the cycle closes. Retailers
    -- stagger these across the month; the demo cohort should too, or
    -- mid-cycle alerting all fires on the same day.
    billing_cycle_day smallint    NOT NULL CHECK (billing_cycle_day BETWEEN 1 AND 28),
    start_date        date        NOT NULL,
    end_date          date,

    -- Term commitment. 0 is flexible; 6 and 12 carry a monthly bill credit,
    -- and leaving early repays what was received rather than paying a break
    -- fee. The AMOUNT is not here -- it is a price, and prices live in
    -- catalogue.term_credit where the pipeline can be tested against them.
    -- docs/sprints/sprint-3/plans-and-terms.md carries the reasoning.
    term_months       smallint    NOT NULL DEFAULT 0,
    term_start        date,

    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT account_dates_ordered
        CHECK (end_date IS NULL OR end_date >= start_date),
    CONSTRAINT account_term_valid CHECK (term_months IN (0, 6, 12)),
    -- A term without a start cannot be measured; a start without a term is a
    -- commitment to nothing.
    CONSTRAINT account_term_start CHECK ((term_months = 0) = (term_start IS NULL)),
    -- Replica freshness. Mirrored from the source system; a stale
    -- value is how the agent can tell it may be answering from old data.
    synced_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_account_customer ON billing.account(customer_id);
CREATE INDEX idx_account_icp      ON billing.account(icp_id);
CREATE INDEX idx_account_open     ON billing.account(icp_id) WHERE end_date IS NULL;
-- ---------------------------------------------------------------------
-- Half-hourly meter reads — the largest table, and the one the whole
-- product rests on.
--
-- RANGE-partitioned by month. RDS does not support the TimescaleDB
-- extension (and Aurora does not either), so native declarative
-- partitioning plus a BRIN index on interval_start is the approach.
--
-- interval_start is timestamptz and stored UTC. NZ observes DST, so
-- local days are 46 or 50 intervals twice a year — never assume 48.
-- ---------------------------------------------------------------------
CREATE TABLE metering.meter_read_hh (
    icp_id          bigint       NOT NULL,
    register_id     bigint       NOT NULL,
    interval_start  timestamptz  NOT NULL,
    kwh             numeric(10,4) NOT NULL,
    quality         read_quality NOT NULL DEFAULT 'actual',
    source_file     text,
    ingested_at     timestamptz  NOT NULL DEFAULT now(),
    -- Always a non-negative magnitude, for both import and export reads.
    -- Direction comes from the register, so this constraint stays strict
    -- and never needs relaxing on a table of this size.
    CONSTRAINT read_kwh_non_negative CHECK (kwh >= 0),
    PRIMARY KEY (icp_id, register_id, interval_start)
) PARTITION BY RANGE (interval_start);

-- BRIN rather than B-tree: reads are appended in time order, so the
-- physical layout correlates strongly with interval_start. Orders of
-- magnitude smaller than the equivalent B-tree at this row count.
CREATE INDEX idx_read_interval_brin
    ON metering.meter_read_hh USING brin (interval_start) WITH (pages_per_range = 64);

-- Example partitions. Generate the full set from the loader.
CREATE TABLE metering.meter_read_hh_2024_01 PARTITION OF metering.meter_read_hh
    FOR VALUES FROM ('2024-01-01 00:00:00+00') TO ('2024-02-01 00:00:00+00');
CREATE TABLE metering.meter_read_hh_2024_02 PARTITION OF metering.meter_read_hh
    FOR VALUES FROM ('2024-02-01 00:00:00+00') TO ('2024-03-01 00:00:00+00');
CREATE TABLE metering.meter_read_hh_default PARTITION OF metering.meter_read_hh DEFAULT;

-- ---------------------------------------------------------------------
-- Bill (invoice header)
-- Mirrors the Summary block on page 1 of the bill.
-- ---------------------------------------------------------------------
CREATE TABLE billing.bill (
    bill_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id         bigint      NOT NULL REFERENCES billing.account(account_id),
    invoice_number     text        NOT NULL UNIQUE,   -- '0000776126'
    -- The plan as rated, which may differ from the account's current
    -- plan. With account_plan_history deferred, this column is the only
    -- record of what applied — so it must always be populated from the
    -- plan in force at period_end, never from account.plan_id at
    -- render time.
    plan_id            integer     NOT NULL REFERENCES catalogue.plan(plan_id),
    issue_date         date        NOT NULL,
    period_start       date        NOT NULL,
    period_end         date        NOT NULL,

    -- Generated, because it is the FIRST thing the bill explainer must
    -- check. A move-in bill can cover 43 days instead of 31; attributing
    -- that increase to cold weather would be confidently wrong.
    period_days        integer GENERATED ALWAYS AS
                         ((period_end - period_start) + 1) STORED,

    opening_balance    numeric(12,2) NOT NULL DEFAULT 0,
    -- The bill prints "Payment Received 16-Mar-2023 ($232.05)". Sprint 2
    -- needs only that figure, so it lives here. The `payment` table
    -- (individual transactions, failed direct debits, part payments)
    -- is deferred until hardship triage needs the behavioural signals.
    payments_applied   numeric(12,2) NOT NULL DEFAULT 0,
    balance_brought_fwd numeric(12,2) NOT NULL DEFAULT 0,

    charges_excl_gst   numeric(12,2) NOT NULL,        -- $215.71
    gst_rate           numeric(5,4)  NOT NULL DEFAULT 0.15,
    gst_amount         numeric(12,2) NOT NULL,        -- $32.36
    total_charges      numeric(12,2) NOT NULL,        -- $248.07

    due_date           date        NOT NULL,
    late_payment_fee   numeric(12,2) NOT NULL DEFAULT 0,   -- $37.21
    total_if_paid_late numeric(12,2),                      -- $285.28
    prompt_payment_discount numeric(12,2) NOT NULL DEFAULT 0,

    total_import_kwh   numeric(12,4),                      -- 1034.665
    total_export_kwh   numeric(12,4) NOT NULL DEFAULT 0,   -- 47.48 on the solar bill
    export_credit      numeric(12,2) NOT NULL DEFAULT 0,   -- shown as a negative line, stored positive
    status             bill_status NOT NULL DEFAULT 'issued',
    created_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT bill_period_ordered CHECK (period_end >= period_start),
    CONSTRAINT bill_due_after_issue CHECK (due_date >= issue_date),
    -- Replica freshness. Mirrored from the source system; a stale
    -- value is how the agent can tell it may be answering from old data.
    synced_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_bill_account_period ON billing.bill(account_id, period_end DESC);
CREATE INDEX idx_bill_status ON billing.bill(status) WHERE status IN ('issued','overdue');

-- ---------------------------------------------------------------------
-- Bill line — one row per priced line on page 2 of the bill.
--
-- quantity + unit + rate + amount are stored rather than derived, so a
-- reissued bill remains a faithful record of what the customer was
-- charged even if a tariff is later corrected.
-- ---------------------------------------------------------------------
CREATE TABLE billing.bill_line (
    line_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bill_id         bigint       NOT NULL REFERENCES billing.bill(bill_id) ON DELETE CASCADE,
    component_id    bigint       REFERENCES catalogue.tariff_component(component_id),
    charge_group    charge_group   NOT NULL,
    direction       flow_direction NOT NULL DEFAULT 'import',
    line_label      text           NOT NULL,   -- 'Peak Usage', 'Export Shoulder-Peak'
    band_name       text,                      -- 'peak' | 'shoulder' | 'off_peak' | NULL
    quantity        numeric(12,4)  NOT NULL,   -- kWh, or number of days
    unit            rate_basis     NOT NULL,
    rate            numeric(12,6)  NOT NULL,   -- magnitude
    -- Line losses, printed alongside the metered quantity on the bill:
    -- '34.08 kWh   1.74 kWh losses @ 0.26'.
    loss_kwh        numeric(12,4),
    -- The customer is billed on metered volume PLUS losses, not on the
    -- metered volume alone. Verified against the bill:
    --   34.08 x 0.26          = 8.86   (does not match)
    --   (34.08 + 1.74) x 0.26 = 9.31   (matches the printed amount)
    -- Made a generated column so the relationship is explicit and the
    -- rating engine cannot quietly bill on the wrong base.
    billed_quantity numeric(12,4) GENERATED ALWAYS AS
                      (quantity + COALESCE(loss_kwh, 0)) STORED,
    -- The only signed column in the schema. Negative twice over: export
    -- credits, which is how they print on the bill, e.g. ($6.33); and from
    -- Sprint 3 the term credit, a flat monthly amount for committing to 6 or
    -- 12 months.
    amount          numeric(12,2)  NOT NULL,   -- excl GST
    sort_order      smallint       NOT NULL DEFAULT 0,
    -- ⚠️ A CREDIT IS NEGATIVE WHATEVER ITS DIRECTION.
    --
    -- The original rule paired sign with direction alone, written when an
    -- export credit was the only signed line there was. A term credit is not
    -- export -- nothing flowed anywhere -- so it carries direction 'import'
    -- and a negative amount, and the load rejected all 2,392 of them.
    -- Charge group decides the sign for credits; direction still decides it
    -- for everything else, so an import charge cannot go negative.
    CONSTRAINT bill_line_sign_matches_direction
        CHECK ((charge_group = 'credit' AND amount <= 0)
               OR (charge_group <> 'credit' AND direction = 'import' AND amount >= 0)
               OR (charge_group <> 'credit' AND direction = 'export' AND amount <= 0)),
    -- Replica freshness. Mirrored from the source system; a stale
    -- value is how the agent can tell it may be answering from old data.
    synced_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_bill_line_bill ON billing.bill_line(bill_id, sort_order);

-- ---------------------------------------------------------------------
-- Bill forecast (Outcome 3)
-- Projected bill mid-cycle. Confidence bounds are stored, not derived,
-- because the agent must be able to say "between $180 and $220" rather
-- than a false-precision single number.
-- ---------------------------------------------------------------------
CREATE TABLE kea.bill_forecast (
    forecast_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id         bigint        NOT NULL REFERENCES billing.account(account_id),
    period_start       date          NOT NULL,
    period_end         date          NOT NULL,
    forecast_date      date          NOT NULL,   -- as at
    days_elapsed       smallint      NOT NULL,
    kwh_to_date        numeric(12,4) NOT NULL,
    projected_kwh      numeric(12,4) NOT NULL,
    projected_amount   numeric(12,2) NOT NULL,
    projected_low      numeric(12,2) NOT NULL,
    projected_high     numeric(12,2) NOT NULL,
    -- Comparison anchor: the same period last year, weather-normalised.
    baseline_amount    numeric(12,2),
    variance_pct       numeric(6,2),
    is_anomalous       boolean       NOT NULL DEFAULT false,
    model_version      text,
    created_at         timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT forecast_bounds_ordered
        CHECK (projected_low <= projected_amount AND projected_amount <= projected_high),
    UNIQUE (account_id, period_start, forecast_date)
);

CREATE INDEX idx_forecast_account ON kea.bill_forecast(account_id, forecast_date DESC);
CREATE INDEX idx_forecast_anomalous ON kea.bill_forecast(account_id) WHERE is_anomalous;

-- ---------------------------------------------------------------------
-- Load audit
--
-- One row per mart per load. Makes "how fresh is this, and did it
-- succeed" answerable without inspecting the data itself. The agent can
-- check staleness before answering; ops can diagnose a partial load.
-- ---------------------------------------------------------------------
CREATE TABLE kea.mart_load_log (
    load_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mart_name       text        NOT NULL,
    started_at      timestamptz NOT NULL,
    completed_at    timestamptz,
    row_count       bigint,
    -- The warehouse run this came from. Traces a serving row back
    -- through dbt to the bronze extract that produced it.
    warehouse_run_id text,
    status          text        NOT NULL DEFAULT 'running',
    error_message   text,
    CONSTRAINT ck_load_status CHECK (status IN ('running','succeeded','failed'))
);

CREATE INDEX idx_load_log_mart ON kea.mart_load_log(mart_name, started_at DESC);


-- ---------------------------------------------------------------------
-- mart_account_context
--
-- Everything the agent needs to know about who it is talking to, in one
-- row. Loaded from mart_serving_account.
--
-- This is the mart that most justifies the whole approach: assembling
-- it requires joining across all three source systems, which is exactly
-- what the brief says Kea cannot currently do.
-- ---------------------------------------------------------------------
CREATE TABLE kea.mart_account_context (
    account_id          bigint      PRIMARY KEY,
    account_number      text        NOT NULL,

    -- Customer (from billing)
    customer_id         bigint      NOT NULL,
    customer_number     text        NOT NULL,
    customer_name       text        NOT NULL,
    email               text,
    phone               text,
    -- Consumer Care flags. These change what the agent is allowed to
    -- say and when it must escalate, so they belong in the context row
    -- rather than behind a lookup the agent might skip.
    is_medically_dependent boolean  NOT NULL DEFAULT false,
    is_vulnerable       boolean     NOT NULL DEFAULT false,

    -- Connection (from metering)
    icp_id              bigint      NOT NULL,
    icp_number          text        NOT NULL,
    address_display     text,
    climate_region      text,                   -- weather join key
    network_code        text,
    network_name        text,
    has_solar           boolean     NOT NULL DEFAULT false,
    has_controlled_load boolean     NOT NULL DEFAULT false,

    -- Plan (from catalogue)
    plan_id             integer     NOT NULL,
    plan_code           text        NOT NULL,
    plan_name           text        NOT NULL,
    user_type           text        NOT NULL,   -- low_user | standard
    is_tou              boolean     NOT NULL DEFAULT false,
    daily_fixed_charge  numeric(12,6),          -- summed per_day components
    payment_method      text        NOT NULL,

    -- Where the customer is in their billing cycle. Pre-computed
    -- because the mid-cycle warning needs it on every request.
    billing_cycle_day   smallint    NOT NULL,
    current_period_start date,
    current_period_end  date,
    days_into_period    smallint,

    -- Data quality, surfaced to the agent rather than hidden.
    estimated_read_pct  numeric(5,2) NOT NULL DEFAULT 0,
    last_read_at        timestamptz,
    -- TRUE when cross-system references could not be resolved (an ICP
    -- or plan code with no match). The agent must DECLINE rather than
    -- answer — an explanation built on a connection we cannot see is
    -- worse than no explanation. See pipeline design section 6.3.
    is_quarantined      boolean     NOT NULL DEFAULT false,
    quarantine_reason   text,

    built_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_ctx_customer ON kea.mart_account_context(customer_number);
CREATE INDEX idx_ctx_icp      ON kea.mart_account_context(icp_number);
CREATE INDEX idx_ctx_quarantined ON kea.mart_account_context(account_id)
    WHERE is_quarantined;


-- ---------------------------------------------------------------------
-- mart_daily_usage
--
-- Daily rollup with weather already joined. Replaces mv_daily_usage.
--
-- The weather columns are denormalised in deliberately: the heating
-- comparison is needed on nearly every bill explanation, and joining
-- kea.weather_daily at query time would add a join to the hottest path
-- for data that never changes retrospectively.
-- ---------------------------------------------------------------------
CREATE TABLE kea.mart_daily_usage (
    icp_id              bigint      NOT NULL,
    usage_date          date        NOT NULL,

    import_kwh          numeric(12,4) NOT NULL DEFAULT 0,
    export_kwh          numeric(12,4) NOT NULL DEFAULT 0,
    controlled_kwh      numeric(12,4) NOT NULL DEFAULT 0,
    uncontrolled_kwh    numeric(12,4) NOT NULL DEFAULT 0,

    -- Overnight percentile, the always-on draw. A core explainer input:
    -- "you draw 350W around the clock, NZD 480/yr before you switch
    -- anything on".
    --
    -- The smallest half-hour in which the WHOLE load is observable, x 48.
    -- For an ICP with no export register that is all 48 half-hours. For one
    -- with panels it is 22:00-05:00 NZ local only: `import = max(0, load -
    -- pv)`, so an all-day minimum returns 0 on any day the panels covered the
    -- draw and measures the sun rather than the house (fixed 19 Sep 2026,
    -- design §8.4). Whether the narrow window should apply to every ICP --
    -- which is what "overnight" above implies -- is open in §12.
    base_load_kwh       numeric(12,4),
    peak_kwh            numeric(12,4),
    off_peak_kwh        numeric(12,4),

    -- Completeness. 48 intervals is the normal day; 46 and 50 occur at
    -- the DST transitions, so this is not a constant.
    interval_count      smallint    NOT NULL,
    expected_intervals  smallint    NOT NULL,
    estimated_count     smallint    NOT NULL DEFAULT 0,

    -- Weather, pre-joined
    temp_mean_c         numeric(5,2),
    heating_degree_days numeric(6,2),

    is_weekend          boolean     NOT NULL,
    is_public_holiday   boolean     NOT NULL DEFAULT false,

    built_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (icp_id, usage_date)
);

CREATE INDEX idx_daily_date ON kea.mart_daily_usage(usage_date);


-- ---------------------------------------------------------------------
-- mart_hourly_profile
--
-- The "Average Weekday Breakdown" chart from the bill, split by
-- direction so import renders above the axis and export below.
-- Replaces mv_hourly_profile.
-- ---------------------------------------------------------------------
CREATE TABLE kea.mart_hourly_profile (
    icp_id              bigint      NOT NULL,
    -- A MONTH, keyed by its first day. 2024-03-01 means "March 2024", not
    -- "1 March 2024". There is no day dimension in this table at all: the
    -- month is already folded up, which is why one (month, hour) pair
    -- yields two rows rather than thirty-one.
    profile_month       date        NOT NULL,
    hour_of_day         smallint    NOT NULL,
    -- Which half of the month this row averages: true is the month's
    -- weekdays, false its weekend days. NOT a property of a date -- no date
    -- survives this far. The March row with is_weekday = true is the mean
    -- over all 21 March weekdays; the false row is the mean over the other
    -- 10. The split is in the key rather than a filter applied afterwards
    -- because weekday and weekend are two different curves, and averaging
    -- them together produces a shape neither day actually has.
    is_weekday          boolean     NOT NULL,

    avg_import_kwh      numeric(10,4) NOT NULL DEFAULT 0,
    avg_export_kwh      numeric(10,4) NOT NULL DEFAULT 0,
    -- How many days went into the average. A profile built from three
    -- days is not the same claim as one built from thirty, and the
    -- agent should be able to tell.
    sample_days         smallint    NOT NULL,

    built_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (icp_id, profile_month, hour_of_day, is_weekday),
    CONSTRAINT ck_hour CHECK (hour_of_day BETWEEN 0 AND 23)
);


-- ---------------------------------------------------------------------
-- mart_bill_explanation
--
-- The core mart for Outcome 1. One row per bill, with the increase
-- already decomposed into named drivers.
--
-- WHY PRE-COMPUTED: the agent must not do attribution arithmetic at
-- request time. Doing it in dbt means it is version-controlled,
-- testable, reviewable, and identical for every customer. Doing it in
-- the prompt means it is none of those things.
--
-- ORDER MATTERS. delta_from_period_length is computed FIRST and removed
-- before anything else is attributed. A 43-day move-in bill looks like
-- a 40% consumption increase and is not one; attributing it to cold
-- weather would be confidently wrong.
-- ---------------------------------------------------------------------
CREATE TABLE kea.mart_bill_explanation (
    bill_id             bigint      PRIMARY KEY,
    account_id          bigint      NOT NULL,
    icp_id              bigint      NOT NULL,

    period_start        date        NOT NULL,
    period_end          date        NOT NULL,
    period_days         smallint    NOT NULL,
    total_amount        numeric(12,2) NOT NULL,
    total_kwh           numeric(12,4) NOT NULL,

    -- The comparison bill. NULL for a first bill, which is itself a
    -- complete explanation the agent should give.
    prior_bill_id       bigint,
    prior_period_days   smallint,
    prior_total_amount  numeric(12,2),
    prior_total_kwh     numeric(12,4),

    amount_delta        numeric(12,2),
    -- WIDER THAN IT LOOKS LIKE IT NEEDS TO BE. numeric(6,2) caps at 9999.99,
    -- and a percentage change has no such bound: a bill of $0.11 followed by a
    -- $14.76 credit is -13518.18%, which is arithmetically correct and
    -- degenerate at the same time. Measured on the real cohort the largest
    -- honest value is already 7377.03 -- 74% of the old ceiling -- so the
    -- overflow was one unusual prior bill away with or without that outlier.
    amount_delta_pct    numeric(10,2),
    kwh_delta           numeric(12,4),

    -- ===== DRIVER DECOMPOSITION, in attribution order =====
    -- 1. Longer or shorter billing period
    delta_from_period_length  numeric(12,2),
    -- 2. Colder or warmer weather, via the degree-day regression
    delta_from_weather        numeric(12,2),
    -- 3. Always-on draw moved up or down
    delta_from_base_load      numeric(12,2),
    -- 4. Rate change or plan switch, usage held constant
    delta_from_tariff         numeric(12,2),
    -- 5. What is left. If this is large the agent says so, rather than
    --    inventing a cause. "I can account for $34 of the $61 increase"
    --    is an honest and useful answer.
    delta_unexplained         numeric(12,2),

    -- Supporting figures, so the agent can quote specifics
    hdd_this_period     numeric(8,2),
    hdd_prior_period    numeric(8,2),
    temp_mean_this      numeric(5,2),
    temp_mean_prior     numeric(5,2),
    base_load_kwh_day_this  numeric(10,4),
    base_load_kwh_day_prior numeric(10,4),
    heating_sensitivity_kwh_per_hdd numeric(10,4),

    -- Sprint 4 (V005): the detected-load driver. The attribution order is
    -- period length, weather, base load, DETECTED LOADS, tariff, unexplained.
    --   detected_load_kwh_day_*    mean of ev_kwh_low + timed_night_kwh_low
    --                              over each period's assessed days
    --                              (kea.mart_daily_detected_load)
    --   delta_from_detected_loads  (this - prior) x period_days, priced at the
    --                              prior period's rate like the other drivers
    detected_load_kwh_day_this  numeric(10,4),
    detected_load_kwh_day_prior numeric(10,4),
    delta_from_detected_loads   numeric(12,2),

    -- Hedging signals. The explainer must soften its language when the
    -- period contains estimated reads or gaps.
    estimated_read_pct  numeric(5,2) NOT NULL DEFAULT 0,
    missing_interval_count integer  NOT NULL DEFAULT 0,
    -- FALSE when the drivers do not account for enough of the change to
    -- be worth stating. The agent then explains what it can see rather
    -- than asserting a cause.
    is_explainable      boolean     NOT NULL DEFAULT true,

    built_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_explain_account ON kea.mart_bill_explanation(account_id, period_end DESC);


-- ---------------------------------------------------------------------
-- mart_forecast_input
--
-- Features for the mid-cycle bill-shock model (Outcome 3). Separate
-- from kea.bill_forecast: this is the INPUT, refreshed nightly by the
-- pipeline; bill_forecast is the OUTPUT, written by the forecasting job.
-- ---------------------------------------------------------------------
CREATE TABLE kea.mart_forecast_input (
    account_id          bigint      NOT NULL,
    period_start        date        NOT NULL,
    as_at_date          date        NOT NULL,

    days_elapsed        smallint    NOT NULL,
    days_total          smallint    NOT NULL,
    kwh_to_date         numeric(12,4) NOT NULL,
    cost_to_date        numeric(12,2) NOT NULL,

    -- Baselines the projection extrapolates from
    baseline_daily_kwh  numeric(10,4),
    same_period_last_year_kwh numeric(12,4),
    same_period_last_year_amount numeric(12,2),

    hdd_to_date         numeric(8,2),
    hdd_expected_remaining numeric(8,2),
    heating_sensitivity_kwh_per_hdd numeric(10,4),

    -- Fixed charges are known exactly for the whole period the moment
    -- it starts; only the variable part needs forecasting.
    fixed_charges_period numeric(12,2),

    -- Sprint 4 (V005), for hdd-linear-v3, whose baseline is fitted without
    -- detected loads: the mean of ev_kwh_low + timed_night_kwh_low over the
    -- assessed days in the 28 days up to as_at_date. NULL with fewer than 7
    -- such days, and is_forecastable is then false.
    detected_load_kwh_day numeric(10,4),

    -- Warehouse-owned safety gate. The output job must not reconstruct this
    -- from reduced serving columns or forecast from incomplete evidence.
    is_forecastable     boolean     NOT NULL,

    built_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, period_start, as_at_date)
);

CREATE INDEX idx_forecast_input_current
    ON kea.mart_forecast_input(account_id, as_at_date DESC);


-- ---------------------------------------------------------------------
-- Roles and grants: kea_loader writes, kea_app only reads.
-- ---------------------------------------------------------------------

-- Guarded so the file is re-runnable: CREATE ROLE has no IF NOT EXISTS.
-- No password here; set it out of band from the secret store.
--   kea_app     the agent and API
--   kea_loader  the nightly pipeline
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['kea_app', 'kea_loader']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I LOGIN', r);
        END IF;
    END LOOP;
END
$$;

GRANT USAGE ON SCHEMA common, metering, billing, catalogue, kea TO kea_app;
GRANT SELECT ON ALL TABLES IN SCHEMA metering  TO kea_app;
GRANT SELECT ON ALL TABLES IN SCHEMA billing   TO kea_app;
GRANT SELECT ON ALL TABLES IN SCHEMA catalogue TO kea_app;
GRANT SELECT ON ALL TABLES IN SCHEMA kea       TO kea_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA metering  GRANT SELECT ON TABLES TO kea_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA billing   GRANT SELECT ON TABLES TO kea_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA catalogue GRANT SELECT ON TABLES TO kea_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA kea       GRANT SELECT ON TABLES TO kea_app;

GRANT USAGE ON SCHEMA common, metering, billing, catalogue, kea TO kea_loader;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA metering  TO kea_loader;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA billing   TO kea_loader;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA catalogue TO kea_loader;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA kea       TO kea_loader;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA kea TO kea_loader;

-- Same reason as the kea_app defaults above: a mart added next sprint must
-- not be silently unwritable by the loader.
ALTER DEFAULT PRIVILEGES IN SCHEMA metering  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kea_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA billing   GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kea_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA catalogue GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kea_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA kea       GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kea_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA kea       GRANT USAGE, SELECT ON SEQUENCES TO kea_loader;


-- ---------------------------------------------------------------------
-- Identity sequences vs. ids that were supplied rather than generated.
--
-- Every primary key above is GENERATED ALWAYS AS IDENTITY. Both things that
-- populate this database supply their own ids instead: the nine fixtures with
-- OVERRIDING SYSTEM VALUE, and Stage 5's loader with the warehouse's key-map
-- surrogates -- which it must, because those ids are what make a serving row
-- traceable back through dbt to the bronze extract that produced it.
--
-- OVERRIDING SYSTEM VALUE DOES NOT ADVANCE THE SEQUENCE. So the sequence sits
-- near 1 while rows sit far above it, and the first insert that does NOT
-- supply an id starts climbing towards a collision. gen_source/01_schemas.sql
-- calls this "a TIME BOMB, not a constant failure" and defuses it for the
-- src_* schemas; that function filters on `src\_%` and does not reach these
-- four. This is the same guard on this side of the pipeline.
--
-- SECURITY DEFINER because setval needs UPDATE on the sequence and kea_loader
-- holds sequence privileges in `kea` only. Granting it UPDATE on every
-- sequence in four schemas would buy one maintenance call at the price of a
-- permanent widening; one EXECUTE grant on one function does not.
--
-- search_path is pinned per the usual SECURITY DEFINER hazard: without it the
-- caller chooses what `pg_class` means.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION common.sync_serving_sequences()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE r record; seq text; hi bigint;
BEGIN
    FOR r IN
        SELECT n.nspname AS sch, c.relname AS tbl, a.attname AS col
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attidentity <> ''
        WHERE n.nspname IN ('metering', 'billing', 'catalogue', 'kea')
          AND c.relkind IN ('r', 'p')
    LOOP
        seq := pg_get_serial_sequence(format('%I.%I', r.sch, r.tbl), r.col);
        CONTINUE WHEN seq IS NULL;
        EXECUTE format('SELECT COALESCE(MAX(%I), 0) FROM %I.%I', r.col, r.sch, r.tbl)
           INTO hi;
        -- is_called = false, so the NEXT value handed out is hi + 1.
        PERFORM setval(seq, hi + 1, false);
    END LOOP;
END $$;

REVOKE ALL ON FUNCTION common.sync_serving_sequences() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION common.sync_serving_sequences() TO kea_loader;


-- ---------------------------------------------------------------------
-- What the partition swap needs, and why meter_read_hh changes hands.
--
-- Design §7.3 loads meter reads by building each month beside the table and
-- swapping it in. That is three privileges the DML grants above do not carry:
--
--   CREATE on the schema   -- the month is a real table before it is a partition
--   ownership of the parent -- ATTACH/DETACH PARTITION is an owner-only operation
--   TRUNCATE               -- not implied by DELETE
--
-- Giving kea_loader ownership of this one table is the narrow answer. It is
-- the only table in the serving store whose entire lifecycle -- rows,
-- partitions, and the partition set itself -- belongs to the pipeline; it
-- carries no foreign keys by design (§7.4), so owning it grants no reach into
-- anything else; and kea_app keeps its SELECT, because changing an owner does
-- not disturb grants already made.
--
-- The alternative was a SECURITY DEFINER wrapper around the swap, as
-- sync_serving_sequences() uses. Rejected here: that function does one fixed
-- thing with no arguments, while a swap wrapper would have to take table and
-- partition names from the caller and run DDL with them, which is a far larger
-- thing to hand out than ownership of one table.
-- ---------------------------------------------------------------------
GRANT CREATE ON SCHEMA metering TO kea_loader;

DO $$
DECLARE r record;
BEGIN
    EXECUTE 'ALTER TABLE metering.meter_read_hh OWNER TO kea_loader';
    FOR r IN
        SELECT c.oid::regclass AS part
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = 'metering.meter_read_hh'::regclass
    LOOP
        EXECUTE format('ALTER TABLE %s OWNER TO kea_loader', r.part);
    END LOOP;
END $$;


-- ---------------------------------------------------------------------
-- Every foreign key becomes DEFERRABLE INITIALLY IMMEDIATE.
--
-- Nothing changes for ordinary writes: INITIALLY IMMEDIATE means each
-- statement is still checked as it runs, so the application and the agent see
-- exactly the behaviour they see now, and a genuine referential mistake still
-- fails at the statement that made it.
--
-- WHAT IT MAKES POSSIBLE is Stage 5's replica load. Design §7.2 replaces these
-- tables wholesale, which means deleting a row and re-inserting it with the
-- same id in one transaction. A foreign key checked at DELETE time fails on
-- that even though the row is back before COMMIT -- kea.bill_forecast's
-- reference to billing.account is the case that surfaced it, because Stage 5
-- deliberately does not load bill_forecast and therefore cannot reorder around
-- it. There is no delete order that avoids this in general: any full reload of
-- a cyclic-looking graph has some parent that a child still points at.
--
-- The loader issues SET CONSTRAINTS ALL DEFERRED inside its transaction, so
-- the whole load is checked once, at commit, against the finished state. That
-- is also a stronger guarantee than checking each statement: a load that would
-- leave a dangling reference fails as a unit rather than part-way through.
-- ---------------------------------------------------------------------
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT n.nspname AS sch, c.relname AS tbl, con.conname AS name
        FROM pg_constraint con
        JOIN pg_class c      ON c.oid = con.conrelid
        JOIN pg_namespace n  ON n.oid = c.relnamespace
        WHERE con.contype = 'f'
          AND NOT con.condeferrable
          AND n.nspname IN ('metering', 'billing', 'catalogue', 'kea')
    LOOP
        EXECUTE format('ALTER TABLE %I.%I ALTER CONSTRAINT %I DEFERRABLE INITIALLY IMMEDIATE',
                       r.sch, r.tbl, r.name);
    END LOOP;
END $$;


-- =====================================================================
-- SPRINT 3 -- graduated from kea_deferred_schema.sql on 2026-09-05
--
-- These four were deferred until the features needing them were built.
-- Sprint 3 builds them, so they live here now. hardship_case stays
-- deferred: it belongs in the `app` schema, created by the agent on
-- escalation, and the data side supplies the signals rather than opening
-- the case. outage and the Sprint 4 tables stay deferred untouched.
-- =====================================================================

CREATE TYPE common.alert_kind AS ENUM ('bill_shock', 'plan_saving', 'hardship_outreach',
                                'outage', 'usage_anomaly');
CREATE TYPE common.alert_channel AS ENUM ('in_app', 'email', 'sms', 'push');
CREATE TYPE common.alert_status AS ENUM ('pending', 'sent', 'delivered', 'failed', 'suppressed');

-- ---------------------------------------------------------------------
-- Payment (Sprint 3 / 4)
--
-- Cut from core because Sprint 2 needs only bill.payments_applied, the
-- single figure the bill prints. This table carries the transaction
-- detail that hardship triage runs on: late-payment streaks, failed
-- direct debits, part payments, arrears trajectory.
-- ---------------------------------------------------------------------
CREATE TABLE billing.payment (
    payment_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id      bigint         NOT NULL REFERENCES billing.account(account_id),
    -- Nullable: a payment does not always map to one bill once part
    -- payments and payment arrangements exist.
    bill_id         bigint         REFERENCES billing.bill(bill_id),
    paid_at         timestamptz    NOT NULL,
    amount          numeric(12,2)  NOT NULL,
    method          payment_method NOT NULL,
    -- A failed direct debit is one of the strongest early signals of
    -- payment difficulty.
    is_failed       boolean        NOT NULL DEFAULT false,
    failure_reason  text,
    created_at      timestamptz    NOT NULL DEFAULT now(),
    -- Replica freshness, like every other pipeline-loaded copy here. Omitted
    -- in the deferred draft, which predated the convention.
    synced_at       timestamptz    NOT NULL DEFAULT now(),
    CONSTRAINT payment_amount_positive CHECK (amount > 0)
);

-- ⚠️ THE SOURCE SPELLS THE METHOD DIFFERENTLY. src_billing.payments.PAY_METHOD
-- is 'DD' / 'BT' / 'CARD'; this column is the payment_method enum. The export
-- maps them. A pass-through would fail the enum at load time, which is the
-- good outcome -- the bad one is mapping 'BT' to 'other' and quietly losing
-- the distinction between a bank transfer and something unclassified.

CREATE INDEX idx_payment_account ON billing.payment(account_id, paid_at DESC);
CREATE INDEX idx_payment_bill    ON billing.payment(bill_id);
CREATE INDEX idx_payment_failed  ON billing.payment(account_id) WHERE is_failed;

-- ---------------------------------------------------------------------
-- Account plan history (Sprint 3)
--
-- Cut from core because Sprint 2 has static plan assignments and
-- bill.plan_id already records the plan as rated. Needed once customers
-- switch plans — which is the point of plan comparison.
-- ---------------------------------------------------------------------
CREATE TABLE billing.account_plan_history (
    history_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id      bigint  NOT NULL REFERENCES billing.account(account_id),
    plan_id         integer NOT NULL REFERENCES catalogue.plan(plan_id),
    -- The term is part of what was signed up to, not a separate fact. A row
    -- recording the plan without the term cannot answer "what did they
    -- commit to", which is the question this table exists for.
    term_months     smallint NOT NULL DEFAULT 0,
    valid_from      date    NOT NULL,
    valid_to        date,
    -- e.g. "switched after plan comparison" — lets you measure whether
    -- recommendations were acted on.
    change_reason   text,
    synced_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT aph_dates_ordered CHECK (valid_to IS NULL OR valid_to > valid_from),
    CONSTRAINT aph_term_valid    CHECK (term_months IN (0, 6, 12))
);

CREATE INDEX idx_aph_account ON billing.account_plan_history(account_id, valid_from DESC);

-- ---------------------------------------------------------------------
-- ⚠️ APPEND-ONLY. NEVER ADD THIS TABLE OR ITS BILL EXAMPLES TO serving.TABLES.
--
-- computed_at is in the unique key, which only means something if successive
-- computations coexist. A TRUNCATE-and-reload keeps the newest run and throws
-- the rest away, so "was this recommendation acted on?" -- the question
-- account_plan_history exists to answer -- would have nothing to join back to.
--
-- Plan recommendation (Outcome 2)
-- The re-rate across the whole catalogue is a batch job, not a
-- request-path computation. Storing the ranked result is what keeps the
-- comparison inside the latency budget.
--
-- The brief requires recommending a switch ONLY when it genuinely saves
-- money; is_recommended carries that decision explicitly rather than
-- leaving it to whoever renders the result.
--
-- IMPLEMENTATION WARNING — the most likely source of a wrong savings
-- figure. Re-rating usage against a candidate plan is not one re-banding
-- pass, it is one PER CHARGE GROUP. A real plan
-- (NZSZ_ZERO_STD_FXTOU_240901_T1) splits the same consumption into 2
-- bands for network charges and 3 for energy charges. Re-band each
-- charge group against that group's own tou_band rows, and remember
-- import lines are billed on (metered + losses), not metered alone.
-- Getting either wrong produces a plausible-looking number that is
-- silently incorrect, and the customer acts on it.
-- ---------------------------------------------------------------------
CREATE TABLE kea.plan_recommendation (
    recommendation_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id         bigint        NOT NULL REFERENCES billing.account(account_id),
    candidate_plan_id  integer       NOT NULL REFERENCES catalogue.plan(plan_id),
    -- The term is a DIMENSION of the recommendation, not a footnote. A credit
    -- of $300 a year beats most usage differences, so "Night Saver on twelve
    -- months" and "Night Saver flexible" can rank in either order and the
    -- table has to be able to hold both.
    term_months        smallint      NOT NULL DEFAULT 0,
    monthly_credit     numeric(12,2) NOT NULL DEFAULT 0,
    current_plan_id    integer       NOT NULL REFERENCES catalogue.plan(plan_id),
    -- The usage window the comparison was computed over.
    usage_period_start date          NOT NULL,
    usage_period_end   date          NOT NULL,
    current_annual_cost   numeric(12,2) NOT NULL,
    candidate_annual_cost numeric(12,2) NOT NULL,

    -- ⚠️ TWO SAVINGS, NOT ONE, and the split is the honesty of the feature.
    -- The brief requires recommending a switch ONLY when it genuinely saves
    -- money. With a term credit in play, "saves money" becomes ambiguous:
    -- a $300 figure that is entirely credit evaporates on leaving in month
    -- three, and a single blended number cannot say so.
    --
    --   usage_saving   unconditional -- what the plan itself saves
    --   term_credit    conditional   -- kept by honouring the term
    --
    -- is_recommended keys off usage_saving alone.
    usage_saving       numeric(12,2) NOT NULL,
    term_credit        numeric(12,2) NOT NULL DEFAULT 0,
    annual_saving      numeric(12,2) NOT NULL,   -- usage_saving + term_credit
    rank               smallint      NOT NULL,
    is_recommended     boolean       NOT NULL DEFAULT false,
    -- Plain-language reason, shown to the customer with the number.
    rationale          text,
    computed_at        timestamptz   NOT NULL DEFAULT now(),
    -- term_months is in the key: without it the three terms of one plan
    -- collide and only one survives.
    UNIQUE (account_id, candidate_plan_id, term_months, computed_at),
    CONSTRAINT rec_term_valid CHECK (term_months IN (0, 6, 12)),
    CONSTRAINT rec_saving_adds_up
        CHECK (ROUND(annual_saving, 2) = ROUND(usage_saving + term_credit, 2))
);

CREATE INDEX idx_plan_rec_account
    ON kea.plan_recommendation(account_id, computed_at DESC, rank);

-- APPEND-ONLY bill-sized evidence behind the annual recommendation. These are completed,
-- issued bill periods, but both modelled amounts are re-rated at the CURRENT
-- catalogue rates used by the recommendation generation.  `actual_bill_amount`
-- is kept beside them as a calibration point; it is not subtracted from the
-- candidate because historical discounts/versioning would then be mistaken
-- for a plan saving.
CREATE TABLE kea.plan_bill_example (
    example_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id             bigint        NOT NULL REFERENCES billing.account(account_id) DEFERRABLE,
    bill_id                bigint        NOT NULL REFERENCES billing.bill(bill_id) DEFERRABLE,
    current_plan_id        integer       NOT NULL REFERENCES catalogue.plan(plan_id) DEFERRABLE,
    candidate_plan_id      integer       NOT NULL REFERENCES catalogue.plan(plan_id) DEFERRABLE,
    term_months            smallint      NOT NULL DEFAULT 0,
    period_start           date          NOT NULL,
    period_end             date          NOT NULL,
    period_days            smallint      NOT NULL,
    import_kwh             numeric(12,4) NOT NULL,
    export_kwh             numeric(12,4) NOT NULL DEFAULT 0,
    current_discount_kwh   numeric(12,4) NOT NULL DEFAULT 0,
    candidate_discount_kwh numeric(12,4) NOT NULL DEFAULT 0,
    actual_bill_amount     numeric(12,2) NOT NULL,
    current_modelled_amount numeric(12,2) NOT NULL,
    candidate_modelled_amount numeric(12,2) NOT NULL,
    usage_saving           numeric(12,2) NOT NULL,
    monthly_credit         numeric(12,2) NOT NULL DEFAULT 0,
    current_energy_amount  numeric(12,2) NOT NULL,
    candidate_energy_amount numeric(12,2) NOT NULL,
    current_network_amount numeric(12,2) NOT NULL,
    candidate_network_amount numeric(12,2) NOT NULL,
    current_other_amount   numeric(12,2) NOT NULL,
    candidate_other_amount numeric(12,2) NOT NULL,
    computed_at            timestamptz   NOT NULL,
    UNIQUE (account_id, bill_id, computed_at),
    CONSTRAINT bill_example_term_valid CHECK (term_months IN (0, 6, 12)),
    CONSTRAINT bill_example_dates_ordered CHECK (period_end >= period_start),
    CONSTRAINT bill_example_days_match
        CHECK (period_days = period_end - period_start + 1),
    CONSTRAINT bill_example_usage_nonnegative
        CHECK (import_kwh >= 0 AND export_kwh >= 0
               AND current_discount_kwh >= 0 AND candidate_discount_kwh >= 0),
    CONSTRAINT bill_example_saving_adds_up
        CHECK (ROUND(usage_saving, 2) =
               ROUND(current_modelled_amount - candidate_modelled_amount, 2))
);

CREATE INDEX idx_plan_bill_example_account
    ON kea.plan_bill_example(account_id, computed_at DESC, period_end DESC);

-- ---------------------------------------------------------------------
-- ⚠️ APPEND-ONLY. NEVER ADD EITHER OF THESE TO serving.TABLES.
--
-- Hardship identification (Outcome 3) -- the half the PRD does not have.
--
-- The PRD's hardship trigger is conversational: the customer says something.
-- The regulation it cites -- the Electricity Authority's Consumer Care
-- Obligations -- asks the retailer to IDENTIFY customers in difficulty, and
-- the households at highest risk are frequently the ones who never say
-- anything. This pair is the identification half. It is a duty rather than a
-- feature, which is why it is a table and not a heuristic in the agent.
--
-- ⚠️ NO SCORE, AND THAT IS THE DESIGN RATHER THAN AN OMISSION.
--
-- Sprint 4's kea.hardship_risk_score assigns weights and a band. This does
-- not, because a score invites acting on the number and skipping the reasons,
-- and the obligation is precisely to justify why a SPECIFIC household was
-- flagged. The reasons are the output here, not metadata attached to one.
-- Which combinations of codes warrant outreach is a rule, and the rule is
-- recorded in ruleset_version rather than baked into a weight -- so a changed
-- policy is legible as a new version beside the old assessments, and a
-- decision made in August can still be explained in December.
--
-- WHY A PARENT ROW AT ALL, WHEN §E ASKS FOR "A PER-ACCOUNT SIGNAL ROW".
--
-- Signals alone cannot say "we looked and this household is fine". Absence of
-- a row would mean both "assessed, nothing found" and "never assessed", and
-- those are opposite answers to a regulator. The parent records that the
-- assessment happened; signal_count = 0 is a real and common result.
-- It also gives kea.alert one thing to reference: a hardship outreach is
-- triggered by the SET of signals, so an alert pointing at a single signal
-- row would misrepresent its own trigger.
--
-- ⚠️ MEDICALLY DEPENDENT AND VULNERABLE ARE NOT SIGNALS. They sit on the
-- parent as flags, deliberately outside hardship_signal_code. Being medically
-- dependent is not evidence of financial difficulty -- it is a care
-- obligation that changes what the retailer may DO (a medically dependent
-- household must not be disconnected), and it is often permanent. Filed as
-- signals they would inflate every count, and a household with one missed
-- payment and a medical dependency would read as twice the concern of a
-- household with one missed payment.
--
-- They are COPIED rather than joined because this is an append-only record of
-- what was known when the decision was made, and billing.customer is a
-- replica that the nightly load overwrites in place. Same argument as
-- bill_line storing its rate rather than joining to tariff_component.
-- ---------------------------------------------------------------------

-- A closed vocabulary, not free text. Stage 7 protects exactly one set of
-- reject reasons for the same reason: a code that can be invented at the call
-- site cannot be counted, cannot be reviewed, and cannot be explained
-- consistently to the household it was applied to. Adding a code is a
-- migration, which is the correct amount of friction for a vocabulary a
-- regulator may read.
--
--   direct_debit_failed    billing.payment.is_failed -- the strongest early
--                          signal, and the one the customer never reports
--   late_payment_streak    paid_at against bill.due_date, consecutive
--   part_payment_arrears   payments_applied < amount_due, compounding
--   payments_stopped       payments, then none -- distinct from never having
--                          paid, which is a new account, not a distressed one
--   usage_collapse         metering. Self-disconnection: the household that
--                          stops heating rather than ask. Costs no new data
--                          and finds the people who say nothing.
--
-- Sprint 4 (V005) adds three early-warning codes, described with
-- kea.hardship_outlook at the end of this file.
CREATE TYPE common.hardship_signal_code AS ENUM (
    'direct_debit_failed', 'late_payment_streak', 'part_payment_arrears',
    'payments_stopped', 'usage_collapse',
    'payment_drift', 'bill_above_usual_payment', 'first_shortfall');

CREATE TABLE kea.hardship_assessment (
    assessment_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id      bigint       NOT NULL REFERENCES billing.account(account_id),
    -- The business date the signals describe, which is not the same as when
    -- the job ran: a re-run in December of the rules as at 30 September must
    -- be storable, and must not claim to be current.
    assessed_on     date         NOT NULL,
    ruleset_version text         NOT NULL,
    -- Denormalised count so "how many households did we flag" needs no join.
    -- CHECK-able against the children, and asserted by the job.
    signal_count    smallint     NOT NULL DEFAULT 0,
    -- Care obligations as at assessment time. See the header: statuses, not
    -- signals.
    is_medically_dependent boolean NOT NULL DEFAULT false,
    is_vulnerable          boolean NOT NULL DEFAULT false,
    -- The window the signals were observed over, so a reader can tell a
    -- three-month view from a twelve-month one without reading the ruleset.
    observed_from   date         NOT NULL,
    observed_to     date         NOT NULL,
    computed_at     timestamptz  NOT NULL DEFAULT now(),
    -- computed_at in the key, exactly as plan_recommendation does it: two runs
    -- of the same day coexist rather than one overwriting the other.
    UNIQUE (account_id, assessed_on, computed_at),
    CONSTRAINT hardship_window_ordered CHECK (observed_to >= observed_from),
    CONSTRAINT hardship_count_non_negative CHECK (signal_count >= 0)
);

CREATE INDEX idx_hardship_account
    ON kea.hardship_assessment(account_id, assessed_on DESC, computed_at DESC);
-- The working query is "who did we flag in the latest run", so the partial
-- index covers the rows with any signal alone (since Sprint 4 that includes
-- warning signs, which are not flags). Every account is assessed every run and
-- most are clean, so indexing all of them to find the few is the wrong way
-- round -- the same shape as idx_alert_pending and idx_payment_failed.
CREATE INDEX idx_hardship_flagged
    ON kea.hardship_assessment(assessed_on DESC) WHERE signal_count > 0;

CREATE TABLE kea.hardship_signal (
    signal_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assessment_id   bigint       NOT NULL REFERENCES kea.hardship_assessment(assessment_id)
                                 ON DELETE CASCADE,
    signal_code     hardship_signal_code NOT NULL,
    -- The observed value as displayed, e.g. '3 failed direct debits' or
    -- '41 % below the same month last year'. Text because the codes measure
    -- different things and a numeric column would force a unit that does not
    -- exist; the machine-readable form is the code plus evidence_count.
    signal_value    text         NOT NULL,
    evidence_count  integer      NOT NULL DEFAULT 0,
    first_seen_on   date         NOT NULL,
    last_seen_on    date         NOT NULL,
    -- ⚠️ FOR STAFF, NEVER SHOWN TO THE CUSTOMER UNMEDIATED. "Our records show
    -- you have missed three direct debits" is a debt-collection letter, not
    -- care. The agent decides how to open the conversation; this is what it
    -- must be able to justify if asked.
    explanation     text         NOT NULL,
    UNIQUE (assessment_id, signal_code),
    CONSTRAINT hardship_signal_dates_ordered CHECK (last_seen_on >= first_seen_on),
    CONSTRAINT hardship_signal_evidence CHECK (evidence_count >= 0)
);

CREATE INDEX idx_hardship_signal_assessment ON kea.hardship_signal(assessment_id);
-- "Which households are self-disconnecting" is a question about one code
-- across all accounts, and it is the question this table exists to answer.
CREATE INDEX idx_hardship_signal_code ON kea.hardship_signal(signal_code, last_seen_on DESC);

-- ---------------------------------------------------------------------
-- ⚠️ APPEND-ONLY. NEVER ADD THIS TABLE TO serving.TABLES.
--
-- kea_deferred_schema.sql flagged this one as MIXED OWNERSHIP: the pipeline
-- creates the alert, and sent_at / delivered_at / opened_at / acted_at are
-- written at runtime by whoever delivered it.
--
-- Every strategy in kea_pipeline.serving REWRITES its table -- TRUNCATE,
-- DELETE_BY_PERIOD and PARTITION_SWAP alike. So an alert in the nightly load
-- path loses its delivery state every night: a message that was sent, landed
-- and was opened comes back 'pending' the next morning, and the engagement
-- data the feature exists to measure is destroyed on a schedule.
--
-- The deferred file proposed splitting the table. Append-only settles it more
-- simply and with a precedent already in this schema: kea.bill_forecast and
-- kea.mart_load_log both live here and are written by their own jobs rather
-- than by the export/load path. An alert is an EVENT. Once created it exists;
-- the pipeline inserts and never rewrites, so a runtime UPDATE is safe.
--
-- Alerts (Outcome 3, Stretch 1)
-- What was sent, to whom, through which channel, and whether it landed.
-- Delivery status matters: an undelivered bill-shock warning is
-- indistinguishable from never having sent one, from the customer's side.
-- ---------------------------------------------------------------------
CREATE TABLE kea.alert (
    alert_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id      bigint        NOT NULL REFERENCES billing.account(account_id),
    kind            alert_kind    NOT NULL,
    channel         alert_channel NOT NULL,
    status          alert_status  NOT NULL DEFAULT 'pending',
    subject         text,
    body            text          NOT NULL,
    -- Optional links to whatever triggered the alert.
    forecast_id     bigint        REFERENCES kea.bill_forecast(forecast_id),
    recommendation_id bigint      REFERENCES kea.plan_recommendation(recommendation_id),
    -- The assessment, not a single signal: a hardship outreach is triggered
    -- by the SET of codes, and an alert that names one of them misstates its
    -- own reason. Without this column alert_kind 'hardship_outreach' has
    -- nothing to point at, which is the one kind whose trigger a regulator
    -- may ask to see.
    assessment_id   bigint        REFERENCES kea.hardship_assessment(assessment_id),
    scheduled_for   timestamptz,
    sent_at         timestamptz,
    delivered_at    timestamptz,
    failure_reason  text,
    -- Engagement, for measuring whether proactive contact actually works.
    opened_at       timestamptz,
    acted_at        timestamptz,
    created_at      timestamptz   NOT NULL DEFAULT now(),
    -- A hardship message must always be traceable to the complete assessment
    -- that triggered it. Other alert kinds keep their trigger nullable.
    CONSTRAINT alert_hardship_has_assessment
        CHECK (kind <> 'hardship_outreach' OR assessment_id IS NOT NULL)
);

CREATE INDEX idx_alert_account ON kea.alert(account_id, created_at DESC);
CREATE INDEX idx_alert_pending ON kea.alert(scheduled_for) WHERE status = 'pending';
-- Retry of the alert job cannot duplicate one assessment, and a newly
-- computed assessment cannot create another outreach while the prior one is
-- still awaiting human/customer action. Runtime status updates retain their
-- ownership: acted_at or a terminal failed state removes the open-row guard.
CREATE UNIQUE INDEX uq_alert_hardship_assessment
    ON kea.alert(assessment_id)
    WHERE kind = 'hardship_outreach';
CREATE UNIQUE INDEX uq_alert_hardship_open_account
    ON kea.alert(account_id)
    WHERE kind = 'hardship_outreach'
      AND acted_at IS NULL
      AND status IN ('pending', 'sent', 'delivered', 'suppressed');


-- =====================================================================
-- SPRINT 4 -- the three stretch goals of Scenario 10 (migration V005)
--
--   appliance-level breakdown   kea.mart_daily_detected_load,
--                               kea.mart_charging_session, and the
--                               detected-load columns folded into
--                               mart_bill_explanation and mart_forecast_input
--   solar payback, EV shifting  kea.solar_estimate, kea.ev_scenario
--   hardship early warning      three codes folded into
--                               common.hardship_signal_code; kea.hardship_outlook
--
-- Design: kea-data-platform-design.md §15. See kea_deferred_schema.sql's
-- header for which drafts graduated and which stay deferred.
--
-- Every new foreign key is DEFERRABLE INITIALLY IMMEDIATE, as a publication
-- needs (see the note above the V003 loop earlier in this file).
-- =====================================================================

-- How sure a detection is. ASCENDING, so `confidence >= 'medium'` reads the
-- way it sounds: enum comparisons follow declaration order.
CREATE TYPE common.detection_confidence AS ENUM ('low', 'medium', 'high');

-- Early-warning codes. Like the five v1 codes, each is a fact about the
-- account, never a weight. None of them makes an outreach on its own:
-- create_hardship_alerts.py rule v2 needs bill_above_usual_payment TOGETHER
-- with payment_drift or first_shortfall.
--
--   payment_drift             each of the last three paid bills was paid
--                             later than the one before, still under a week
--                             late -- before late_payment_streak can fire
--   bill_above_usual_payment  the latest bill, or the open cycle's forecast,
--                             is well above what the account usually pays
--   first_shortfall           the first part payment after a run of bills
--                             paid in full -- before part_payment_arrears,
--                             which needs two
--   (the values are declared in common.hardship_signal_code above)


-- ---------------------------------------------------------------------
-- mart_daily_detected_load                           (export load, TRUNCATE)
--
-- EVIDENCE, NOT A BREAKDOWN. Daily totals cannot show a load that is only
-- visible half-hour by half-hour: an EV session, a hot-water timer. This
-- carries what the half-hourly import shows, per NZ day, and the backend
-- composes the breakdown from it (kea_deferred_schema.sql's
-- load_decomposition stays deferred for that reason).
--
-- PER DAY, NOT PER MONTH, because bills do not follow calendar months: any
-- billing period is a plain sum over its days, as mart_daily_usage is.
--
-- ONE ROW FOR EVERY ASSESSED DAY -- a day on which the uncontrolled import
-- register read all expected_intervals half-hours -- with zeros when nothing
-- was detected. No row means "not assessed", which is a different answer
-- from "assessed, nothing there".
--
-- ⚠️ A RANGE, NEVER ONE FIGURE. For each half-hour of a published run:
--   low   import minus the level just before and after the run
--   high  import minus the day's floor (floor_kwh_per_hh)
-- The floor itself belongs to base load, so high never counts a kWh the
-- base-load driver already counts. Each half-hour counts on its own date,
-- so a run across midnight is split between two rows.
-- ---------------------------------------------------------------------
CREATE TABLE kea.mart_daily_detected_load (
    icp_id                  bigint        NOT NULL,
    usage_date              date          NOT NULL,

    -- mart_daily_usage.base_load_kwh / 48: the day's smallest import half-hour,
    -- taken over the dark hours only where the ICP generates (design §8.4).
    floor_kwh_per_hh        numeric(10,4) NOT NULL,

    -- A load with the charging pattern of an EV. NOT "your EV": the meter
    -- sees a pattern, not a vehicle.
    ev_kwh_low              numeric(12,4) NOT NULL DEFAULT 0,
    ev_kwh_high             numeric(12,4) NOT NULL DEFAULT 0,
    ev_half_hours           smallint      NOT NULL DEFAULT 0,
    -- From the day's largest run: high_power is a wallbox (5 kW and up),
    -- low_power a portable charger. Never higher than medium confidence.
    -- ⚠️ ev_charger_class is NULL when the day's largest run was one the
    -- panels hid (detect-v2, V006): a shaved wallbox and a portable charger
    -- are the same signal. NULL is "we cannot tell", not "no EV found".
    ev_charger_class        text,
    ev_confidence           common.detection_confidence,

    -- A load that switches on at the same time most nights, all year round:
    -- often a water heater on a timer. A block that runs only in winter is
    -- NOT published here; it is left to the heating slope.
    timed_night_kwh_low     numeric(12,4) NOT NULL DEFAULT 0,
    timed_night_kwh_high    numeric(12,4) NOT NULL DEFAULT 0,
    timed_night_half_hours  smallint      NOT NULL DEFAULT 0,
    timed_night_confidence  common.detection_confidence,

    -- detect-v2 (V006): half-hours of this day the import register could not
    -- see in full -- exporting, or between the day's first and last export.
    -- 0 without panels and for every night; NULL on a pre-detect-v2 row,
    -- which measured no masking. A property of the DAY, not of its runs: a
    -- day that detected nothing can still be one the panels hid.
    masked_intervals        smallint,

    -- The dbt var detect_method_version. A changed threshold is a new version.
    method_version          text          NOT NULL,

    built_at                timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (icp_id, usage_date),
    CONSTRAINT detected_floor_non_negative CHECK (floor_kwh_per_hh >= 0),
    CONSTRAINT detected_ev_bounds
        CHECK (ev_kwh_low >= 0 AND ev_kwh_low <= ev_kwh_high),
    CONSTRAINT detected_night_bounds
        CHECK (timed_night_kwh_low >= 0 AND timed_night_kwh_low <= timed_night_kwh_high),
    CONSTRAINT detected_half_hours_in_day
        CHECK (ev_half_hours BETWEEN 0 AND 50 AND timed_night_half_hours BETWEEN 0 AND 50),
    -- Nothing detected means zeros and no labels. "Confidence null exactly
    -- when nothing was detected" still holds -- masking caps confidence at
    -- 'low', it never empties it -- but the class no longer follows: V006
    -- lets a detection carry no class at all.
    CONSTRAINT detected_ev_labelled
        CHECK ((ev_half_hours = 0) = (ev_confidence IS NULL)
           AND (ev_half_hours > 0 OR ev_charger_class IS NULL)
           AND (ev_half_hours > 0 OR ev_kwh_high = 0)),
    CONSTRAINT detected_night_labelled
        CHECK ((timed_night_half_hours = 0) = (timed_night_confidence IS NULL)
           AND (timed_night_half_hours > 0 OR timed_night_kwh_high = 0)),
    CONSTRAINT detected_ev_class
        CHECK (ev_charger_class IN ('high_power', 'low_power')),
    CONSTRAINT detected_masked_in_day
        CHECK (masked_intervals BETWEEN 0 AND 50)
    -- A masked DAY may still have a clear night-time low_power run. V006
    -- checks masked claims at session grain using masked_half_hours.
);


-- ---------------------------------------------------------------------
-- mart_charging_session                              (export load, TRUNCATE)
--
-- The EV-pattern runs themselves, for the shift-to-night scenario and for
-- staff. A run is consecutive half-hours on the continuous timeline, so a
-- missing read ends it, and its length in absolute time is exactly
-- half_hours x 30 minutes, across a DST change too. No two sessions of one
-- ICP overlap: a dbt test, since a CHECK cannot compare rows.
-- ---------------------------------------------------------------------
CREATE TABLE kea.mart_charging_session (
    icp_id          bigint        NOT NULL,
    -- Start of the first half-hour, end of the last (exclusive). UTC, like
    -- metering.meter_read_hh.interval_start.
    session_start   timestamptz   NOT NULL,
    session_end     timestamptz   NOT NULL,
    half_hours      smallint      NOT NULL,
    kwh_low         numeric(12,4) NOT NULL,
    kwh_high        numeric(12,4) NOT NULL,
    -- ⚠️ NULLABLE since detect-v2 (V006). NULL means the panels hid part of
    -- this session and the two charger kinds cannot be told apart -- NOT
    -- that no EV was found. The session is published either way.
    charger_class   text,
    confidence      common.detection_confidence NOT NULL,
    -- Half-hours of this session the panels hid. NULL before detect-v2.
    masked_half_hours smallint,
    method_version  text          NOT NULL,
    built_at        timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (icp_id, session_start),
    CONSTRAINT charging_session_length
        CHECK (half_hours > 0
           AND session_end = session_start + half_hours * interval '30 minutes'),
    CONSTRAINT charging_session_bounds CHECK (kwh_low >= 0 AND kwh_low <= kwh_high),
    CONSTRAINT charging_session_class
        CHECK (charger_class IS NULL
            OR charger_class IN ('high_power', 'low_power')),
    CONSTRAINT charging_session_masking
        CHECK (masked_half_hours BETWEEN 0 AND half_hours
           AND (masked_half_hours = 0
                OR (confidence = 'low'
                    AND (charger_class IS NULL
                      OR charger_class = 'high_power'))))
);


-- ---------------------------------------------------------------------
-- ⚠️ APPEND-ONLY. NEVER ADD THESE TWO TO serving.TABLES.
--
-- Written inside the publication by build_solar_estimates.py and
-- build_ev_scenarios.py, like plan_recommendation, and reset only by
-- make rebuild-all. computed_at is left to its DEFAULT, so every row of one
-- publication carries that transaction's time; publish_serving.validate()
-- relies on it to tell this publication's rows from history.
--
-- WHAT-IFS, NOT MEASUREMENTS. Every row names its assumptions_version
-- (pipeline/scripts/decisions/estimate_assumptions.py) and the metered window it
-- replays. Paybacks are simple: no panel or battery degradation, no price
-- change.
-- ---------------------------------------------------------------------

-- Solar payback, for an account with no export register today.
-- Output comes from a regional yield table (low and high kWh per kW per day,
-- by month), spread over daylight half-hours, netted against the account's
-- real half-hourly import and priced on its current plan.
--
-- ⚠️ UP TO NINE ROWS PER ACCOUNT PER PUBLICATION: each system size with no
-- battery (V007), a battery storing only the home's surplus (V007) and, on a
-- plan with more than one import rate, a battery also charged from the grid
-- (V008). `battery_kwh = 0` is the three panels-only rows. A battery row is
-- not always the better row: where the buy-back is close to the rate the
-- stored energy avoids, the round-trip loss can make it save less than its
-- sibling, and it is published all the same.
CREATE TABLE kea.solar_estimate (
    estimate_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id              bigint        NOT NULL
                            REFERENCES billing.account(account_id) DEFERRABLE INITIALLY IMMEDIATE,
    -- The plan both costs were priced on: the account's plan when computed.
    plan_id                 integer       NOT NULL
                            REFERENCES catalogue.plan(plan_id) DEFERRABLE INITIALLY IMMEDIATE,
    system_kw               numeric(4,1)  NOT NULL,
    -- Usable size of the home battery assumed (V007); 0 is panels alone.
    battery_kwh             numeric(4,1)  NOT NULL DEFAULT 0,
    -- What the battery does beyond storing the home's surplus (V008). NULL:
    -- no battery. self_consumption: nothing. grid_charge: also tops up from
    -- the grid in the plan's cheapest half-hours; only on a plan with more
    -- than one import rate.
    battery_dispatch        text,
    -- The yield table's region (Auckland, Waikato, Wellington, Christchurch),
    -- so the answer can say whose sunshine it assumed.
    weather_region          text          NOT NULL,
    basis_from              date          NOT NULL,
    basis_to                date          NOT NULL,

    annual_generation_kwh_low   numeric(10,1) NOT NULL,
    annual_generation_kwh_high  numeric(10,1) NOT NULL,
    -- Share of the generation used in the home rather than exported; on a
    -- battery row, the solar share of what the home drew from the battery
    -- counts too. Grid energy never does.
    self_used_pct_low       numeric(5,2)  NOT NULL,
    self_used_pct_high      numeric(5,2)  NOT NULL,
    annual_saving_nzd_low   numeric(12,2) NOT NULL,
    annual_saving_nzd_high  numeric(12,2) NOT NULL,
    -- Panels, plus the battery on a battery row.
    install_cost_nzd_low    numeric(12,2) NOT NULL,
    install_cost_nzd_high   numeric(12,2) NOT NULL,
    -- low = lowest cost / highest saving; high = highest cost / lowest
    -- saving. NULL when that saving is zero: there is no payback to state.
    -- Wide on purpose: a tiny positive saving gives a huge, honest figure,
    -- and the wording, not the column, decides how to say "never".
    payback_years_low       numeric(8,1),
    payback_years_high      numeric(8,1),

    assumptions_version     text          NOT NULL,
    computed_at             timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT solar_estimate_account_size_battery_dispatch_computed_key
        UNIQUE NULLS NOT DISTINCT
            (account_id, system_kw, battery_kwh, battery_dispatch, computed_at),
    CONSTRAINT solar_estimate_kw_positive CHECK (system_kw > 0),
    CONSTRAINT solar_estimate_battery_non_negative CHECK (battery_kwh >= 0),
    CONSTRAINT solar_estimate_battery_dispatch
        CHECK ((battery_kwh = 0) = (battery_dispatch IS NULL)
           AND (battery_dispatch IS NULL
                OR battery_dispatch IN ('self_consumption', 'grid_charge'))),
    CONSTRAINT solar_estimate_basis_ordered CHECK (basis_to >= basis_from),
    CONSTRAINT solar_estimate_generation_bounds
        CHECK (annual_generation_kwh_low >= 0
           AND annual_generation_kwh_low <= annual_generation_kwh_high),
    CONSTRAINT solar_estimate_self_used_bounds
        CHECK (self_used_pct_low >= 0 AND self_used_pct_low <= self_used_pct_high
           AND self_used_pct_high <= 100),
    CONSTRAINT solar_estimate_saving_bounds
        CHECK (annual_saving_nzd_low <= annual_saving_nzd_high),
    CONSTRAINT solar_estimate_cost_bounds
        CHECK (install_cost_nzd_low > 0 AND install_cost_nzd_low <= install_cost_nzd_high),
    -- If even the best case has no payback, neither has the worst case.
    CONSTRAINT solar_estimate_payback_bounds
        CHECK ((payback_years_low IS NOT NULL OR payback_years_high IS NULL)
           AND payback_years_low >= 0
           AND payback_years_low <= payback_years_high)
);

CREATE INDEX idx_solar_estimate_account
    ON kea.solar_estimate(account_id, computed_at DESC, system_kw, battery_kwh,
                          battery_dispatch);

-- EV charging what-ifs.
--
--   add_ev          every cohort account: a year of charging from the
--                   assumed annual km, starting at 18:00 (evening, the
--                   baseline) or 23:00 (night), on each Kea plan
--   shift_detected  accounts with a wallbox-shaped session -- high_power,
--                   or PV-masked and therefore unclassed (V006) -- each
--                   session's kwh_low as it happened (as_detected, the
--                   baseline) or moved to 23:00 the same night, on the
--                   current plan and on Time of Use. On a flat plan the
--                   saving is honestly 0.
--   solar_soak      generating accounts, both scenarios: the charging placed
--                   in the half-hours the meter actually exported, most
--                   first. ⚠️ May carry a NEGATIVE saving -- on Time of Use
--                   the night rate sometimes still wins, and a losing row is
--                   published rather than suppressed.
--
-- annual_bill_nzd is the whole household's modelled annual bill with that
-- charging, so plans compare directly; annual_ev_cost_nzd is the part the
-- charging adds on that plan. saving_vs_baseline_nzd compares this row with
-- the same account, scenario and plan's baseline pattern.
CREATE TABLE kea.ev_scenario (
    scenario_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id              bigint        NOT NULL
                            REFERENCES billing.account(account_id) DEFERRABLE INITIALLY IMMEDIATE,
    scenario                text          NOT NULL,
    plan_id                 integer       NOT NULL
                            REFERENCES catalogue.plan(plan_id) DEFERRABLE INITIALLY IMMEDIATE,
    current_plan_id         integer       NOT NULL
                            REFERENCES catalogue.plan(plan_id) DEFERRABLE INITIALLY IMMEDIATE,
    charging_pattern        text          NOT NULL,
    basis_from              date          NOT NULL,
    basis_to                date          NOT NULL,

    annual_ev_kwh           numeric(10,1) NOT NULL,
    annual_bill_nzd         numeric(12,2) NOT NULL,
    annual_ev_cost_nzd      numeric(12,2) NOT NULL,
    saving_vs_baseline_nzd  numeric(12,2) NOT NULL,

    assumptions_version     text          NOT NULL,
    computed_at             timestamptz   NOT NULL DEFAULT now(),
    UNIQUE (account_id, scenario, plan_id, charging_pattern, computed_at),
    CONSTRAINT ev_scenario_pattern
        CHECK ((scenario = 'add_ev'
                AND charging_pattern IN ('evening', 'night', 'solar_soak'))
            OR (scenario = 'shift_detected'
                AND charging_pattern IN ('as_detected', 'night', 'solar_soak'))),
    CONSTRAINT ev_scenario_baseline_saves_nothing
        CHECK (charging_pattern IN ('night', 'solar_soak')
            OR saving_vs_baseline_nzd = 0),
    CONSTRAINT ev_scenario_basis_ordered CHECK (basis_to >= basis_from),
    CONSTRAINT ev_scenario_kwh_non_negative CHECK (annual_ev_kwh >= 0)
);

CREATE INDEX idx_ev_scenario_account
    ON kea.ev_scenario(account_id, computed_at DESC, scenario);


-- ---------------------------------------------------------------------
-- ⚠️ APPEND-ONLY. NEVER ADD THIS TABLE TO serving.TABLES.
--
-- hardship_outlook -- the "weeks ahead" half of the early warning, written
-- by build_hardship_assessments.py (ruleset hardship-identification-v2) when
-- payment_drift or first_shortfall is present.
--
-- ⚠️ STILL NO SCORE. V001's rule stands: this is a date and an amount, with
-- the arithmetic that produced them. A staff member can check every step of
-- `basis`; nobody can check a weight.
--
-- One row per assessment at most, hence the key. A CHECK cannot read the
-- parent, so projected_arrears_on > assessed_on is asserted by the job and
-- by a Soda check.
-- ---------------------------------------------------------------------
CREATE TABLE kea.hardship_outlook (
    assessment_id           bigint        PRIMARY KEY
                            REFERENCES kea.hardship_assessment(assessment_id)
                            ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    -- The first date the projected balance meets the arrears definition:
    -- a bill still at least 5 % unpaid 30 days after its due date.
    projected_arrears_on    date          NOT NULL,
    projected_overdue_nzd   numeric(12,2) NOT NULL,
    -- ⚠️ FOR STAFF, NEVER SHOWN TO THE CUSTOMER UNMEDIATED -- the same rule
    -- as hardship_signal.explanation.
    basis                   text          NOT NULL,
    CONSTRAINT hardship_outlook_overdue_positive CHECK (projected_overdue_nzd > 0)
);
