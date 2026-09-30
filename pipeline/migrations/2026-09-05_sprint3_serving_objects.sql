-- Sprint 3 objects that exist in design/kea_serving_schema.sql and never
-- reached a database that already existed.
--
-- ⚠️ THAT FILE IS A FRESH-INSTALL SCRIPT. Its CREATE TYPE and CREATE TABLE
-- carry no IF NOT EXISTS, so adding an object to it does not migrate a running
-- store -- re-applying it makes every CREATE error on what is already there.
-- (The same is true of the source DDL, which is why `make init-sources`
-- probes before applying it.) The same trap hid the charge_group enum, found only
-- when the serving load rejected all 2,392 term-credit lines.
--
-- Generated from that file rather than retyped, so the two cannot drift.

SET search_path TO kea, billing, metering, catalogue, common, public;

BEGIN;

ALTER TABLE billing.account
    ADD COLUMN IF NOT EXISTS term_months smallint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS term_start  date;
DO $$ BEGIN
    ALTER TABLE billing.account ADD CONSTRAINT account_term_valid
        CHECK (term_months IN (0, 6, 12));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE billing.account ADD CONSTRAINT account_term_start
        CHECK ((term_months = 0) = (term_start IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
CREATE TYPE common.alert_kind AS ENUM ('bill_shock', 'plan_saving', 'hardship_outreach',
                                'outage', 'usage_anomaly');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
CREATE TYPE common.alert_channel AS ENUM ('in_app', 'email', 'sms', 'push');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
CREATE TYPE common.alert_status AS ENUM ('pending', 'sent', 'delivered', 'failed', 'suppressed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---- catalogue.term_credit -------------------------------------
CREATE TABLE IF NOT EXISTS catalogue.term_credit (
    term_months     smallint      PRIMARY KEY,
    monthly_credit  numeric(12,2) NOT NULL,      -- GST-inclusive
    effective_from  date          NOT NULL,
    effective_to    date,
    synced_at       timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT term_credit_term   CHECK (term_months IN (6, 12)),
    CONSTRAINT term_credit_amount CHECK (monthly_credit > 0)
); 

-- ---- billing.payment -------------------------------------------
CREATE TABLE IF NOT EXISTS billing.payment (
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

-- ---- billing.account_plan_history ------------------------------
CREATE TABLE IF NOT EXISTS billing.account_plan_history (
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

-- ---- kea.plan_recommendation -----------------------------------
CREATE TABLE IF NOT EXISTS kea.plan_recommendation (
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

-- ---- kea.alert -------------------------------------------------
CREATE TABLE IF NOT EXISTS kea.alert (
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

-- ---------------------------------------------------------------------
-- The indexes the design defines for these five tables.
--
-- Missed on the first pass: the tables were extracted from
-- kea_serving_schema.sql by their CREATE TABLE statements alone, so all seven
-- CREATE INDEX lines that follow them were left behind. Nothing failed --
-- every one of these tables is small enough today that a sequential scan is
-- invisible -- which is exactly why it would have stayed missing.
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_payment_account ON billing.payment(account_id, paid_at DESC);
CREATE INDEX IF NOT EXISTS idx_payment_bill    ON billing.payment(bill_id);
CREATE INDEX IF NOT EXISTS idx_payment_failed  ON billing.payment(account_id) WHERE is_failed;

CREATE INDEX IF NOT EXISTS idx_aph_account ON billing.account_plan_history(account_id, valid_from DESC);

CREATE INDEX IF NOT EXISTS idx_plan_rec_account
    ON kea.plan_recommendation(account_id, computed_at DESC, rank);

CREATE INDEX IF NOT EXISTS idx_alert_account ON kea.alert(account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_pending ON kea.alert(scheduled_for) WHERE status = 'pending';

COMMIT;
