-- Kea's footprint on its own Snowflake account.
--
-- THE ACCOUNT IDENTIFIER IS NOT WRITTEN DOWN HERE. It lives in .env as
-- SNOWFLAKE_ACCOUNT, which is gitignored. It is not a credential -- it is an
-- address, and Snowflake puts it in every connection URL -- but this repo is
-- public, and git history is the awkward thing to scrub. Costs nothing to
-- keep it out.
--
-- Run ONCE, by hand, in a Snowsight worksheet as ACCOUNTADMIN. Every statement
-- is IF NOT EXISTS or a re-runnable GRANT, so a second run changes nothing.
--
-- WHY BY HAND, AND NOT A MAKE TARGET. Every container in this repo authenticates
-- as KEA_TRANSFORMER, which does not exist until this script creates it. A make
-- target would need an ACCOUNTADMIN credential sitting in .env, which is a much
-- larger thing to leave lying around than the least-privilege one it would be
-- creating. This runs once; the automation that follows never needs this much.
--
-- ⚠️ Snowsight may show a "Get started with CoCo" modal with no close control, and
-- #/sql bounces back to it. Navigating once to
--   https://<SNOWFLAKE_ACCOUNT>.snowflakecomputing.com/console
-- clears it for the session. CoCo is an optional AI add-on: nothing here needs
-- it, and no card is required to run any of this.
--
-- THE ACCOUNT IS KEA'S ALONE. The sibling project taxipulse lives on a
-- different ACCOUNT, not a different database on this one, so nothing below can
-- reach it and no name here needs to avoid a collision.
-- The .env.example comment about "shared account, separate database" describes
-- an earlier plan and is corrected alongside this file.

USE ROLE ACCOUNTADMIN;

-- Bound once, then used for both the grant and the key registration, so the
-- script never hard-codes a user name. The name shown in the Snowsight UI is a
-- DISPLAY name and is not necessarily the login -- a wrong SNOWFLAKE_USER fails
-- later as an authentication error that reads like a bad key, which is slow to
-- diagnose. Read what this prints into .env verbatim.
SET login_user = CURRENT_USER();
SELECT $login_user AS "put this in SNOWFLAKE_USER", CURRENT_ACCOUNT() AS "account";

-- ----------------------------------------------------------------- database
CREATE DATABASE IF NOT EXISTS KEA
  COMMENT = 'Kea Energy data platform. Owned by KEA_TRANSFORMER.';

-- The loader's landing schema. dbt creates every other schema itself via the
-- CREATE SCHEMA grant below; this one exists explicitly so KEA.RAW does not
-- depend on which script happens to run first.
CREATE SCHEMA IF NOT EXISTS KEA.RAW
  COMMENT = 'Landed by pipeline/scripts/warehouse/load_bronze_to_snowflake.py: one table per source entity. Truncate-and-reload for snapshots, delete-by-load_date-and-append for incrementals.';

-- ---------------------------------------------------------------- warehouse
-- AUTO_SUSPEND matters more than usual here. The trial is a fixed $400 that
-- converts to paid on 3 Oct 2026, and a warehouse left running bills against
-- it while doing nothing. 60s is aggressive on purpose: dbt builds here are
-- minutes apart, not seconds, and a resume costs latency rather than credits.
CREATE WAREHOUSE IF NOT EXISTS KEA_WH
  WAREHOUSE_SIZE = XSMALL
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE
  COMMENT = 'Kea: the bronze loader and dbt build. COMPUTE_WH is left alone.';

-- --------------------------------------------------------------------- role
-- dbt connects as this, never as ACCOUNTADMIN. `dbt build` drops and recreates
-- what it owns, so the blast radius of a wrong DBT_SCHEMA should be a
-- permission error, not a dropped database.
CREATE ROLE IF NOT EXISTS KEA_TRANSFORMER
  COMMENT = 'The bronze loader and dbt. Least privilege: KEA and KEA_WH only.';

-- Standard hygiene: custom roles roll up to SYSADMIN so an account admin can
-- see and manage them without being granted the role itself.
GRANT ROLE KEA_TRANSFORMER TO ROLE SYSADMIN;
GRANT ROLE KEA_TRANSFORMER TO USER IDENTIFIER($login_user);

-- ------------------------------------------------------------------- grants
GRANT USAGE ON WAREHOUSE KEA_WH TO ROLE KEA_TRANSFORMER;

-- CREATE SCHEMA, not just USAGE: dbt makes kea_staging itself on first build,
-- and silver/gold/marts the same way when Stage 4's second pass lands -- no
-- further edit to this file needed.
GRANT USAGE, CREATE SCHEMA ON DATABASE KEA TO ROLE KEA_TRANSFORMER;

-- ALL rather than a table-by-table list: KEA_TRANSFORMER is the only role that
-- will ever write here and owns everything it creates. write_pandas needs the
-- stage and file-format privileges for its internal PUT + COPY.
GRANT ALL ON SCHEMA KEA.RAW TO ROLE KEA_TRANSFORMER;

-- ---------------------------------------------------------- key-pair auth
-- Snowflake blocks single-factor password logins, so every programmatic
-- connection here is key-pair. This is the PUBLIC half of
-- ~/.snowflake/kea_rsa_key.p8 -- a dedicated Kea key, not the one the sibling
-- project uses, so rotating or revoking either has no bearing on the other.
-- The private key never enters this repo; .env points at it by path and the
-- Makefile mounts it read-only.
ALTER USER IDENTIFIER($login_user) SET RSA_PUBLIC_KEY = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvZqLNepg9aoMaB1CzHKpXyQmIB0cYXxndCcGXMKVWVG3FSiZITOxxTnAX3DUBBsspIEr8lke6YCOeG7IEkjpJeqH9vsji371wbkhqdg0/FjfaD/dAegXQfc14PXo6R+SBK2dafpUZBvqEazOJije25ENJ47HgAswEqmPBK2VWUr+C61iGi557Kq5ilaVEhiteAqedAT93TiQgvsSX5Cig1tcdomAIpRb2PCjHxaGRQP46ikCtXLx4zz6ZEOwvfOgWgHi2NbJ88DzP9tQObYIct/yxwHk+kn7D9V4AfxzycgRry0gRTX7O+4l2G6mUHqYmWFANWec3t8zzrzvghibMwIDAQAB';

-- --------------------------------------------------------------- session tz
-- DEFENCE IN DEPTH, NOT THE DEFENCE. Snowflake defaults a session to
-- America/Los_Angeles, and comparing a TIMESTAMP_TZ to a DATE resolves the
-- instant using that. Measured: all 41 loaded invoices resolved to a date one
-- day EARLIER than their NZ calendar date, because a period ending at midnight
-- NZ is 05:00 the previous day in Los Angeles. On a bill whose period end sits
-- on a plan-version boundary that silently picks the wrong tariff -- design
-- §2.1.2's failure exactly.
--
-- The models do NOT rely on this: int_billing__plan_resolution converts to an
-- NZ date explicitly, because a correctness property that depends on a session
-- parameter is not a property. This just makes the default sane, so an ad-hoc
-- query in a worksheet reads the same way the pipeline does.
ALTER USER IDENTIFIER($login_user) SET TIMEZONE = 'Pacific/Auckland';

-- ------------------------------------------------------------------- verify
SHOW GRANTS TO ROLE KEA_TRANSFORMER;   -- KEA_WH, KEA, KEA.RAW -- and nothing else
DESCRIBE USER IDENTIFIER($login_user); -- RSA_PUBLIC_KEY_FP should now be set
