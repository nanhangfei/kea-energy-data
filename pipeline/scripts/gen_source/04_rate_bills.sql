-- =====================================================================
-- The rating engine.
--
-- Turns metered half-hours plus a plan code into invoice lines. No such
-- thing existed: services/api/.../engine.py is flat-rate and collapses to
-- daily totals, and generate_scenarios.py hardcodes band boundaries as
-- numeric literals and never joins tou_windows or rate_components. This
-- is the first code in the repo that prices a bill from the catalogue.
--
-- Set-based, one pass. Five things here are wrong SILENTLY -- each
-- produces a plausible invoice that still balances internally:
--
--  1. days_csv parsed with position() instead of string_to_array().
--     Every weekend would band as a weekday. Marked below.
--  2. An unbanded half-hour is simply never priced. The invoice still
--     adds up. Gaplessness is asserted in 02_catalogue.sql, not here,
--     because by the time a bill is wrong it is too late to notice.
--  3. Banding on UTC. The container runs TZ=UTC, so a naive
--     interval_start::time mis-bands every reading -- worst around the
--     peak boundaries, and worst of all on the two DST days.
--  4. LOSS_QTY omitted. Line losses are ~5.11% of import energy; leaving
--     them out undercharges every energy line by 5% and the invoice
--     still balances. Verified against the printed bill: 34.08 kWh
--     carries 1.74 kWh of loss.
--  5. Export sign. LINE_AMT is negative for export; EXPORT_CREDIT on the
--     header is the same number, positive.
--
-- Exposed as a function so it can be proved against ONE known invoice
-- before it is pointed at thirteen thousand generated ones.
-- =====================================================================

CREATE OR REPLACE FUNCTION gen_audit.rate_lines(
    p_icp        text,
    p_plan_code  text,
    p_from       date,
    p_to         date            -- INCLUSIVE, as printed on the bill
)
RETURNS TABLE (
    chg_group  text, flow_dir  text, line_desc text, tou_band text,
    qty        numeric, uom     text, unit_rate numeric,
    loss_qty   numeric, line_amt numeric, sort_seq int
)
LANGUAGE sql STABLE AS $$
WITH
-- The period as the bill states it. There is no period_days column in
-- src_billing.invoices on purpose; it is derived, and both endpoints
-- count -- 2024-08-08 to 2024-08-31 is 24 days, not 23.
period AS (
    SELECT (p_to - p_from) + 1 AS days
),
-- Revisions coexist by design: the same interval can be re-issued and
-- the source keeps every version. Highest version wins.
latest AS (
    SELECT DISTINCT ON (icp_identifier, register_code, interval_start)
           icp_identifier, register_code, interval_start, kwh
    FROM src_metering.interval_reads
    WHERE icp_identifier = p_icp
      AND (interval_start AT TIME ZONE 'Pacific/Auckland')::date
          BETWEEN p_from AND p_to
    ORDER BY icp_identifier, register_code, interval_start, version_no DESC
),
-- NZ LOCAL wall clock. Not interval_start::time -- see trap 3.
localised AS (
    SELECT l.*,
           (l.interval_start AT TIME ZONE 'Pacific/Auckland') AS loc,
           CASE WHEN r.flow_direction = 'export' THEN 'export' ELSE 'import' END AS flow
    FROM latest l
    LEFT JOIN src_metering.connection_points c ON c.icp_identifier = l.icp_identifier
    LEFT JOIN src_metering.meters m  ON m.icp_id = c.icp_id
    LEFT JOIN src_metering.registers r ON r.meter_id = m.meter_id
                                     AND r.register_code = l.register_code
),
-- The plan version in force at the END of the period. The platform doc
-- calls this the dangerous resolution: valid_to is exclusive, and using
-- <= would match two versions on a boundary day and double every line.
plan_version AS (
    SELECT plan_id FROM src_catalogue.price_plans
    WHERE code = p_plan_code
      AND valid_from <= p_to
      AND (valid_to IS NULL OR p_to < valid_to)
    ORDER BY valid_from DESC
    LIMIT 1
),
-- Each reading is banded once PER CHARGE SCOPE, because network and
-- energy band the same day differently on the same plan.
banded AS (
    SELECT w.charge_scope, w.band_label, x.flow, x.kwh
    FROM localised x
    CROSS JOIN plan_version pv
    JOIN src_catalogue.tou_windows w
      ON w.plan_id = pv.plan_id
     -- = ANY(string_to_array(...)) and NOT position(): see trap 1.
     AND extract(isodow FROM x.loc)::text = ANY(string_to_array(w.days_csv, ','))
     AND x.loc::time >= w.time_from
     AND x.loc::time <  w.time_to
),
band_totals AS (
    SELECT charge_scope, band_label, flow, ROUND(SUM(kwh), 2) AS kwh
    FROM banded GROUP BY 1, 2, 3
),
-- For un-banded per-kWh components (transmission, the EA levy) the
-- quantity is the scope total, rounded once -- not the sum of the
-- already-rounded bands, which differs by a cent.
scope_totals AS (
    SELECT flow, ROUND(SUM(kwh), 2) AS kwh
    FROM localised GROUP BY 1
),
priced AS (
    SELECT rc.charge_scope, rc.flow_direction, rc.component_desc, rc.rate_uom,
           w.band_label, rc.unit_rate,
           CASE
             WHEN rc.rate_uom = 'DAY'      THEN (SELECT days FROM period)::numeric
             WHEN rc.window_id IS NOT NULL THEN COALESCE(bt.kwh, 0)
             ELSE COALESCE(st.kwh, 0)
           END AS qty
    FROM src_catalogue.rate_components rc
    CROSS JOIN plan_version pv
    LEFT JOIN src_catalogue.tou_windows w ON w.window_id = rc.window_id
    LEFT JOIN band_totals bt ON bt.charge_scope = rc.charge_scope
                            AND bt.band_label   = w.band_label
                            AND bt.flow         = rc.flow_direction
    LEFT JOIN scope_totals st ON st.flow = rc.flow_direction
    WHERE rc.plan_id = pv.plan_id
      AND rc.valid_from <= p_to
      AND (rc.valid_to IS NULL OR p_to < rc.valid_to)
),
withloss AS (
    SELECT p.*,
           -- Line losses, import energy only. Trap 4.
           CASE WHEN p.charge_scope = 'ENE' AND p.flow_direction = 'import'
                     AND p.rate_uom = 'KWH'
                THEN ROUND(p.qty * 0.0511, 2) END AS loss
    FROM priced p
)
SELECT charge_scope,
       upper(flow_direction),
       component_desc,
       band_label,
       qty,
       rate_uom,
       unit_rate,
       loss,
       -- Trap 5: signed negative for export.
       ROUND((qty + COALESCE(loss, 0)) * unit_rate, 2)
           * CASE WHEN flow_direction = 'export' THEN -1 ELSE 1 END,
       (CASE charge_scope WHEN 'NET' THEN 100 WHEN 'ENE' THEN 200 ELSE 300 END
        + CASE WHEN flow_direction = 'export' THEN 50 ELSE 0 END)::int
FROM withloss
WHERE qty <> 0
ORDER BY 10, 3;
$$;


-- =====================================================================
-- Drive the rating engine over the generated cohort.
--
-- Note the INVERSION relative to the nine fixtures. Those rate backwards:
-- generate_scenarios.py computes kea.billing.bill_line and then back-fills
-- src_billing.invoice_lines to match. Here the source is authoritative --
-- lines are priced from metered half-hours and the catalogue, and the
-- header totals are derived from the lines. Nothing downstream is
-- consulted, because the warehouse does not exist yet.
--
-- Idempotent: generated invoices are deleted by ICP prefix first. The
-- nine fixture ICPs do not start '09', so they cannot be reached.
-- =====================================================================

BEGIN;

DELETE FROM src_billing.invoice_lines l
 USING src_billing.invoices i, src_billing.accounts a
 WHERE l.INV_ID = i.INV_ID AND i.ACCT_ID = a.ACCT_ID AND a.ICP_NUMBER LIKE '09%';
DELETE FROM src_billing.invoices i
 USING src_billing.accounts a
 WHERE i.ACCT_ID = a.ACCT_ID AND a.ICP_NUMBER LIKE '09%';

-- Identity sequences still sit below the fixtures' hand-assigned ids
-- unless this runs. See 01_schemas.sql.
SELECT gen_audit.sync_sequences();

INSERT INTO src_billing.customers
    (CUST_NO, CUST_NAME, EMAIL_ADDR, PHONE_NO, ADDR_LINE1, ADDR_LINE2,
     ADDR_SUBURB, ADDR_CITY, ADDR_POSTCODE, MED_DEPENDENT, VULNERABLE_FLAG)
SELECT cust_no, cust_name, NULLIF(email_addr, ''), phone_no, addr_line1,
       NULLIF(addr_line2, ''), addr_suburb, addr_city, addr_postcode,
       med_dependent, vulnerable_flag
FROM gen_audit.stg_customers
ON CONFLICT (CUST_NO) DO UPDATE SET
    CUST_NAME = EXCLUDED.CUST_NAME, EMAIL_ADDR = EXCLUDED.EMAIL_ADDR,
    MED_DEPENDENT = EXCLUDED.MED_DEPENDENT,
    VULNERABLE_FLAG = EXCLUDED.VULNERABLE_FLAG, LAST_MOD_TS = now();

INSERT INTO src_billing.accounts
    (ACCT_NO, CUST_ID, ICP_NUMBER, PLAN_CODE, PAY_METHOD, BILL_CYCLE_DAY, START_DT)
SELECT s.acct_no, c.CUST_ID, s.icp_number, s.plan_code, s.pay_method,
       s.bill_cycle_day, s.start_dt
FROM gen_audit.stg_accounts s
JOIN src_billing.customers c ON c.CUST_NO = s.cust_no
ON CONFLICT (ACCT_NO) DO UPDATE SET
    ICP_NUMBER = EXCLUDED.ICP_NUMBER, PLAN_CODE = EXCLUDED.PLAN_CODE,
    PAY_METHOD = EXCLUDED.PAY_METHOD, LAST_MOD_TS = now();

-- Headers first, with zero money: the totals are an aggregate of lines
-- that do not exist yet.
INSERT INTO src_billing.invoices
    (INV_NO, ACCT_ID, PLAN_CODE, ISSUE_DT, PERIOD_FROM, PERIOD_TO,
     CHARGES_EXGST, GST_AMT, TOTAL_AMT, DUE_DT)
SELECT p.inv_no, a.ACCT_ID, p.plan_code, p.issue_dt, p.period_from, p.period_to,
       0, 0, 0, p.due_dt
FROM gen_audit.stg_invoice_periods p
JOIN src_billing.accounts a ON a.ACCT_NO = p.acct_no
ON CONFLICT (INV_NO) DO NOTHING;

INSERT INTO src_billing.invoice_lines
    (INV_ID, CHG_GROUP, FLOW_DIR, LINE_DESC, TOU_BAND, QTY, UOM,
     UNIT_RATE, LOSS_QTY, LINE_AMT, SORT_SEQ)
SELECT i.INV_ID, r.chg_group, r.flow_dir, r.line_desc, r.tou_band,
       r.qty, r.uom, r.unit_rate, r.loss_qty, r.line_amt, r.sort_seq
FROM src_billing.invoices i
JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
CROSS JOIN LATERAL gen_audit.rate_lines(a.ICP_NUMBER, i.PLAN_CODE,
                                        i.PERIOD_FROM::date, i.PERIOD_TO::date) r
WHERE a.ICP_NUMBER LIKE '09%';

-- Header totals from the lines. EXPORT_CREDIT is the same money as the
-- negative export lines, stated positive -- the layout the printed bill
-- uses, and the reason a naive sum of "credits" double-counts.
--
-- BOTH CTEs ARE SCOPED TO GENERATED ACCOUNTS. Aggregating every row of
-- invoice_lines instead would sweep the nine fixtures into this UPDATE
-- and rewrite their headers -- recomputing GST on Bill B, whose printed
-- total is GST-inclusive with GST_AMT 0.00, and destroying the $50.54
-- ground truth the whole reconciliation depends on. The DELETEs at the
-- top of this file are scoped by ICP prefix; so must these be.
WITH agg AS (
    SELECT l.INV_ID,
           SUM(l.LINE_AMT) AS charges,
           -SUM(l.LINE_AMT) FILTER (WHERE l.FLOW_DIR = 'EXPORT') AS credit
    FROM src_billing.invoice_lines l
    JOIN src_billing.invoices i ON i.INV_ID = l.INV_ID
    JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
    WHERE a.ICP_NUMBER LIKE '09%'
    GROUP BY l.INV_ID
),
-- Metered volume, in ONE pass. Collapsing to daily totals first turns
-- 11,506 range scans over 18.8 M reads into a single scan plus a join
-- over ~710k daily rows.
daily AS (
    SELECT v.icp_identifier,
           (v.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           r.flow_direction,
           SUM(v.kwh) AS kwh
    FROM (
        SELECT DISTINCT ON (icp_identifier, register_code, interval_start)
               icp_identifier, register_code, interval_start, kwh
        FROM src_metering.interval_reads
        WHERE source_file LIKE 'lcl-%'
        ORDER BY icp_identifier, register_code, interval_start, version_no DESC
    ) v
    JOIN src_metering.connection_points c ON c.icp_identifier = v.icp_identifier
    JOIN src_metering.meters m ON m.icp_id = c.icp_id
    JOIN src_metering.registers r ON r.meter_id = m.meter_id
                                 AND r.register_code = v.register_code
    GROUP BY 1, 2, 3
),
vol AS (
    SELECT i.INV_ID,
           SUM(d.kwh) FILTER (WHERE d.flow_direction = 'import') AS imp,
           SUM(d.kwh) FILTER (WHERE d.flow_direction = 'export') AS exp
    FROM src_billing.invoices i
    JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID AND a.ICP_NUMBER LIKE '09%'
    JOIN daily d ON d.icp_identifier = a.ICP_NUMBER
                AND d.d BETWEEN i.PERIOD_FROM::date AND i.PERIOD_TO::date
    GROUP BY i.INV_ID
)
UPDATE src_billing.invoices i
   SET CHARGES_EXGST = ROUND(agg.charges, 2),
       GST_AMT       = ROUND(agg.charges * i.GST_RATE, 2),
       TOTAL_AMT     = ROUND(agg.charges, 2) + ROUND(agg.charges * i.GST_RATE, 2),
       EXPORT_CREDIT = COALESCE(agg.credit, 0),
       TOTAL_IMPORT_KWH = ROUND(COALESCE(vol.imp, 0), 4),
       TOTAL_EXPORT_KWH = ROUND(COALESCE(vol.exp, 0), 4),
       LAST_MOD_TS = now()
  FROM agg LEFT JOIN vol ON vol.INV_ID = agg.INV_ID
 WHERE i.INV_ID = agg.INV_ID;

-- A period with no reads produces no lines and therefore no money. Drop
-- it rather than leave a $0.00 invoice that nothing explains.
DELETE FROM src_billing.invoices i
 USING src_billing.accounts a
 WHERE i.ACCT_ID = a.ACCT_ID AND a.ICP_NUMBER LIKE '09%'
   AND NOT EXISTS (SELECT 1 FROM src_billing.invoice_lines l WHERE l.INV_ID = i.INV_ID);

COMMIT;
