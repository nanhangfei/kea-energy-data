-- =====================================================================
-- Kea Energy — mid-cycle forecast seed
-- MID-CYCLE FORECAST — a bill shock caught before the bill arrives
-- =====================================================================
--
-- Ten days into a thirty-day winter period, tracking about 28 percent above the same period last year. The bill has not been issued and the customer has not asked yet.
--
-- Synthetic fixture. No real customer, address, ICP or meter appears here;
-- every id carries the table it belongs to in its first two digits and this
-- fixture's number (9) in its third -- see "Fixture ids" in data/README.md.
-- The half-hourly reads are generated deterministically by the CTE below
-- rather than copied from a feed.
--
-- WHY THIS FIXTURE EXISTS
-- kea.mart_forecast_input and kea.bill_forecast had no sample data. Every
-- other file in data/sample seeds bills that have already been issued, which
-- exercises the explainer (Outcome 2) and leaves the forecast (Outcome 3)
-- with nothing behind it. This file is the unfinished-period case.
--
-- THE SHAPE OF IT
--   2023-06-01 .. 2023-06-30   completed bill — the same period LAST YEAR,
--                                the comparison anchor
--   2024-04-01 .. 2024-04-30   completed bill — the most recent MILD month,
--                                where the blended variable rate and the
--                                weather-stripped baseline come from
--   2024-06-01 .. 2024-06-30   IN FLIGHT. Reads stop at 2024-06-10; there is
--                                no bill, because the period has not ended
--
-- Reads deliberately stop at the as-at date. A mid-cycle fixture carrying
-- reads for days that have not happened would be seeding the future, and
-- every "days_elapsed" figure derived from it would be a lie.
--
-- WHAT IT DEMONSTRATES
--   * 10 daily snapshots of the input, one per elapsed day, INSERTed rather
--     than overwritten — as_at_date is in the key, so nothing is lost
--   * hdd_to_date rising while hdd_expected_remaining falls: fact displacing
--     forecast, day by day
--   * the confidence band narrowing along that same curve, because the
--     uncertainty only ever applies to the part that has not happened
--   * baseline_daily_kwh recovered by STRIPPING weather out of history, not
--     by averaging it — the self-check asserts the stripped figure comes back
--     to the 9.20 kWh/day the reads were actually generated from
--   * fixed_charges_period pulled out before the band is applied, which is
--     what keeps "$3 either side of certain" from being priced as uncertain
--
-- WHAT THE EXPLAINER MAY SAY
-- The cause here is weather, which is the one driver the pipeline can measure
-- directly. It still may not name an appliance: data/README.md is explicit
-- that half-hourly interval data cannot identify one, and calls the refusal
-- deliberate. "This June is running about five degrees colder than last June"
-- is measured. "Your heat pump is running more" is a guess.
--
-- NO mart_bill_explanation ROWS. The two completed bills are ten months
-- apart, so a period-over-period decomposition between them would be
-- meaningless. The explainer mart is exercised by the six bill_higher_*
-- fixtures; this one is about the forecast.
--
-- Note 2024-04-07: the day daylight saving ends. That local day is 50 half
-- hours long, not 48, and expected_intervals says so.
--
-- Re-runnable: every write is either scoped-DELETE-then-INSERT or
-- ON CONFLICT DO UPDATE, so running this file twice leaves the same rows.
-- The DO block at the end re-derives the forecast from the seeded data and
-- raises if anything disagrees.
--
-- Run after kea_source_schema.sql and kea_serving_schema.sql.
-- Independent of every other file in data/sample: its own household, its own
-- network, its own plan.

BEGIN;

SET search_path TO public;

-- ---------------------------------------------------------------------
-- Idempotency: remove only this fixture, children before parents.
-- ---------------------------------------------------------------------
DELETE FROM src_metering.interval_reads WHERE icp_identifier = 'DEMO0000000901AA';
DELETE FROM metering.meter_read_hh      WHERE icp_id = 139001;
DELETE FROM src_billing.invoice_lines   WHERE INV_ID IN (239001, 239002);
DELETE FROM src_billing.invoices        WHERE INV_ID IN (239001, 239002);
DELETE FROM src_billing.accounts        WHERE ACCT_ID = 229001;
DELETE FROM src_billing.customers       WHERE CUST_ID = 219001;
DELETE FROM src_catalogue.rate_components WHERE component_id BETWEEN 339001 AND 339011;
DELETE FROM src_catalogue.tou_windows     WHERE window_id    BETWEEN 329001 AND 329011;
DELETE FROM src_catalogue.price_plans     WHERE plan_id = 319001;
DELETE FROM src_metering.registers        WHERE register_id = 159001;
DELETE FROM src_metering.meters           WHERE meter_id = 149001;
DELETE FROM src_metering.connection_points WHERE icp_id = 139001;
DELETE FROM src_metering.networks         WHERE network_id = 119001;

-- bill_forecast keys on a GENERATED IDENTITY, so it is cleared by account
-- rather than upserted by id.
DELETE FROM kea.bill_forecast         WHERE account_id = 229001;
DELETE FROM kea.mart_forecast_input   WHERE account_id = 229001;
DELETE FROM kea.mart_hourly_profile   WHERE icp_id = 139001;
DELETE FROM kea.mart_daily_usage      WHERE icp_id = 139001;
DELETE FROM kea.mart_account_context  WHERE account_id = 229001;
DELETE FROM kea.mart_load_log         WHERE warehouse_run_id = 'sample-scenario-mid_cycle_forecast';
DELETE FROM billing.bill_line WHERE bill_id IN (239001, 239002);
DELETE FROM billing.bill      WHERE bill_id IN (239001, 239002);
DELETE FROM billing.account   WHERE account_id = 229001;
DELETE FROM catalogue.tariff_component WHERE component_id BETWEEN 339001 AND 339011;
DELETE FROM catalogue.tou_band         WHERE band_id      BETWEEN 329001 AND 329011;
DELETE FROM catalogue.plan             WHERE plan_id = 319001;
DELETE FROM metering.meter_register WHERE register_id = 159001;
DELETE FROM metering.icp            WHERE icp_id = 139001;
DELETE FROM metering.property       WHERE property_id = 129001;
DELETE FROM metering.network        WHERE network_id = 119001;
DELETE FROM billing.customer        WHERE customer_id = 219001;

-- =====================================================================
-- SOURCE SYSTEMS
-- =====================================================================

INSERT INTO src_billing.customers
    (CUST_ID, CUST_NO, CUST_NAME, EMAIL_ADDR, PHONE_NO,
     ADDR_LINE1, ADDR_SUBURB, ADDR_CITY, ADDR_POSTCODE)
OVERRIDING SYSTEM VALUE
VALUES (219001, '219001', 'Mereana Tipene', 'mereana@kea.example', '021 219 1001',
        '8 Waimea Road', 'Hope', 'Richmond', NULL)
ON CONFLICT (CUST_NO) DO UPDATE SET
    CUST_NAME = EXCLUDED.CUST_NAME,
    ADDR_LINE1 = EXCLUDED.ADDR_LINE1,
    ADDR_SUBURB = EXCLUDED.ADDR_SUBURB,
    ADDR_CITY = EXCLUDED.ADDR_CITY;

INSERT INTO src_metering.networks (network_id, network_code, network_name, region)
OVERRIDING SYSTEM VALUE
VALUES (119001, 'NTSM', 'Network Tasman', 'Tasman')
ON CONFLICT (network_code) DO UPDATE SET
    network_name = EXCLUDED.network_name, region = EXCLUDED.region;

INSERT INTO src_metering.connection_points
    (icp_id, icp_identifier, network_id, address_full, suburb, city,
     postcode, climate_region, connection_date)
OVERRIDING SYSTEM VALUE
VALUES (139001, 'DEMO0000000901AA', 119001, '8 Waimea Road, Hope',
        'Hope', 'Richmond', NULL, 'Tasman', '2019-01-01')
ON CONFLICT (icp_identifier) DO UPDATE SET
    network_id = EXCLUDED.network_id,
    address_full = EXCLUDED.address_full,
    climate_region = EXCLUDED.climate_region;

INSERT INTO src_metering.meters (meter_id, icp_id, serial_number, is_smart, installed_on)
OVERRIDING SYSTEM VALUE
VALUES (149001, 139001, 'DEMO-METER-149001', true, '2019-01-01')
ON CONFLICT (meter_id) DO UPDATE SET
    icp_id = EXCLUDED.icp_id, serial_number = EXCLUDED.serial_number;

INSERT INTO src_metering.registers
    (register_id, meter_id, register_code, description, is_controlled, flow_direction)
OVERRIDING SYSTEM VALUE
VALUES (159001, 149001, 'UN', 'Grid import', false, 'import')
ON CONFLICT (register_id) DO UPDATE SET
    meter_id = EXCLUDED.meter_id, register_code = EXCLUDED.register_code;

-- ---------------------------------------------------------------------
-- Half-hourly reads for three windows: the two completed periods and the
-- elapsed part of the period in flight.
--
-- kwh = daily_total * shape(hh) / sum(shape over day)      the household
--     + heating(hh) * heating_degree_days(day)             weather response
--
-- There is no scenario_extra term. Nothing about this household changes
-- across the three windows -- daily_total is 9.20 throughout -- so the
-- entire rise in the period in flight is weather, and the forecast has a
-- single legible cause. That also makes the baseline recoverable exactly:
-- strip 0.93 kWh per degree day back out of any historical day and
-- 9.20 is what must remain.
-- ---------------------------------------------------------------------
WITH half_hours AS (
    -- Anchored to NZ LOCAL midnight via AT TIME ZONE rather than a fixed +12
    -- literal: +12 is NZST, and April is partly NZDT (+13).
    SELECT generate_series(
               (date '2023-06-01')::timestamp AT TIME ZONE 'Pacific/Auckland',
               (date '2023-06-30' + 1)::timestamp AT TIME ZONE 'Pacific/Auckland'
                   - interval '30 minutes',
               interval '30 minutes') AS interval_start
    UNION ALL
    SELECT generate_series(
               (date '2024-04-01')::timestamp AT TIME ZONE 'Pacific/Auckland',
               (date '2024-04-30' + 1)::timestamp AT TIME ZONE 'Pacific/Auckland'
                   - interval '30 minutes',
               interval '30 minutes')
    UNION ALL
    -- stops at the as-at date, not at period end
    SELECT generate_series(
               (date '2024-06-01')::timestamp AT TIME ZONE 'Pacific/Auckland',
               (date '2024-06-10' + 1)::timestamp AT TIME ZONE 'Pacific/Auckland'
                   - interval '30 minutes',
               interval '30 minutes')
), tagged AS (
    SELECT interval_start,
           (interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour   FROM interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr
    FROM half_hours
), weathered AS (
    SELECT t.*, ROUND(((CASE WHEN d BETWEEN date '2023-06-01' AND date '2023-06-30' THEN 11.2 WHEN d BETWEEN date '2024-04-01' AND date '2024-04-30' THEN 15.4 ELSE 6.4 END)::numeric + 2.2 * SIN(EXTRACT(day FROM d) * 0.7))::numeric, 2) AS temp_c FROM tagged t
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
           9.20::numeric AS daily_kwh,
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
SELECT 'DEMO0000000901AA', 'UN', interval_start,
       ROUND(daily_kwh * w / w_day + heat_kwh, 4),
       'A', 1, 'sample-scenario-mid_cycle_forecast.csv'
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
VALUES (319001, 'DEMO_STD_FXTOU_MID_CYCLE_FORECAST', 'Demo Standard Fixed TOU',
        'NTSM', 'STANDARD', true, 0, '2024-01-01', NULL, true)
ON CONFLICT (code, valid_from) DO UPDATE SET
    name = EXCLUDED.name, network_code = EXCLUDED.network_code, active = EXCLUDED.active;

-- Gapless, all seven days. Network peak runs 30 minutes later than energy
-- peak, the per-charge-group band scoping the design doc calls out in §8.2.1.
INSERT INTO src_catalogue.tou_windows
    (window_id, plan_id, charge_scope, band_label, days_csv, time_from, time_to)
OVERRIDING SYSTEM VALUE
VALUES
    (329001,  319001, 'NET', 'PEAK',     '1,2,3,4,5,6,7', '07:00', '11:00'),
    (329002,  319001, 'NET', 'PEAK',     '1,2,3,4,5,6,7', '17:00', '21:30'),
    (329003,  319001, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '00:00', '07:00'),
    (329004,  319001, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '11:00', '17:00'),
    (329005,  319001, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '21:30', '24:00'),
    (329006,  319001, 'ENE', 'PEAK',     '1,2,3,4,5,6,7', '07:00', '11:00'),
    (329007,  319001, 'ENE', 'PEAK',     '1,2,3,4,5,6,7', '17:00', '21:00'),
    (329008,  319001, 'ENE', 'SHOULDER', '1,2,3,4,5,6,7', '11:00', '17:00'),
    (329009,  319001, 'ENE', 'SHOULDER', '1,2,3,4,5,6,7', '21:00', '23:00'),
    (329010, 319001, 'ENE', 'OFFPEAK',  '1,2,3,4,5,6,7', '00:00', '07:00'),
    (329011, 319001, 'ENE', 'OFFPEAK',  '1,2,3,4,5,6,7', '23:00', '24:00');

INSERT INTO src_catalogue.rate_components
    (component_id, plan_id, charge_scope, component_desc, rate_uom,
     window_id, flow_direction, unit_rate, valid_from)
OVERRIDING SYSTEM VALUE
VALUES
    (339001,  319001, 'NET', 'Peak Usage',           'KWH', 329001,  'import', 0.1361, '2024-01-01'),
    (339002,  319001, 'NET', 'Off Peak Usage',       'KWH', 329003,  'import', 0.0378, '2024-01-01'),
    (339003,  319001, 'NET', 'NTSM + Daily charges', 'DAY', NULL, 'import', 0.6, '2024-01-01'),
    (339004,  319001, 'NET', 'Transmission Charges', 'KWH', NULL,  'import', 0.0235, '2024-01-01'),
    (339005,  319001, 'ENE', 'Peak Usage',           'KWH', 329006,  'import', 0.26, '2024-01-01'),
    (339006,  319001, 'ENE', 'Off Peak Usage',       'KWH', 329010, 'import', 0.1, '2024-01-01'),
    (339007,  319001, 'ENE', 'Shoulder Usage',       'KWH', 329008,  'import', 0.14, '2024-01-01'),
    (339011, 319001, 'OTH', 'ELECTRICITY AUTHORITY Levy', 'KWH', NULL, 'import', 0.0013, '2024-01-01');

INSERT INTO src_billing.accounts
    (ACCT_ID, ACCT_NO, CUST_ID, ICP_NUMBER, PLAN_CODE, PAY_METHOD, BILL_CYCLE_DAY, START_DT)
OVERRIDING SYSTEM VALUE
VALUES (229001, 'DEMO-ACCT-229001', 219001, 'DEMO0000000901AA', 'DEMO_STD_FXTOU_MID_CYCLE_FORECAST', 'DD', 28, '2019-01-01')
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
    (239001, 'DEMO-INV-239001', 229001, 'DEMO_STD_FXTOU_MID_CYCLE_FORECAST', '2023-07-01', '2023-06-01', '2023-06-30',
     0, 0, 0, 0, 0.00, 0.00, 0, '2023-07-15', 0, NULL, 0, 0, 0, 0, 'ISSUED'),
    (239002, 'DEMO-INV-239002', 229001, 'DEMO_STD_FXTOU_MID_CYCLE_FORECAST', '2024-05-01', '2024-04-01', '2024-04-30',
     0, 0, 0, 0, 0.00, 0.00, 0, '2024-05-15', 0, NULL, 0, 0, 0, 0, 'ISSUED')
ON CONFLICT (INV_NO) DO UPDATE SET
    ACCT_ID = EXCLUDED.ACCT_ID, PERIOD_FROM = EXCLUDED.PERIOD_FROM,
    PERIOD_TO = EXCLUDED.PERIOD_TO;

-- =====================================================================
-- SERVING REPLICA
-- =====================================================================

INSERT INTO metering.network (network_id, network_code, network_name, region)
OVERRIDING SYSTEM VALUE
VALUES (119001, 'NTSM', 'Network Tasman', 'Tasman')
ON CONFLICT (network_code) DO UPDATE SET network_name = EXCLUDED.network_name;

INSERT INTO billing.customer
    (customer_id, customer_number, full_name, email, is_medically_dependent, is_vulnerable)
OVERRIDING SYSTEM VALUE
VALUES (219001, '219001', 'Mereana Tipene', 'mereana@kea.example', false, false)
ON CONFLICT (customer_number) DO UPDATE SET full_name = EXCLUDED.full_name;

INSERT INTO metering.property (property_id, address_line_1, suburb, city, climate_region)
OVERRIDING SYSTEM VALUE
VALUES (129001, '8 Waimea Road', 'Hope', 'Richmond', 'Tasman');

INSERT INTO metering.icp (icp_id, icp_number, property_id, network_id, connection_date)
OVERRIDING SYSTEM VALUE
VALUES (139001, 'DEMO0000000901AA', 129001, 119001, '2019-01-01')
ON CONFLICT (icp_number) DO UPDATE SET
    property_id = EXCLUDED.property_id, network_id = EXCLUDED.network_id;

INSERT INTO metering.meter_register
    (register_id, icp_id, register_code, description, is_controlled, direction)
OVERRIDING SYSTEM VALUE
VALUES (159001, 139001, 'UN', 'Grid import', false, 'import')
ON CONFLICT (register_id) DO UPDATE SET icp_id = EXCLUDED.icp_id;

-- Resolve the source feed into serving surrogate keys. read_status 'E' in the
-- source becomes quality 'estimated' here; that mapping is what lets the
-- explainer know how much of a period it can actually stand behind.
INSERT INTO metering.meter_read_hh
    (icp_id, register_id, interval_start, kwh, quality, source_file)
SELECT 139001, 159001, interval_start, kwh,
       CASE read_status WHEN 'E' THEN 'estimated'::common.read_quality
                        ELSE 'actual'::common.read_quality END,
       source_file
FROM src_metering.interval_reads
WHERE icp_identifier = 'DEMO0000000901AA';

INSERT INTO catalogue.plan
    (plan_id, plan_code, plan_name, network_id, user_type, is_tou,
     prompt_payment_discount_pct, effective_from, is_active)
OVERRIDING SYSTEM VALUE
VALUES (319001, 'DEMO_STD_FXTOU_MID_CYCLE_FORECAST', 'Demo Standard Fixed TOU', 119001, 'standard', true,
        0, '2024-01-01', true)
ON CONFLICT (plan_code, effective_from) DO UPDATE SET
    plan_name = EXCLUDED.plan_name, network_id = EXCLUDED.network_id;

INSERT INTO catalogue.tou_band
    (band_id, plan_id, charge_group, band_name, days_of_week, start_time, end_time)
OVERRIDING SYSTEM VALUE
VALUES
    (329001,  319001, 'network', 'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '07:00', '11:00'),
    (329002,  319001, 'network', 'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '17:00', '21:30'),
    (329003,  319001, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '00:00', '07:00'),
    (329004,  319001, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '11:00', '17:00'),
    (329005,  319001, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '21:30', '24:00'),
    (329006,  319001, 'energy',  'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '07:00', '11:00'),
    (329007,  319001, 'energy',  'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '17:00', '21:00'),
    (329008,  319001, 'energy',  'shoulder', ARRAY[1,2,3,4,5,6,7]::smallint[], '11:00', '17:00'),
    (329009,  319001, 'energy',  'shoulder', ARRAY[1,2,3,4,5,6,7]::smallint[], '21:00', '23:00'),
    (329010, 319001, 'energy',  'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '00:00', '07:00'),
    (329011, 319001, 'energy',  'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '23:00', '24:00');

INSERT INTO catalogue.tariff_component
    (component_id, plan_id, charge_group, component_name, basis, band_id, direction, rate, effective_from)
OVERRIDING SYSTEM VALUE
VALUES
    (339001,  319001, 'network', 'Peak Usage',     'per_kwh', 329001,  'import', 0.1361, '2024-01-01'),
    (339002,  319001, 'network', 'Off Peak Usage', 'per_kwh', 329003,  'import', 0.0378, '2024-01-01'),
    (339003,  319001, 'network', 'NTSM + Daily charges', 'per_day', NULL, 'import', 0.6, '2024-01-01'),
    (339004,  319001, 'network', 'Transmission Charges', 'per_kwh', NULL, 'import', 0.0235, '2024-01-01'),
    (339005,  319001, 'energy',  'Peak Usage',     'per_kwh', 329006,  'import', 0.26, '2024-01-01'),
    (339006,  319001, 'energy',  'Off Peak Usage', 'per_kwh', 329010, 'import', 0.1, '2024-01-01'),
    (339007,  319001, 'energy',  'Shoulder Usage', 'per_kwh', 329008,  'import', 0.14, '2024-01-01'),
    (339011, 319001, 'other',   'ELECTRICITY AUTHORITY Levy', 'per_kwh', NULL, 'import', 0.0013, '2024-01-01');

INSERT INTO billing.account
    (account_id, account_number, customer_id, icp_id, plan_id, payment_method,
     billing_cycle_day, start_date)
OVERRIDING SYSTEM VALUE
VALUES (229001, 'DEMO-ACCT-229001', 219001, 139001, 319001, 'direct_debit', 28, '2019-01-01')
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
    (239001, 229001, 'DEMO-INV-239001', 319001, '2023-07-01', '2023-06-01', '2023-06-30',
     0, 0, 0, 0, 0.00, 0.00, 0, '2023-07-15', 0, NULL, 0, 0, 0, 0, 'issued'),
    (239002, 229001, 'DEMO-INV-239002', 319001, '2024-05-01', '2024-04-01', '2024-04-30',
     0, 0, 0, 0, 0.00, 0.00, 0, '2024-05-15', 0, NULL, 0, 0, 0, 0, 'issued')
ON CONFLICT (invoice_number) DO UPDATE SET
    period_start = EXCLUDED.period_start, period_end = EXCLUDED.period_end;

-- ---------------------------------------------------------------------
-- Bill lines, RATED from the reads. Identical machinery to the bill_higher_*
-- fixtures, so the blended variable rate this forecast leans on is the same
-- rate those files charge.
-- ---------------------------------------------------------------------
WITH banded AS (
    SELECT b.bill_id, b.period_start, b.period_end,
           (b.period_end - b.period_start) + 1 AS days,
           r.kwh,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr
    FROM billing.bill b
    JOIN metering.meter_read_hh r
      ON r.icp_id = 139001
     AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date
         BETWEEN b.period_start AND b.period_end
    WHERE b.bill_id IN (239001, 239002)
), totals AS (
    SELECT bill_id, days,
           ROUND(SUM(kwh), 2) AS total_kwh,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 7 AND hr < 11) OR (hr >= 17 AND hr < 21.5)), 2) AS net_peak,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 7 AND hr < 11) OR (hr >= 17 AND hr < 21)), 2)   AS ene_peak,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 11 AND hr < 17) OR (hr >= 21 AND hr < 23)), 2)  AS ene_shoulder
    FROM banded GROUP BY bill_id, days
), lines AS (
    SELECT bill_id, 339001 AS component_id, 'network'::common.charge_group AS cg, 'Peak Usage' AS label,
           'peak' AS band, net_peak AS qty, 'per_kwh'::common.rate_basis AS unit, 0.1361::numeric AS rate,
           NULL::numeric AS loss, ROUND(net_peak * 0.1361, 2) AS amount, 10 AS sort_order FROM totals
    UNION ALL SELECT bill_id, 339002, 'network', 'Off Peak Usage', 'off_peak',
           total_kwh - net_peak, 'per_kwh', 0.0378,
           NULL, ROUND((total_kwh - net_peak) * 0.0378, 2), 20 FROM totals
    UNION ALL SELECT bill_id, 339003, 'network', 'NTSM + Daily charges', NULL,
           days::numeric, 'per_day', 0.6,
           NULL, ROUND(days * 0.6, 2), 30 FROM totals
    UNION ALL SELECT bill_id, 339004, 'network', 'Transmission Charges', NULL,
           total_kwh, 'per_kwh', 0.0235,
           NULL, ROUND(total_kwh * 0.0235, 2), 40 FROM totals
    UNION ALL SELECT bill_id, 339005, 'energy', 'Peak Usage', 'peak',
           ene_peak, 'per_kwh', 0.26, ROUND(ene_peak * 0.0511, 2),
           ROUND((ene_peak + ROUND(ene_peak * 0.0511, 2)) * 0.26, 2), 50 FROM totals
    UNION ALL SELECT bill_id, 339007, 'energy', 'Shoulder Usage', 'shoulder',
           ene_shoulder, 'per_kwh', 0.14, ROUND(ene_shoulder * 0.0511, 2),
           ROUND((ene_shoulder + ROUND(ene_shoulder * 0.0511, 2)) * 0.14, 2), 60 FROM totals
    UNION ALL SELECT bill_id, 339006, 'energy', 'Off Peak Usage', 'off_peak',
           total_kwh - ene_peak - ene_shoulder, 'per_kwh', 0.1,
           ROUND((total_kwh - ene_peak - ene_shoulder) * 0.0511, 2),
           ROUND(((total_kwh - ene_peak - ene_shoulder)
                  + ROUND((total_kwh - ene_peak - ene_shoulder) * 0.0511, 2)) * 0.1, 2), 70 FROM totals
    UNION ALL SELECT bill_id, 339011, 'other', 'ELECTRICITY AUTHORITY Levy', NULL,
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
FROM billing.bill_line WHERE bill_id IN (239001, 239002);

UPDATE billing.bill AS b
SET charges_excl_gst = t.charges,
    gst_amount       = ROUND(t.charges * b.gst_rate, 2),
    total_charges    = ROUND(t.charges * (1 + b.gst_rate), 2),
    total_import_kwh = t.import_kwh
FROM (SELECT bill_id, ROUND(SUM(amount), 2) AS charges,
             SUM(quantity) FILTER (WHERE charge_group = 'energy' AND unit = 'per_kwh') AS import_kwh
      FROM billing.bill_line WHERE bill_id IN (239001, 239002) GROUP BY bill_id) AS t
WHERE b.bill_id = t.bill_id;

UPDATE src_billing.invoices AS i
SET CHARGES_EXGST = t.charges,
    GST_AMT       = ROUND(t.charges * i.GST_RATE, 2),
    TOTAL_AMT     = ROUND(t.charges * (1 + i.GST_RATE), 2),
    TOTAL_IMPORT_KWH = t.import_kwh
FROM (SELECT INV_ID, ROUND(SUM(LINE_AMT), 2) AS charges,
             SUM(CASE WHEN CHG_GROUP = 'ENE' AND UOM = 'KWH' THEN QTY ELSE 0 END) AS import_kwh
      FROM src_billing.invoice_lines WHERE INV_ID IN (239001, 239002) GROUP BY INV_ID) AS t
WHERE i.INV_ID = t.INV_ID;

-- =====================================================================
-- MARTS
-- =====================================================================

-- days_into_period is 10, not 30: the period is in flight, and this is
-- the column the agent checks before it says anything about "this bill".
INSERT INTO kea.mart_account_context
    (account_id, account_number, customer_id, customer_number, customer_name,
     email, phone, icp_id, icp_number, address_display, climate_region, network_code,
     network_name, has_solar, has_controlled_load, plan_id, plan_code, plan_name,
     user_type, is_tou, daily_fixed_charge, payment_method, billing_cycle_day,
     current_period_start, current_period_end, days_into_period, estimated_read_pct,
     last_read_at, is_quarantined)
VALUES
    (229001, 'DEMO-ACCT-229001', 219001, '219001', 'Mereana Tipene', 'mereana@kea.example', '021 219 1001',
     139001, 'DEMO0000000901AA', '8 Waimea Road, Hope', 'Tasman', 'NTSM', 'Network Tasman',
     false, false, 319001, 'DEMO_STD_FXTOU_MID_CYCLE_FORECAST', 'Demo Standard Fixed TOU', 'standard', true,
     0.6, 'direct_debit', 28, '2024-06-01', '2024-06-30', 10,
     0, timestamptz '2024-06-10 23:30:00+12', false)
ON CONFLICT (account_id) DO UPDATE SET
    current_period_start = EXCLUDED.current_period_start,
    current_period_end   = EXCLUDED.current_period_end,
    days_into_period     = EXCLUDED.days_into_period,
    last_read_at         = EXCLUDED.last_read_at;

-- Daily usage over all three windows. The spine covers the two billed periods
-- plus the ELAPSED days of the period in flight -- days 2024-06-11 to 2024-06-30
-- get no row, because the meter has not reported them and an empty row would
-- read as a gap rather than as the future.
INSERT INTO kea.mart_daily_usage
    (icp_id, usage_date, import_kwh, export_kwh, controlled_kwh, uncontrolled_kwh,
     base_load_kwh, peak_kwh, off_peak_kwh, interval_count, expected_intervals,
     estimated_count, temp_mean_c, heating_degree_days, is_weekend)
SELECT 139001,
       sp.d,
       COALESCE(ROUND(SUM(x.kwh), 4), 0), 0, 0, COALESCE(ROUND(SUM(x.kwh), 4), 0),
       ROUND(MIN(x.kwh) * 48, 4),
       COALESCE(ROUND(SUM(x.kwh) FILTER (WHERE (x.hr >= 7 AND x.hr < 11) OR (x.hr >= 17 AND x.hr < 21)), 4), 0),
       COALESCE(ROUND(SUM(x.kwh) FILTER (WHERE x.hr < 7 OR x.hr >= 23), 4), 0),
       COUNT(x.kwh)::smallint,
       (EXTRACT(epoch FROM ((sp.d + 1)::timestamp AT TIME ZONE 'Pacific/Auckland')
                          - (sp.d::timestamp AT TIME ZONE 'Pacific/Auckland')) / 1800)::smallint,
       COUNT(*) FILTER (WHERE x.quality = 'estimated')::smallint,
       ROUND(((CASE WHEN sp.d BETWEEN date '2023-06-01' AND date '2023-06-30' THEN 11.2 WHEN sp.d BETWEEN date '2024-04-01' AND date '2024-04-30' THEN 15.4 ELSE 6.4 END)::numeric + 2.2 * SIN(EXTRACT(day FROM sp.d) * 0.7))::numeric, 2),
       ROUND(GREATEST(16.0 - ROUND(((CASE WHEN sp.d BETWEEN date '2023-06-01' AND date '2023-06-30' THEN 11.2 WHEN sp.d BETWEEN date '2024-04-01' AND date '2024-04-30' THEN 15.4 ELSE 6.4 END)::numeric + 2.2 * SIN(EXTRACT(day FROM sp.d) * 0.7))::numeric, 2), 0), 2),
       EXTRACT(isodow FROM sp.d) >= 6
FROM (
    SELECT gs::date AS d
    FROM billing.bill b
    CROSS JOIN LATERAL generate_series(b.period_start, b.period_end, interval '1 day') gs
    WHERE b.bill_id IN (239001, 239002)
    UNION ALL
    SELECT gs::date
    FROM generate_series(date '2024-06-01', date '2024-06-10', interval '1 day') gs
) sp
LEFT JOIN (
    SELECT (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr,
           r.kwh, r.quality
    FROM metering.meter_read_hh r WHERE r.icp_id = 139001
) x ON x.d = sp.d
GROUP BY sp.d
ON CONFLICT (icp_id, usage_date) DO UPDATE SET
    import_kwh = EXCLUDED.import_kwh, base_load_kwh = EXCLUDED.base_load_kwh,
    peak_kwh = EXCLUDED.peak_kwh, off_peak_kwh = EXCLUDED.off_peak_kwh,
    interval_count = EXCLUDED.interval_count, estimated_count = EXCLUDED.estimated_count,
    expected_intervals = EXCLUDED.expected_intervals,
    temp_mean_c = EXCLUDED.temp_mean_c, heating_degree_days = EXCLUDED.heating_degree_days;

INSERT INTO kea.mart_hourly_profile
    (icp_id, profile_month, hour_of_day, is_weekday, avg_import_kwh, avg_export_kwh, sample_days)
SELECT 139001,
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
    FROM metering.meter_read_hh r WHERE r.icp_id = 139001
) y
GROUP BY 2, 3, 4
ON CONFLICT (icp_id, profile_month, hour_of_day, is_weekday) DO UPDATE SET
    avg_import_kwh = EXCLUDED.avg_import_kwh, sample_days = EXCLUDED.sample_days;

-- ---------------------------------------------------------------------
-- FORECAST INPUT — one row per elapsed day, the nightly pipeline's output.
--
-- Every figure is DERIVED from what is already seeded, so retuning the reads
-- carries straight through:
--
--   kwh_to_date          summed from mart_daily_usage up to as_at_date
--   hdd_to_date          likewise -- FACT, the meter and the weather station
--   hdd_expected_remaining  the same temperature model run over the days that
--                        have NOT happened. In production this is the NIWA
--                        forecast, and it carries a forecast's error; that
--                        error is most of why bill_forecast stores a band
--
-- The weather forecast here is DELIBERATELY PERFECT -- the remaining days are
-- run through the same formula the reads were built from. Two consequences a
-- reader should not mistake for a claim about real forecasts:
--
--   * hdd_to_date + hdd_expected_remaining is exactly constant across the
--     snapshots, which is a strong invariant to test against and is not
--     something a real NIWA feed would give you
--   * projected_kwh barely moves, because the model IS the process that
--     generated the data. In production the point estimate wanders and
--     converges; here only the BAND narrows
--
-- That is the trade: one cause of narrowing instead of two, so a fixture that
-- stops narrowing has exactly one thing wrong with it.
--   baseline_daily_kwh   history with weather STRIPPED OUT, not averaged.
--                        Averaging a winter month and then adding the heating
--                        term back counts weather twice
--   fixed_charges_period daily rate x the WHOLE period, known on day one
--   cost_to_date         fixed accrued so far + kWh so far at the blended rate
-- ---------------------------------------------------------------------
WITH asat AS (
    SELECT gs::date AS as_at
    FROM generate_series(date '2024-06-01', date '2024-06-10', interval '1 day') gs
), rate AS (
    SELECT ROUND((b.charges_excl_gst - 0.6 * b.period_days)
                 / NULLIF(b.total_import_kwh, 0), 6) AS rate_per_kwh
    FROM billing.bill b WHERE b.bill_id = 239002
), baseline AS (
    SELECT ROUND(AVG(u.import_kwh - u.heating_degree_days * 0.93), 4) AS baseline_daily_kwh
    FROM kea.mart_daily_usage u
    WHERE u.icp_id = 139001
      AND (u.usage_date BETWEEN date '2023-06-01' AND date '2023-06-30'
        OR u.usage_date BETWEEN date '2024-04-01' AND date '2024-04-30')
), lastyear AS (
    SELECT b.total_import_kwh AS kwh, b.charges_excl_gst AS amount
    FROM billing.bill b WHERE b.bill_id = 239001
), todate AS (
    SELECT a.as_at,
           (a.as_at - date '2024-06-01' + 1)::smallint AS days_elapsed,
           ROUND(SUM(u.import_kwh), 4) AS kwh_to_date,
           ROUND(SUM(u.heating_degree_days), 2) AS hdd_to_date
    FROM asat a
    JOIN kea.mart_daily_usage u
      ON u.icp_id = 139001
     AND u.usage_date BETWEEN date '2024-06-01' AND a.as_at
    GROUP BY a.as_at
), future AS (
    SELECT a.as_at,
           ROUND(COALESCE(SUM(GREATEST(16.0 - ROUND(((CASE WHEN g.d BETWEEN date '2023-06-01' AND date '2023-06-30' THEN 11.2 WHEN g.d BETWEEN date '2024-04-01' AND date '2024-04-30' THEN 15.4 ELSE 6.4 END)::numeric + 2.2 * SIN(EXTRACT(day FROM g.d) * 0.7))::numeric, 2), 0)), 0), 2) AS hdd_expected_remaining
    FROM asat a
    LEFT JOIN LATERAL generate_series(a.as_at + 1, date '2024-06-30', interval '1 day') AS g(d)
           ON true
    GROUP BY a.as_at
)
INSERT INTO kea.mart_forecast_input
    (account_id, period_start, as_at_date, days_elapsed, days_total,
     kwh_to_date, cost_to_date, baseline_daily_kwh,
     same_period_last_year_kwh, same_period_last_year_amount,
     hdd_to_date, hdd_expected_remaining, heating_sensitivity_kwh_per_hdd,
     fixed_charges_period, is_forecastable)
SELECT 229001, date '2024-06-01', t.as_at,
       t.days_elapsed, 30,
       t.kwh_to_date,
       ROUND(0.6 * t.days_elapsed + t.kwh_to_date * r.rate_per_kwh, 2),
       bl.baseline_daily_kwh,
       ly.kwh, ly.amount,
       t.hdd_to_date, f.hdd_expected_remaining, 0.93,
       ROUND(0.6 * 30, 2),
       -- NOT NULL with no default, so it has to be stated. True for this fixture:
       -- the whole point of it is a period still in flight with enough history behind
       -- it to project from.
       true
FROM todate t
JOIN future f ON f.as_at = t.as_at
CROSS JOIN rate r
CROSS JOIN baseline bl
CROSS JOIN lastyear ly
ON CONFLICT (account_id, period_start, as_at_date) DO UPDATE SET
    days_elapsed = EXCLUDED.days_elapsed,
    kwh_to_date = EXCLUDED.kwh_to_date, cost_to_date = EXCLUDED.cost_to_date,
    baseline_daily_kwh = EXCLUDED.baseline_daily_kwh,
    hdd_to_date = EXCLUDED.hdd_to_date,
    hdd_expected_remaining = EXCLUDED.hdd_expected_remaining,
    fixed_charges_period = EXCLUDED.fixed_charges_period,
    is_forecastable = EXCLUDED.is_forecastable;

-- ---------------------------------------------------------------------
-- FORECAST OUTPUT — the model's row, one per input row.
--
-- Written by a DIFFERENT job in production (kea_forecast_refresh, not
-- kea_daily_pipeline), which is why it is a separate table. It reads nothing
-- but mart_forecast_input.
--
--   rate           re-derived from cost_to_date and fixed_charges_period,
--                  no tariff join -- the self-check asserts it comes back to
--                  the rate the April bill was charged at
--   projected_kwh  what has happened + remaining baseline + remaining weather,
--                  NOT kwh_to_date / days_elapsed x days_total. Straight
--                  extrapolation assumes the rest of the month is as cold as
--                  the part already measured
--   the band       15 percent, applied ONLY to the kWh that has not
--                  happened. The metered kWh is fact and the daily charge was
--                  fixed on day one; pricing either as uncertain would widen
--                  the band by an amount that is pure fiction
-- ---------------------------------------------------------------------
WITH i AS (
    SELECT * FROM kea.mart_forecast_input
     WHERE account_id = 229001 AND period_start = date '2024-06-01'
), step1 AS (
    SELECT i.*,
           ROUND(i.fixed_charges_period * i.days_elapsed / i.days_total, 2) AS fixed_so_far,
           ROUND((i.days_total - i.days_elapsed) * i.baseline_daily_kwh, 4) AS rem_base_kwh,
           ROUND(i.hdd_expected_remaining * i.heating_sensitivity_kwh_per_hdd, 4) AS rem_heat_kwh
    FROM i
), step2 AS (
    SELECT s.*,
           ROUND((s.cost_to_date - s.fixed_so_far) / NULLIF(s.kwh_to_date, 0), 6) AS rate_per_kwh
    FROM step1 s
), step3 AS (
    SELECT s.*,
           ROUND(s.kwh_to_date + s.rem_base_kwh + s.rem_heat_kwh, 4) AS projected_kwh,
           ROUND((s.rem_base_kwh + s.rem_heat_kwh) * s.rate_per_kwh, 2) AS rem_cost
    FROM step2 s
), step4 AS (
    SELECT s.*,
           ROUND(s.fixed_charges_period + s.projected_kwh * s.rate_per_kwh, 2) AS projected_amount,
           ROUND(s.rem_cost * 0.15, 2) AS half_band
    FROM step3 s
)
INSERT INTO kea.bill_forecast
    (account_id, period_start, period_end, forecast_date, days_elapsed,
     kwh_to_date, projected_kwh, projected_amount, projected_low, projected_high,
     baseline_amount, variance_pct, is_anomalous, model_version)
SELECT 229001, date '2024-06-01', date '2024-06-30', as_at_date, days_elapsed,
       kwh_to_date, projected_kwh, projected_amount,
       projected_amount - half_band,
       projected_amount + half_band,
       same_period_last_year_amount,
       ROUND((projected_amount - same_period_last_year_amount)
             / NULLIF(same_period_last_year_amount, 0) * 100, 2),
       ABS(ROUND((projected_amount - same_period_last_year_amount)
                 / NULLIF(same_period_last_year_amount, 0) * 100, 2)) >= 15,
       'hdd-linear-v2'
FROM step4
ON CONFLICT (account_id, period_start, forecast_date) DO UPDATE SET
    projected_kwh = EXCLUDED.projected_kwh,
    projected_amount = EXCLUDED.projected_amount,
    projected_low = EXCLUDED.projected_low,
    projected_high = EXCLUDED.projected_high,
    variance_pct = EXCLUDED.variance_pct,
    is_anomalous = EXCLUDED.is_anomalous,
    model_version = EXCLUDED.model_version;

INSERT INTO kea.mart_load_log
    (mart_name, started_at, completed_at, row_count, warehouse_run_id, status)
VALUES ('sample_scenario_mid_cycle_forecast', now(), now(), 10, 'sample-scenario-mid_cycle_forecast', 'succeeded');

-- =====================================================================
-- SELF-CHECK — re-derives the fixture and fails the transaction on drift
-- =====================================================================
DO $$
DECLARE
    v_lines numeric; v_ly numeric; v_ap_rate numeric;
    v_base numeric; v_n integer; v_bad integer;
    v_span_min numeric; v_span_max numeric;
    v_w_first numeric; v_w_last numeric;
    v_amt numeric; v_var numeric; v_anom boolean;
    v_kwh numeric; v_true_kwh numeric;
BEGIN
    -- 1. both bills must equal the sum of their own lines
    SELECT COUNT(*) INTO v_bad
      FROM billing.bill b
      JOIN (SELECT bill_id, ROUND(SUM(amount), 2) AS s FROM billing.bill_line
             WHERE bill_id IN (239001, 239002) GROUP BY bill_id) l
        ON l.bill_id = b.bill_id
     WHERE b.charges_excl_gst <> l.s;
    IF v_bad > 0 THEN
        RAISE EXCEPTION '% bill(s) do not match their own lines', v_bad;
    END IF;

    -- 2. the weather-stripped baseline must recover the kWh/day the reads
    --    were generated from. This is the assertion that would catch someone
    --    "simplifying" baseline_daily_kwh into a plain historical average.
    SELECT DISTINCT baseline_daily_kwh INTO v_base
      FROM kea.mart_forecast_input WHERE account_id = 229001;
    IF ABS(v_base - 9.20) > 0.05 THEN
        RAISE EXCEPTION
            'weather-stripped baseline came back as % but the reads were built from 9.20',
            v_base;
    END IF;

    -- 3. one snapshot per elapsed day, and one forecast per snapshot
    SELECT COUNT(*) INTO v_n FROM kea.mart_forecast_input WHERE account_id = 229001;
    IF v_n <> 10 THEN
        RAISE EXCEPTION 'expected 10 forecast input rows, found %', v_n;
    END IF;
    SELECT COUNT(*) INTO v_bad
      FROM kea.mart_forecast_input i
      FULL JOIN kea.bill_forecast f
        ON f.account_id = i.account_id AND f.period_start = i.period_start
       AND f.forecast_date = i.as_at_date
     WHERE i.account_id IS NULL OR f.account_id IS NULL;
    IF v_bad > 0 THEN
        -- No FK enforces this pairing: mart_forecast_input is rebuildable and
        -- must not be able to cascade into forecast history. The join is the
        -- job's responsibility, so the fixture checks it.
        RAISE EXCEPTION '% forecast row(s) have no matching input row, or vice versa', v_bad;
    END IF;

    -- 4. fact and forecast must PARTITION the period: hdd_to_date +
    --    hdd_expected_remaining is the same total on every snapshot
    SELECT MIN(hdd_to_date + hdd_expected_remaining),
           MAX(hdd_to_date + hdd_expected_remaining)
      INTO v_span_min, v_span_max
      FROM kea.mart_forecast_input WHERE account_id = 229001;
    IF v_span_max - v_span_min > 0.05 THEN
        RAISE EXCEPTION
            'hdd_to_date + hdd_expected_remaining drifts from % to % across the snapshots',
            v_span_min, v_span_max;
    END IF;

    -- 5. the re-derived rate must come back to what the April bill charged
    SELECT ROUND((charges_excl_gst - 0.6 * period_days) / total_import_kwh, 4)
      INTO v_ap_rate FROM billing.bill WHERE bill_id = 239002;
    SELECT COUNT(*) INTO v_bad
      FROM kea.mart_forecast_input
     WHERE account_id = 229001
       AND ABS(ROUND((cost_to_date - ROUND(fixed_charges_period * days_elapsed / days_total, 2))
                     / kwh_to_date, 4) - v_ap_rate) > 0.001;
    IF v_bad > 0 THEN
        RAISE EXCEPTION
            '% snapshot(s) re-derive a variable rate that is not the billed rate of %',
            v_bad, v_ap_rate;
    END IF;

    -- 6. the band must NARROW as the period fills in
    SELECT projected_high - projected_low INTO v_w_first
      FROM kea.bill_forecast WHERE account_id = 229001 ORDER BY forecast_date LIMIT 1;
    SELECT projected_high - projected_low INTO v_w_last
      FROM kea.bill_forecast WHERE account_id = 229001 ORDER BY forecast_date DESC LIMIT 1;
    IF v_w_last >= v_w_first THEN
        RAISE EXCEPTION
            'the confidence band did not narrow: % wide on day 1, % wide on day 10',
            v_w_first, v_w_last;
    END IF;
    SELECT COUNT(*) INTO v_bad FROM (
        SELECT projected_high - projected_low AS w,
               LAG(projected_high - projected_low) OVER (ORDER BY forecast_date) AS prev
          FROM kea.bill_forecast WHERE account_id = 229001) z
     WHERE prev IS NOT NULL AND w > prev;
    IF v_bad > 0 THEN
        RAISE EXCEPTION 'the band widened on % day(s) instead of narrowing', v_bad;
    END IF;

    -- 7. the latest forecast must actually be the bill shock this fixture claims
    SELECT projected_amount, variance_pct, is_anomalous, projected_kwh
      INTO v_amt, v_var, v_anom, v_kwh
      FROM kea.bill_forecast WHERE account_id = 229001
     ORDER BY forecast_date DESC LIMIT 1;
    IF NOT v_anom OR v_var < 15 THEN
        RAISE EXCEPTION
            'expected an anomalous forecast above 15 percent, got % percent (anomalous %)',
            v_var, v_anom;
    END IF;

    -- 8. and it must be close to what the whole period will actually come to,
    --    computed straight from the generative model over all 30 days
    SELECT ROUND(9.20 * 30
                 + 0.93 * SUM(GREATEST(16.0 - ROUND(((CASE WHEN v.d BETWEEN date '2023-06-01' AND date '2023-06-30' THEN 11.2 WHEN v.d BETWEEN date '2024-04-01' AND date '2024-04-30' THEN 15.4 ELSE 6.4 END)::numeric + 2.2 * SIN(EXTRACT(day FROM v.d) * 0.7))::numeric, 2), 0)), 4)
      INTO v_true_kwh
      FROM generate_series(date '2024-06-01', date '2024-06-30', interval '1 day') AS v(d);
    IF ABS(v_kwh - v_true_kwh) / v_true_kwh > 0.02 THEN
        RAISE EXCEPTION
            'day-10 projection of % kWh is more than 2 percent off the period total of % kWh',
            v_kwh, v_true_kwh;
    END IF;

    SELECT charges_excl_gst INTO v_ly FROM billing.bill WHERE bill_id = 239001;
    RAISE NOTICE
        'mid_cycle_forecast: last June $%, projected $% (% percent, anomalous %); band $% wide on day 1, $% on day 10; baseline % kWh/day',
        v_ly, v_amt, v_var, v_anom, v_w_first, v_w_last, v_base;
END $$;

COMMIT;

-- Verification queries:
-- -- the snapshot series: fact displacing forecast, day by day
-- SELECT as_at_date, days_elapsed, kwh_to_date, hdd_to_date, hdd_expected_remaining
--   FROM kea.mart_forecast_input WHERE account_id = 229001 ORDER BY as_at_date;
-- -- and the band narrowing along it
-- SELECT forecast_date, projected_kwh, projected_low, projected_amount, projected_high,
--        projected_high - projected_low AS band, variance_pct, is_anomalous
--   FROM kea.bill_forecast WHERE account_id = 229001 ORDER BY forecast_date;
-- -- what the agent would actually read
-- SELECT projected_low, projected_amount, projected_high, baseline_amount, variance_pct
--   FROM kea.bill_forecast WHERE account_id = 229001
--  ORDER BY forecast_date DESC LIMIT 1;
