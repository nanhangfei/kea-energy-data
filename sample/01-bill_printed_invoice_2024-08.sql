-- Kea Energy sample bill seed
-- Source: the two supplied bill images.
--
-- Values read from the image:
--   ICP: 0165841877LCC54
--   Address: 1 Alpha Road, Deltaville
--   Billing period: 2024-08-08 through 2024-08-31 (24 days)
--   Plan: NZSZ_ZERO_STD_FXTOU_240901_T1
--   Network total: $29.22
--   Energy total: $21.10
--   Other total: $0.22
--   Total charges: $50.54
--
-- Customer identity/address comes from image 1: customer number 196501,
-- Julia Fox, 1 Alpha Road, Deltaville, Deltaville 1234. Bill-line details,
-- tariff, ICP, period, and usage come from image 2. The account number,
-- meter serial, coordinates, and half-hourly reads are not printed; account
-- and meter values are explicit DEMO values, while half-hourly reads are
-- deterministic synthetic values for testing.
--
-- Run after both schema files have been applied to the same PostgreSQL database.

BEGIN;

SET search_path TO public;

-- Remove only this demo fixture so the script can be run repeatedly.
DELETE FROM src_metering.interval_reads
WHERE icp_identifier = '0165841877LCC54'
    AND source_file = 'sample-synthetic-2024-08.csv';
DELETE FROM metering.meter_read_hh
WHERE icp_id = 131001
    AND source_file = 'sample-synthetic-2024-08.csv';
DELETE FROM src_billing.invoice_lines WHERE INV_ID = 231001;
DELETE FROM src_billing.invoices WHERE INV_ID = 231001;
DELETE FROM src_billing.account_plan_history WHERE ACCT_ID = 221001;
DELETE FROM src_billing.accounts WHERE ACCT_ID = 221001;
DELETE FROM src_billing.customers WHERE CUST_ID = 211001;
DELETE FROM src_catalogue.rate_components WHERE component_id BETWEEN 331001 AND 331011;
DELETE FROM src_catalogue.tou_windows WHERE window_id BETWEEN 321001 AND 321011;
DELETE FROM src_catalogue.price_plans WHERE plan_id = 311001;
DELETE FROM src_metering.registers WHERE register_id BETWEEN 151001 AND 151002;
DELETE FROM src_metering.meters WHERE meter_id = 141001;
DELETE FROM src_metering.connection_points WHERE icp_id = 131001;
DELETE FROM src_metering.networks WHERE network_id = 111001;
DELETE FROM billing.bill_line WHERE bill_id = 231001;
DELETE FROM billing.bill WHERE bill_id = 231001;
DELETE FROM kea.bill_forecast WHERE account_id = 221001;
DELETE FROM kea.mart_bill_explanation WHERE bill_id = 231001;
DELETE FROM kea.mart_hourly_profile WHERE icp_id = 131001;
DELETE FROM kea.mart_daily_usage WHERE icp_id = 131001;
DELETE FROM kea.mart_account_context WHERE account_id = 221001;
DELETE FROM kea.mart_load_log WHERE warehouse_run_id = 'sample-image-2026-08-21';
DELETE FROM billing.account WHERE account_id = 221001;
DELETE FROM catalogue.tariff_component WHERE component_id BETWEEN 331001 AND 331011;
DELETE FROM catalogue.tou_band WHERE band_id BETWEEN 321001 AND 321011;
DELETE FROM catalogue.plan WHERE plan_id = 311001;
DELETE FROM metering.meter_register WHERE register_id BETWEEN 151001 AND 151002;
DELETE FROM metering.icp WHERE icp_id = 131001;
DELETE FROM metering.property WHERE property_id = 121001;
DELETE FROM metering.network WHERE network_id = 111001;
DELETE FROM billing.customer WHERE customer_id = 211001;

-- =====================================================================
-- SOURCE SYSTEMS
-- =====================================================================

INSERT INTO src_billing.customers
    (CUST_ID, CUST_NO, CUST_NAME, EMAIL_ADDR, PHONE_NO,
     ADDR_LINE1, ADDR_SUBURB, ADDR_CITY, ADDR_POSTCODE)
OVERRIDING SYSTEM VALUE
VALUES
        (211001, '196501', 'Julia Fox',
         'julia@kea.example', '021 196 1501',
        '1 Alpha Road', 'Deltaville', 'Deltaville', '1234')
ON CONFLICT (CUST_NO) DO UPDATE SET
    CUST_NAME = EXCLUDED.CUST_NAME,
    EMAIL_ADDR = EXCLUDED.EMAIL_ADDR,
    PHONE_NO = EXCLUDED.PHONE_NO,
    ADDR_LINE1 = EXCLUDED.ADDR_LINE1,
    ADDR_SUBURB = EXCLUDED.ADDR_SUBURB,
    ADDR_CITY = EXCLUDED.ADDR_CITY;

INSERT INTO src_metering.networks
    (network_id, network_code, network_name, region)
OVERRIDING SYSTEM VALUE
VALUES
    (111001, 'VECT', 'Vector', 'Auckland')
ON CONFLICT (network_code) DO UPDATE SET
    network_name = EXCLUDED.network_name,
    region = EXCLUDED.region;

INSERT INTO src_metering.connection_points
    (icp_id, icp_identifier, network_id, address_full, suburb, city,
     postcode, climate_region, connection_date)
OVERRIDING SYSTEM VALUE
VALUES
    (131001, '0165841877LCC54', 111001, '1 Alpha Road, Deltaville',
     'Deltaville', 'Auckland', NULL, 'Auckland', '2020-01-01')
ON CONFLICT (icp_identifier) DO UPDATE SET
    network_id = EXCLUDED.network_id,
    address_full = EXCLUDED.address_full,
    suburb = EXCLUDED.suburb,
    city = EXCLUDED.city,
    climate_region = EXCLUDED.climate_region;

INSERT INTO src_metering.meters
    (meter_id, icp_id, serial_number, is_smart, installed_on)
OVERRIDING SYSTEM VALUE
VALUES
    (141001, 131001, 'DEMO-METER-0165841877', true, '2020-01-01')
ON CONFLICT (meter_id) DO UPDATE SET
    icp_id = EXCLUDED.icp_id,
    serial_number = EXCLUDED.serial_number,
    is_smart = EXCLUDED.is_smart;

INSERT INTO src_metering.registers
    (register_id, meter_id, register_code, description, is_controlled, flow_direction)
OVERRIDING SYSTEM VALUE
VALUES
    (151001, 141001, 'UN', 'Grid import', false, 'import'),
    (151002, 141001, 'EX', 'Solar export', false, 'export')
ON CONFLICT (register_id) DO UPDATE SET
    meter_id = EXCLUDED.meter_id,
    register_code = EXCLUDED.register_code,
    description = EXCLUDED.description,
    is_controlled = EXCLUDED.is_controlled,
    flow_direction = EXCLUDED.flow_direction;

-- Synthetic half-hour source reads.
--
-- These are NOT read from the image — the bill prints band totals, not
-- intervals. They are generated so that re-rating them under the TOU windows
-- above reproduces every figure the bill prints:
--
--   energy  peak 34.08 | shoulder 71.39 | off_peak 74.96
--   network peak 38.22 | off_peak 142.22
--   export  peak  0.28 | shoulder 45.20 | off_peak  2.00
--
-- Doing that requires distributing by the INTERSECTION of the two banding
-- schemes, not by energy band alone. The four cells are:
--
--   A  energy peak   & network peak      07:00-11:00, 17:00-21:00   34.08
--   B  energy should & network peak      21:00-21:30                 4.14
--   C  energy should & network off_peak  11:00-17:00, 21:30-23:00   67.25
--   D  energy offpk  & network off_peak  23:00-07:00                74.96
--
-- B is exactly network peak minus energy peak, which is what makes both
-- schemes reconcile at once. Network off_peak lands at 142.21 against the
-- printed 142.22 — a 0.01 rounding artefact present in the bill itself,
-- whose network bands sum to 180.44 while its energy bands sum to 180.43.
--
-- Within each cell a fixed diurnal shape spreads the total, and the final
-- interval absorbs the rounding residue so each cell sums exactly.
WITH half_hours AS (
    SELECT generate_series(
        timestamptz '2024-08-08 00:00:00+12',
        timestamptz '2024-08-31 23:30:00+12',
        interval '30 minutes'
    ) AS interval_start
), tagged AS (
    SELECT interval_start,
           EXTRACT(hour FROM interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0
             AS hr
    FROM half_hours
), celled AS (
    SELECT interval_start, hr,
           CASE
               WHEN (hr >= 7 AND hr < 11) OR (hr >= 17 AND hr < 21) THEN 'A'
               WHEN hr >= 21 AND hr < 21.5                          THEN 'B'
               WHEN (hr >= 11 AND hr < 17) OR (hr >= 21.5 AND hr < 23) THEN 'C'
               ELSE 'D'
           END AS cell
    FROM tagged
), shaped AS (
    -- import: morning and evening emphasis; overnight carries controlled load
    SELECT interval_start, hr, cell, 'UN' AS register_code,
           CASE
               WHEN hr >= 7  AND hr < 9  THEN 1.30
               WHEN hr >= 9  AND hr < 11 THEN 0.80
               WHEN hr >= 17 AND hr < 19 THEN 0.95
               WHEN hr >= 19 AND hr < 21 THEN 1.15
               WHEN hr >= 21 AND hr < 23 THEN 0.90
               WHEN hr >= 11 AND hr < 17 THEN 0.70
               WHEN hr >= 23 OR  hr < 2  THEN 1.25
               ELSE 0.95
           END::numeric AS weight
    FROM celled
    UNION ALL
    -- export: solar bell centred on solar noon, plus a small battery
    -- discharge before dawn which is what puts 2.00 kWh in off_peak
    SELECT interval_start, hr, cell, 'EX' AS register_code,
           CASE
               WHEN hr >= 11 AND hr < 13 THEN 1.10
               WHEN hr >= 13 AND hr < 15 THEN 1.15
               WHEN hr >= 15 AND hr < 17 THEN 0.75
               WHEN hr >= 9  AND hr < 11 THEN 0.55
               WHEN hr >= 17 AND hr < 18 THEN 0.20
               WHEN hr >= 1  AND hr < 3  THEN 0.60
               WHEN hr >= 6  AND hr < 7  THEN 0.40
               ELSE 0.01
           END::numeric AS weight
    FROM celled
), targets AS (
    SELECT * FROM (VALUES
        ('UN','A', 34.08::numeric), ('UN','B',  4.14::numeric),
        ('UN','C', 67.25::numeric), ('UN','D', 74.96::numeric),
        -- export by energy band: peak = cell A, shoulder = cells B+C,
        -- off_peak = cell D. B carries none (21:00-21:30 is dark).
        ('EX','A',  0.28::numeric), ('EX','B',  0.00::numeric),
        ('EX','C', 45.20::numeric), ('EX','D',  2.00::numeric)
    ) AS t(register_code, cell, target_kwh)
), weighted AS (
    SELECT s.interval_start, s.register_code, s.cell, s.weight, t.target_kwh,
           SUM(s.weight) OVER (PARTITION BY s.register_code, s.cell) AS cell_weight,
           ROW_NUMBER() OVER (PARTITION BY s.register_code, s.cell
                              ORDER BY s.interval_start)              AS row_no,
           COUNT(*)     OVER (PARTITION BY s.register_code, s.cell)   AS row_count
    FROM shaped s
    JOIN targets t
      ON t.register_code = s.register_code AND t.cell = s.cell
), rounded AS (
    SELECT interval_start, register_code, cell, target_kwh, row_no, row_count,
           ROUND(target_kwh * weight / cell_weight, 4) AS kwh
    FROM weighted
), finalized AS (
    -- last interval of each cell absorbs the rounding residue
    SELECT interval_start, register_code,
           CASE WHEN row_no = row_count
                THEN ROUND(target_kwh
                     - (SUM(kwh) OVER (PARTITION BY register_code, cell) - kwh), 4)
                ELSE kwh END AS kwh
    FROM rounded
)
INSERT INTO src_metering.interval_reads
    (icp_identifier, register_code, interval_start, kwh, read_status,
     version_no, source_file)
SELECT '0165841877LCC54', register_code, interval_start, kwh,
       'A', 1, 'sample-synthetic-2024-08.csv'
FROM finalized
WHERE kwh > 0;

INSERT INTO src_catalogue.price_plans
    (plan_id, code, name, network_code, user_category, is_tou,
     ppd_percent, valid_from, valid_to, active)
OVERRIDING SYSTEM VALUE
VALUES
    (311001, 'NZSZ_ZERO_STD_FXTOU_240901_T1',
     'Zero Standard Fixed TOU', 'VECT', 'STANDARD', true,
     0, '2024-08-01', NULL, true)
ON CONFLICT (code, valid_from) DO UPDATE SET
    name = EXCLUDED.name,
    network_code = EXCLUDED.network_code,
    user_category = EXCLUDED.user_category,
    is_tou = EXCLUDED.is_tou,
    active = EXCLUDED.active;

-- TOU windows.
--
-- The image prints band NAMES and rates but not clock windows, so these are
-- derived: they are the windows that REPRODUCE the bill's own band splits.
-- That constraint pins them down almost completely.
--
--   energy: peak 34.08 / shoulder 71.39 / off_peak 74.96  = 180.43 kWh
--   network: peak 38.22 / off_peak 142.22                 = 180.44 kWh
--
-- Network peak (38.22) exceeds energy peak (34.08) by 4.14 kWh, so the
-- network peak window must be slightly WIDER than the energy one — hence
-- 17:00-21:30 for network against 17:00-21:00 for energy. This is the
-- per-charge-group band scoping the design doc calls out: one plan, two
-- different band sets.
--
-- Export confirms the shoulder placement: 45.20 of 47.48 kWh exported lands
-- in shoulder, and solar exports at midday — so shoulder must be 11:00-17:00,
-- not overnight.
--
-- Windows are GAPLESS and cover all seven days. Every half hour of the
-- period falls in exactly one band per charge group; an unbanded interval
-- would be silently unpriced.
INSERT INTO src_catalogue.tou_windows
    (window_id, plan_id, charge_scope, band_label, days_csv, time_from, time_to)
OVERRIDING SYSTEM VALUE
VALUES
    -- network: 2 bands
    (321001, 311001, 'NET', 'PEAK',     '1,2,3,4,5,6,7', '07:00', '11:00'),
    (321002, 311001, 'NET', 'PEAK',     '1,2,3,4,5,6,7', '17:00', '21:30'),
    (321003, 311001, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '00:00', '07:00'),
    (321004, 311001, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '11:00', '17:00'),
    (321005, 311001, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '21:30', '24:00'),
    -- energy: 3 bands
    (321006, 311001, 'ENE', 'PEAK',     '1,2,3,4,5,6,7', '07:00', '11:00'),
    (321007, 311001, 'ENE', 'PEAK',     '1,2,3,4,5,6,7', '17:00', '21:00'),
    (321008, 311001, 'ENE', 'SHOULDER', '1,2,3,4,5,6,7', '11:00', '17:00'),
    (321009, 311001, 'ENE', 'SHOULDER', '1,2,3,4,5,6,7', '21:00', '23:00'),
    (321010, 311001, 'ENE', 'OFFPEAK',  '1,2,3,4,5,6,7', '00:00', '07:00'),
    (321011, 311001, 'ENE', 'OFFPEAK',  '1,2,3,4,5,6,7', '23:00', '24:00');

INSERT INTO src_catalogue.rate_components
    (component_id, plan_id, charge_scope, component_desc, rate_uom,
     window_id, flow_direction, unit_rate, valid_from)
OVERRIDING SYSTEM VALUE
VALUES
    (331001, 311001, 'NET', 'Peak Usage', 'KWH', 321001, 'import', 0.1361, '2024-08-01'),
    (331002, 311001, 'NET', 'Off Peak Usage', 'KWH', 321003, 'import', 0.0378, '2024-08-01'),
    (331003, 311001, 'NET', 'VECT + Daily charges', 'DAY', NULL, 'import', 0.6000, '2024-08-01'),
    (331004, 311001, 'NET', 'Transmission Charges', 'KWH', NULL, 'import', 0.0235, '2024-08-01'),
    (331005, 311001, 'ENE', 'Peak Usage', 'KWH', 321006, 'import', 0.2600, '2024-08-01'),
    (331006, 311001, 'ENE', 'Off Peak Usage', 'KWH', 321010, 'import', 0.1000, '2024-08-01'),
    (331007, 311001, 'ENE', 'Shoulder Usage', 'KWH', 321008, 'import', 0.1400, '2024-08-01'),
    (331008, 311001, 'ENE', 'Export On-Peak', 'KWH', 321006, 'export', 0.2600, '2024-08-01'),
    (331009, 311001, 'ENE', 'Export Off-Peak', 'KWH', 321010, 'export', 0.1000, '2024-08-01'),
    (331010, 311001, 'ENE', 'Export Shoulder-Peak', 'KWH', 321008, 'export', 0.1400, '2024-08-01'),
    (331011, 311001, 'OTH', 'ELECTRICITY AUTHORITY Levy', 'KWH', NULL, 'import', 0.0013, '2024-08-01');

INSERT INTO src_billing.accounts
    (ACCT_ID, ACCT_NO, CUST_ID, ICP_NUMBER, PLAN_CODE, PAY_METHOD,
     BILL_CYCLE_DAY, START_DT)
OVERRIDING SYSTEM VALUE
VALUES
    (221001, 'DEMO-ACCT-221001', 211001, '0165841877LCC54',
    'NZSZ_ZERO_STD_FXTOU_240901_T1', 'DD', 28, '2020-01-01')
ON CONFLICT (ACCT_NO) DO UPDATE SET
    CUST_ID = EXCLUDED.CUST_ID,
    ICP_NUMBER = EXCLUDED.ICP_NUMBER,
    PLAN_CODE = EXCLUDED.PLAN_CODE,
    PAY_METHOD = EXCLUDED.PAY_METHOD;

INSERT INTO src_billing.invoices
    (INV_ID, INV_NO, ACCT_ID, PLAN_CODE, ISSUE_DT, PERIOD_FROM, PERIOD_TO,
     OPEN_BAL, PAYMENTS_APPL, BAL_BROUGHT_FWD, CHARGES_EXGST, GST_RATE,
     GST_AMT, TOTAL_AMT, DUE_DT, LATE_FEE, TOTAL_IF_LATE, PPD_AMT,
     TOTAL_IMPORT_KWH, TOTAL_EXPORT_KWH, EXPORT_CREDIT, INV_STATUS)
OVERRIDING SYSTEM VALUE
VALUES
    (231001, 'DEMO-INV-20240831', 221001,
     'NZSZ_ZERO_STD_FXTOU_240901_T1', '2024-09-01', '2024-08-08', '2024-08-31',
    0, 0, 0, 0, 0.00, 0.00, 0, '2024-09-15',
     0, NULL, 0, 0, 0, 0, 'ISSUED')
ON CONFLICT (INV_NO) DO UPDATE SET
    ACCT_ID = EXCLUDED.ACCT_ID,
    PLAN_CODE = EXCLUDED.PLAN_CODE,
    ISSUE_DT = EXCLUDED.ISSUE_DT,
    PERIOD_FROM = EXCLUDED.PERIOD_FROM,
    PERIOD_TO = EXCLUDED.PERIOD_TO,
    CHARGES_EXGST = EXCLUDED.CHARGES_EXGST,
    GST_RATE = EXCLUDED.GST_RATE,
    GST_AMT = EXCLUDED.GST_AMT,
    TOTAL_AMT = EXCLUDED.TOTAL_AMT,
    TOTAL_IMPORT_KWH = EXCLUDED.TOTAL_IMPORT_KWH,
    TOTAL_EXPORT_KWH = EXCLUDED.TOTAL_EXPORT_KWH,
    EXPORT_CREDIT = EXCLUDED.EXPORT_CREDIT;

DELETE FROM src_billing.invoice_lines WHERE INV_ID = 231001;
INSERT INTO src_billing.invoice_lines
    (LINE_ID, INV_ID, CHG_GROUP, FLOW_DIR, LINE_DESC, TOU_BAND,
     QTY, UOM, UNIT_RATE, LOSS_QTY, LINE_AMT, SORT_SEQ)
OVERRIDING SYSTEM VALUE
VALUES
    (241001, 231001, 'NET', 'IMPORT', 'Peak Usage', 'PEAK', 38.22, 'KWH', 0.1361, NULL, 5.20, 10),
    (241002, 231001, 'NET', 'IMPORT', 'Off Peak Usage', 'OFFPEAK', 142.22, 'KWH', 0.0378, NULL, 5.38, 20),
    (241003, 231001, 'NET', 'IMPORT', 'VECT + Daily charges', NULL, 24.00, 'DAY', 0.6000, NULL, 14.40, 30),
    (241004, 231001, 'NET', 'IMPORT', 'Transmission Charges', NULL, 180.43, 'KWH', 0.0235, NULL, 4.24, 40),
    (241005, 231001, 'ENE', 'IMPORT', 'Peak Usage', 'PEAK', 34.08, 'KWH', 0.2600, 1.74, 9.31, 50),
    (241006, 231001, 'ENE', 'IMPORT', 'Off Peak Usage', 'OFFPEAK', 74.96, 'KWH', 0.1000, 3.83, 7.88, 60),
    (241007, 231001, 'ENE', 'IMPORT', 'Shoulder Usage', 'SHOULDER', 71.39, 'KWH', 0.1400, 3.65, 10.51, 70),
    (241008, 231001, 'ENE', 'EXPORT', 'Export On-Peak', 'PEAK', 0.28, 'KWH', 0.2600, NULL, -0.07, 80),
    (241009, 231001, 'ENE', 'EXPORT', 'Export Off-Peak', 'OFFPEAK', 2.00, 'KWH', 0.1000, NULL, -0.20, 90),
    (241010, 231001, 'ENE', 'EXPORT', 'Export Shoulder-Peak', 'SHOULDER', 45.20, 'KWH', 0.1400, NULL, -6.33, 100),
    -- NOTE: this line does not reconcile from qty x rate.
    --   180.43 x 0.0013 = 0.2346, but the bill prints $0.22.
    -- The same anomaly appears on the other sample bill (1034.67 x 0.0013 =
    -- $1.35 printed as $1.29), so the displayed rate is rounded from a finer
    -- underlying figure. The PRINTED amount is seeded, because fidelity to
    -- the bill matters more here than internal consistency — but a rating
    -- engine test asserting "amount = billed_quantity x rate" will fail on
    -- this row and should exclude it.
    (241011, 231001, 'OTH', 'IMPORT', 'ELECTRICITY AUTHORITY Levy', NULL, 180.43, 'KWH', 0.0013, NULL, 0.22, 110);

-- Calculate source invoice summaries from the inserted source details.
UPDATE src_billing.invoices AS invoice
SET CHARGES_EXGST = totals.charges_exgst,
    GST_AMT = ROUND(totals.charges_exgst * invoice.GST_RATE, 2),
    TOTAL_AMT = ROUND(totals.charges_exgst * (1 + invoice.GST_RATE), 2),
    TOTAL_IMPORT_KWH = totals.import_kwh,
    TOTAL_EXPORT_KWH = totals.export_kwh,
    EXPORT_CREDIT = totals.export_credit
FROM (
    SELECT INV_ID,
           ROUND(SUM(LINE_AMT), 2) AS charges_exgst,
           SUM(CASE WHEN CHG_GROUP = 'ENE' AND FLOW_DIR = 'IMPORT'
                    AND UOM = 'KWH' THEN QTY ELSE 0 END) AS import_kwh,
           SUM(CASE WHEN CHG_GROUP = 'ENE' AND FLOW_DIR = 'EXPORT'
                    AND UOM = 'KWH' THEN QTY ELSE 0 END) AS export_kwh,
           ABS(ROUND(SUM(CASE WHEN FLOW_DIR = 'EXPORT'
                              THEN LINE_AMT ELSE 0 END), 2)) AS export_credit
    FROM src_billing.invoice_lines
    WHERE INV_ID = 231001
    GROUP BY INV_ID
) AS totals
WHERE invoice.INV_ID = totals.INV_ID;

-- =====================================================================
-- SERVING REPLICA AND MARTS
-- =====================================================================

INSERT INTO metering.network
    (network_id, network_code, network_name, region)
OVERRIDING SYSTEM VALUE
VALUES (111001, 'VECT', 'Vector', 'Auckland')
ON CONFLICT (network_code) DO UPDATE SET network_name = EXCLUDED.network_name;

INSERT INTO billing.customer
    (customer_id, customer_number, full_name, email, is_medically_dependent, is_vulnerable)
OVERRIDING SYSTEM VALUE
VALUES (211001, '196501', 'Julia Fox',
    'julia@kea.example', false, false)
ON CONFLICT (customer_number) DO UPDATE SET
    full_name = EXCLUDED.full_name,
    email = EXCLUDED.email;

INSERT INTO metering.property
    (property_id, address_line_1, suburb, city, climate_region)
OVERRIDING SYSTEM VALUE
VALUES (121001, '1 Alpha Road', 'Deltaville', 'Auckland', 'Auckland');

INSERT INTO metering.icp
    (icp_id, icp_number, property_id, network_id, connection_date)
OVERRIDING SYSTEM VALUE
VALUES (131001, '0165841877LCC54', 121001, 111001, '2020-01-01')
ON CONFLICT (icp_number) DO UPDATE SET
    property_id = EXCLUDED.property_id,
    network_id = EXCLUDED.network_id;

INSERT INTO metering.meter_register
    (register_id, icp_id, register_code, description, is_controlled, direction)
OVERRIDING SYSTEM VALUE
VALUES
    (151001, 131001, 'UN', 'Grid import', false, 'import'),
    (151002, 131001, 'EX', 'Solar export', false, 'export')
ON CONFLICT (register_id) DO UPDATE SET
    icp_id = EXCLUDED.icp_id,
    register_code = EXCLUDED.register_code,
    description = EXCLUDED.description,
    direction = EXCLUDED.direction;

-- Resolve the synthetic source feed into serving surrogate keys.
INSERT INTO metering.meter_read_hh
        (icp_id, register_id, interval_start, kwh, quality, source_file)
SELECT 131001,
             CASE register_code WHEN 'UN' THEN 151001 ELSE 151002 END,
             interval_start,
             kwh,
             'actual',
             source_file
FROM src_metering.interval_reads
WHERE icp_identifier = '0165841877LCC54'
    AND source_file = 'sample-synthetic-2024-08.csv';

INSERT INTO catalogue.plan
    (plan_id, plan_code, plan_name, network_id, user_type, is_tou,
     prompt_payment_discount_pct, effective_from, is_active)
OVERRIDING SYSTEM VALUE
VALUES (311001, 'NZSZ_ZERO_STD_FXTOU_240901_T1', 'Zero Standard Fixed TOU',
        111001, 'standard', true, 0, '2024-08-01', true)
ON CONFLICT (plan_code, effective_from) DO UPDATE SET
    plan_name = EXCLUDED.plan_name,
    network_id = EXCLUDED.network_id,
    user_type = EXCLUDED.user_type,
    is_tou = EXCLUDED.is_tou,
    is_active = EXCLUDED.is_active;

-- Mirrors src_catalogue.tou_windows. Gapless, all seven days, and chosen so
-- that re-rating the half-hourly reads reproduces the bill's band splits
-- exactly. Network peak is wider than energy peak (21:30 vs 21:00) — that
-- 30-minute difference is what accounts for network peak 38.22 against
-- energy peak 34.08.
INSERT INTO catalogue.tou_band
    (band_id, plan_id, charge_group, band_name, days_of_week, start_time, end_time)
OVERRIDING SYSTEM VALUE
VALUES
    (321001, 311001, 'network', 'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '07:00', '11:00'),
    (321002, 311001, 'network', 'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '17:00', '21:30'),
    (321003, 311001, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '00:00', '07:00'),
    (321004, 311001, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '11:00', '17:00'),
    (321005, 311001, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '21:30', '24:00'),
    (321006, 311001, 'energy',  'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '07:00', '11:00'),
    (321007, 311001, 'energy',  'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '17:00', '21:00'),
    (321008, 311001, 'energy',  'shoulder', ARRAY[1,2,3,4,5,6,7]::smallint[], '11:00', '17:00'),
    (321009, 311001, 'energy',  'shoulder', ARRAY[1,2,3,4,5,6,7]::smallint[], '21:00', '23:00'),
    (321010, 311001, 'energy',  'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '00:00', '07:00'),
    (321011, 311001, 'energy',  'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '23:00', '24:00');

INSERT INTO catalogue.tariff_component
    (component_id, plan_id, charge_group, component_name, basis, band_id,
     direction, rate, effective_from)
OVERRIDING SYSTEM VALUE
VALUES
    (331001, 311001, 'network', 'Peak Usage', 'per_kwh', 321001, 'import', 0.1361, '2024-08-01'),
    (331002, 311001, 'network', 'Off Peak Usage', 'per_kwh', 321003, 'import', 0.0378, '2024-08-01'),
    (331003, 311001, 'network', 'VECT + Daily charges', 'per_day', NULL, 'import', 0.6000, '2024-08-01'),
    (331004, 311001, 'network', 'Transmission Charges', 'per_kwh', NULL, 'import', 0.0235, '2024-08-01'),
    (331005, 311001, 'energy', 'Peak Usage', 'per_kwh', 321006, 'import', 0.2600, '2024-08-01'),
    (331006, 311001, 'energy', 'Off Peak Usage', 'per_kwh', 321010, 'import', 0.1000, '2024-08-01'),
    (331007, 311001, 'energy', 'Shoulder Usage', 'per_kwh', 321008, 'import', 0.1400, '2024-08-01'),
    (331008, 311001, 'energy', 'Export On-Peak', 'per_kwh', 321006, 'export', 0.2600, '2024-08-01'),
    (331009, 311001, 'energy', 'Export Off-Peak', 'per_kwh', 321010, 'export', 0.1000, '2024-08-01'),
    (331010, 311001, 'energy', 'Export Shoulder-Peak', 'per_kwh', 321008, 'export', 0.1400, '2024-08-01'),
    (331011, 311001, 'other', 'ELECTRICITY AUTHORITY Levy', 'per_kwh', NULL, 'import', 0.0013, '2024-08-01');

INSERT INTO billing.account
    (account_id, account_number, customer_id, icp_id, plan_id, payment_method,
     billing_cycle_day, start_date)
OVERRIDING SYSTEM VALUE
VALUES (221001, 'DEMO-ACCT-221001', 211001, 131001, 311001,
        'direct_debit', 28, '2020-01-01')
ON CONFLICT (account_number) DO UPDATE SET
    customer_id = EXCLUDED.customer_id,
    icp_id = EXCLUDED.icp_id,
    plan_id = EXCLUDED.plan_id,
    payment_method = EXCLUDED.payment_method;

INSERT INTO billing.bill
    (bill_id, account_id, invoice_number, plan_id, issue_date, period_start, period_end,
     opening_balance, payments_applied, balance_brought_fwd, charges_excl_gst,
     gst_rate, gst_amount, total_charges, due_date, late_payment_fee,
     total_if_paid_late, prompt_payment_discount, total_import_kwh,
     total_export_kwh, export_credit, status)
OVERRIDING SYSTEM VALUE
VALUES (231001, 221001, 'DEMO-INV-20240831', 311001,
        '2024-09-01', '2024-08-08', '2024-08-31',
    0, 0, 0, 0, 0.00, 0.00, 0, '2024-09-15',
    0, NULL, 0, 0, 0, 0, 'issued')
ON CONFLICT (invoice_number) DO UPDATE SET
    account_id = EXCLUDED.account_id,
    plan_id = EXCLUDED.plan_id,
    issue_date = EXCLUDED.issue_date,
    period_start = EXCLUDED.period_start,
    period_end = EXCLUDED.period_end,
    charges_excl_gst = EXCLUDED.charges_excl_gst,
    gst_rate = EXCLUDED.gst_rate,
    gst_amount = EXCLUDED.gst_amount,
    total_charges = EXCLUDED.total_charges,
    total_import_kwh = EXCLUDED.total_import_kwh,
    total_export_kwh = EXCLUDED.total_export_kwh,
    export_credit = EXCLUDED.export_credit;

DELETE FROM billing.bill_line WHERE bill_id = 231001;
INSERT INTO billing.bill_line
    (line_id, bill_id, component_id, charge_group, direction, line_label,
     band_name, quantity, unit, rate, loss_kwh, amount, sort_order)
OVERRIDING SYSTEM VALUE
VALUES
    (241001, 231001, 331001, 'network', 'import', 'Peak Usage', 'peak', 38.22, 'per_kwh', 0.1361, NULL, 5.20, 10),
    (241002, 231001, 331002, 'network', 'import', 'Off Peak Usage', 'off_peak', 142.22, 'per_kwh', 0.0378, NULL, 5.38, 20),
    (241003, 231001, 331003, 'network', 'import', 'VECT + Daily charges', NULL, 24.00, 'per_day', 0.6000, NULL, 14.40, 30),
    (241004, 231001, 331004, 'network', 'import', 'Transmission Charges', NULL, 180.43, 'per_kwh', 0.0235, NULL, 4.24, 40),
    (241005, 231001, 331005, 'energy', 'import', 'Peak Usage', 'peak', 34.08, 'per_kwh', 0.2600, 1.74, 9.31, 50),
    (241006, 231001, 331006, 'energy', 'import', 'Off Peak Usage', 'off_peak', 74.96, 'per_kwh', 0.1000, 3.83, 7.88, 60),
    (241007, 231001, 331007, 'energy', 'import', 'Shoulder Usage', 'shoulder', 71.39, 'per_kwh', 0.1400, 3.65, 10.51, 70),
    (241008, 231001, 331008, 'energy', 'export', 'Export On-Peak', 'peak', 0.28, 'per_kwh', 0.2600, NULL, -0.07, 80),
    (241009, 231001, 331009, 'energy', 'export', 'Export Off-Peak', 'off_peak', 2.00, 'per_kwh', 0.1000, NULL, -0.20, 90),
    (241010, 231001, 331010, 'energy', 'export', 'Export Shoulder-Peak', 'shoulder', 45.20, 'per_kwh', 0.1400, NULL, -6.33, 100),
    (241011, 231001, 331011, 'other', 'import', 'ELECTRICITY AUTHORITY Levy', NULL, 180.43, 'per_kwh', 0.0013, NULL, 0.22, 110);

-- Calculate serving bill summaries from the inserted serving details.
UPDATE billing.bill AS bill
SET charges_excl_gst = totals.charges_excl_gst,
    gst_amount = ROUND(totals.charges_excl_gst * bill.gst_rate, 2),
    total_charges = ROUND(totals.charges_excl_gst * (1 + bill.gst_rate), 2),
    total_import_kwh = totals.import_kwh,
    total_export_kwh = totals.export_kwh,
    export_credit = totals.export_credit
FROM (
    SELECT bill_id,
           ROUND(SUM(amount), 2) AS charges_excl_gst,
           SUM(CASE WHEN charge_group = 'energy' AND direction = 'import'
                    AND unit = 'per_kwh' THEN quantity ELSE 0 END) AS import_kwh,
           SUM(CASE WHEN charge_group = 'energy' AND direction = 'export'
                    AND unit = 'per_kwh' THEN quantity ELSE 0 END) AS export_kwh,
           ABS(ROUND(SUM(CASE WHEN direction = 'export'
                              THEN amount ELSE 0 END), 2)) AS export_credit
    FROM billing.bill_line
    WHERE bill_id = 231001
    GROUP BY bill_id
) AS totals
WHERE bill.bill_id = totals.bill_id;

INSERT INTO kea.mart_account_context
    (account_id, account_number, customer_id, customer_number, customer_name,
     email, phone, icp_id, icp_number, address_display, climate_region, network_code,
     network_name, has_solar, has_controlled_load, plan_id, plan_code, plan_name,
     user_type, is_tou, daily_fixed_charge, payment_method, billing_cycle_day,
     current_period_start, current_period_end, days_into_period, estimated_read_pct,
     last_read_at, is_quarantined)
VALUES
    (221001, 'DEMO-ACCT-221001', 211001, '196501',
     'Julia Fox', 'julia@kea.example', '021 196 1501',
     131001, '0165841877LCC54', '1 Alpha Road, Deltaville', 'Auckland',
     'VECT', 'Vector', true, false, 311001,
     'NZSZ_ZERO_STD_FXTOU_240901_T1', 'Zero Standard Fixed TOU', 'standard', true,
     0.600000, 'direct_debit', 28, '2024-08-08', '2024-08-31', 24, 0,
     '2024-08-31 00:00:00+00', false)
ON CONFLICT (account_id) DO UPDATE SET
    account_number = EXCLUDED.account_number,
    customer_id = EXCLUDED.customer_id,
    customer_number = EXCLUDED.customer_number,
    customer_name = EXCLUDED.customer_name,
    email = EXCLUDED.email,
    phone = EXCLUDED.phone,
    icp_id = EXCLUDED.icp_id,
    icp_number = EXCLUDED.icp_number,
    address_display = EXCLUDED.address_display,
    plan_id = EXCLUDED.plan_id,
    plan_code = EXCLUDED.plan_code,
    current_period_start = EXCLUDED.current_period_start,
    current_period_end = EXCLUDED.current_period_end,
    days_into_period = EXCLUDED.days_into_period,
    last_read_at = EXCLUDED.last_read_at;

-- Hourly profile, DERIVED from the half-hourly reads rather than transcribed
-- from the chart.
--
-- The chart in the image does not reconcile with the same page's charges:
-- read as hourly kWh its bars total about 20.5 kWh/day, which over 24 days is
-- ~492 kWh against the 180.43 kWh the bill charges for — roughly 2.7x. It
-- appears to be illustrative. Deriving from the reads keeps the mart
-- consistent with the bill, which is what the agent needs.
--
-- sample_days counts actual weekdays in 2024-08-08..2024-08-31: 17, not 24.
-- (8 August 2024 is a Thursday; the period holds 17 weekdays and 7 weekend days.)
INSERT INTO kea.mart_hourly_profile
    (icp_id, profile_month, hour_of_day, is_weekday,
     avg_import_kwh, avg_export_kwh, sample_days)
SELECT 131001,
       date '2024-08-01',
       EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')::smallint,
       EXTRACT(isodow FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') <= 5,
       -- Divided by hours observed rather than by days, so a daylight-time
       -- transition cannot distort one hour. August holds neither, but the
       -- expression should not depend on that. COUNT(DISTINCT interval_start)
       -- counts each half hour once no matter how many registers report it,
       -- which COUNT(*) would not with import and export both joined here.
       ROUND(SUM(r.kwh) FILTER (WHERE reg.direction = 'import')
             / NULLIF(COUNT(DISTINCT r.interval_start) / 2.0, 0), 4),
       ROUND(COALESCE(SUM(r.kwh) FILTER (WHERE reg.direction = 'export'), 0)
             / NULLIF(COUNT(DISTINCT r.interval_start) / 2.0, 0), 4),
       COUNT(DISTINCT (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date)::smallint
FROM metering.meter_read_hh r
JOIN metering.meter_register reg ON reg.register_id = r.register_id
WHERE r.icp_id = 131001
GROUP BY 3, 4
ON CONFLICT (icp_id, profile_month, hour_of_day, is_weekday) DO UPDATE SET
    avg_import_kwh = EXCLUDED.avg_import_kwh,
    avg_export_kwh = EXCLUDED.avg_export_kwh,
    sample_days = EXCLUDED.sample_days;

-- Daily usage mart. Was missing entirely; it is the mart the bill explainer
-- reads most. Derived from the same reads.
INSERT INTO kea.mart_daily_usage
    (icp_id, usage_date, import_kwh, export_kwh, controlled_kwh,
     uncontrolled_kwh, base_load_kwh, interval_count, expected_intervals,
     estimated_count, is_weekend)
SELECT 131001,
       (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date,
       ROUND(COALESCE(SUM(r.kwh) FILTER (WHERE reg.direction = 'import'), 0), 4),
       ROUND(COALESCE(SUM(r.kwh) FILTER (WHERE reg.direction = 'export'), 0), 4),
       0,
       ROUND(COALESCE(SUM(r.kwh) FILTER (WHERE reg.direction = 'import'), 0), 4),
       -- base load: the quietest half hour of the day, annualised to a day
       ROUND(MIN(r.kwh) FILTER (WHERE reg.direction = 'import') * 48, 4),
       COUNT(*) FILTER (WHERE reg.direction = 'import')::smallint,
       48,
       0,
       EXTRACT(isodow FROM (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date) >= 6
FROM metering.meter_read_hh r
JOIN metering.meter_register reg ON reg.register_id = r.register_id
WHERE r.icp_id = 131001
GROUP BY 2
ON CONFLICT (icp_id, usage_date) DO UPDATE SET
    import_kwh = EXCLUDED.import_kwh,
    export_kwh = EXCLUDED.export_kwh,
    base_load_kwh = EXCLUDED.base_load_kwh,
    interval_count = EXCLUDED.interval_count;

INSERT INTO kea.mart_bill_explanation
    (bill_id, account_id, icp_id, period_start, period_end, period_days,
     total_amount, total_kwh, amount_delta, amount_delta_pct, kwh_delta,
     delta_from_period_length, delta_from_weather, delta_from_base_load,
     delta_from_tariff, delta_unexplained, estimated_read_pct,
     missing_interval_count, is_explainable)
VALUES
    (231001, 221001, 131001, '2024-08-08', '2024-08-31', 24,
     50.54, 180.43, NULL, NULL, NULL,
     -- No prior bill exists for this account, so there is nothing to compare
     -- against and no driver can be attributed. is_explainable = false is the
     -- honest state: the agent should say "this is your first bill" rather
     -- than assert a cause. Setting it true with all-NULL deltas would invite
     -- the agent to explain a change it cannot see.
     NULL, NULL, NULL, NULL, NULL, 0, 0, false)
ON CONFLICT (bill_id) DO UPDATE SET
    total_amount = EXCLUDED.total_amount,
    total_kwh = EXCLUDED.total_kwh,
    period_start = EXCLUDED.period_start,
    period_end = EXCLUDED.period_end,
    period_days = EXCLUDED.period_days,
    estimated_read_pct = EXCLUDED.estimated_read_pct,
    missing_interval_count = EXCLUDED.missing_interval_count,
    is_explainable = EXCLUDED.is_explainable;

INSERT INTO kea.mart_load_log
    (mart_name, started_at, completed_at, row_count, warehouse_run_id, status)
VALUES
    ('sample_bill_0165841877LCC54', now(), now(), 1,
     'sample-image-2026-08-21', 'succeeded');

-- =====================================================================
-- SELF-CHECK — fails the transaction if the seed does not reproduce the bill
-- =====================================================================
DO $$
DECLARE v_total numeric; v_peak numeric; v_shoulder numeric;
        v_offpeak numeric; v_export numeric;
BEGIN
    SELECT ROUND(SUM(amount),2) INTO v_total
      FROM billing.bill_line WHERE bill_id = 231001;
    IF v_total <> 50.54 THEN
        RAISE EXCEPTION 'bill total is %, expected 50.54', v_total;
    END IF;

    SELECT ROUND(SUM(r.kwh),2) INTO v_peak
      FROM metering.meter_read_hh r
      JOIN metering.meter_register g ON g.register_id = r.register_id
     WHERE r.icp_id = 131001 AND g.direction = 'import'
       AND ((EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') >= 7
             AND EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') < 11)
         OR (EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') >= 17
             AND EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') < 21));
    IF ABS(v_peak - 34.08) > 0.02 THEN
        RAISE EXCEPTION 'energy peak import is %, expected 34.08', v_peak;
    END IF;

    SELECT ROUND(SUM(r.kwh),2) INTO v_export
      FROM metering.meter_read_hh r
      JOIN metering.meter_register g ON g.register_id = r.register_id
     WHERE r.icp_id = 131001 AND g.direction = 'export';
    IF ABS(v_export - 47.48) > 0.02 THEN
        RAISE EXCEPTION 'total export is %, expected 47.48', v_export;
    END IF;

    RAISE NOTICE 'seed OK: bill total %, energy peak %, export %',
                 v_total, v_peak, v_export;
END $$;

COMMIT;

-- Quick verification queries:
-- SELECT line_label, quantity, loss_kwh, rate, amount
-- FROM billing.bill_line WHERE bill_id = 231001 ORDER BY sort_order;
-- SELECT SUM(amount) AS total_ex_gst
-- FROM billing.bill_line WHERE bill_id = 231001;
-- SELECT * FROM kea.mart_hourly_profile
-- WHERE icp_id = 131001 ORDER BY hour_of_day;
