-- Sprint 3 item F: enforce safe, traceable hardship-outreach creation on an
-- already-running serving store. Fresh installs receive the same objects from
-- design/kea_serving_schema.sql.

SET search_path TO kea, billing, metering, catalogue, common, public;

BEGIN;

DO $$ BEGIN
    ALTER TABLE kea.alert ADD CONSTRAINT alert_hardship_has_assessment
        CHECK (kind <> 'hardship_outreach' OR assessment_id IS NOT NULL);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- One immutable alert event per assessment. ON CONFLICT DO NOTHING in the job
-- makes an orchestration retry a no-op rather than a second message.
CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_hardship_assessment
    ON kea.alert(assessment_id)
    WHERE kind = 'hardship_outreach';

-- Do not create a fresh outreach every day while an earlier one is still open.
-- Runtime owns status/acted_at; once it records action (or terminal failure),
-- a later assessment is eligible to create a new event.
CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_hardship_open_account
    ON kea.alert(account_id)
    WHERE kind = 'hardship_outreach'
      AND acted_at IS NULL
      AND status IN ('pending', 'sent', 'delivered', 'suppressed');

COMMIT;
