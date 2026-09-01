-- =====================================================================
-- Kea Energy — bill-increase scenario seed
-- OTHER BILL CHANGES — the period contains estimated reads
-- =====================================================================
--
-- The meter stopped communicating on 10 June. Reads from 11 June on are estimates, and they run high.
--
-- Synthetic fixture. No real customer, address, ICP or meter appears here;
-- every id carries the table it belongs to in its first two digits and this
-- fixture's number (6) in its third -- see "Fixture ids" in data/README.md.
-- The half-hourly reads are generated deterministically by the CTE below
-- rather than copied from a feed.
--
-- WHAT THE EXPLAINER MAY SAY
-- This file names a cause, because a generator knows what it generated. The
-- pipeline does not know, and must not claim to: data/README.md is explicit
-- that half-hourly interval data cannot identify an appliance, and calls the
-- refusal deliberate. The scenario name is ground truth for testing, never an
-- inference the agent is licensed to make.
--
-- What the agent may do is describe the SHAPE it can actually measure -- "a
-- constant draw around the clock", "a load between 9pm and 11pm", "your usage
-- between 9am and 5pm has roughly doubled" -- and then ask. Naming the dryer
-- is the customer's job, not the model's.
--
-- WHY TWO BILLS
-- "Higher than normal" is a comparison, so a single bill cannot express it.
-- Each scenario seeds a normal period and the period that follows it, and
-- kea.mart_bill_explanation.prior_bill_id links them. The prior bill is
-- deliberately left unexplained (is_explainable = false) because nothing
-- precedes it.
--
-- WHAT THIS FIXTURE TEACHES
-- Nothing is wrong with this household. Two thirds of the period was
-- ESTIMATED rather than measured, and the estimates over-read.
--
-- This fixture exists to exercise the hedging path, and it sets a trap. The
-- estimates over-read by a constant amount every half hour, so the
-- decomposition attributes most of the rise to BASE LOAD -- it looks exactly
-- like the faulty-cylinder fixture. An explainer that reads the drivers and
-- stops would tell this customer they have something stuck on.
--
-- What stops it is estimated_read_pct being well above zero and
-- missing_interval_count being non-zero, which force is_explainable to false
-- NO MATTER how cleanly the drivers add up. The agent must not assert a cause
-- from numbers the meter never produced. The correct answer is roughly "two
-- thirds of this bill is estimated, so I would not read too much into the
-- increase -- a real read will true it up."
--
-- metering.meter_read_hh.quality is 'estimated' for those intervals, and 10
-- June is missing entirely, so completeness checks have something to find.
--
-- PERIODS
--   prior    2024-05-01 .. 2024-05-31  (31 days)
--   current  2024-06-01 .. 2024-06-30  (30 days)
--
-- Re-runnable: every write is either scoped-DELETE-then-INSERT or
-- ON CONFLICT DO UPDATE, so running this file twice leaves the same rows.
-- The DO block at the end re-derives the bill from the reads and raises if
-- the two disagree, so a silent drift fails the transaction.
--
-- Run after kea_source_schema.sql and kea_serving_schema.sql.
-- Independent of every other file in data/sample: its own household, its own
-- network, its own plan.

BEGIN;

SET search_path TO public;

-- ---------------------------------------------------------------------
-- Idempotency: remove only this fixture, children before parents.
-- ---------------------------------------------------------------------
DELETE FROM src_metering.interval_reads WHERE icp_identifier = 'DEMO0000000601AA';
DELETE FROM metering.meter_read_hh      WHERE icp_id = 136001;
DELETE FROM src_billing.invoice_lines   WHERE INV_ID IN (236001, 236002);
DELETE FROM src_billing.invoices        WHERE INV_ID IN (236001, 236002);
DELETE FROM src_billing.accounts        WHERE ACCT_ID = 226001;
DELETE FROM src_billing.customers       WHERE CUST_ID = 216001;
DELETE FROM src_catalogue.rate_components WHERE component_id BETWEEN 336001 AND 336011;
DELETE FROM src_catalogue.tou_windows     WHERE window_id    BETWEEN 326001 AND 326011;
DELETE FROM src_catalogue.price_plans     WHERE plan_id = 316001;
DELETE FROM src_metering.registers        WHERE register_id = 156001;
DELETE FROM src_metering.meters           WHERE meter_id = 146001;
DELETE FROM src_metering.connection_points WHERE icp_id = 136001;
DELETE FROM src_metering.networks         WHERE network_id = 116001;

DELETE FROM kea.mart_bill_explanation WHERE bill_id IN (236001, 236002);
DELETE FROM kea.mart_hourly_profile   WHERE icp_id = 136001;
DELETE FROM kea.mart_daily_usage      WHERE icp_id = 136001;
DELETE FROM kea.mart_account_context  WHERE account_id = 226001;
DELETE FROM kea.mart_load_log         WHERE warehouse_run_id = 'sample-scenario-estimated_reads';
DELETE FROM billing.bill_line WHERE bill_id IN (236001, 236002);
DELETE FROM billing.bill      WHERE bill_id IN (236001, 236002);
DELETE FROM billing.account   WHERE account_id = 226001;
DELETE FROM catalogue.tariff_component WHERE component_id BETWEEN 336001 AND 336011;
DELETE FROM catalogue.tou_band         WHERE band_id      BETWEEN 326001 AND 326011;
DELETE FROM catalogue.plan             WHERE plan_id = 316001;
DELETE FROM metering.meter_register WHERE register_id = 156001;
DELETE FROM metering.icp            WHERE icp_id = 136001;
DELETE FROM metering.property       WHERE property_id = 126001;
DELETE FROM metering.network        WHERE network_id = 116001;
DELETE FROM billing.customer        WHERE customer_id = 216001;

-- =====================================================================
-- SOURCE SYSTEMS
-- =====================================================================

INSERT INTO src_billing.customers
    (CUST_ID, CUST_NO, CUST_NAME, EMAIL_ADDR, PHONE_NO,
     ADDR_LINE1, ADDR_SUBURB, ADDR_CITY, ADDR_POSTCODE)
OVERRIDING SYSTEM VALUE
VALUES (216001, '216001', 'Priya Raman', NULL, NULL,
        '3 Marine Parade', 'Napier South', 'Napier', NULL)
ON CONFLICT (CUST_NO) DO UPDATE SET
    CUST_NAME = EXCLUDED.CUST_NAME,
    ADDR_LINE1 = EXCLUDED.ADDR_LINE1,
    ADDR_SUBURB = EXCLUDED.ADDR_SUBURB,
    ADDR_CITY = EXCLUDED.ADDR_CITY;

INSERT INTO src_metering.networks (network_id, network_code, network_name, region)
OVERRIDING SYSTEM VALUE
VALUES (116001, 'UNSN', 'Unison Networks', 'Hawke''s Bay')
ON CONFLICT (network_code) DO UPDATE SET
    network_name = EXCLUDED.network_name, region = EXCLUDED.region;

INSERT INTO src_metering.connection_points
    (icp_id, icp_identifier, network_id, address_full, suburb, city,
     postcode, climate_region, connection_date)
OVERRIDING SYSTEM VALUE
VALUES (136001, 'DEMO0000000601AA', 116001, '3 Marine Parade, Napier South',
        'Napier South', 'Napier', NULL, 'Hawke''s Bay', '2019-01-01')
ON CONFLICT (icp_identifier) DO UPDATE SET
    network_id = EXCLUDED.network_id,
    address_full = EXCLUDED.address_full,
    climate_region = EXCLUDED.climate_region;

INSERT INTO src_metering.meters (meter_id, icp_id, serial_number, is_smart, installed_on)
OVERRIDING SYSTEM VALUE
VALUES (146001, 136001, 'DEMO-METER-146001', true, '2019-01-01')
ON CONFLICT (meter_id) DO UPDATE SET
    icp_id = EXCLUDED.icp_id, serial_number = EXCLUDED.serial_number;

-- Import only. These households have no solar, which keeps the fixture about
-- consumption: an export credit moving at the same time would muddy the
-- driver decomposition this file is here to demonstrate.
INSERT INTO src_metering.registers
    (register_id, meter_id, register_code, description, is_controlled, flow_direction)
OVERRIDING SYSTEM VALUE
VALUES (156001, 146001, 'UN', 'Grid import', false, 'import')
ON CONFLICT (register_id) DO UPDATE SET
    meter_id = EXCLUDED.meter_id, register_code = EXCLUDED.register_code;

-- ---------------------------------------------------------------------
-- Half-hourly reads for BOTH periods.
--
-- kwh = daily_total * shape(hh) / sum(shape over day)      the household
--     + heating(hh) * heating_degree_days(day)             weather response
--     + scenario_extra(day, hh)                            what this file is about
--
-- Weather is deterministic: a period mean temperature with a fixed daily
-- wobble, so heating degree days differ between the two periods by a known
-- amount and mart_bill_explanation.delta_from_weather can be checked by hand.
-- Every scenario carries the SAME heating response (0.93 kWh per degree
-- day) so that weather is never the thing that distinguishes them, except in
-- the season fixture where the degree-day swing is the whole story.
-- ---------------------------------------------------------------------
WITH half_hours AS (
    -- Anchored to NZ LOCAL midnight via AT TIME ZONE rather than to a fixed
    -- +12 literal. +12 is NZST; between late September and early April the
    -- country is on NZDT (+13), so a literal offset starts the series an hour
    -- late and silently drops the first two half hours of the period. That is
    -- invisible in a total and only shows up as interval_count = 46 on a day
    -- that is not a DST day.
    SELECT generate_series(
               (date '2024-05-01')::timestamp AT TIME ZONE 'Pacific/Auckland',
               (date '2024-05-31' + 1)::timestamp AT TIME ZONE 'Pacific/Auckland'
                   - interval '30 minutes',
               interval '30 minutes') AS interval_start,
           'prior' AS period
    UNION ALL
    SELECT generate_series(
               (date '2024-06-01')::timestamp AT TIME ZONE 'Pacific/Auckland',
               (date '2024-06-30' + 1)::timestamp AT TIME ZONE 'Pacific/Auckland'
                   - interval '30 minutes',
               interval '30 minutes'),
           'current'
), tagged AS (
    SELECT interval_start, period,
           (interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour   FROM interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr
    FROM half_hours
), weathered AS (
    -- SIN() returns double precision, so the whole sum is cast back to
    -- numeric before rounding: round(double, int) has no overload.
    SELECT t.*,
           ROUND(((13.6 + CASE WHEN period = 'current'
                                   THEN 11.1 - 13.6 ELSE 0 END)
                  + 2.2 * SIN(EXTRACT(day FROM d) * 0.7))::numeric, 2) AS temp_c
    FROM tagged t
), hdd AS (
    SELECT w.*, GREATEST(16.0 - temp_c, 0)::numeric AS hdd_day FROM weathered w
), shaped AS (
    SELECT h.*,
           CASE
               WHEN hr >= 0  AND hr < 6  THEN 0.55
               WHEN hr >= 6  AND hr < 9  THEN 1.60
               WHEN hr >= 9  AND hr < 12 THEN 0.80
               WHEN hr >= 12 AND hr < 16 THEN 0.70
               WHEN hr >= 16 AND hr < 18 THEN 1.10
               WHEN hr >= 18 AND hr < 21 THEN 1.80
               WHEN hr >= 21 AND hr < 23 THEN 1.00
               ELSE 0.65
           END::numeric AS w,
           CASE WHEN period = 'current' THEN 9.0 ELSE 8.8 END::numeric AS daily_kwh,
           CASE
               WHEN hr >= 6  AND hr < 9  THEN 0.040
               WHEN hr >= 17 AND hr < 22 THEN 0.045
               WHEN hr >= 22 OR  hr < 6  THEN 0.005
               ELSE 0.010
           END::numeric * hdd_day AS heat_kwh,
           (CASE WHEN d >= date '2024-06-11' THEN 0.075 ELSE 0 END)::numeric AS extra_kwh
    FROM hdd h
), normed AS (
    SELECT s.*, SUM(w) OVER (PARTITION BY d) AS w_day FROM shaped s
)
INSERT INTO src_metering.interval_reads
    (icp_identifier, register_code, interval_start, kwh, read_status, version_no, source_file)
SELECT 'DEMO0000000601AA', 'UN', interval_start,
       ROUND(daily_kwh * w / w_day + heat_kwh + extra_kwh, 4),
       CASE WHEN d >= date '2024-06-11' THEN 'E' ELSE 'A' END,
       1, 'sample-scenario-estimated_reads.csv'
FROM normed
WHERE d <> date '2024-06-10';

-- ---------------------------------------------------------------------
-- Plan and tariff. Rates match the sample bill in
-- 01-bill_printed_invoice_2024-08.sql so figures across data/sample are
-- comparable; only the plan CODE differs, so that each scenario owns its own
-- catalogue rows and the files stay independent of one another.
-- ---------------------------------------------------------------------
INSERT INTO src_catalogue.price_plans
    (plan_id, code, name, network_code, user_category, is_tou,
     ppd_percent, valid_from, valid_to, active)
OVERRIDING SYSTEM VALUE
VALUES (316001, 'DEMO_STD_FXTOU_ESTIMATED_READS', 'Demo Standard Fixed TOU',
        'UNSN', 'STANDARD', true, 0, '2024-01-01', NULL, true)
ON CONFLICT (code, valid_from) DO UPDATE SET
    name = EXCLUDED.name, network_code = EXCLUDED.network_code, active = EXCLUDED.active;

-- Gapless, all seven days. Network peak runs 30 minutes later than energy
-- peak, the per-charge-group band scoping the design doc calls out in §8.2.1.
INSERT INTO src_catalogue.tou_windows
    (window_id, plan_id, charge_scope, band_label, days_csv, time_from, time_to)
OVERRIDING SYSTEM VALUE
VALUES
    (326001,  316001, 'NET', 'PEAK',     '1,2,3,4,5,6,7', '07:00', '11:00'),
    (326002,  316001, 'NET', 'PEAK',     '1,2,3,4,5,6,7', '17:00', '21:30'),
    (326003,  316001, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '00:00', '07:00'),
    (326004,  316001, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '11:00', '17:00'),
    (326005,  316001, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '21:30', '24:00'),
    (326006,  316001, 'ENE', 'PEAK',     '1,2,3,4,5,6,7', '07:00', '11:00'),
    (326007,  316001, 'ENE', 'PEAK',     '1,2,3,4,5,6,7', '17:00', '21:00'),
    (326008,  316001, 'ENE', 'SHOULDER', '1,2,3,4,5,6,7', '11:00', '17:00'),
    (326009,  316001, 'ENE', 'SHOULDER', '1,2,3,4,5,6,7', '21:00', '23:00'),
    (326010, 316001, 'ENE', 'OFFPEAK',  '1,2,3,4,5,6,7', '00:00', '07:00'),
    (326011, 316001, 'ENE', 'OFFPEAK',  '1,2,3,4,5,6,7', '23:00', '24:00');

INSERT INTO src_catalogue.rate_components
    (component_id, plan_id, charge_scope, component_desc, rate_uom,
     window_id, flow_direction, unit_rate, valid_from)
OVERRIDING SYSTEM VALUE
VALUES
    (336001,  316001, 'NET', 'Peak Usage',           'KWH', 326001,  'import', 0.1361, '2024-01-01'),
    (336002,  316001, 'NET', 'Off Peak Usage',       'KWH', 326003,  'import', 0.0378, '2024-01-01'),
    (336003,  316001, 'NET', 'UNSN + Daily charges', 'DAY', NULL, 'import', 0.6, '2024-01-01'),
    (336004,  316001, 'NET', 'Transmission Charges', 'KWH', NULL,  'import', 0.0235, '2024-01-01'),
    (336005,  316001, 'ENE', 'Peak Usage',           'KWH', 326006,  'import', 0.26, '2024-01-01'),
    (336006,  316001, 'ENE', 'Off Peak Usage',       'KWH', 326010, 'import', 0.1, '2024-01-01'),
    (336007,  316001, 'ENE', 'Shoulder Usage',       'KWH', 326008,  'import', 0.14, '2024-01-01'),
    (336011, 316001, 'OTH', 'ELECTRICITY AUTHORITY Levy', 'KWH', NULL, 'import', 0.0013, '2024-01-01');

INSERT INTO src_billing.accounts
    (ACCT_ID, ACCT_NO, CUST_ID, ICP_NUMBER, PLAN_CODE, PAY_METHOD, BILL_CYCLE_DAY, START_DT)
OVERRIDING SYSTEM VALUE
VALUES (226001, 'DEMO-ACCT-226001', 216001, 'DEMO0000000601AA', 'DEMO_STD_FXTOU_ESTIMATED_READS', 'DD', 28, '2019-01-01')
ON CONFLICT (ACCT_NO) DO UPDATE SET
    CUST_ID = EXCLUDED.CUST_ID, ICP_NUMBER = EXCLUDED.ICP_NUMBER,
    PLAN_CODE = EXCLUDED.PLAN_CODE;

INSERT INTO src_billing.invoices
    (INV_ID, INV_NO, ACCT_ID, PLAN_CODE, ISSUE_DT, PERIOD_FROM, PERIOD_TO,
     OPEN_BAL, PAYMENTS_APPL, BAL_BROUGHT_FWD, CHARGES_EXGST, GST_RATE,
     GST_AMT, TOTAL_AMT, DUE_DT, LATE_FEE, TOTAL_IF_LATE, PPD_AMT,
     TOTAL_IMPORT_KWH, TOTAL_EXPORT_KWH, EXPORT_CREDIT, INV_STATUS)
OVERRIDING SYSTEM VALUE
VALUES
    (236001, 'DEMO-INV-236001', 226001, 'DEMO_STD_FXTOU_ESTIMATED_READS', '2024-06-01', '2024-05-01', '2024-05-31',
     0, 0, 0, 0, 0.00, 0.00, 0, '2024-06-15', 0, NULL, 0, 0, 0, 0, 'ISSUED'),
    (236002, 'DEMO-INV-236002', 226001, 'DEMO_STD_FXTOU_ESTIMATED_READS', '2024-07-01', '2024-06-01', '2024-06-30',
     0, 0, 0, 0, 0.00, 0.00, 0, '2024-07-15', 0, NULL, 0, 0, 0, 0, 'ISSUED')
ON CONFLICT (INV_NO) DO UPDATE SET
    ACCT_ID = EXCLUDED.ACCT_ID, PERIOD_FROM = EXCLUDED.PERIOD_FROM,
    PERIOD_TO = EXCLUDED.PERIOD_TO;

-- =====================================================================
-- SERVING REPLICA
-- =====================================================================

INSERT INTO metering.network (network_id, network_code, network_name, region)
OVERRIDING SYSTEM VALUE
VALUES (116001, 'UNSN', 'Unison Networks', 'Hawke''s Bay')
ON CONFLICT (network_code) DO UPDATE SET network_name = EXCLUDED.network_name;

INSERT INTO billing.customer
    (customer_id, customer_number, full_name, email, is_medically_dependent, is_vulnerable)
OVERRIDING SYSTEM VALUE
VALUES (216001, '216001', 'Priya Raman', NULL, false, false)
ON CONFLICT (customer_number) DO UPDATE SET full_name = EXCLUDED.full_name;

INSERT INTO metering.property (property_id, address_line_1, suburb, city, climate_region)
OVERRIDING SYSTEM VALUE
VALUES (126001, '3 Marine Parade', 'Napier South', 'Napier', 'Hawke''s Bay');

INSERT INTO metering.icp (icp_id, icp_number, property_id, network_id, connection_date)
OVERRIDING SYSTEM VALUE
VALUES (136001, 'DEMO0000000601AA', 126001, 116001, '2019-01-01')
ON CONFLICT (icp_number) DO UPDATE SET
    property_id = EXCLUDED.property_id, network_id = EXCLUDED.network_id;

INSERT INTO metering.meter_register
    (register_id, icp_id, register_code, description, is_controlled, direction)
OVERRIDING SYSTEM VALUE
VALUES (156001, 136001, 'UN', 'Grid import', false, 'import')
ON CONFLICT (register_id) DO UPDATE SET icp_id = EXCLUDED.icp_id;

-- Resolve the source feed into serving surrogate keys. read_status 'E' in the
-- source becomes quality 'estimated' here; that mapping is what lets the
-- explainer know how much of a period it can actually stand behind.
INSERT INTO metering.meter_read_hh
    (icp_id, register_id, interval_start, kwh, quality, source_file)
SELECT 136001, 156001, interval_start, kwh,
       CASE read_status WHEN 'E' THEN 'estimated'::common.read_quality
                        ELSE 'actual'::common.read_quality END,
       source_file
FROM src_metering.interval_reads
WHERE icp_identifier = 'DEMO0000000601AA';

INSERT INTO catalogue.plan
    (plan_id, plan_code, plan_name, network_id, user_type, is_tou,
     prompt_payment_discount_pct, effective_from, is_active)
OVERRIDING SYSTEM VALUE
VALUES (316001, 'DEMO_STD_FXTOU_ESTIMATED_READS', 'Demo Standard Fixed TOU', 116001, 'standard', true,
        0, '2024-01-01', true)
ON CONFLICT (plan_code, effective_from) DO UPDATE SET
    plan_name = EXCLUDED.plan_name, network_id = EXCLUDED.network_id;

INSERT INTO catalogue.tou_band
    (band_id, plan_id, charge_group, band_name, days_of_week, start_time, end_time)
OVERRIDING SYSTEM VALUE
VALUES
    (326001,  316001, 'network', 'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '07:00', '11:00'),
    (326002,  316001, 'network', 'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '17:00', '21:30'),
    (326003,  316001, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '00:00', '07:00'),
    (326004,  316001, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '11:00', '17:00'),
    (326005,  316001, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '21:30', '24:00'),
    (326006,  316001, 'energy',  'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '07:00', '11:00'),
    (326007,  316001, 'energy',  'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '17:00', '21:00'),
    (326008,  316001, 'energy',  'shoulder', ARRAY[1,2,3,4,5,6,7]::smallint[], '11:00', '17:00'),
    (326009,  316001, 'energy',  'shoulder', ARRAY[1,2,3,4,5,6,7]::smallint[], '21:00', '23:00'),
    (326010, 316001, 'energy',  'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '00:00', '07:00'),
    (326011, 316001, 'energy',  'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '23:00', '24:00');

INSERT INTO catalogue.tariff_component
    (component_id, plan_id, charge_group, component_name, basis, band_id, direction, rate, effective_from)
OVERRIDING SYSTEM VALUE
VALUES
    (336001,  316001, 'network', 'Peak Usage',     'per_kwh', 326001,  'import', 0.1361, '2024-01-01'),
    (336002,  316001, 'network', 'Off Peak Usage', 'per_kwh', 326003,  'import', 0.0378, '2024-01-01'),
    (336003,  316001, 'network', 'UNSN + Daily charges', 'per_day', NULL, 'import', 0.6, '2024-01-01'),
    (336004,  316001, 'network', 'Transmission Charges', 'per_kwh', NULL, 'import', 0.0235, '2024-01-01'),
    (336005,  316001, 'energy',  'Peak Usage',     'per_kwh', 326006,  'import', 0.26, '2024-01-01'),
    (336006,  316001, 'energy',  'Off Peak Usage', 'per_kwh', 326010, 'import', 0.1, '2024-01-01'),
    (336007,  316001, 'energy',  'Shoulder Usage', 'per_kwh', 326008,  'import', 0.14, '2024-01-01'),
    (336011, 316001, 'other',   'ELECTRICITY AUTHORITY Levy', 'per_kwh', NULL, 'import', 0.0013, '2024-01-01');

INSERT INTO billing.account
    (account_id, account_number, customer_id, icp_id, plan_id, payment_method,
     billing_cycle_day, start_date)
OVERRIDING SYSTEM VALUE
VALUES (226001, 'DEMO-ACCT-226001', 216001, 136001, 316001, 'direct_debit', 28, '2019-01-01')
ON CONFLICT (account_number) DO UPDATE SET
    customer_id = EXCLUDED.customer_id, icp_id = EXCLUDED.icp_id, plan_id = EXCLUDED.plan_id;

INSERT INTO billing.bill
    (bill_id, account_id, invoice_number, plan_id, issue_date, period_start, period_end,
     opening_balance, payments_applied, balance_brought_fwd, charges_excl_gst,
     gst_rate, gst_amount, total_charges, due_date, late_payment_fee,
     total_if_paid_late, prompt_payment_discount, total_import_kwh,
     total_export_kwh, export_credit, status)
OVERRIDING SYSTEM VALUE
VALUES
    (236001, 226001, 'DEMO-INV-236001', 316001, '2024-06-01', '2024-05-01', '2024-05-31',
     0, 0, 0, 0, 0.00, 0.00, 0, '2024-06-15', 0, NULL, 0, 0, 0, 0, 'issued'),
    (236002, 226001, 'DEMO-INV-236002', 316001, '2024-07-01', '2024-06-01', '2024-06-30',
     0, 0, 0, 0, 0.00, 0.00, 0, '2024-07-15', 0, NULL, 0, 0, 0, 0, 'issued')
ON CONFLICT (invoice_number) DO UPDATE SET
    period_start = EXCLUDED.period_start, period_end = EXCLUDED.period_end;

-- ---------------------------------------------------------------------
-- Bill lines, RATED from the reads rather than transcribed.
--
-- The sample bill in this folder was typed from a printed invoice, because it
-- had to reproduce one exactly. These fixtures are generated, so the lines are
-- derived instead: band the half hours, sum them, apply the rate. The bill
-- therefore reconciles to the meter by construction, and any future change to
-- the shape of the reads flows straight through to the charges.
--
-- Line losses follow the sample bill's implied factor of 5.11%; the
-- energy import amount is (quantity + loss) x rate.
-- ---------------------------------------------------------------------
WITH banded AS (
    SELECT b.bill_id, b.period_start, b.period_end,
           (b.period_end - b.period_start) + 1 AS days,
           r.kwh,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr
    FROM billing.bill b
    JOIN metering.meter_read_hh r
      ON r.icp_id = 136001
     AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date
         BETWEEN b.period_start AND b.period_end
    WHERE b.bill_id IN (236001, 236002)
), totals AS (
    SELECT bill_id, days,
           ROUND(SUM(kwh), 2) AS total_kwh,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 7 AND hr < 11) OR (hr >= 17 AND hr < 21.5)), 2) AS net_peak,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 7 AND hr < 11) OR (hr >= 17 AND hr < 21)), 2)   AS ene_peak,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 11 AND hr < 17) OR (hr >= 21 AND hr < 23)), 2)  AS ene_shoulder
    FROM banded GROUP BY bill_id, days
), lines AS (
    SELECT bill_id, 336001 AS component_id, 'network'::common.charge_group AS cg, 'Peak Usage' AS label,
           'peak' AS band, net_peak AS qty, 'per_kwh'::common.rate_basis AS unit, 0.1361::numeric AS rate,
           NULL::numeric AS loss, ROUND(net_peak * 0.1361, 2) AS amount, 10 AS sort_order FROM totals
    UNION ALL SELECT bill_id, 336002, 'network', 'Off Peak Usage', 'off_peak',
           total_kwh - net_peak, 'per_kwh', 0.0378,
           NULL, ROUND((total_kwh - net_peak) * 0.0378, 2), 20 FROM totals
    UNION ALL SELECT bill_id, 336003, 'network', 'UNSN + Daily charges', NULL,
           days::numeric, 'per_day', 0.6,
           NULL, ROUND(days * 0.6, 2), 30 FROM totals
    UNION ALL SELECT bill_id, 336004, 'network', 'Transmission Charges', NULL,
           total_kwh, 'per_kwh', 0.0235,
           NULL, ROUND(total_kwh * 0.0235, 2), 40 FROM totals
    UNION ALL SELECT bill_id, 336005, 'energy', 'Peak Usage', 'peak',
           ene_peak, 'per_kwh', 0.26, ROUND(ene_peak * 0.0511, 2),
           ROUND((ene_peak + ROUND(ene_peak * 0.0511, 2)) * 0.26, 2), 50 FROM totals
    UNION ALL SELECT bill_id, 336007, 'energy', 'Shoulder Usage', 'shoulder',
           ene_shoulder, 'per_kwh', 0.14, ROUND(ene_shoulder * 0.0511, 2),
           ROUND((ene_shoulder + ROUND(ene_shoulder * 0.0511, 2)) * 0.14, 2), 60 FROM totals
    UNION ALL SELECT bill_id, 336006, 'energy', 'Off Peak Usage', 'off_peak',
           total_kwh - ene_peak - ene_shoulder, 'per_kwh', 0.1,
           ROUND((total_kwh - ene_peak - ene_shoulder) * 0.0511, 2),
           ROUND(((total_kwh - ene_peak - ene_shoulder)
                  + ROUND((total_kwh - ene_peak - ene_shoulder) * 0.0511, 2)) * 0.1, 2), 70 FROM totals
    UNION ALL SELECT bill_id, 336011, 'other', 'ELECTRICITY AUTHORITY Levy', NULL,
           total_kwh, 'per_kwh', 0.0013,
           NULL, ROUND(total_kwh * 0.0013, 2), 80 FROM totals
)
INSERT INTO billing.bill_line
    (bill_id, component_id, charge_group, direction, line_label, band_name,
     quantity, unit, rate, loss_kwh, amount, sort_order)
SELECT bill_id, component_id, cg, 'import', label, band, qty, unit, rate, loss, amount, sort_order
FROM lines;

-- Mirror the rated lines back into the source invoice, so the source system
-- and the replica agree the way they would in production.
INSERT INTO src_billing.invoice_lines
    (INV_ID, CHG_GROUP, FLOW_DIR, LINE_DESC, TOU_BAND, QTY, UOM, UNIT_RATE, LOSS_QTY, LINE_AMT, SORT_SEQ)
SELECT bill_id,
       CASE charge_group WHEN 'network' THEN 'NET' WHEN 'energy' THEN 'ENE' ELSE 'OTH' END,
       'IMPORT', line_label, UPPER(band_name), quantity,
       CASE unit WHEN 'per_day' THEN 'DAY' ELSE 'KWH' END,
       rate, loss_kwh, amount, sort_order
FROM billing.bill_line WHERE bill_id IN (236001, 236002);

UPDATE billing.bill AS b
SET charges_excl_gst = t.charges,
    gst_amount       = ROUND(t.charges * b.gst_rate, 2),
    total_charges    = ROUND(t.charges * (1 + b.gst_rate), 2),
    total_import_kwh = t.import_kwh
FROM (SELECT bill_id, ROUND(SUM(amount), 2) AS charges,
             SUM(quantity) FILTER (WHERE charge_group = 'energy' AND unit = 'per_kwh') AS import_kwh
      FROM billing.bill_line WHERE bill_id IN (236001, 236002) GROUP BY bill_id) AS t
WHERE b.bill_id = t.bill_id;

UPDATE src_billing.invoices AS i
SET CHARGES_EXGST = t.charges,
    GST_AMT       = ROUND(t.charges * i.GST_RATE, 2),
    TOTAL_AMT     = ROUND(t.charges * (1 + i.GST_RATE), 2),
    TOTAL_IMPORT_KWH = t.import_kwh
FROM (SELECT INV_ID, ROUND(SUM(LINE_AMT), 2) AS charges,
             SUM(CASE WHEN CHG_GROUP = 'ENE' AND UOM = 'KWH' THEN QTY ELSE 0 END) AS import_kwh
      FROM src_billing.invoice_lines WHERE INV_ID IN (236001, 236002) GROUP BY INV_ID) AS t
WHERE i.INV_ID = t.INV_ID;

-- =====================================================================
-- MARTS
-- =====================================================================

INSERT INTO kea.mart_account_context
    (account_id, account_number, customer_id, customer_number, customer_name,
     email, icp_id, icp_number, address_display, climate_region, network_code,
     network_name, has_solar, has_controlled_load, plan_id, plan_code, plan_name,
     user_type, is_tou, daily_fixed_charge, payment_method, billing_cycle_day,
     current_period_start, current_period_end, days_into_period, estimated_read_pct,
     last_read_at, is_quarantined)
VALUES
    (226001, 'DEMO-ACCT-226001', 216001, '216001', 'Priya Raman', NULL,
     136001, 'DEMO0000000601AA', '3 Marine Parade, Napier South', 'Hawke''s Bay', 'UNSN', 'Unison Networks',
     false, false, 316001, 'DEMO_STD_FXTOU_ESTIMATED_READS', 'Demo Standard Fixed TOU', 'standard', true,
     0.6, 'direct_debit', 28, '2024-06-01', '2024-06-30', 30,
     66.67, timestamptz '2024-06-30 23:30:00+12', false)
ON CONFLICT (account_id) DO UPDATE SET
    current_period_start = EXCLUDED.current_period_start,
    current_period_end   = EXCLUDED.current_period_end,
    days_into_period     = EXCLUDED.days_into_period,
    estimated_read_pct   = EXCLUDED.estimated_read_pct,
    last_read_at         = EXCLUDED.last_read_at;

-- Daily usage, derived from the reads. base_load_kwh is the quietest half hour
-- of the day scaled to 24 hours -- the always-on draw, and the figure the
-- faulty-appliance and new-appliance fixtures are designed to separate.
-- Weather is re-derived here with the same formula the reads were built from,
-- so mart and meter cannot drift apart.
--
-- Built from a DATE SPINE left-joined to the reads, not from the reads alone.
-- A day the meter never reported would otherwise be absent from the mart
-- entirely, and a row that does not exist cannot be counted as missing --
-- mart_bill_explanation.missing_interval_count would read 0 for a period with
-- a hole in it. With the spine, such a day lands with interval_count = 0 and
-- the gap is visible to anything that looks.
INSERT INTO kea.mart_daily_usage
    (icp_id, usage_date, import_kwh, export_kwh, controlled_kwh, uncontrolled_kwh,
     base_load_kwh, peak_kwh, off_peak_kwh, interval_count, expected_intervals,
     estimated_count, temp_mean_c, heating_degree_days, is_weekend)
SELECT 136001,
       sp.d,
       COALESCE(ROUND(SUM(x.kwh), 4), 0), 0, 0, COALESCE(ROUND(SUM(x.kwh), 4), 0),
       ROUND(MIN(x.kwh) * 48, 4),
       COALESCE(ROUND(SUM(x.kwh) FILTER (WHERE (x.hr >= 7 AND x.hr < 11) OR (x.hr >= 17 AND x.hr < 21)), 4), 0),
       COALESCE(ROUND(SUM(x.kwh) FILTER (WHERE x.hr < 7 OR x.hr >= 23), 4), 0),
       COUNT(x.kwh)::smallint,
       -- Never a fixed 48: a local day is 46 or 50 half hours across a DST
       -- change, and hardcoding 48 books phantom missing intervals on one and
       -- a NEGATIVE missing count on the other.
       (EXTRACT(epoch FROM ((sp.d + 1)::timestamp AT TIME ZONE 'Pacific/Auckland')
                          - (sp.d::timestamp AT TIME ZONE 'Pacific/Auckland')) / 1800)::smallint,
       COUNT(*) FILTER (WHERE x.quality = 'estimated')::smallint,
       ROUND(((13.6 + CASE WHEN sp.d >= date '2024-06-01' THEN 11.1 - 13.6 ELSE 0 END)
              + 2.2 * SIN(EXTRACT(day FROM sp.d) * 0.7))::numeric, 2),
       ROUND(GREATEST(16.0 - ((13.6 + CASE WHEN sp.d >= date '2024-06-01' THEN 11.1 - 13.6 ELSE 0 END)
              + 2.2 * SIN(EXTRACT(day FROM sp.d) * 0.7))::numeric, 0), 2),
       EXTRACT(isodow FROM sp.d) >= 6
FROM (
    SELECT gs::date AS d
    FROM billing.bill b
    CROSS JOIN LATERAL generate_series(b.period_start, b.period_end, interval '1 day') gs
    WHERE b.bill_id IN (236001, 236002)
) sp
LEFT JOIN (
    SELECT (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr,
           r.kwh, r.quality
    FROM metering.meter_read_hh r WHERE r.icp_id = 136001
) x ON x.d = sp.d
GROUP BY sp.d
ON CONFLICT (icp_id, usage_date) DO UPDATE SET
    import_kwh = EXCLUDED.import_kwh, base_load_kwh = EXCLUDED.base_load_kwh,
    peak_kwh = EXCLUDED.peak_kwh, off_peak_kwh = EXCLUDED.off_peak_kwh,
    interval_count = EXCLUDED.interval_count, estimated_count = EXCLUDED.estimated_count,
    expected_intervals = EXCLUDED.expected_intervals,
    temp_mean_c = EXCLUDED.temp_mean_c, heating_degree_days = EXCLUDED.heating_degree_days;

-- Hourly profile per calendar month. This is the mart that tells the
-- appliance scenarios apart: compare the same hour across the two months.
INSERT INTO kea.mart_hourly_profile
    (icp_id, profile_month, hour_of_day, is_weekday, avg_import_kwh, avg_export_kwh, sample_days)
SELECT 136001,
       date_trunc('month', d)::date,
       hour_of_day, is_weekday,
       -- Divided by HOURS OBSERVED, not by days. A local clock hour is not
       -- always an hour long: when New Zealand leaves daylight time the 2am
       -- hour runs twice and that date brings FOUR half hours to the group,
       -- and when it enters daylight time the 2am hour never happens and the
       -- date brings none. COUNT(DISTINCT d) books the doubled hour as a
       -- single day and inflates that hour's average -- 12.5% on the April
       -- weekend rows of the 13-month fixture, which spans both transitions.
       -- COUNT(DISTINCT interval_start) / 2 is the true number of hours behind
       -- the sum, so both transitions normalise themselves.
       ROUND(SUM(kwh) / NULLIF(COUNT(DISTINCT interval_start) / 2.0, 0), 4), 0,
       -- sample_days stays a DAY count on purpose. It answers "how much
       -- evidence is behind this bar", which is a different question from
       -- "what is the rate in this hour". On the spring-forward date the hour
       -- is genuinely absent, and one fewer sample day is the honest record.
       COUNT(DISTINCT d)::smallint
FROM (
    SELECT r.interval_start,
           (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')::smallint AS hour_of_day,
           EXTRACT(isodow FROM (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date) <= 5 AS is_weekday,
           r.kwh
    FROM metering.meter_read_hh r WHERE r.icp_id = 136001
) y
GROUP BY 2, 3, 4
ON CONFLICT (icp_id, profile_month, hour_of_day, is_weekday) DO UPDATE SET
    avg_import_kwh = EXCLUDED.avg_import_kwh, sample_days = EXCLUDED.sample_days;

-- ---------------------------------------------------------------------
-- Bill explanation.
--
-- The drivers are COMPUTED from the seeded data, in the attribution order the
-- schema lays out, each one measured against the prior period and converted to
-- dollars at the prior period's own average variable rate:
--
--   1 period length  extra days x prior daily kWh x rate, plus the fixed charge
--   2 weather        heating sensitivity x extra degree days x rate
--   3 base load      extra always-on kWh per day x days x rate
--   4 tariff         zero -- same plan, same rates in both periods
--   5 unexplained    whatever the first four do not account for
--
-- Sequential attribution, so nothing is counted twice: each driver is measured
-- after the ones above it have been taken out. Because the figures are derived
-- rather than typed, they stay true if the read shape is ever retuned.
-- ---------------------------------------------------------------------
WITH per_bill AS (
    SELECT b.bill_id, b.period_start, b.period_end, b.period_days,
           b.charges_excl_gst AS amount, b.total_import_kwh AS kwh,
           (SELECT ROUND(AVG(base_load_kwh), 4) FROM kea.mart_daily_usage u
             WHERE u.icp_id = 136001 AND u.usage_date BETWEEN b.period_start AND b.period_end) AS base_day,
           (SELECT ROUND(SUM(heating_degree_days), 2) FROM kea.mart_daily_usage u
             WHERE u.icp_id = 136001 AND u.usage_date BETWEEN b.period_start AND b.period_end) AS hdd,
           (SELECT ROUND(AVG(temp_mean_c), 2) FROM kea.mart_daily_usage u
             WHERE u.icp_id = 136001 AND u.usage_date BETWEEN b.period_start AND b.period_end) AS temp,
           (SELECT COALESCE(SUM(estimated_count), 0) FROM kea.mart_daily_usage u
             WHERE u.icp_id = 136001 AND u.usage_date BETWEEN b.period_start AND b.period_end) AS est_intervals,
           (SELECT COALESCE(SUM(expected_intervals - interval_count), 0) FROM kea.mart_daily_usage u
             WHERE u.icp_id = 136001 AND u.usage_date BETWEEN b.period_start AND b.period_end) AS missing_intervals,
           (SELECT COALESCE(SUM(expected_intervals), 0) FROM kea.mart_daily_usage u
             WHERE u.icp_id = 136001 AND u.usage_date BETWEEN b.period_start AND b.period_end) AS expected_intervals
    FROM billing.bill b WHERE b.bill_id IN (236001, 236002)
), pair AS (
    SELECT c.*, p.bill_id AS prior_id, p.period_days AS prior_days,
           p.amount AS prior_amount, p.kwh AS prior_kwh,
           p.base_day AS prior_base, p.hdd AS prior_hdd, p.temp AS prior_temp,
           -- the prior period's own average variable rate: everything that
           -- scales with kWh, divided by the kWh it was charged on
           ROUND((p.amount - 0.6 * p.period_days) / NULLIF(p.kwh, 0), 6) AS rate_per_kwh
    FROM per_bill c JOIN per_bill p ON p.bill_id = 236001
    WHERE c.bill_id = 236002
), attributed AS (
    SELECT pair.*,
           ROUND(pair.amount - pair.prior_amount, 2) AS d_amount,
           ROUND((pair.period_days - pair.prior_days)
                 * (pair.prior_kwh / NULLIF(pair.prior_days, 0) * pair.rate_per_kwh + 0.6), 2) AS d_period,
           ROUND(0.93 * (pair.hdd - pair.prior_hdd) * pair.rate_per_kwh, 2) AS d_weather,
           ROUND((pair.base_day - pair.prior_base) * pair.period_days * pair.rate_per_kwh, 2) AS d_base
    FROM pair
)
INSERT INTO kea.mart_bill_explanation
    (bill_id, account_id, icp_id, period_start, period_end, period_days,
     total_amount, total_kwh, prior_bill_id, prior_period_days, prior_total_amount,
     prior_total_kwh, amount_delta, amount_delta_pct, kwh_delta,
     delta_from_period_length, delta_from_weather, delta_from_base_load,
     delta_from_tariff, delta_unexplained, hdd_this_period, hdd_prior_period,
     temp_mean_this, temp_mean_prior, base_load_kwh_day_this, base_load_kwh_day_prior,
     heating_sensitivity_kwh_per_hdd, estimated_read_pct, missing_interval_count,
     is_explainable)
SELECT bill_id, 226001, 136001, period_start, period_end, period_days,
       amount, kwh, prior_id, prior_days, prior_amount, prior_kwh,
       d_amount,
       ROUND(d_amount / NULLIF(prior_amount, 0) * 100, 2),
       ROUND(kwh - prior_kwh, 4),
       d_period, d_weather, d_base, 0.00,
       ROUND(d_amount - d_period - d_weather - d_base, 2),
       hdd, prior_hdd, temp, prior_temp, base_day, prior_base, 0.93,
       ROUND(est_intervals * 100.0 / NULLIF(expected_intervals, 0), 2),
       missing_intervals,
       -- Honest by construction: claim the increase is explained only when the
       -- measured drivers cover most of it AND the period was actually read.
       (ABS(d_period + d_weather + d_base) >= ABS(d_amount) * 0.60
        AND est_intervals = 0 AND missing_intervals = 0)
FROM attributed;

-- The prior bill has nothing before it, so no driver can be attributed.
-- is_explainable = false is the honest state: the agent should say "this is
-- the earliest bill I have" rather than assert a cause.
INSERT INTO kea.mart_bill_explanation
    (bill_id, account_id, icp_id, period_start, period_end, period_days,
     total_amount, total_kwh, estimated_read_pct, missing_interval_count, is_explainable)
SELECT bill_id, 226001, 136001, period_start, period_end, period_days,
       charges_excl_gst, COALESCE(total_import_kwh, 0), 0, 0, false
FROM billing.bill WHERE bill_id = 236001;

INSERT INTO kea.mart_load_log
    (mart_name, started_at, completed_at, row_count, warehouse_run_id, status)
VALUES ('sample_scenario_estimated_reads', now(), now(), 2, 'sample-scenario-estimated_reads', 'succeeded');

-- =====================================================================
-- SELF-CHECK — re-derives the fixture and fails the transaction on drift
-- =====================================================================
DO $$
DECLARE
    v_prior numeric; v_curr numeric; v_delta numeric;
    v_kwh_p numeric; v_kwh_c numeric;
    v_lines numeric; v_drivers numeric; v_expl boolean;
    v_top text; v_share numeric;
BEGIN
    SELECT charges_excl_gst, total_import_kwh INTO v_prior, v_kwh_p
      FROM billing.bill WHERE bill_id = 236001;
    SELECT charges_excl_gst, total_import_kwh INTO v_curr, v_kwh_c
      FROM billing.bill WHERE bill_id = 236002;

    -- 1. the bill header must equal the sum of its own lines
    SELECT ROUND(SUM(amount), 2) INTO v_lines
      FROM billing.bill_line WHERE bill_id = 236002;
    IF v_lines <> v_curr THEN
        RAISE EXCEPTION 'current bill header % does not match its lines %', v_curr, v_lines;
    END IF;

    -- 2. the scenario must actually produce a HIGHER bill
    v_delta := ROUND(v_curr - v_prior, 2);
    IF v_delta <= 0 THEN
        RAISE EXCEPTION 'scenario estimated_reads did not raise the bill: prior %, current %',
                        v_prior, v_curr;
    END IF;

    -- 3. the five drivers must add up to the delta exactly
    SELECT ROUND(delta_from_period_length + delta_from_weather + delta_from_base_load
                 + delta_from_tariff + delta_unexplained, 2), is_explainable
      INTO v_drivers, v_expl
      FROM kea.mart_bill_explanation WHERE bill_id = 236002;
    IF v_drivers <> v_delta THEN
        RAISE EXCEPTION 'drivers sum to % but the delta is %', v_drivers, v_delta;
    END IF;

    -- 4. the driver this fixture exists to demonstrate must be the largest one
    SELECT driver, ROUND(amt / NULLIF(v_delta, 0) * 100, 1) INTO v_top, v_share
      FROM (SELECT 'period_length' AS driver, delta_from_period_length AS amt
              FROM kea.mart_bill_explanation WHERE bill_id = 236002
            UNION ALL SELECT 'weather', delta_from_weather
              FROM kea.mart_bill_explanation WHERE bill_id = 236002
            UNION ALL SELECT 'base_load', delta_from_base_load
              FROM kea.mart_bill_explanation WHERE bill_id = 236002
            UNION ALL SELECT 'unexplained', delta_unexplained
              FROM kea.mart_bill_explanation WHERE bill_id = 236002) z
     ORDER BY amt DESC LIMIT 1;
    IF v_top <> 'base_load' THEN
        RAISE EXCEPTION
            'expected % to be the largest driver for estimated_reads, got % at % percent of the delta',
            'base_load', v_top, v_share;
    END IF;

    -- 5. and the hedging flag must land where the fixture intends
    IF v_expl <> false THEN
        RAISE EXCEPTION 'expected is_explainable = false for estimated_reads, got %', v_expl;
    END IF;

    RAISE NOTICE
        'estimated_reads: prior $%, current $% (up $%, % percent); % kWh -> % kWh; top driver % at % percent; explainable %',
        v_prior, v_curr, v_delta,
        ROUND(v_delta / NULLIF(v_prior, 0) * 100, 1),
        v_kwh_p, v_kwh_c, v_top, v_share, v_expl;
END $$;

COMMIT;

-- Verification queries:
-- SELECT period_start, period_end, period_days, charges_excl_gst, total_import_kwh
--   FROM billing.bill WHERE account_id = 226001 ORDER BY period_start;
-- SELECT amount_delta, delta_from_period_length, delta_from_weather,
--        delta_from_base_load, delta_unexplained, is_explainable
--   FROM kea.mart_bill_explanation WHERE bill_id = 236002;
-- SELECT profile_month, hour_of_day, avg_import_kwh
--   FROM kea.mart_hourly_profile WHERE icp_id = 136001 AND is_weekday
--   ORDER BY hour_of_day, profile_month;
