-- The serving gate (pipeline/scripts/assert_serving.sql) runs as kea_loader,
-- the publisher's own credential, inside a transaction its caller rolls back.
-- It used to run as the owner, whose login also reaches the backend's app
-- schema, and Airflow had to hold that login for it.
--
-- Its sequence check reads every identity sequence in the served schemas, and
-- V001/V002 gave kea_loader only kea's. The ids in metering, billing and
-- catalogue arrive from the warehouse and common.sync_serving_sequences()
-- moves their sequences, so the loader never needed to read them itself.
--
-- SELECT on a sequence reads its state and nothing more. It allows neither
-- nextval nor setval, so the publisher gains no way to change an id.
GRANT SELECT ON ALL SEQUENCES IN SCHEMA metering, billing, catalogue TO kea_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA metering  GRANT SELECT ON SEQUENCES TO kea_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA billing   GRANT SELECT ON SEQUENCES TO kea_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA catalogue GRANT SELECT ON SEQUENCES TO kea_loader;
