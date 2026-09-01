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
CREATE TYPE common.charge_group AS ENUM ('network', 'energy', 'other');

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
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT account_dates_ordered
        CHECK (end_date IS NULL OR end_date >= start_date),
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
    -- The only signed column in the schema: negative for export credits,
    -- which is how they print on the bill, e.g. ($6.33).
    amount          numeric(12,2)  NOT NULL,   -- excl GST
    sort_order      smallint       NOT NULL DEFAULT 0,
    CONSTRAINT bill_line_sign_matches_direction
        CHECK ((direction = 'import' AND amount >= 0)
               OR (direction = 'export' AND amount <= 0)),
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
    amount_delta_pct    numeric(6,2),
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
