-- =====================================================================
-- Kea Energy — APPLICATION SCHEMA (targets PostgreSQL 17; developed against 16)
-- File 4 of 4. Companion to kea_source_schema.sql, kea_serving_schema.sql
-- and kea_deferred_schema.sql. See kea-data-platform-design.md.
--
-- Written by the agent (kea_app); NOT visible to the loader.
--
--   app   conversation history, verification evidence, alerts
--
-- This is the half of the merged schema the platform files do not cover.
-- The namespace table in the design doc gives `app` to "conversation
-- history", with kea_app holding write and kea_loader no access at all —
-- this file is that row, implemented.
--
-- ## What each side brought
--
-- The serving schema answers "what is true about this customer". It has
-- no place to record **what we told them and whether the figures were
-- checked**, and two of the project's deliverables are counted from
-- exactly that: "N answers, 0 fabricated figures", and cost-to-serve.
-- Neither can be reconstructed after the fact if it was never written
-- down, so the verification spine below is not optional bookkeeping.
--
-- Conversely the operational columns here — outcome, was_escalated,
-- hardship_detected, message_count, retain_until, model_version,
-- latency_ms, cited_sources — are Harvey's, adopted rather than
-- reinvented. They make deflection and hardship rates answerable without
-- a scan of `turn`.
--
-- ## The read/write split, which is the whole point
--
--   app reads   kea.*        marts and forecasts   (SELECT only)
--   app writes  app.*        this file
--
-- The agent never reads metering/billing/catalogue directly. Everything
-- it needs about a customer arrives pre-joined in kea.mart_account_context
-- and kea.mart_daily_usage, which is what makes the analytics layer a
-- single query rather than a five-way join it could get wrong.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS common;
CREATE SCHEMA IF NOT EXISTS app;

SET search_path TO app, kea, common, public;

-- ---------------------------------------------------------------------
-- Enumerations
--
-- alert_channel and alert_status are Harvey's from the deferred schema,
-- reused verbatim so the two files do not define the same type twice.
-- `voice` is added to alert_channel: the tier-3 escalation is a phone
-- call, and 'push' is not that.
-- ---------------------------------------------------------------------

CREATE TYPE common.conversation_channel AS ENUM ('chat', 'voice');
CREATE TYPE common.turn_role AS ENUM ('customer', 'agent');

CREATE TYPE common.conversation_outcome AS ENUM
    ('ongoing', 'resolved', 'escalated', 'abandoned');

CREATE TYPE common.escalation_reason AS ENUM
    ('hardship', 'out_of_scope', 'model_unavailable', 'customer_request');

-- Tier is the severity ladder, channel is the pipe. They are separate
-- columns because the mapping is policy and policy changes: today tier 1
-- goes by email and tier 3 by phone, but a customer who has opted out of
-- email still has a tier-1 alert — it just goes somewhere else.
CREATE TYPE common.alert_kind AS ENUM
    ('bill_shock', 'plan_saving', 'hardship_outreach', 'outage', 'usage_anomaly');
CREATE TYPE common.alert_channel AS ENUM ('in_app', 'email', 'sms', 'voice', 'push');
CREATE TYPE common.alert_status AS ENUM
    ('pending', 'sent', 'delivered', 'failed', 'suppressed');


-- ---------------------------------------------------------------------
-- conversation
--
-- One customer session, on one channel. `channel` is load-bearing rather
-- than descriptive: call deflection is the business case (900+ bill
-- queries a week at 12-15 minutes each), so "how many of these were
-- voice" has to be a GROUP BY, not an inference from the transcript.
-- ---------------------------------------------------------------------
CREATE TABLE app.conversation (
    conversation_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The customer as the marts know them. Not a foreign key: `kea` is a
    -- separately-loaded schema and the app must keep working through a
    -- reload. Integrity here is a check on read, not a constraint.
    account_ref      text        NOT NULL,
    channel          common.conversation_channel NOT NULL,

    started_at       timestamptz NOT NULL DEFAULT now(),
    ended_at         timestamptz,

    -- Operational columns, from Harvey's app.conversation.
    outcome          common.conversation_outcome NOT NULL DEFAULT 'ongoing',
    message_count    integer     NOT NULL DEFAULT 0,
    was_escalated    boolean     NOT NULL DEFAULT false,
    hardship_detected boolean    NOT NULL DEFAULT false,

    -- Deliberately no default. This table holds payment-difficulty
    -- disclosures; an unset retention date should be visible and fixable,
    -- not silently mean "forever".
    retain_until     date,

    CONSTRAINT ck_conversation_dates
        CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE INDEX idx_conversation_account ON app.conversation(account_ref, started_at DESC);
CREATE INDEX idx_conversation_escalated ON app.conversation(started_at DESC)
    WHERE was_escalated;
CREATE INDEX idx_conversation_hardship ON app.conversation(started_at DESC)
    WHERE hardship_detected;


-- ---------------------------------------------------------------------
-- turn
--
-- One message. `intent` is layer 0's decision, logged on every turn so
-- misroutes are visible rather than inferred.
-- ---------------------------------------------------------------------
CREATE TABLE app.turn (
    turn_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id  bigint      NOT NULL
                     REFERENCES app.conversation(conversation_id) ON DELETE CASCADE,
    seq              integer     NOT NULL,
    role             common.turn_role NOT NULL,
    body             text        NOT NULL,
    intent           text,

    -- Without model_version the bake-off cannot attribute a regression to
    -- a model change after the fact.
    model_version    text,
    -- Measured, never estimated. Reported as percentiles against the
    -- ADR-0003 latency budget.
    latency_ms       integer,
    -- The short citation shown to the customer, as distinct from
    -- fact_bundle, which is the full receipt.
    cited_sources    jsonb,

    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, seq)
);

CREATE INDEX idx_turn_conversation ON app.turn(conversation_id, seq);
CREATE INDEX idx_turn_intent ON app.turn(intent) WHERE intent IS NOT NULL;


-- ---------------------------------------------------------------------
-- fact_bundle
--
-- Exactly what layer 2 was given. Stored so any answer can be
-- reconstructed and audited afterwards — if a figure is ever disputed,
-- this is the receipt.
-- ---------------------------------------------------------------------
CREATE TABLE app.fact_bundle (
    fact_bundle_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    turn_id          bigint      NOT NULL UNIQUE
                     REFERENCES app.turn(turn_id) ON DELETE CASCADE,
    intent           text        NOT NULL,
    period           text        NOT NULL,
    facts            jsonb       NOT NULL
);


-- ---------------------------------------------------------------------
-- verification
--
-- Layer 3's verdict, per turn. A table rather than a log line because
-- "N answers, 0 fabricated figures" is a deliverable, and evidence you
-- can count beats evidence you have to grep for.
-- ---------------------------------------------------------------------
CREATE TABLE app.verification (
    verification_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    turn_id          bigint      NOT NULL UNIQUE
                     REFERENCES app.turn(turn_id) ON DELETE CASCADE,
    passed           boolean     NOT NULL,
    checked          jsonb       NOT NULL,
    unverified       jsonb       NOT NULL,
    regenerated      boolean     NOT NULL DEFAULT false,
    fell_back_to_template boolean NOT NULL DEFAULT false,

    -- True when the template was served because the model was unreachable.
    -- Without this column the two fallback paths are indistinguishable,
    -- and this table is what the "0 fabricated figures" claim is counted
    -- from. A day when Ollama was down would otherwise read as a day the
    -- number-checker caught six fabrications — the opposite claim, and a
    -- flattering one, which is exactly the kind of number this product
    -- exists to refuse.
    provider_error   boolean     NOT NULL DEFAULT false
);

CREATE INDEX idx_verification_failed ON app.verification(turn_id) WHERE NOT passed;


-- ---------------------------------------------------------------------
-- token_usage
--
-- What each answer cost the model. Running locally there is no invoice,
-- so cost-to-serve is a projection: measured tokens x published rates.
-- That only works if the tokens were recorded at the time.
-- ---------------------------------------------------------------------
CREATE TABLE app.token_usage (
    token_usage_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    turn_id          bigint      NOT NULL UNIQUE
                     REFERENCES app.turn(turn_id) ON DELETE CASCADE,
    provider         text        NOT NULL,
    model            text        NOT NULL,
    prompt_tokens    integer     NOT NULL DEFAULT 0,
    completion_tokens integer    NOT NULL DEFAULT 0
);


-- ---------------------------------------------------------------------
-- escalation
--
-- Every handoff to a person. Under the Consumer Care Obligations Part 6
-- a retailer on notice of payment difficulty has duties towards that
-- customer. If we detect hardship, the fact that we detected it — and
-- what happened next — needs to be on the record.
-- ---------------------------------------------------------------------
CREATE TABLE app.escalation (
    escalation_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id  bigint      NOT NULL
                     REFERENCES app.conversation(conversation_id) ON DELETE CASCADE,
    turn_id          bigint      REFERENCES app.turn(turn_id) ON DELETE SET NULL,
    reason           common.escalation_reason NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------
-- alert
--
-- Harvey's kea.alert, moved into `app` and given a tier.
--
-- Moved because the namespace rules give kea_app SELECT-only on `kea.*`,
-- and this row is written by the agent at the moment the warning fires,
-- not by the nightly loader. Leaving it in `kea` would have meant either
-- granting the app write access to the loader's schema or having no
-- record of what was sent.
--
-- Tiers, per the escalation policy:
--
--   1  email   — the default. Implemented and delivering.
--   2  sms     — recorded and rendered; no gateway wired.
--   3  voice   — recorded and rendered; no gateway wired.
--
-- Tier and channel are separate columns on purpose: tier is how bad it
-- is, channel is how we reached them, and the mapping between them is
-- policy that will change. `suppressed` exists so a deliberate
-- non-send is distinguishable from a failure — an undelivered bill-shock
-- warning is indistinguishable from never having sent one, from the
-- customer's side, and that distinction is the point of the table.
-- ---------------------------------------------------------------------
CREATE TABLE app.alert (
    alert_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_ref      text        NOT NULL,
    kind             common.alert_kind    NOT NULL,
    tier             smallint    NOT NULL,
    channel          common.alert_channel NOT NULL,
    status           common.alert_status  NOT NULL DEFAULT 'pending',

    subject          text,
    body             text        NOT NULL,

    -- The assessment that triggered it, stored whole. Same reasoning as
    -- fact_bundle: an alert whose figures cannot be reproduced later is
    -- an alert we cannot defend.
    facts            jsonb,
    -- Which cycle this was about, so the idempotency key below has
    -- something to key on.
    period           text        NOT NULL,
    as_at_day        smallint,

    scheduled_for    timestamptz,
    sent_at          timestamptz,
    delivered_at     timestamptz,
    failure_reason   text,

    -- Engagement, for measuring whether proactive contact actually works.
    opened_at        timestamptz,
    acted_at         timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_alert_tier CHECK (tier BETWEEN 1 AND 3),

    -- One alert per customer per cycle per tier. A dashboard that polls
    -- every 30 seconds must not email the customer every 30 seconds;
    -- without this the first rehearsal sends a hundred messages.
    CONSTRAINT uq_alert_once_per_cycle UNIQUE (account_ref, kind, period, tier)
);

CREATE INDEX idx_alert_account ON app.alert(account_ref, created_at DESC);
CREATE INDEX idx_alert_pending ON app.alert(scheduled_for) WHERE status = 'pending';
CREATE INDEX idx_alert_failed ON app.alert(created_at DESC) WHERE status = 'failed';
