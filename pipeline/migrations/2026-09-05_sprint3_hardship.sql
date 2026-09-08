-- Sprint 3 item E: hardship identification.
--
-- Generated from design/kea_serving_schema.sql rather than retyped, so the two
-- cannot drift. Same reason as 2026-09-05_sprint3_serving_objects.sql: that
-- file is a FRESH-INSTALL script whose CREATE statements carry no
-- IF NOT EXISTS, so adding an object to it does not migrate a running store.
--
-- The rationale for every column lives in the design file. Only what a
-- migration has to do differently is repeated here.

SET search_path TO kea, billing, metering, catalogue, common, public;

BEGIN;

DO $$ BEGIN
CREATE TYPE common.hardship_signal_code AS ENUM (
    'direct_debit_failed', 'late_payment_streak', 'part_payment_arrears',
    'payments_stopped', 'usage_collapse');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---- kea.hardship_assessment -----------------------------------
-- The parent exists so that "assessed, nothing found" is a row rather than an
-- absence, and so kea.alert has one thing to point at: a hardship outreach is
-- triggered by the SET of signals, not by any one of them.
CREATE TABLE IF NOT EXISTS kea.hardship_assessment (
    assessment_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id      bigint       NOT NULL REFERENCES billing.account(account_id),
    assessed_on     date         NOT NULL,
    ruleset_version text         NOT NULL,
    signal_count    smallint     NOT NULL DEFAULT 0,
    -- Care obligations as at assessment time, deliberately NOT signal codes:
    -- medical dependency is not evidence of financial difficulty, it changes
    -- what the retailer may do. Copied, not joined -- billing.customer is a
    -- replica the nightly load overwrites in place.
    is_medically_dependent boolean NOT NULL DEFAULT false,
    is_vulnerable          boolean NOT NULL DEFAULT false,
    observed_from   date         NOT NULL,
    observed_to     date         NOT NULL,
    computed_at     timestamptz  NOT NULL DEFAULT now(),
    -- computed_at in the key, as plan_recommendation does it: append-only, so
    -- two runs of the same day coexist rather than one overwriting the other.
    UNIQUE (account_id, assessed_on, computed_at),
    CONSTRAINT hardship_window_ordered CHECK (observed_to >= observed_from),
    CONSTRAINT hardship_count_non_negative CHECK (signal_count >= 0)
);

-- ---- kea.hardship_signal ---------------------------------------
CREATE TABLE IF NOT EXISTS kea.hardship_signal (
    signal_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assessment_id   bigint       NOT NULL REFERENCES kea.hardship_assessment(assessment_id)
                                 ON DELETE CASCADE,
    signal_code     hardship_signal_code NOT NULL,
    signal_value    text         NOT NULL,
    evidence_count  integer      NOT NULL DEFAULT 0,
    first_seen_on   date         NOT NULL,
    last_seen_on    date         NOT NULL,
    -- For staff, never shown to the customer unmediated.
    explanation     text         NOT NULL,
    UNIQUE (assessment_id, signal_code),
    CONSTRAINT hardship_signal_dates_ordered CHECK (last_seen_on >= first_seen_on),
    CONSTRAINT hardship_signal_evidence CHECK (evidence_count >= 0)
);

-- ---- kea.alert gains the link ----------------------------------
-- Without it alert_kind 'hardship_outreach' is the one kind with nothing to
-- point at, which is also the one kind a regulator may ask to see the trigger
-- for.
ALTER TABLE kea.alert
    ADD COLUMN IF NOT EXISTS assessment_id bigint;
DO $$ BEGIN
    ALTER TABLE kea.alert ADD CONSTRAINT alert_assessment_fk
        FOREIGN KEY (assessment_id) REFERENCES kea.hardship_assessment(assessment_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---- indexes ---------------------------------------------------
-- 2026-09-05_sprint3_serving_objects.sql shipped without its seven indexes
-- because they were extracted by CREATE TABLE alone. Not repeating that.
CREATE INDEX IF NOT EXISTS idx_hardship_account
    ON kea.hardship_assessment(account_id, assessed_on DESC, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_hardship_flagged
    ON kea.hardship_assessment(assessed_on DESC) WHERE signal_count > 0;

CREATE INDEX IF NOT EXISTS idx_hardship_signal_assessment
    ON kea.hardship_signal(assessment_id);
CREATE INDEX IF NOT EXISTS idx_hardship_signal_code
    ON kea.hardship_signal(signal_code, last_seen_on DESC);

COMMIT;
