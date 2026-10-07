-- Pin the serving database to one "today" so the app opens on the story.
--
--     psql -v ON_ERROR_STOP=1 -v dry_run=1 -f serving_as_of.sql      # report, roll back
--     psql -v ON_ERROR_STOP=1 -f serving_as_of.sql                   # apply
--     psql -v ON_ERROR_STOP=1 -v as_of=2026-08-17 -f serving_as_of.sql
--
-- WHY. The generated span runs to NZ_SPAN_END (2027-08-27, uk_to_nz.py) so the
-- nightly extracts always have work, but the forecasts were last run on
-- 2026-08-17 -- day 16 of the demo household's flagged cycle. The serving
-- export leaves current_period_* null (no notion of "now"), so the API falls
-- back to the last calendar month with data and the app opens on August 2027,
-- a year after the story.
--
-- WHAT IT DOES, in one transaction:
--   1. current_period_start/end and days_into_period: the billing cycle that
--      contains as_of, from billing_cycle_day (1-28 on every account).
--   2. Removes OBSERVED rows dated after as_of from the marts the app reads:
--      daily usage, detected loads, charging sessions, forecast inputs, and
--      bills whose period has not closed. last_read_at is capped at as_of; an
--      account with no reading time keeps NULL.
--   3. Removes hourly profiles for months that do not end by as_of. A profile
--      averages its whole month, so the month containing as_of would carry
--      later readings; the API then answers from the latest complete month.
--
-- as_of IS AN NZ DAY. Date columns compare with it directly; timestamptz
-- columns compare with the NZ midnight that ends it, converted explicitly, so
-- the result does not depend on the session's TimeZone (a UTC session would
-- otherwise end the day 12 or 13 hours late).
--
-- WHAT IT LEAVES ALONE: bill_forecast (already <= as_of), and the precomputed
-- results -- plan_recommendation, plan_bill_example, solar_estimate,
-- ev_scenario, hardship_* -- which were computed over the full span and carry
-- the walkthrough's figures. Also the source mirrors (billing.*, metering.*),
-- which the app does not read.
--
-- as_of defaults to the newest forecast date. Idempotent: a second run with
-- the same as_of deletes nothing and rewrites the same periods. It is a
-- one-way trim; restore from the export dump to undo.
\if :{?as_of}
\else
\set as_of ''
\endif

BEGIN;

SELECT set_config('kea.as_of',
                  coalesce(nullif(:'as_of', ''),
                           (SELECT max(forecast_date)::text FROM kea.bill_forecast)),
                  true) AS as_of;

DO $$
DECLARE
    a date := current_setting('kea.as_of')::date;
    lo date;
    hi date;
BEGIN
    SELECT min(usage_date), max(usage_date) INTO lo, hi FROM kea.mart_daily_usage;
    IF a IS NULL OR lo IS NULL OR a < lo OR a > hi THEN
        RAISE EXCEPTION 'as_of % is outside the served usage (% to %)', a, lo, hi;
    END IF;
    IF EXISTS (SELECT 1 FROM kea.mart_account_context
               WHERE billing_cycle_day NOT BETWEEN 1 AND 28) THEN
        RAISE EXCEPTION 'a billing_cycle_day outside 1-28 has no date in every month';
    END IF;
END $$;

-- 1. The cycle in progress on as_of.
WITH c AS (
    SELECT account_id,
           CASE WHEN extract(day FROM a) >= billing_cycle_day
                THEN make_date(extract(year FROM a)::int, extract(month FROM a)::int,
                               billing_cycle_day)
                ELSE (make_date(extract(year FROM a)::int, extract(month FROM a)::int,
                                billing_cycle_day) - interval '1 month')::date
           END AS start_on,
           a
    FROM kea.mart_account_context,
         (SELECT current_setting('kea.as_of')::date AS a) s
)
UPDATE kea.mart_account_context m
SET current_period_start = c.start_on,
    current_period_end   = (c.start_on + interval '1 month')::date - 1,
    days_into_period     = c.a - c.start_on + 1,
    -- Not least(): it ignores NULL, and would invent a reading time.
    last_read_at         = CASE WHEN m.last_read_at > (c.a + 1)::timestamp
                                                       AT TIME ZONE 'Pacific/Auckland'
                                THEN (c.a + 1)::timestamp AT TIME ZONE 'Pacific/Auckland'
                                ELSE m.last_read_at
                           END
FROM c
WHERE c.account_id = m.account_id;

-- 2. Observed rows after as_of.
CREATE TEMP TABLE trimmed (tbl text, removed bigint) ON COMMIT DROP;

WITH d AS (DELETE FROM kea.mart_daily_usage
           WHERE usage_date > current_setting('kea.as_of')::date RETURNING 1)
INSERT INTO trimmed SELECT 'mart_daily_usage', count(*) FROM d;

WITH d AS (DELETE FROM kea.mart_daily_detected_load
           WHERE usage_date > current_setting('kea.as_of')::date RETURNING 1)
INSERT INTO trimmed SELECT 'mart_daily_detected_load', count(*) FROM d;

-- 3. Profile months that end after as_of.
WITH d AS (DELETE FROM kea.mart_hourly_profile
           WHERE (profile_month + interval '1 month')::date - 1
                 > current_setting('kea.as_of')::date RETURNING 1)
INSERT INTO trimmed SELECT 'mart_hourly_profile', count(*) FROM d;

WITH d AS (DELETE FROM kea.mart_charging_session
           WHERE session_start >= (current_setting('kea.as_of')::date + 1)::timestamp
                                  AT TIME ZONE 'Pacific/Auckland' RETURNING 1)
INSERT INTO trimmed SELECT 'mart_charging_session', count(*) FROM d;

WITH d AS (DELETE FROM kea.mart_forecast_input
           WHERE as_at_date > current_setting('kea.as_of')::date RETURNING 1)
INSERT INTO trimmed SELECT 'mart_forecast_input', count(*) FROM d;

WITH d AS (DELETE FROM kea.mart_bill_explanation
           WHERE period_end > current_setting('kea.as_of')::date RETURNING 1)
INSERT INTO trimmed SELECT 'mart_bill_explanation', count(*) FROM d;

SELECT tbl AS "removed after as_of", removed FROM trimmed ORDER BY tbl;

SELECT count(*) FILTER (WHERE current_period_start IS NULL) AS accounts_without_period,
       min(current_period_start) AS earliest_start,
       max(current_period_end)   AS latest_end,
       (SELECT max(usage_date) FROM kea.mart_daily_usage)     AS newest_usage,
       (SELECT max(period_end) FROM kea.mart_bill_explanation) AS newest_bill
FROM kea.mart_account_context;

\if :{?dry_run}
ROLLBACK;
\echo 'dry run: rolled back'
\else
COMMIT;
\endif
