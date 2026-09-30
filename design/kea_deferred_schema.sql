-- =====================================================================
-- Kea Energy — DEFERRED SCHEMA (targets PostgreSQL 17; developed against 16)
-- Sprint 3 and beyond. Reference DDL, NEVER APPLIED: nothing runs this file,
-- and no Make target points at it.
--
-- ⚠️ HOW TO ADOPT A BLOCK. The served schema has been migration-managed since
-- 2026-09-13: migrate_serving.py (`make publish-serving MIGRATE=1`) applies migrations/serving (V001 is the
-- frozen initial DDL, V005 is Sprint 4), and kea_serving_schema.sql is the
-- readable reference for what those migrations have built. So adopting
-- anything below means writing the NEXT migration -- migrations/serving/README.md
-- names its number -- and copying the amended definition into
-- kea_serving_schema.sql, exactly as Sprint 3 and Sprint 4 did. Running this file against a served database is never the
-- route, and against a migrated one it would drift the two apart.
--
-- Schemas follow the same split by writer:
--   metering / billing / catalogue   replicas, pipeline-loaded
--   kea                              derived marts, pipeline-loaded
--   app                              written by the agent at runtime, and
--                                    defined in the application's repo,
--                                    not here -- see the note on
--                                    hardship_case.conversation_id below
--
-- One table here still has MIXED ownership and needs a decision when built:
--   hardship_case  created by the agent on escalation -> belongs in app
--
-- alert was the other one. It graduated on 2026-09-05 and the split this
-- file proposed was NOT made: kea.alert is APPEND-ONLY in
-- kea_serving_schema.sql, so the pipeline inserts and never rewrites and a
-- runtime UPDATE to sent_at / delivered_at / opened_at / acted_at is safe.
-- Do not re-derive the split from this file's history.
--
-- Note the split cuts across features: solar_installation is metering
-- data, while load_decomposition (our inference from it) is ours.
--
-- Each section is independent: take only the block for the feature you are
-- building. None of these require altering an existing table, so a migration
-- that adds one is additive and they can be adopted one at a time -- but each
-- is still a migration, not a CREATE TABLE run by hand.
--
--   Sprint 3  : hardship_case (app-owned), outage
--               -- payment, account_plan_history, plan_recommendation and
--               alert GRADUATED to kea_serving_schema.sql on 2026-09-05,
--               with three amendments the originals here did not have:
--               account_plan_history gained term_months, and
--               plan_recommendation gained term_months, monthly_credit and
--               a usage_saving / term_credit split of annual_saving.
--   Any time  : meter (optional — see below)
--   Sprint 4  : NONE of the drafts below graduated. Sprint 4 (migration
--               V005, 2026-09-17) built the stretch goals from metered
--               evidence instead, in kea_serving_schema.sql:
--                 load_decomposition  -> kea.mart_daily_detected_load and
--                                        kea.mart_charging_session: daily
--                                        evidence with low/high bounds;
--                                        the backend composes the breakdown
--                 solar_installation, -> kea.solar_estimate and
--                 ev_vehicle             kea.ev_scenario: what-ifs priced on
--                                        the real half-hourly import; no
--                                        source record of panels or vehicles
--                 hardship_risk_score -> three reason codes on
--                 risk_reason_code       common.hardship_signal_code and
--                                        kea.hardship_outlook: a date and an
--                                        amount, still no score (decision
--                                        log, 5 Sep and 17 Sep)
--               These stay here, deferred: solar_installation, ev_vehicle,
--               hardship_risk_score, risk_reason_code, load_decomposition,
--               and the enums common.load_category, common.risk_band and
--               common.panel_orientation. Graduating any of them is a new
--               decision, not a follow-up.
--
--               ⚠️ THE HOME BATTERY DID NOT COME FROM HERE EITHER (V007,
--               2026-09-30). solar_installation.has_battery / battery_kwh
--               describe panels a household ALREADY has: there is no source
--               for that record, and build_solar_estimates.py skips every
--               account that already exports. ev_vehicle.battery_kwh is the
--               car's battery. The what-if battery is kea.solar_estimate's
--               battery_kwh column instead, priced from assumptions like the
--               panels it sits beside.
-- =====================================================================

SET search_path TO kea, app, billing, metering, catalogue, common, public;

-- ---------------------------------------------------------------------
-- Enumerations for the deferred features
-- ---------------------------------------------------------------------
CREATE TYPE common.case_status AS ENUM ('open', 'in_progress', 'resolved', 'closed');
CREATE TYPE common.risk_band AS ENUM ('low', 'moderate', 'elevated', 'high');
CREATE TYPE common.load_category AS ENUM ('base_load', 'heating', 'hot_water', 'ev', 'other');
CREATE TYPE common.panel_orientation AS ENUM ('north', 'north_east', 'north_west',
                                       'east', 'west', 'south', 'mixed');

-- =====================================================================
-- SPRINT 3
-- =====================================================================

-- ---------------------------------------------------------------------
-- Meter (optional, any time)
--
-- Sits logically between icp and meter_register. Cut from core because
-- neither Sprint 2 feature reads serial numbers or install dates.
--
-- To adopt (in one migration): create this table, add a nullable meter_id to
-- meter_register, backfill, then tighten to NOT NULL if wanted. It does
-- NOT touch meter_read_hh, which is why deferring it was safe.
-- ---------------------------------------------------------------------
CREATE TABLE metering.meter (
    meter_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    icp_id          bigint      NOT NULL REFERENCES metering.icp(icp_id),
    serial_number   text        NOT NULL,
    -- Legacy meters cannot produce half-hourly data; this flags
    -- households whose interval data will be absent or estimated.
    is_smart_meter  boolean     NOT NULL DEFAULT true,
    installed_on    date,
    removed_on      date,
    -- NULLS NOT DISTINCT (PG15+): without it, two rows with the same
    -- serial and a NULL installed_on would both be allowed.
    UNIQUE NULLS NOT DISTINCT (serial_number, installed_on)
);

CREATE INDEX idx_meter_icp ON metering.meter(icp_id);

-- ALTER TABLE metering.meter_register ADD COLUMN meter_id bigint REFERENCES metering.meter(meter_id);
-- CREATE INDEX idx_register_meter ON metering.meter_register(meter_id);

-- ---------------------------------------------------------------------
-- Hardship case (Outcome 4)
-- The escalation record. When the agent hands to a human, this is what
-- the human sees — so the summary and recommended action are stored,
-- not regenerated.
-- ---------------------------------------------------------------------
CREATE TABLE kea.hardship_case (
    case_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id        bigint      NOT NULL REFERENCES billing.account(account_id),
    -- Deliberately no REFERENCES: conversation lives in the app schema,
    -- which the application owns and this repo does not define, so the
    -- target table does not exist here to point at. hardship_case is
    -- itself created by the agent on escalation, so when the feature is
    -- built it belongs in app too, not kea -- and the FK becomes a
    -- local one at that point.
    conversation_id   bigint,
    status            case_status NOT NULL DEFAULT 'open',
    opened_at         timestamptz NOT NULL DEFAULT now(),
    -- Context handed to the human agent.
    context_summary   text        NOT NULL,
    recommended_action text,
    -- Consumer Care: disconnection risk and medical dependency change
    -- what the retailer is obliged to do, so they are explicit columns.
    disconnection_risk boolean    NOT NULL DEFAULT false,
    arrears_amount    numeric(12,2),
    assigned_to       text,
    resolution        text,
    resolved_at       timestamptz,
    CONSTRAINT case_dates_ordered
        CHECK (resolved_at IS NULL OR resolved_at >= opened_at)
);

CREATE INDEX idx_case_account ON kea.hardship_case(account_id, opened_at DESC);
CREATE INDEX idx_case_open ON kea.hardship_case(status) WHERE status IN ('open','in_progress');

-- ---------------------------------------------------------------------
-- Outage (Outcome 5)
-- ---------------------------------------------------------------------
CREATE TABLE metering.outage (
    outage_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    network_id      integer     NOT NULL REFERENCES metering.network(network_id),
    reference       text,
    is_planned      boolean     NOT NULL DEFAULT false,
    started_at      timestamptz NOT NULL,
    estimated_restore_at timestamptz,
    restored_at     timestamptz,
    cause           text,
    affected_area   text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_outage_active ON metering.outage(network_id, started_at DESC)
    WHERE restored_at IS NULL;

-- Which connections an outage affects. Many-to-many, because one outage
-- covers many ICPs and an ICP may see several over time.
CREATE TABLE metering.outage_icp (
    outage_id       bigint NOT NULL REFERENCES metering.outage(outage_id) ON DELETE CASCADE,
    icp_id          bigint NOT NULL REFERENCES metering.icp(icp_id),
    PRIMARY KEY (outage_id, icp_id)
);

CREATE INDEX idx_outage_icp_lookup ON metering.outage_icp(icp_id);


-- =====================================================================
-- SPRINT 4 (stretch goals)
-- =====================================================================
-- ---------------------------------------------------------------------
-- Solar installation
-- Feeds the Sprint 4 payback model (KEA-540 / KEA-542). Capacity,
-- orientation and tilt are the inputs PVWatts needs to model generation;
-- install cost is what payback is measured against.
-- ---------------------------------------------------------------------
CREATE TABLE metering.solar_installation (
    installation_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    icp_id            bigint            NOT NULL REFERENCES metering.icp(icp_id),
    capacity_kw       numeric(8,3)      NOT NULL,
    panel_count       smallint,
    orientation       panel_orientation,
    azimuth_degrees   numeric(5,2),          -- 0 = north, clockwise
    tilt_degrees      numeric(5,2),
    has_battery       boolean           NOT NULL DEFAULT false,
    battery_kwh       numeric(8,3),
    install_date      date              NOT NULL,
    install_cost_nzd  numeric(12,2),
    decommissioned_on date,
    created_at        timestamptz       NOT NULL DEFAULT now(),
    CONSTRAINT solar_capacity_positive CHECK (capacity_kw > 0),
    CONSTRAINT solar_battery_consistent
        CHECK ((has_battery = false AND battery_kwh IS NULL)
               OR (has_battery = true AND battery_kwh > 0)),
    CONSTRAINT solar_dates_ordered
        CHECK (decommissioned_on IS NULL OR decommissioned_on >= install_date)
);

CREATE INDEX idx_solar_icp ON metering.solar_installation(icp_id);
CREATE INDEX idx_solar_active ON metering.solar_installation(icp_id)
    WHERE decommissioned_on IS NULL;

-- ---------------------------------------------------------------------
-- Hardship risk score (Stretch 1)
--
-- Deliberately NOT a trained model's output. This is a weighted
-- rule-based triage score, and every flag must be explainable — the
-- Electricity Authority's consumer-care obligations mean Kea has to
-- justify why a specific vulnerable household was flagged, and "the
-- model said so" is not an acceptable answer.
--
-- Hence risk_reason_code as a first-class child table rather than a
-- JSON blob: the reasons are the point, not metadata.
-- ---------------------------------------------------------------------
CREATE TABLE kea.hardship_risk_score (
    score_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id      bigint       NOT NULL REFERENCES billing.account(account_id),
    assessed_on     date         NOT NULL,
    total_score     numeric(6,2) NOT NULL,
    band            risk_band    NOT NULL,
    ruleset_version text         NOT NULL,
    -- Whether an outreach was actually triggered, and the resulting case.
    outreach_sent   boolean      NOT NULL DEFAULT false,
    case_id         bigint       REFERENCES kea.hardship_case(case_id),
    created_at      timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (account_id, assessed_on)
);

CREATE INDEX idx_risk_account ON kea.hardship_risk_score(account_id, assessed_on DESC);
CREATE INDEX idx_risk_elevated ON kea.hardship_risk_score(assessed_on)
    WHERE band IN ('elevated','high');

CREATE TABLE kea.risk_reason_code (
    reason_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    score_id        bigint       NOT NULL REFERENCES kea.hardship_risk_score(score_id)
                                 ON DELETE CASCADE,
    -- e.g. 'late_payment_streak', 'direct_debit_failed',
    --      'usage_drop_self_disconnection', 'arrears_trajectory'
    signal_code     text         NOT NULL,
    signal_value    text,                    -- the observed value, for display
    weight          numeric(6,2) NOT NULL,
    contribution    numeric(6,2) NOT NULL,
    -- Plain-language explanation shown to staff, never to the customer
    -- unmediated.
    explanation     text         NOT NULL,
    UNIQUE (score_id, signal_code)
);

CREATE INDEX idx_reason_score ON kea.risk_reason_code(score_id);

-- ---------------------------------------------------------------------
-- EV (Stretch 2)
-- Solar is covered by solar_installation; this is the other half.
-- Load-shifting saving = re-rate current charging profile, re-rate the
-- shifted profile, subtract.
-- ---------------------------------------------------------------------
CREATE TABLE kea.ev_vehicle (
    vehicle_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id       bigint       NOT NULL REFERENCES billing.account(account_id),
    make_model       text,
    battery_kwh      numeric(8,3),
    charger_kw       numeric(6,2),           -- 7.0 typical for home AC
    weekly_km        numeric(8,2),
    efficiency_kwh_per_100km numeric(6,2),
    -- Observed vs recommended charging windows.
    typical_charge_start time,
    typical_charge_end   time,
    added_on         date         NOT NULL DEFAULT CURRENT_DATE,
    removed_on       date
);

CREATE INDEX idx_ev_account ON kea.ev_vehicle(account_id);

-- ---------------------------------------------------------------------
-- Load decomposition (Stretch 3)
--
-- Coarse categories only, at 30-minute resolution — the resolution a NZ
-- smart meter actually records.
--
-- confidence_low / confidence_high are NOT NULL by design. The agent
-- must speak in ranges ("roughly 30-40% of your bill is hot water") and
-- must say nothing at all when confidence is too low. A point estimate
-- here would be the schema inviting a claim the data cannot support.
-- ---------------------------------------------------------------------
CREATE TABLE kea.load_decomposition (
    decomposition_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    icp_id           bigint        NOT NULL REFERENCES metering.icp(icp_id),
    period_start     date          NOT NULL,
    period_end       date          NOT NULL,
    category         load_category NOT NULL,
    estimated_kwh    numeric(12,4) NOT NULL,
    confidence_low   numeric(12,4) NOT NULL,
    confidence_high  numeric(12,4) NOT NULL,
    share_of_total   numeric(5,4)  NOT NULL,
    estimated_cost   numeric(12,2),
    -- Below this, the agent reports nothing for the category rather than
    -- reporting a wide useless range.
    is_reportable    boolean       NOT NULL DEFAULT true,
    method_version   text          NOT NULL,
    computed_at      timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT decomp_bounds_ordered
        CHECK (confidence_low <= estimated_kwh AND estimated_kwh <= confidence_high),
    CONSTRAINT decomp_share_valid CHECK (share_of_total BETWEEN 0 AND 1),
    UNIQUE (icp_id, period_start, category, method_version)
);

CREATE INDEX idx_decomp_icp ON kea.load_decomposition(icp_id, period_end DESC);

