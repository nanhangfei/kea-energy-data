-- The published cohort as each feature sees it. Read by measure_cohort_designs.py
-- --serving, which runs each block in a READ ONLY transaction and rolls it back.
-- One block per "-- @<title>" line. Aggregates only: generated accounts are
-- account_id >= 1,000,000, so the nine fixtures never count.

-- @Accounts on supply, by plan and panels
SELECT plan_code, has_solar, count(*) AS accounts,
       sum(is_vulnerable::int) AS vulnerable, sum(is_medically_dependent::int) AS medical
  FROM kea.mart_account_context WHERE account_id >= 1000000
 GROUP BY 1, 2 ORDER BY 1, 2;

-- @Feature 1: bills, and the ones the explanation may present
SELECT count(*) AS bills, count(DISTINCT account_id) AS accounts,
       sum(is_explainable::int) AS explainable,
       sum((is_explainable AND amount_delta_pct >= 10)::int) AS explainable_rises_10pct,
       count(DISTINCT account_id) FILTER (WHERE is_explainable AND amount_delta_pct >= 10) AS accounts_with_one
  FROM kea.mart_bill_explanation WHERE account_id >= 1000000;

-- @Feature 1: the largest driver of each explainable rise of 10% or more
WITH d AS (
    SELECT unnest(ARRAY['period length', 'weather', 'base load', 'tariff', 'detected loads', 'unexplained']) AS driver,
           unnest(ARRAY[delta_from_period_length, delta_from_weather, delta_from_base_load, delta_from_tariff,
                        delta_from_detected_loads, delta_unexplained]) AS delta, bill_id
      FROM kea.mart_bill_explanation
     WHERE account_id >= 1000000 AND is_explainable AND amount_delta_pct >= 10)
SELECT driver, count(*) AS bills FROM (
    SELECT DISTINCT ON (bill_id) bill_id, driver FROM d ORDER BY bill_id, abs(coalesce(delta, 0)) DESC) x
 GROUP BY 1 ORDER BY 2 DESC;

-- @Feature 2: the latest recommendation generation
SELECT count(DISTINCT r.account_id) AS accounts,
       count(DISTINCT r.account_id) FILTER (WHERE r.is_recommended AND r.annual_saving > 0) AS told_to_switch,
       string_agg(DISTINCT p.plan_code, ' ') FILTER (WHERE r.is_recommended AND r.annual_saving > 0) AS switch_to
  FROM kea.plan_recommendation r
  LEFT JOIN (SELECT DISTINCT plan_id, plan_code FROM kea.mart_account_context) p ON p.plan_id = r.candidate_plan_id
 WHERE r.account_id >= 1000000 AND r.computed_at = (SELECT max(computed_at) FROM kea.plan_recommendation);

-- @Feature 3: forecast coverage
SELECT count(*) AS snapshots, sum(is_forecastable::int) AS forecastable,
       count(DISTINCT account_id) AS accounts,
       count(DISTINCT account_id) FILTER (WHERE is_forecastable) AS accounts_ever_forecastable
  FROM kea.mart_forecast_input WHERE account_id >= 1000000;

-- @Feature 3: forecasts published, and the ones flagged unusually high
SELECT model_version, count(*) AS forecasts, count(DISTINCT account_id) AS accounts,
       sum(is_anomalous::int) AS anomalous, count(DISTINCT account_id) FILTER (WHERE is_anomalous) AS accounts_anomalous
  FROM kea.bill_forecast WHERE account_id >= 1000000 GROUP BY 1;

-- @Feature 4 and S3: signals on each account's latest assessment
WITH l AS (SELECT DISTINCT ON (account_id) * FROM kea.hardship_assessment
            WHERE account_id >= 1000000 ORDER BY account_id, assessed_on DESC, computed_at DESC)
SELECT s.signal_code, count(DISTINCT l.account_id) AS accounts
  FROM l JOIN kea.hardship_signal s USING (assessment_id) GROUP BY 1 ORDER BY 2 DESC;

-- @S3: accounts with a bill that could start a slide (>= 130% of the median of the six before, three bills after)
WITH b AS (SELECT account_id, total_charges AS amt,
                  row_number() OVER (PARTITION BY account_id ORDER BY period_end) AS seq,
                  count(*) OVER (PARTITION BY account_id) AS n
             FROM billing.bill WHERE account_id >= 1000000),
     u AS (SELECT b.*, (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY e.amt) FROM b e
                         WHERE e.account_id = b.account_id AND e.seq BETWEEN b.seq - 6 AND b.seq - 1) AS usual
             FROM b WHERE seq >= 7)
SELECT count(DISTINCT account_id) FILTER (WHERE amt >= 1.3 * usual AND seq <= n - 3) AS accounts_with_trigger,
       count(DISTINCT account_id) AS accounts_with_seven_bills
  FROM u;

-- @S1: generated ICPs with a detected load, by class and confidence
SELECT 'ev' AS class, ev_confidence AS confidence, count(DISTINCT d.icp_id) AS icps
  FROM kea.mart_daily_detected_load d JOIN kea.mart_account_context c USING (icp_id)
 WHERE c.account_id >= 1000000 AND d.ev_half_hours > 0 GROUP BY 1, 2
UNION ALL
SELECT 'timed_night', timed_night_confidence, count(DISTINCT d.icp_id)
  FROM kea.mart_daily_detected_load d JOIN kea.mart_account_context c USING (icp_id)
 WHERE c.account_id >= 1000000 AND d.timed_night_half_hours > 0 GROUP BY 1, 2
 ORDER BY 1, 2;

-- @S2: solar estimates, by system and battery
SELECT system_kw, coalesce(battery_kwh, 0) AS battery_kwh, count(DISTINCT account_id) AS accounts,
       round(min(payback_years_low), 1) AS payback_low, round(max(payback_years_high), 1) AS payback_high
  FROM kea.solar_estimate WHERE account_id >= 1000000 GROUP BY 1, 2 ORDER BY 1, 2;

-- @S2: EV what-ifs
SELECT scenario, count(DISTINCT account_id) AS accounts,
       round(min(saving_vs_baseline_nzd)) AS saving_min,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY saving_vs_baseline_nzd)) AS saving_median,
       round(max(saving_vs_baseline_nzd)) AS saving_max
  FROM kea.ev_scenario WHERE account_id >= 1000000 GROUP BY 1 ORDER BY 1;
