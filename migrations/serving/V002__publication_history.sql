-- Existing databases can baseline V001, so create its two global roles when
-- this database is the first Kea database on the PostgreSQL cluster.
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

CREATE TABLE data_ops.serving_publication (
    release_id text PRIMARY KEY,
    published_at timestamptz NOT NULL DEFAULT now(),
    row_counts jsonb NOT NULL
);

-- V001 is executed on a clean database but only recorded when an existing
-- database is baselined. Repeat its role grants here so both paths finish with
-- the same permissions. GRANT and ALTER DEFAULT PRIVILEGES are idempotent.
GRANT USAGE ON SCHEMA common, metering, billing, catalogue, kea TO kea_app;
GRANT SELECT ON ALL TABLES IN SCHEMA metering, billing, catalogue, kea TO kea_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA metering GRANT SELECT ON TABLES TO kea_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA billing GRANT SELECT ON TABLES TO kea_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA catalogue GRANT SELECT ON TABLES TO kea_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA kea GRANT SELECT ON TABLES TO kea_app;

GRANT USAGE ON SCHEMA common, metering, billing, catalogue, kea TO kea_loader;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA metering, billing, catalogue, kea TO kea_loader;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA kea TO kea_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA metering GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kea_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA billing GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kea_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA catalogue GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kea_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA kea GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kea_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA kea GRANT USAGE, SELECT ON SEQUENCES TO kea_loader;
GRANT EXECUTE ON FUNCTION common.sync_serving_sequences() TO kea_loader;

-- Partition replacement needs CREATE on the schema and ownership of the
-- partitioned table. Existing partitions inherit neither ownership nor future
-- ownership changes, so align each one explicitly during baseline.
GRANT CREATE ON SCHEMA metering TO kea_loader;
DO $$
DECLARE r record;
BEGIN
    EXECUTE 'ALTER TABLE metering.meter_read_hh OWNER TO kea_loader';
    FOR r IN
        SELECT c.oid::regclass AS part
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = 'metering.meter_read_hh'::regclass
    LOOP
        EXECUTE format('ALTER TABLE %s OWNER TO kea_loader', r.part);
    END LOOP;
END
$$;

GRANT USAGE ON SCHEMA data_ops TO kea_loader, kea_app;
GRANT SELECT ON data_ops.schema_migration TO kea_loader;
GRANT SELECT, INSERT ON data_ops.serving_publication TO kea_loader;
GRANT SELECT ON data_ops.serving_publication TO kea_app;
