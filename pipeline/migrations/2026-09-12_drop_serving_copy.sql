-- Remove the old copy of the served tables from the PIPELINE database.
--
-- ⚠️ FOR kea-source-db ONLY. In the backend's database these schemas ARE the
-- served tables the API reads; the guard below refuses to run there.
--
-- Since 2026-09-12 the served tables live in the backend's database (kea-db)
-- and only a publish writes them. This copy in kea-source-db stopped being
-- written that day, and nothing reads it: the Stage 5 dashboard, Soda's
-- serving report and assert-serving all read the backend. What stays is the
-- pipeline's own data -- src_*, gen_audit, lake_audit -- and none of it
-- depends on anything dropped here (checked against pg_depend on 2026-09-12).
--
-- Keep a dump first if the old copy matters to you:
--
--   docker compose exec -T kea-source-db pg_dump -U kea -d kea \
--     -n common -n metering -n billing -n catalogue -n kea > serving_copy.sql
--
-- Then, from data/:
--
--   docker compose exec -T kea-source-db psql -v ON_ERROR_STOP=1 -U kea -d kea \
--     -f - < pipeline/migrations/2026-09-12_drop_serving_copy.sql
--
-- kea_loader and kea_app stay as roles, owning nothing and granted nothing.

BEGIN;

DO $$
BEGIN
    -- data_ops holds publication metadata, which only a publish target has.
    IF to_regnamespace('data_ops') IS NOT NULL THEN
        RAISE EXCEPTION 'this database has data_ops: it is a publish target, not kea-source-db';
    END IF;
    IF to_regnamespace('src_metering') IS NULL THEN
        RAISE EXCEPTION 'this database has no src_metering: it is not kea-source-db';
    END IF;
END $$;

DROP SCHEMA IF EXISTS kea, billing, catalogue, metering, common CASCADE;

COMMIT;
