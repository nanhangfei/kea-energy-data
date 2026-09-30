-- Clear the decision history a demo rebuild is about to replace.
--
-- `make publish-serving DEMO_RESET=1` runs it, as the OWNER (KEA_MIGRATION_DSN),
-- after the export and immediately before the publish, and only once
-- ops/assert_local_serving.sh has confirmed the database is this machine's.
-- A publish never deletes history; this is the one place that does.
--
-- WHY A REBUILD NEEDS IT. Decision records may belong to an older or larger
-- generation and reference rows the compact publish will remove. The publish
-- that follows recreates them.
--
-- EVERY TABLE IS LISTED FOR A REASON. hardship_outlook references
-- hardship_assessment, so a TRUNCATE that left it out would fail;
-- solar_estimate and ev_scenario reference the accounts and plans the publish
-- replaces, so their old rows would fail its deferred key check.
--
-- NO RESTART IDENTITY: the backend's own tables may hold these ids without a
-- foreign key, and re-issuing one would quietly re-point them.
TRUNCATE TABLE kea.alert, kea.hardship_outlook, kea.hardship_signal,
               kea.hardship_assessment, kea.plan_bill_example,
               kea.plan_recommendation, kea.bill_forecast,
               kea.solar_estimate, kea.ev_scenario;
