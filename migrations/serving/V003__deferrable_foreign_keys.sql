-- V001 makes every foreign key DEFERRABLE INITIALLY IMMEDIATE (the comment
-- above its loop says why), but the loop runs before V001's Sprint 3 section
-- creates billing.payment, billing.account_plan_history,
-- kea.plan_recommendation, kea.hardship_assessment, kea.hardship_signal and
-- kea.alert. None of their thirteen keys was converted.
--
-- Publishing into the backend's database exposed it. A publish replaces
-- billing.account and catalogue.plan wholesale under SET CONSTRAINTS ALL
-- DEFERRED, and a NOT DEFERRABLE key ignores that instruction. So the publish
-- fails at DELETE FROM billing.account as soon as one payment, plan history
-- row or Sprint 3 decision points at an account, even though the same
-- transaction puts that account back before COMMIT.
--
-- The same loop, with the same effect on ordinary writes: each statement is
-- still checked as it runs. `common` is included so that a key added there
-- later is not missed in the same way. Only keys that are still NOT DEFERRABLE
-- are touched, so the migration does nothing on a database where they were
-- already converted.
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
          AND n.nspname IN ('common', 'metering', 'billing', 'catalogue', 'kea')
    LOOP
        EXECUTE format('ALTER TABLE %I.%I ALTER CONSTRAINT %I DEFERRABLE INITIALLY IMMEDIATE',
                       r.sch, r.tbl, r.name);
    END LOOP;
END $$;
