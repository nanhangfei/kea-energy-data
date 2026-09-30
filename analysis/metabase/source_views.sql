-- =====================================================================
-- Summary views for the Stage 2 source-data dashboard.
--
-- src_metering.interval_reads holds millions of rows even in the compact
-- profile. A dashboard whose every
-- panel re-aggregates that takes a minute to load and hammers the
-- database on each filter change, so the rollups are materialised once
-- and the panels read them.
--
-- They live in gen_audit deliberately. These are ANALYSIS artefacts, not
-- pipeline surface: gen_audit has no grant to any kea_extract_* role, so
-- nothing here can leak into bronze and be mistaken for source data.
--
-- Refreshed by `make dashboards` (ONLY=source for just this). Re-run after regenerating.
-- =====================================================================

-- Latest version per interval, rolled up to the local day.
-- Revisions coexist with the rows they supersede, so DISTINCT ON is not
-- optional -- summing raw rows double-counts every corrected day.
DROP MATERIALIZED VIEW IF EXISTS gen_audit.mv_daily CASCADE;
CREATE MATERIALIZED VIEW gen_audit.mv_daily AS
SELECT r.icp_identifier,
       (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS nz_date,
       r.register_code,
       ROUND(SUM(r.kwh), 4)                                  AS kwh,
       COUNT(*)                                              AS intervals,
       COUNT(*) FILTER (WHERE r.read_status = 'E')           AS estimated,
       COUNT(*) FILTER (WHERE r.read_status = 'F')           AS final_reads,
       MIN(r.received_at)                                    AS first_received,
       MAX(r.version_no)                                     AS max_version
FROM  (SELECT DISTINCT ON (icp_identifier, register_code, interval_start)
              icp_identifier, register_code, interval_start, kwh,
              read_status, received_at, version_no
       FROM   src_metering.interval_reads
       WHERE  source_file LIKE 'lcl%'
       ORDER  BY icp_identifier, register_code, interval_start, version_no DESC) r
GROUP BY 1, 2, 3;

CREATE INDEX ix_mvdaily ON gen_audit.mv_daily(icp_identifier, nz_date);
CREATE INDEX ix_mvdaily_date ON gen_audit.mv_daily(nz_date);

-- Average half-hourly shape by month. 12 x 48 x 2 rows.
-- The local wall clock is what the shape means; UTC would smear the
-- evening peak by an hour for half the year.
DROP MATERIALIZED VIEW IF EXISTS gen_audit.mv_profile CASCADE;
CREATE MATERIALIZED VIEW gen_audit.mv_profile AS
SELECT EXTRACT(month FROM loc)::int                                   AS nz_month,
       (EXTRACT(hour FROM loc) * 2 + EXTRACT(minute FROM loc) / 30)::int AS half_hour,
       register_code,
       ROUND(AVG(kwh), 4) AS avg_kwh
FROM  (SELECT (interval_start AT TIME ZONE 'Pacific/Auckland') AS loc,
              register_code, kwh
       FROM   src_metering.interval_reads
       WHERE  source_file LIKE 'lcl%' AND version_no = 1) t
GROUP BY 1, 2, 3;

-- One row per invoice, with the previous period alongside it. This is
-- the bill-shock view: the whole project is about explaining the delta.
DROP MATERIALIZED VIEW IF EXISTS gen_audit.mv_bills CASCADE;
CREATE MATERIALIZED VIEW gen_audit.mv_bills AS
SELECT a.ICP_NUMBER                                       AS icp_identifier,
       split_part(a.PLAN_CODE, '_', 2)                    AS plan,
       a.PLAN_CODE,
       i.INV_NO, i.PERIOD_FROM::date                      AS period_from,
       i.PERIOD_TO::date                                  AS period_to,
       (i.PERIOD_TO::date - i.PERIOD_FROM::date) + 1      AS period_days,
       i.TOTAL_AMT, i.CHARGES_EXGST, i.GST_AMT,
       i.TOTAL_IMPORT_KWH, i.TOTAL_EXPORT_KWH, i.EXPORT_CREDIT,
       LAG(i.TOTAL_AMT)        OVER w                     AS prev_amt,
       LAG(i.TOTAL_IMPORT_KWH) OVER w                     AS prev_kwh,
       LAG((i.PERIOD_TO::date - i.PERIOD_FROM::date) + 1) OVER w AS prev_days
FROM   src_billing.invoices i
JOIN   src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
WHERE  a.ICP_NUMBER LIKE '09%'
WINDOW w AS (PARTITION BY a.ACCT_ID ORDER BY i.PERIOD_FROM);

CREATE INDEX ix_mvbills ON gen_audit.mv_bills(icp_identifier, period_from);

ANALYZE gen_audit.mv_daily;
ANALYZE gen_audit.mv_profile;
ANALYZE gen_audit.mv_bills;

DO $$
BEGIN
    RAISE NOTICE 'mv_daily % rows, mv_profile % rows, mv_bills % rows',
        (SELECT count(*) FROM gen_audit.mv_daily),
        (SELECT count(*) FROM gen_audit.mv_profile),
        (SELECT count(*) FROM gen_audit.mv_bills);
END $$;
