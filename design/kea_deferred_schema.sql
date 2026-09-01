-- =====================================================================
-- Kea Energy — DEFERRED SCHEMA (targets PostgreSQL 17; developed against 16)
-- Sprint 3 and beyond. Run kea_serving_schema.sql first.
--
-- Schemas follow the same split by writer:
--   metering / billing / catalogue   replicas, pipeline-loaded
--   kea                              derived marts, pipeline-loaded
--   app                              written by the agent at runtime, and
--                                    defined in the application's repo,
--                                    not here -- see the note on
--                                    hardship_case.conversation_id below
--
-- Two tables here have MIXED ownership and need splitting when built:
--   alert          created by the pipeline, but sent_at / delivered_at /
--                  opened_at / acted_at are written at runtime
--   hardship_case  created by the agent on escalation -> belongs in app
--
-- Note the split cuts across features: solar_installation is metering
-- data, while load_decomposition (our inference from it) is ours.
--
-- Each section is independent: apply only the block for the feature you
-- are building. None of these require altering an existing table, so
-- there is no migration cost to adding them one at a time.
--
--   Sprint 3  : payment, account_plan_history, plan_recommendation,
--               alert, hardship_case, outage
--   Any time  : meter (optional — see below)
--   Sprint 4  : solar_installation, ev_vehicle, hardship_risk_score,
--               risk_reason_code, load_decomposition
-- =====================================================================

SET search_path TO kea, app, billing, metering, catalogue, common, public;

-- ---------------------------------------------------------------------
-- Enumerations for the deferred features
-- ---------------------------------------------------------------------
CREATE TYPE common.alert_kind AS ENUM ('bill_shock', 'plan_saving', 'hardship_outreach',
                                'outage', 'usage_anomaly');
CREATE TYPE common.alert_channel AS ENUM ('in_app', 'email', 'sms', 'push');
CREATE TYPE common.alert_status AS ENUM ('pending', 'sent', 'delivered', 'failed', 'suppressed');
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
-- To adopt: create this table, add a nullable meter_id to
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
    CONSTRAINT payment_amount_positive CHECK (amount > 0)
);

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
    valid_from      date    NOT NULL,
    valid_to        date,
    -- e.g. "switched after plan comparison" — lets you measure whether
    -- recommendations were acted on.
    change_reason   text,
    CONSTRAINT aph_dates_ordered CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE INDEX idx_aph_account ON billing.account_plan_history(account_id, valid_from DESC);

-- ---------------------------------------------------------------------
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
    current_plan_id    integer       NOT NULL REFERENCES catalogue.plan(plan_id),
    -- The usage window the comparison was computed over.
    usage_period_start date          NOT NULL,
    usage_period_end   date          NOT NULL,
    current_annual_cost   numeric(12,2) NOT NULL,
    candidate_annual_cost numeric(12,2) NOT NULL,
    annual_saving      numeric(12,2) NOT NULL,
    rank               smallint      NOT NULL,
    is_recommended     boolean       NOT NULL DEFAULT false,
    -- Plain-language reason, shown to the customer with the number.
    rationale          text,
    computed_at        timestamptz   NOT NULL DEFAULT now(),
    UNIQUE (account_id, candidate_plan_id, computed_at)
);

CREATE INDEX idx_plan_rec_account
    ON kea.plan_recommendation(account_id, computed_at DESC, rank);

-- ---------------------------------------------------------------------
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
    scheduled_for   timestamptz,
    sent_at         timestamptz,
    delivered_at    timestamptz,
    failure_reason  text,
    -- Engagement, for measuring whether proactive contact actually works.
    opened_at       timestamptz,
    acted_at        timestamptz,
    created_at      timestamptz   NOT NULL DEFAULT now()
);

CREATE INDEX idx_alert_account ON kea.alert(account_id, created_at DESC);
CREATE INDEX idx_alert_pending ON kea.alert(scheduled_for) WHERE status = 'pending';

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

