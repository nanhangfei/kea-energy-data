-- =====================================================================
-- Kea Energy — THIRTEEN MONTHS — a full year of half-hourly reads, plus one
-- =====================================================================
--
-- Thirteen consecutive monthly bills for one household, every half hour of meter data behind them, and a real seasonal swing between winter and summer.
--
-- Synthetic fixture. No real customer, address, ICP or meter appears here;
-- every id carries the table it belongs to in its first two digits and this
-- fixture's number (8) in its third -- see "Fixture ids" in data/README.md.
-- The half-hourly reads are generated deterministically by the CTE below
-- rather than copied from a feed.
--
-- WHAT IS IN HERE
--   13 monthly bills                2023-08-01 .. 2024-08-31
--   397 days of half-hourly reads      import only, one register
--   mart_daily_usage             one row per day, built from a date spine
--   mart_hourly_profile          13 months x 24 hours x weekday/weekend
--   mart_bill_explanation        every bill except the first, against the one before it
--
-- WHY IT EXISTS
-- The six bill_higher_* fixtures each hold two months. Two months cannot
-- answer "how does this winter compare with last winter", and cannot exercise
-- anything that reads a year of history: seasonal normalisation, year-on-year
-- comparison, or a mart that groups by month and expects more than two.
-- This file holds thirteen, so the same calendar month appears twice.
--
-- WEATHER IS THE ONLY THING DONE DIFFERENTLY. The sibling fixtures use one
-- mean temperature per period. Here temperature follows an annual sinusoid --
-- mean 15.2 degrees, swinging 4.6 either side, warmest in mid-January --
-- so heating degree days rise into winter and fall away again, and the usage
-- curve has a real shape. A year of flat reads would be a single month
-- repeated thirteen times and would demonstrate nothing.
--
-- THE DST DAYS ARE REAL HERE. Thirteen months crosses both New Zealand clock
-- changes, so this is the only fixture in data/sample where the 46- and
-- 50-interval local days that metering.meter_read_hh's comment warns about
-- actually occur -- 24 September 2023 and 7 April 2024. expected_intervals is
-- therefore COMPUTED per day from the local day length, never assumed to be
-- 48, and the self-check fails if those two days are not present. Anything
-- that hardcodes 48 will report phantom missing intervals against this file,
-- which is exactly what it is here to catch.
--
-- Re-runnable: every write is either scoped-DELETE-then-INSERT or
-- ON CONFLICT DO UPDATE, so running this file twice leaves the same rows.
-- The DO block at the end re-derives the fixture and raises on drift.
--
-- Run after kea_source_schema.sql and kea_serving_schema.sql.
-- Independent of every other file in data/sample: its own household, its own
-- network, its own plan.

BEGIN;

SET search_path TO public;

-- ---------------------------------------------------------------------
-- Idempotency: remove only this fixture, children before parents.
-- ---------------------------------------------------------------------
DELETE FROM src_metering.interval_reads WHERE icp_identifier = 'DEMO0000000801AA';
DELETE FROM metering.meter_read_hh      WHERE icp_id = 138001;
DELETE FROM src_billing.invoice_lines   WHERE INV_ID BETWEEN 238001 AND 238013;
DELETE FROM src_billing.invoices        WHERE INV_ID BETWEEN 238001 AND 238013;
DELETE FROM src_billing.accounts        WHERE ACCT_ID = 228001;
DELETE FROM src_billing.customers       WHERE CUST_ID = 218001;
DELETE FROM src_catalogue.rate_components WHERE component_id BETWEEN 338001 AND 338011;
DELETE FROM src_catalogue.tou_windows     WHERE window_id    BETWEEN 328001 AND 328011;
DELETE FROM src_catalogue.price_plans     WHERE plan_id = 318001;
DELETE FROM src_metering.registers        WHERE register_id = 158001;
DELETE FROM src_metering.meters           WHERE meter_id = 148001;
DELETE FROM src_metering.connection_points WHERE icp_id = 138001;
DELETE FROM src_metering.networks         WHERE network_id = 118001;

DELETE FROM kea.mart_bill_explanation WHERE bill_id BETWEEN 238001 AND 238013;
DELETE FROM kea.mart_hourly_profile   WHERE icp_id = 138001;
DELETE FROM kea.mart_daily_usage      WHERE icp_id = 138001;
DELETE FROM kea.mart_account_context  WHERE account_id = 228001;
DELETE FROM kea.mart_load_log         WHERE warehouse_run_id = 'sample-scenario-13_month_history';
DELETE FROM billing.bill_line WHERE bill_id BETWEEN 238001 AND 238013;
DELETE FROM billing.bill      WHERE bill_id BETWEEN 238001 AND 238013;
DELETE FROM billing.account   WHERE account_id = 228001;
DELETE FROM catalogue.tariff_component WHERE component_id BETWEEN 338001 AND 338011;
DELETE FROM catalogue.tou_band         WHERE band_id      BETWEEN 328001 AND 328011;
DELETE FROM catalogue.plan             WHERE plan_id = 318001;
DELETE FROM metering.meter_register WHERE register_id = 158001;
DELETE FROM metering.icp            WHERE icp_id = 138001;
DELETE FROM metering.property       WHERE property_id = 128001;
DELETE FROM metering.network        WHERE network_id = 118001;
DELETE FROM billing.customer        WHERE customer_id = 218001;

-- =====================================================================
-- SOURCE SYSTEMS
-- =====================================================================

INSERT INTO src_billing.customers
    (CUST_ID, CUST_NO, CUST_NAME, EMAIL_ADDR, PHONE_NO,
     ADDR_LINE1, ADDR_SUBURB, ADDR_CITY, ADDR_POSTCODE)
OVERRIDING SYSTEM VALUE
VALUES (218001, '218001', 'Rangi Paterson', 'rangi@kea.example', '021 218 1001',
        '27 Rattray Street', 'Mornington', 'Dunedin', NULL)
ON CONFLICT (CUST_NO) DO UPDATE SET
    CUST_NAME = EXCLUDED.CUST_NAME,
    ADDR_LINE1 = EXCLUDED.ADDR_LINE1,
    ADDR_SUBURB = EXCLUDED.ADDR_SUBURB,
    ADDR_CITY = EXCLUDED.ADDR_CITY;

INSERT INTO src_metering.networks (network_id, network_code, network_name, region)
OVERRIDING SYSTEM VALUE
VALUES (118001, 'AUNT', 'Aurora Energy', 'Otago')
ON CONFLICT (network_code) DO UPDATE SET
    network_name = EXCLUDED.network_name, region = EXCLUDED.region;

INSERT INTO src_metering.connection_points
    (icp_id, icp_identifier, network_id, address_full, suburb, city,
     postcode, climate_region, connection_date)
OVERRIDING SYSTEM VALUE
VALUES (138001, 'DEMO0000000801AA', 118001, '27 Rattray Street, Mornington',
        'Mornington', 'Dunedin', NULL, 'Otago', '2019-01-01')
ON CONFLICT (icp_identifier) DO UPDATE SET
    network_id = EXCLUDED.network_id,
    address_full = EXCLUDED.address_full,
    climate_region = EXCLUDED.climate_region;

INSERT INTO src_metering.meters (meter_id, icp_id, serial_number, is_smart, installed_on)
OVERRIDING SYSTEM VALUE
VALUES (148001, 138001, 'DEMO-METER-148001', true, '2019-01-01')
ON CONFLICT (meter_id) DO UPDATE SET
    icp_id = EXCLUDED.icp_id, serial_number = EXCLUDED.serial_number;

-- Import only, like the six comparison fixtures: no solar. A year of export
-- would be a second seasonal curve running opposite to the first, and this
-- file is about the length of the history, not about generation.
INSERT INTO src_metering.registers
    (register_id, meter_id, register_code, description, is_controlled, flow_direction)
OVERRIDING SYSTEM VALUE
VALUES (158001, 148001, 'UN', 'Grid import', false, 'import')
ON CONFLICT (register_id) DO UPDATE SET
    meter_id = EXCLUDED.meter_id, register_code = EXCLUDED.register_code;

-- ---------------------------------------------------------------------
-- 397 days of half-hourly reads.
--
-- kwh = daily_total * shape(hh) / sum(shape over day)      the household
--     + heating(hh) * heating_degree_days(day)             weather response
--
-- Same two terms as every other fixture, and the same 0.93 kWh per
-- degree day. The household term is CONSTANT at 8.50 kWh/day across the
-- whole thirteen months -- nothing changes in this house. Every bit of the
-- seasonal swing comes from the heating term, which is what makes the fixture
-- honest: a year-on-year comparison over these reads should attribute almost
-- all of the difference to weather, because that is all there is.
--
-- generate_series steps in absolute time, so DST is handled by the timezone
-- conversion rather than by the series: the local day on 24 September 2023 has
-- 46 half hours and 7 April 2024 has 50. Normalising by SUM(w) OVER
-- (PARTITION BY d) -- the LOCAL date -- means the daily total still lands on
-- 8.50 kWh on those days rather than being 4 percent out.
-- ---------------------------------------------------------------------
WITH half_hours AS (
    -- Both ends are anchored to NZ LOCAL midnight via AT TIME ZONE, not to a
    -- fixed +12 literal. +12 is NZST; from late September to early April the
    -- country is on NZDT (+13), so a literal offset silently starts the series
    -- an hour late and loses the first two half hours of the period. This
    -- fixture spans both, and would be wrong at whichever end fell in NZDT.
    SELECT generate_series(
        (date '2023-08-01')::timestamp AT TIME ZONE 'Pacific/Auckland',
        (date '2024-08-31' + 1)::timestamp AT TIME ZONE 'Pacific/Auckland'
            - interval '30 minutes',
        interval '30 minutes') AS interval_start
), tagged AS (
    SELECT interval_start,
           (interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour   FROM interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr
    FROM half_hours
), weathered AS (
    -- SIN()/COS() return double precision, so the whole sum is cast back to
    -- numeric before rounding: round(double, int) has no overload.
    SELECT t.*, ROUND((15.2 + 4.6 * COS(2 * PI() * (EXTRACT(doy FROM d) - 15) / 365.25) + 2.2 * SIN(EXTRACT(day FROM d) * 0.7))::numeric, 2) AS temp_c
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
           8.50::numeric AS daily_kwh,
           CASE
               WHEN hr >= 6  AND hr < 9  THEN 0.040
               WHEN hr >= 17 AND hr < 22 THEN 0.045
               WHEN hr >= 22 OR  hr < 6  THEN 0.005
               ELSE 0.010
           END::numeric * hdd_day AS heat_kwh
    FROM hdd h
), normed AS (
    SELECT s.*, SUM(w) OVER (PARTITION BY d) AS w_day FROM shaped s
)
INSERT INTO src_metering.interval_reads
    (icp_identifier, register_code, interval_start, kwh, read_status, version_no, source_file)
SELECT 'DEMO0000000801AA', 'UN', interval_start,
       ROUND(daily_kwh * w / w_day + heat_kwh, 4),
       'A', 1, 'sample-scenario-13_month_history.csv'
FROM normed;

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
VALUES (318001, 'DEMO_STD_FXTOU_13_MONTH_HISTORY', 'Demo Standard Fixed TOU',
        'AUNT', 'STANDARD', true, 0, '2024-01-01', NULL, true)
ON CONFLICT (code, valid_from) DO UPDATE SET
    name = EXCLUDED.name, network_code = EXCLUDED.network_code, active = EXCLUDED.active;

-- Gapless, all seven days. Network peak runs 30 minutes later than energy
-- peak, the per-charge-group band scoping the design doc calls out in §8.2.1.
INSERT INTO src_catalogue.tou_windows
    (window_id, plan_id, charge_scope, band_label, days_csv, time_from, time_to)
OVERRIDING SYSTEM VALUE
VALUES
    (328001,  318001, 'NET', 'PEAK',     '1,2,3,4,5,6,7', '07:00', '11:00'),
    (328002,  318001, 'NET', 'PEAK',     '1,2,3,4,5,6,7', '17:00', '21:30'),
    (328003,  318001, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '00:00', '07:00'),
    (328004,  318001, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '11:00', '17:00'),
    (328005,  318001, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '21:30', '24:00'),
    (328006,  318001, 'ENE', 'PEAK',     '1,2,3,4,5,6,7', '07:00', '11:00'),
    (328007,  318001, 'ENE', 'PEAK',     '1,2,3,4,5,6,7', '17:00', '21:00'),
    (328008,  318001, 'ENE', 'SHOULDER', '1,2,3,4,5,6,7', '11:00', '17:00'),
    (328009,  318001, 'ENE', 'SHOULDER', '1,2,3,4,5,6,7', '21:00', '23:00'),
    (328010, 318001, 'ENE', 'OFFPEAK',  '1,2,3,4,5,6,7', '00:00', '07:00'),
    (328011, 318001, 'ENE', 'OFFPEAK',  '1,2,3,4,5,6,7', '23:00', '24:00');

INSERT INTO src_catalogue.rate_components
    (component_id, plan_id, charge_scope, component_desc, rate_uom,
     window_id, flow_direction, unit_rate, valid_from)
OVERRIDING SYSTEM VALUE
VALUES
    (338001,  318001, 'NET', 'Peak Usage',           'KWH', 328001,  'import', 0.1361, '2024-01-01'),
    (338002,  318001, 'NET', 'Off Peak Usage',       'KWH', 328003,  'import', 0.0378, '2024-01-01'),
    (338003,  318001, 'NET', 'AUNT + Daily charges', 'DAY', NULL, 'import', 0.6, '2024-01-01'),
    (338004,  318001, 'NET', 'Transmission Charges', 'KWH', NULL,  'import', 0.0235, '2024-01-01'),
    (338005,  318001, 'ENE', 'Peak Usage',           'KWH', 328006,  'import', 0.26, '2024-01-01'),
    (338006,  318001, 'ENE', 'Off Peak Usage',       'KWH', 328010, 'import', 0.1, '2024-01-01'),
    (338007,  318001, 'ENE', 'Shoulder Usage',       'KWH', 328008,  'import', 0.14, '2024-01-01'),
    (338011, 318001, 'OTH', 'ELECTRICITY AUTHORITY Levy', 'KWH', NULL, 'import', 0.0013, '2024-01-01');

INSERT INTO src_billing.accounts
    (ACCT_ID, ACCT_NO, CUST_ID, ICP_NUMBER, PLAN_CODE, PAY_METHOD, BILL_CYCLE_DAY, START_DT)
OVERRIDING SYSTEM VALUE
VALUES (228001, 'DEMO-ACCT-228001', 218001, 'DEMO0000000801AA', 'DEMO_STD_FXTOU_13_MONTH_HISTORY', 'DD', 28, '2019-01-01')
ON CONFLICT (ACCT_NO) DO UPDATE SET
    CUST_ID = EXCLUDED.CUST_ID, ICP_NUMBER = EXCLUDED.ICP_NUMBER,
    PLAN_CODE = EXCLUDED.PLAN_CODE;

-- One invoice per calendar month, generated rather than listed. Issue date is
-- the day after the period ends; due date fourteen days after that.
INSERT INTO src_billing.invoices
    (INV_ID, INV_NO, ACCT_ID, PLAN_CODE, ISSUE_DT, PERIOD_FROM, PERIOD_TO,
     OPEN_BAL, PAYMENTS_APPL, BAL_BROUGHT_FWD, CHARGES_EXGST, GST_RATE,
     GST_AMT, TOTAL_AMT, DUE_DT, LATE_FEE, TOTAL_IF_LATE, PPD_AMT,
     TOTAL_IMPORT_KWH, TOTAL_EXPORT_KWH, EXPORT_CREDIT, INV_STATUS)
OVERRIDING SYSTEM VALUE
SELECT 238001 + n - 1,
       'INV-228001-' || to_char(m, 'YYYYMM'),
       228001, 'DEMO_STD_FXTOU_13_MONTH_HISTORY',
       (m + interval '1 month')::date,
       m,
       (m + interval '1 month' - interval '1 day')::date,
       0, 0, 0, 0, 0.00, 0.00, 0,
       (m + interval '1 month' + interval '14 days')::date,
       0, NULL, 0, 0, 0, 0, 'ISSUED'
FROM (SELECT gs::date AS m, ROW_NUMBER() OVER (ORDER BY gs) AS n
      FROM generate_series(date '2023-08-01', date '2024-08-01', interval '1 month') gs) months
ON CONFLICT (INV_NO) DO UPDATE SET
    ACCT_ID = EXCLUDED.ACCT_ID, PERIOD_FROM = EXCLUDED.PERIOD_FROM,
    PERIOD_TO = EXCLUDED.PERIOD_TO;

-- =====================================================================
-- SERVING REPLICA
-- =====================================================================

INSERT INTO metering.network (network_id, network_code, network_name, region)
OVERRIDING SYSTEM VALUE
VALUES (118001, 'AUNT', 'Aurora Energy', 'Otago')
ON CONFLICT (network_code) DO UPDATE SET network_name = EXCLUDED.network_name;

INSERT INTO billing.customer
    (customer_id, customer_number, full_name, email, is_medically_dependent, is_vulnerable)
OVERRIDING SYSTEM VALUE
VALUES (218001, '218001', 'Rangi Paterson', 'rangi@kea.example', false, false)
ON CONFLICT (customer_number) DO UPDATE SET full_name = EXCLUDED.full_name;

INSERT INTO metering.property (property_id, address_line_1, suburb, city, climate_region)
OVERRIDING SYSTEM VALUE
VALUES (128001, '27 Rattray Street', 'Mornington', 'Dunedin', 'Otago');

INSERT INTO metering.icp (icp_id, icp_number, property_id, network_id, connection_date)
OVERRIDING SYSTEM VALUE
VALUES (138001, 'DEMO0000000801AA', 128001, 118001, '2019-01-01')
ON CONFLICT (icp_number) DO UPDATE SET
    property_id = EXCLUDED.property_id, network_id = EXCLUDED.network_id;

INSERT INTO metering.meter_register
    (register_id, icp_id, register_code, description, is_controlled, direction)
OVERRIDING SYSTEM VALUE
VALUES (158001, 138001, 'UN', 'Grid import', false, 'import')
ON CONFLICT (register_id) DO UPDATE SET icp_id = EXCLUDED.icp_id;

-- Resolve the source feed into serving surrogate keys. read_status 'E' in the
-- source becomes quality 'estimated' here; that mapping is what lets the
-- explainer know how much of a period it can actually stand behind.
INSERT INTO metering.meter_read_hh
    (icp_id, register_id, interval_start, kwh, quality, source_file)
SELECT 138001, 158001, interval_start, kwh,
       CASE read_status WHEN 'E' THEN 'estimated'::common.read_quality
                        ELSE 'actual'::common.read_quality END,
       source_file
FROM src_metering.interval_reads
WHERE icp_identifier = 'DEMO0000000801AA';

INSERT INTO catalogue.plan
    (plan_id, plan_code, plan_name, network_id, user_type, is_tou,
     prompt_payment_discount_pct, effective_from, is_active)
OVERRIDING SYSTEM VALUE
VALUES (318001, 'DEMO_STD_FXTOU_13_MONTH_HISTORY', 'Demo Standard Fixed TOU', 118001, 'standard', true,
        0, '2024-01-01', true)
ON CONFLICT (plan_code, effective_from) DO UPDATE SET
    plan_name = EXCLUDED.plan_name, network_id = EXCLUDED.network_id;

INSERT INTO catalogue.tou_band
    (band_id, plan_id, charge_group, band_name, days_of_week, start_time, end_time)
OVERRIDING SYSTEM VALUE
VALUES
    (328001,  318001, 'network', 'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '07:00', '11:00'),
    (328002,  318001, 'network', 'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '17:00', '21:30'),
    (328003,  318001, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '00:00', '07:00'),
    (328004,  318001, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '11:00', '17:00'),
    (328005,  318001, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '21:30', '24:00'),
    (328006,  318001, 'energy',  'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '07:00', '11:00'),
    (328007,  318001, 'energy',  'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '17:00', '21:00'),
    (328008,  318001, 'energy',  'shoulder', ARRAY[1,2,3,4,5,6,7]::smallint[], '11:00', '17:00'),
    (328009,  318001, 'energy',  'shoulder', ARRAY[1,2,3,4,5,6,7]::smallint[], '21:00', '23:00'),
    (328010, 318001, 'energy',  'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '00:00', '07:00'),
    (328011, 318001, 'energy',  'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '23:00', '24:00');

INSERT INTO catalogue.tariff_component
    (component_id, plan_id, charge_group, component_name, basis, band_id, direction, rate, effective_from)
OVERRIDING SYSTEM VALUE
VALUES
    (338001,  318001, 'network', 'Peak Usage',     'per_kwh', 328001,  'import', 0.1361, '2024-01-01'),
    (338002,  318001, 'network', 'Off Peak Usage', 'per_kwh', 328003,  'import', 0.0378, '2024-01-01'),
    (338003,  318001, 'network', 'AUNT + Daily charges', 'per_day', NULL, 'import', 0.6, '2024-01-01'),
    (338004,  318001, 'network', 'Transmission Charges', 'per_kwh', NULL, 'import', 0.0235, '2024-01-01'),
    (338005,  318001, 'energy',  'Peak Usage',     'per_kwh', 328006,  'import', 0.26, '2024-01-01'),
    (338006,  318001, 'energy',  'Off Peak Usage', 'per_kwh', 328010, 'import', 0.1, '2024-01-01'),
    (338007,  318001, 'energy',  'Shoulder Usage', 'per_kwh', 328008,  'import', 0.14, '2024-01-01'),
    (338011, 318001, 'other',   'ELECTRICITY AUTHORITY Levy', 'per_kwh', NULL, 'import', 0.0013, '2024-01-01');

INSERT INTO billing.account
    (account_id, account_number, customer_id, icp_id, plan_id, payment_method,
     billing_cycle_day, start_date)
OVERRIDING SYSTEM VALUE
VALUES (228001, 'DEMO-ACCT-228001', 218001, 138001, 318001, 'direct_debit', 28, '2019-01-01')
ON CONFLICT (account_number) DO UPDATE SET
    customer_id = EXCLUDED.customer_id, icp_id = EXCLUDED.icp_id, plan_id = EXCLUDED.plan_id;

-- The same thirteen periods on the serving side. period_days is a GENERATED
-- column, so it is never inserted -- it is derived from the two dates and
-- cannot disagree with them.
INSERT INTO billing.bill
    (bill_id, account_id, invoice_number, plan_id, issue_date, period_start, period_end,
     opening_balance, payments_applied, balance_brought_fwd, charges_excl_gst,
     gst_rate, gst_amount, total_charges, due_date, late_payment_fee,
     total_if_paid_late, prompt_payment_discount, total_import_kwh,
     total_export_kwh, export_credit, status)
OVERRIDING SYSTEM VALUE
SELECT 238001 + n - 1,
       228001,
       'INV-228001-' || to_char(m, 'YYYYMM'),
       318001,
       (m + interval '1 month')::date,
       m,
       (m + interval '1 month' - interval '1 day')::date,
       0, 0, 0, 0, 0.00, 0.00, 0,
       (m + interval '1 month' + interval '14 days')::date,
       0, NULL, 0, 0, 0, 0, 'issued'
FROM (SELECT gs::date AS m, ROW_NUMBER() OVER (ORDER BY gs) AS n
      FROM generate_series(date '2023-08-01', date '2024-08-01', interval '1 month') gs) months
ON CONFLICT (invoice_number) DO UPDATE SET
    period_start = EXCLUDED.period_start, period_end = EXCLUDED.period_end;

-- ---------------------------------------------------------------------
-- Bill lines, RATED from the reads -- the same SQL the two-period fixtures
-- use, with the bill filter widened from two ids to the whole account. The
-- aggregation was already per-bill, so thirteen months need no new logic.
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
      ON r.icp_id = 138001
     AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date
         BETWEEN b.period_start AND b.period_end
    WHERE b.account_id = 228001
), totals AS (
    SELECT bill_id, days,
           ROUND(SUM(kwh), 2) AS total_kwh,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 7 AND hr < 11) OR (hr >= 17 AND hr < 21.5)), 2) AS net_peak,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 7 AND hr < 11) OR (hr >= 17 AND hr < 21)), 2)   AS ene_peak,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 11 AND hr < 17) OR (hr >= 21 AND hr < 23)), 2)  AS ene_shoulder
    FROM banded GROUP BY bill_id, days
), lines AS (
    SELECT bill_id, 338001 AS component_id, 'network'::common.charge_group AS cg, 'Peak Usage' AS label,
           'peak' AS band, net_peak AS qty, 'per_kwh'::common.rate_basis AS unit, 0.1361::numeric AS rate,
           NULL::numeric AS loss, ROUND(net_peak * 0.1361, 2) AS amount, 10 AS sort_order FROM totals
    UNION ALL SELECT bill_id, 338002, 'network', 'Off Peak Usage', 'off_peak',
           total_kwh - net_peak, 'per_kwh', 0.0378,
           NULL, ROUND((total_kwh - net_peak) * 0.0378, 2), 20 FROM totals
    UNION ALL SELECT bill_id, 338003, 'network', 'AUNT + Daily charges', NULL,
           days::numeric, 'per_day', 0.6,
           NULL, ROUND(days * 0.6, 2), 30 FROM totals
    UNION ALL SELECT bill_id, 338004, 'network', 'Transmission Charges', NULL,
           total_kwh, 'per_kwh', 0.0235,
           NULL, ROUND(total_kwh * 0.0235, 2), 40 FROM totals
    UNION ALL SELECT bill_id, 338005, 'energy', 'Peak Usage', 'peak',
           ene_peak, 'per_kwh', 0.26, ROUND(ene_peak * 0.0511, 2),
           ROUND((ene_peak + ROUND(ene_peak * 0.0511, 2)) * 0.26, 2), 50 FROM totals
    UNION ALL SELECT bill_id, 338007, 'energy', 'Shoulder Usage', 'shoulder',
           ene_shoulder, 'per_kwh', 0.14, ROUND(ene_shoulder * 0.0511, 2),
           ROUND((ene_shoulder + ROUND(ene_shoulder * 0.0511, 2)) * 0.14, 2), 60 FROM totals
    UNION ALL SELECT bill_id, 338006, 'energy', 'Off Peak Usage', 'off_peak',
           total_kwh - ene_peak - ene_shoulder, 'per_kwh', 0.1,
           ROUND((total_kwh - ene_peak - ene_shoulder) * 0.0511, 2),
           ROUND(((total_kwh - ene_peak - ene_shoulder)
                  + ROUND((total_kwh - ene_peak - ene_shoulder) * 0.0511, 2)) * 0.1, 2), 70 FROM totals
    UNION ALL SELECT bill_id, 338011, 'other', 'ELECTRICITY AUTHORITY Levy', NULL,
           total_kwh, 'per_kwh', 0.0013,
           NULL, ROUND(total_kwh * 0.0013, 2), 80 FROM totals
)
INSERT INTO billing.bill_line
    (bill_id, component_id, charge_group, direction, line_label, band_name,
     quantity, unit, rate, loss_kwh, amount, sort_order)
SELECT bill_id, component_id, cg, 'import', label, band, qty, unit, rate, loss, amount, sort_order
FROM lines;

INSERT INTO src_billing.invoice_lines
    (INV_ID, CHG_GROUP, FLOW_DIR, LINE_DESC, TOU_BAND, QTY, UOM, UNIT_RATE, LOSS_QTY, LINE_AMT, SORT_SEQ)
SELECT bill_id,
       CASE charge_group WHEN 'network' THEN 'NET' WHEN 'energy' THEN 'ENE' ELSE 'OTH' END,
       'IMPORT', line_label, UPPER(band_name), quantity,
       CASE unit WHEN 'per_day' THEN 'DAY' ELSE 'KWH' END,
       rate, loss_kwh, amount, sort_order
FROM billing.bill_line WHERE bill_id BETWEEN 238001 AND 238013;

UPDATE billing.bill AS b
SET charges_excl_gst = t.charges,
    gst_amount       = ROUND(t.charges * b.gst_rate, 2),
    total_charges    = ROUND(t.charges * (1 + b.gst_rate), 2),
    total_import_kwh = t.import_kwh
FROM (SELECT bill_id, ROUND(SUM(amount), 2) AS charges,
             SUM(quantity) FILTER (WHERE charge_group = 'energy' AND unit = 'per_kwh') AS import_kwh
      FROM billing.bill_line WHERE bill_id BETWEEN 238001 AND 238013
      GROUP BY bill_id) AS t
WHERE b.bill_id = t.bill_id;

UPDATE src_billing.invoices AS i
SET CHARGES_EXGST = t.charges,
    GST_AMT       = ROUND(t.charges * i.GST_RATE, 2),
    TOTAL_AMT     = ROUND(t.charges * (1 + i.GST_RATE), 2),
    TOTAL_IMPORT_KWH = t.import_kwh
FROM (SELECT INV_ID, ROUND(SUM(LINE_AMT), 2) AS charges,
             SUM(CASE WHEN CHG_GROUP = 'ENE' AND UOM = 'KWH' THEN QTY ELSE 0 END) AS import_kwh
      FROM src_billing.invoice_lines WHERE INV_ID BETWEEN 238001 AND 238013
      GROUP BY INV_ID) AS t
WHERE i.INV_ID = t.INV_ID;

-- =====================================================================
-- MARTS
-- =====================================================================

INSERT INTO kea.mart_account_context
    (account_id, account_number, customer_id, customer_number, customer_name,
     email, phone, icp_id, icp_number, address_display, climate_region, network_code,
     network_name, has_solar, has_controlled_load, plan_id, plan_code, plan_name,
     user_type, is_tou, daily_fixed_charge, payment_method, billing_cycle_day,
     current_period_start, current_period_end, days_into_period, estimated_read_pct,
     last_read_at, is_quarantined)
SELECT 228001, 'DEMO-ACCT-228001', 218001, '218001', 'Rangi Paterson', 'rangi@kea.example', '021 218 1001',
       138001, 'DEMO0000000801AA', '27 Rattray Street, Mornington', 'Otago', 'AUNT', 'Aurora Energy',
       false, false, 318001, 'DEMO_STD_FXTOU_13_MONTH_HISTORY', 'Demo Standard Fixed TOU', 'standard', true,
       0.6, 'direct_debit', 28,
       b.period_start, b.period_end, b.period_days, 0,
       (SELECT MAX(interval_start) FROM metering.meter_read_hh WHERE icp_id = 138001),
       false
FROM billing.bill b
WHERE b.bill_id = 238013
ON CONFLICT (account_id) DO UPDATE SET
    current_period_start = EXCLUDED.current_period_start,
    current_period_end   = EXCLUDED.current_period_end,
    days_into_period     = EXCLUDED.days_into_period,
    last_read_at         = EXCLUDED.last_read_at;

-- Daily usage, one row per day for all 397 days, built from a DATE SPINE
-- left-joined to the reads so a day the meter never reported still lands with
-- interval_count = 0 rather than vanishing.
--
-- expected_intervals is COMPUTED from the length of the LOCAL day, not fixed
-- at 48. Over thirteen months that matters twice: 24 September 2023 is a
-- 46-interval day and 7 April 2024 is a 50-interval day. Hardcoding 48 would
-- book two missing intervals against a complete September day and two
-- phantom extra reads in April, and mart_bill_explanation would then refuse
-- to call those bills explainable because it checks missing_interval_count.
INSERT INTO kea.mart_daily_usage
    (icp_id, usage_date, import_kwh, export_kwh, controlled_kwh, uncontrolled_kwh,
     base_load_kwh, peak_kwh, off_peak_kwh, interval_count, expected_intervals,
     estimated_count, temp_mean_c, heating_degree_days, is_weekend)
SELECT 138001,
       sp.d,
       COALESCE(ROUND(SUM(x.kwh), 4), 0), 0, 0, COALESCE(ROUND(SUM(x.kwh), 4), 0),
       ROUND(MIN(x.kwh) * 48, 4),
       COALESCE(ROUND(SUM(x.kwh) FILTER (WHERE (x.hr >= 7 AND x.hr < 11) OR (x.hr >= 17 AND x.hr < 21)), 4), 0),
       COALESCE(ROUND(SUM(x.kwh) FILTER (WHERE x.hr < 7 OR x.hr >= 23), 4), 0),
       COUNT(x.kwh)::smallint,
       (EXTRACT(epoch FROM ((sp.d + 1)::timestamp AT TIME ZONE 'Pacific/Auckland')
                          - (sp.d::timestamp AT TIME ZONE 'Pacific/Auckland')) / 1800)::smallint,
       COUNT(*) FILTER (WHERE x.quality = 'estimated')::smallint,
       ROUND((15.2 + 4.6 * COS(2 * PI() * (EXTRACT(doy FROM sp.d) - 15) / 365.25) + 2.2 * SIN(EXTRACT(day FROM sp.d) * 0.7))::numeric, 2),
       ROUND(GREATEST(16.0 - ROUND((15.2 + 4.6 * COS(2 * PI() * (EXTRACT(doy FROM sp.d) - 15) / 365.25) + 2.2 * SIN(EXTRACT(day FROM sp.d) * 0.7))::numeric, 2), 0), 2),
       EXTRACT(isodow FROM sp.d) >= 6
FROM (
    SELECT gs::date AS d
    FROM billing.bill b
    CROSS JOIN LATERAL generate_series(b.period_start, b.period_end, interval '1 day') gs
    WHERE b.account_id = 228001
) sp
LEFT JOIN (
    SELECT (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr,
           r.kwh, r.quality
    FROM metering.meter_read_hh r WHERE r.icp_id = 138001
) x ON x.d = sp.d
GROUP BY sp.d
ON CONFLICT (icp_id, usage_date) DO UPDATE SET
    import_kwh = EXCLUDED.import_kwh, base_load_kwh = EXCLUDED.base_load_kwh,
    peak_kwh = EXCLUDED.peak_kwh, off_peak_kwh = EXCLUDED.off_peak_kwh,
    interval_count = EXCLUDED.interval_count,
    expected_intervals = EXCLUDED.expected_intervals,
    temp_mean_c = EXCLUDED.temp_mean_c, heating_degree_days = EXCLUDED.heating_degree_days;

-- Hourly profile per calendar month: 13 months of it, which is what
-- makes a year-on-year read possible. August appears twice -- profile_month
-- 2023-08-01 and 2024-08-01 -- so the same hour can be compared against the
-- same hour a year earlier rather than against last month.
INSERT INTO kea.mart_hourly_profile
    (icp_id, profile_month, hour_of_day, is_weekday, avg_import_kwh, avg_export_kwh, sample_days)
SELECT 138001,
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
    FROM metering.meter_read_hh r WHERE r.icp_id = 138001
) y
GROUP BY 2, 3, 4
ON CONFLICT (icp_id, profile_month, hour_of_day, is_weekday) DO UPDATE SET
    avg_import_kwh = EXCLUDED.avg_import_kwh, sample_days = EXCLUDED.sample_days;

-- ---------------------------------------------------------------------
-- Bill explanation, every month against the one before it.
--
-- Same attribution as the two-period fixtures, but sequenced with LAG over
-- the thirteen bills instead of a join between two known ids. Nothing else
-- changes: the drivers are still measured in the schema's order and converted
-- to dollars at the PRIOR period's own average variable rate.
--
-- Because the household term is constant here, delta_from_weather should
-- dominate in both directions -- positive going into winter, negative coming
-- out of it. A fixture where the explained driver is negative half the time is
-- worth having: an explainer that only ever handles increases will show it.
-- ---------------------------------------------------------------------
WITH per_bill AS (
    SELECT b.bill_id, b.period_start, b.period_end, b.period_days,
           b.charges_excl_gst AS amount, b.total_import_kwh AS kwh,
           (SELECT ROUND(AVG(base_load_kwh), 4) FROM kea.mart_daily_usage u
             WHERE u.icp_id = 138001 AND u.usage_date BETWEEN b.period_start AND b.period_end) AS base_day,
           (SELECT ROUND(SUM(heating_degree_days), 2) FROM kea.mart_daily_usage u
             WHERE u.icp_id = 138001 AND u.usage_date BETWEEN b.period_start AND b.period_end) AS hdd,
           (SELECT ROUND(AVG(temp_mean_c), 2) FROM kea.mart_daily_usage u
             WHERE u.icp_id = 138001 AND u.usage_date BETWEEN b.period_start AND b.period_end) AS temp,
           (SELECT COALESCE(SUM(estimated_count), 0) FROM kea.mart_daily_usage u
             WHERE u.icp_id = 138001 AND u.usage_date BETWEEN b.period_start AND b.period_end) AS est_intervals,
           (SELECT COALESCE(SUM(expected_intervals - interval_count), 0) FROM kea.mart_daily_usage u
             WHERE u.icp_id = 138001 AND u.usage_date BETWEEN b.period_start AND b.period_end) AS missing_intervals,
           (SELECT COALESCE(SUM(expected_intervals), 0) FROM kea.mart_daily_usage u
             WHERE u.icp_id = 138001 AND u.usage_date BETWEEN b.period_start AND b.period_end) AS expected_intervals
    FROM billing.bill b WHERE b.account_id = 228001
), seq AS (
    SELECT p.*,
           LAG(p.bill_id)      OVER w AS prior_id,
           LAG(p.period_days)  OVER w AS prior_days,
           LAG(p.amount)       OVER w AS prior_amount,
           LAG(p.kwh)          OVER w AS prior_kwh,
           LAG(p.base_day)     OVER w AS prior_base,
           LAG(p.hdd)          OVER w AS prior_hdd,
           LAG(p.temp)         OVER w AS prior_temp
    FROM per_bill p
    WINDOW w AS (ORDER BY p.period_start)
), rated AS (
    SELECT s.*,
           ROUND((s.prior_amount - 0.6 * s.prior_days) / NULLIF(s.prior_kwh, 0), 6) AS rate_per_kwh
    FROM seq s WHERE s.prior_id IS NOT NULL
), attributed AS (
    SELECT r.*,
           ROUND(r.amount - r.prior_amount, 2) AS d_amount,
           ROUND((r.period_days - r.prior_days)
                 * (r.prior_kwh / NULLIF(r.prior_days, 0) * r.rate_per_kwh + 0.6), 2) AS d_period,
           ROUND(0.93 * (r.hdd - r.prior_hdd) * r.rate_per_kwh, 2) AS d_weather,
           ROUND((r.base_day - r.prior_base) * r.period_days * r.rate_per_kwh, 2) AS d_base
    FROM rated r
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
SELECT bill_id, 228001, 138001, period_start, period_end, period_days,
       amount, kwh, prior_id, prior_days, prior_amount, prior_kwh,
       d_amount,
       ROUND(d_amount / NULLIF(prior_amount, 0) * 100, 2),
       ROUND(kwh - prior_kwh, 4),
       d_period, d_weather, d_base, 0.00,
       ROUND(d_amount - d_period - d_weather - d_base, 2),
       hdd, prior_hdd, temp, prior_temp, base_day, prior_base, 0.93,
       ROUND(est_intervals * 100.0 / NULLIF(expected_intervals, 0), 2),
       missing_intervals,
       (ABS(d_period + d_weather + d_base) >= ABS(d_amount) * 0.60
        AND est_intervals = 0 AND missing_intervals = 0)
FROM attributed;

-- The earliest bill has nothing before it, so no driver can be attributed.
INSERT INTO kea.mart_bill_explanation
    (bill_id, account_id, icp_id, period_start, period_end, period_days,
     total_amount, total_kwh, estimated_read_pct, missing_interval_count, is_explainable)
SELECT bill_id, 228001, 138001, period_start, period_end, period_days,
       charges_excl_gst, COALESCE(total_import_kwh, 0), 0, 0, false
FROM billing.bill WHERE bill_id = 238001;

INSERT INTO kea.mart_load_log
    (mart_name, started_at, completed_at, row_count, warehouse_run_id, status)
VALUES ('sample_scenario_13_month_history', now(), now(), 13, 'sample-scenario-13_month_history', 'succeeded');

-- =====================================================================
-- SELF-CHECK — re-derives the fixture and fails the transaction on drift
-- =====================================================================
DO $$
DECLARE
    v_span integer; v_days integer; v_rows integer; v_gaps integer;
    v_bills integer; v_months integer; v_bad integer;
    v_short integer; v_long integer;
    v_winter numeric; v_summer numeric;
    v_first numeric; v_last numeric; v_kwh numeric;
BEGIN
    -- 1. THE POINT OF THIS FILE: more than a year of meter data.
    SELECT (MAX(interval_start AT TIME ZONE 'Pacific/Auckland')::date
            - MIN(interval_start AT TIME ZONE 'Pacific/Auckland')::date) + 1
      INTO v_span
      FROM metering.meter_read_hh WHERE icp_id = 138001;
    IF v_span < 366 THEN
        RAISE EXCEPTION 'reads span only % days; this fixture exists to carry more than a year', v_span;
    END IF;
    IF v_span <> 397 THEN
        RAISE EXCEPTION 'reads span % days, expected 397', v_span;
    END IF;

    -- 2. thirteen bills, each header equal to the sum of its own lines
    SELECT COUNT(*) INTO v_bills FROM billing.bill WHERE account_id = 228001;
    IF v_bills <> 13 THEN
        RAISE EXCEPTION 'expected 13 bills, found %', v_bills;
    END IF;
    SELECT COUNT(*) INTO v_bad
      FROM billing.bill b
      JOIN (SELECT bill_id, ROUND(SUM(amount), 2) AS s FROM billing.bill_line
             GROUP BY bill_id) l ON l.bill_id = b.bill_id
     WHERE b.account_id = 228001 AND l.s <> b.charges_excl_gst;
    IF v_bad > 0 THEN
        RAISE EXCEPTION '% bill headers disagree with their lines', v_bad;
    END IF;

    -- 3. one daily row per day, and no holes in the middle
    SELECT COUNT(*), (MAX(usage_date) - MIN(usage_date)) + 1
      INTO v_rows, v_days
      FROM kea.mart_daily_usage WHERE icp_id = 138001;
    IF v_rows <> 397 OR v_days <> v_rows THEN
        RAISE EXCEPTION 'mart_daily_usage has % rows spanning % days, expected 397 of each',
                        v_rows, v_days;
    END IF;
    SELECT COUNT(*) INTO v_gaps
      FROM kea.mart_daily_usage
     WHERE icp_id = 138001 AND interval_count <> expected_intervals;
    IF v_gaps > 0 THEN
        RAISE EXCEPTION '% days where the reads do not fill the local day', v_gaps;
    END IF;

    -- 4. thirteen calendar months in the hourly profile, so the same month
    --    appears twice and a year-on-year comparison is possible
    SELECT COUNT(DISTINCT profile_month) INTO v_months
      FROM kea.mart_hourly_profile WHERE icp_id = 138001;
    IF v_months <> 13 THEN
        RAISE EXCEPTION 'hourly profile covers % months, expected 13', v_months;
    END IF;

    -- 5. both DST transitions are inside the window, so the 46- and
    --    50-interval local days the schema warns about are actually present.
    --    If this ever fails, the fixture has stopped exercising them.
    SELECT COUNT(*) FILTER (WHERE expected_intervals = 46),
           COUNT(*) FILTER (WHERE expected_intervals = 50)
      INTO v_short, v_long
      FROM kea.mart_daily_usage WHERE icp_id = 138001;
    IF v_short < 1 OR v_long < 1 THEN
        RAISE EXCEPTION 'expected at least one 46-interval and one 50-interval day, found % and %',
                        v_short, v_long;
    END IF;

    -- 6. the seasonal swing must be real, or a year of reads demonstrates
    --    nothing a single month could not
    SELECT ROUND(AVG(import_kwh), 2) INTO v_winter
      FROM kea.mart_daily_usage
     WHERE icp_id = 138001 AND usage_date BETWEEN '2024-07-01' AND '2024-07-31';
    SELECT ROUND(AVG(import_kwh), 2) INTO v_summer
      FROM kea.mart_daily_usage
     WHERE icp_id = 138001 AND usage_date BETWEEN '2024-01-01' AND '2024-01-31';
    IF v_winter < v_summer * 1.25 THEN
        RAISE EXCEPTION 'winter % kWh/day is not meaningfully above summer % kWh/day',
                        v_winter, v_summer;
    END IF;

    -- 7. on every bill that has a predecessor, the drivers must add up
    SELECT COUNT(*) INTO v_bad
      FROM kea.mart_bill_explanation
     WHERE account_id = 228001 AND prior_bill_id IS NOT NULL
       AND ROUND(delta_from_period_length + delta_from_weather + delta_from_base_load
                 + delta_from_tariff + delta_unexplained, 2) <> amount_delta;
    IF v_bad > 0 THEN
        RAISE EXCEPTION 'drivers fail to sum to the delta on % bills', v_bad;
    END IF;

    SELECT charges_excl_gst INTO v_first FROM billing.bill WHERE bill_id = 238001;
    SELECT charges_excl_gst INTO v_last  FROM billing.bill WHERE bill_id = 238013;
    SELECT ROUND(SUM(kwh), 0) INTO v_kwh FROM metering.meter_read_hh WHERE icp_id = 138001;

    RAISE NOTICE
        '13_month_history: % days of reads (% intervals, % kWh) across % bills; winter % vs summer % kWh/day; % first bill $%, last $%',
        v_span,
        (SELECT COUNT(*) FROM metering.meter_read_hh WHERE icp_id = 138001),
        v_kwh, v_bills, v_winter, v_summer,
        v_short || ' short/' || v_long || ' long DST days;', v_first, v_last;
END $$;

COMMIT;

-- Verification queries:
-- SELECT period_start, period_end, period_days, charges_excl_gst, total_import_kwh
--   FROM billing.bill WHERE account_id = 228001 ORDER BY period_start;
-- -- the same month, a year apart
-- SELECT profile_month, hour_of_day, avg_import_kwh
--   FROM kea.mart_hourly_profile
--  WHERE icp_id = 138001 AND is_weekday
--    AND profile_month IN (date '2023-08-01', date '2024-08-01')
--  ORDER BY hour_of_day, profile_month;
-- -- the seasonal curve
-- SELECT date_trunc('month', usage_date)::date AS month,
--        ROUND(AVG(import_kwh), 2) AS kwh_per_day,
--        ROUND(AVG(temp_mean_c), 1) AS temp_c
--   FROM kea.mart_daily_usage WHERE icp_id = 138001
--  GROUP BY 1 ORDER BY 1;
-- -- the two days that are not 48 intervals long
-- SELECT usage_date, interval_count, expected_intervals
--   FROM kea.mart_daily_usage
--  WHERE icp_id = 138001 AND expected_intervals <> 48;
