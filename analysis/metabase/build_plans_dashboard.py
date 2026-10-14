#!/usr/bin/env python3
"""Build the Sprint 3 decision dashboard in Metabase, with Sprint 4's staff queue.

    make dashboards ONLY=plans

WHAT IS NEW HERE, AND WHY IT COULD NOT EXIST BEFORE.

Every earlier dashboard plots something the pipeline OBSERVED -- readings that
arrived, bills that were issued, rows that crossed from the warehouse. This one
plots something the pipeline DECIDED. `kea.plan_recommendation` contains
counterfactual plan costs; `kea.hardship_assessment` records whether each
account was assessed and why it was flagged; `kea.alert` records the resulting
outreach queue without sending anything during a demo.

That difference sets what the dashboard has to do. A load can be checked
against its source; a recommendation cannot, because the counterfactual never
happened. So half of these panels do not show the recommendations at all --
they show the reasons to doubt them. `How close is second place` and
`Modelled cost against what was actually billed` are the two that decide
whether the top half means anything.

⚠️ EVERY QUERY PINS ONE GENERATION. `kea.plan_recommendation` is append-only:
each run inserts a fresh set of rows stamped with one `computed_at` and deletes
nothing. A panel that forgets the pin does not error and does not look wrong --
it multiplies every count and every dollar as further generations arrive.
Hence `WHERE computed_at = (SELECT max(computed_at) ...)` in every panel below,
repeated rather than shared because a Metabase native question is one statement
with nothing to share a CTE with.

**This file is the record.** A Metabase question lives in Metabase's own
application database: not in git, not reviewable in a PR, and gone after a
`docker compose down -v`. The queries below are the version-controlled source.

Idempotent: questions and the dashboard are matched by name and updated in
place, so the URL stays stable.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

MB = "http://localhost:3002"
DB_NAME = "Kea serving"
COLLECTION = "Kea Energy"
SUBCOLLECTION = "Sprint 3 — plans and recommendations"
DASHBOARD = "Plan analysis & recommendation"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

INTRO = """### Plan analysis & recommendation — start with the bills, then zoom out

The first section answers the customer question directly: **what is wrong with
my current plan, and what would the recommended plan have charged for the same
three real bill periods?** It deliberately shows two different kinds of move,
so this is evidence of plan fit rather than one unusually favourable household.

For each completed invoice, the actual issued amount is shown as a calibration
point. The two comparable figures are the **current-plan re-rate** and the
**recommended-plan re-rate**: both use exactly the same half-hour readings,
days, current catalogue rates and pricing engine. The usage saving is their
difference. A monthly term credit is shown separately because it is conditional
on staying for the selected term.

Each published release re-rates every generated household against the three
Standard User plan families available on Vector, over a year of its own
half-hourly readings, using the same
pricing engine the app uses — `pipeline/lib/kea_pipeline/tariff.py`, a copy
of the app's own, kept identical so the batch job and the app never disagree
about what a plan costs.

**Everything here is a counterfactual.** No household has switched; nothing on
this page can be checked against an outcome, because the outcome never
happened. That is why the bottom two sections exist, and why they are not an
appendix: *How close is second place* and *Modelled against actually billed*
are what decide whether the top half means anything.

⚠️ **Every panel pins one generation.** The table is append-only — each run
inserts a fresh set stamped `computed_at` and deletes nothing. A panel missing
the pin doesn't error; it silently doubles every count and every dollar.

⚠️ **The dollar figures are GST-inclusive.** The catalogue rates are stored
GST-exclusive, but the recommendation job grosses its calculated amounts up
before it writes them here. That makes every displayed saving comparable with a
customer's bill and with the advertised term credits.

⚠️ **A term credit is conditional.** Rankings include the credit available for
honouring a selected 6- or 12-month term, while the move recommendation itself
still requires an unconditional usage saving. The tables below keep those two
amounts separate.

The final sections show the other Sprint 3 decisions: explainable hardship
signals and the pending in-app outreach they created. Those panels pin the
latest assessment generation for the same append-only reason.

**Sprint 4 adds warning signs** that appear before arrears: payment drift, a
bill well above the usual payment, and a first shortfall. *Flagged* keeps its
Sprint 3 meaning — at least one of the five original signals. A large bill
together with drift or a shortfall is an **early warning**, which also gets an
outreach; any other warning sign on its own is shown to staff and creates
none. Every signal carries a staff-only explanation, and an early warning
carries the projected arrears date and the arithmetic behind it. Nothing on
this page is sent to a customer."""


def bar(dim, *metrics, stack=None, series=None):
    v = {"graph.dimensions": [dim] + ([series] if series else []),
         "graph.metrics": list(metrics)}
    if stack:
        v["stackable.stack_type"] = stack
    return v


# Axis assignments are declared, never guessed. verify.py enforces the harder
# half: every column a query returns must be a dimension or a metric, because a
# column Metabase does not know about is silently dropped from the chart and
# the result still renders.
LAYOUT = {
    "Why the current plan is a poor fit — two real households": {"w": 24, "h": 7},
    "Same usage, different plan — three real bills each": {"w": 24, "h": 10},
    "Where the three-bill saving comes from": {
        "viz": bar("case / cost component", "three-bill cost $", series="plan"),
        "w": 24, "h": 9},
    "How much is on the table": {"viz": bar("annual saving", "households"), "w": 12, "h": 7},
    "Annual saving by current plan family": {
        "viz": bar("current plan", "total annual saving $"), "w": 12, "h": 7},
    "Which plan comes out cheapest": {"viz": bar("plan", "households"), "w": 24, "h": 7},
    "How close is second place": {"viz": bar("gap to the runner-up", "households"),
                                  "w": 12, "h": 7},
    "Latest hardship run": {"w": 12, "h": 8},
    "Hardship signals by reason": {"viz": bar("signal", "households"),
                                    "w": 12, "h": 8},
    "Signal evidence — for staff only": {"w": 24, "h": 9},
    "Open outreach — flagged and early warning": {"w": 24, "h": 8},
    "Hardship alert delivery state": {"viz": bar("status", "alerts"),
                                       "w": 12, "h": 7},
}

# ⚠️ Repeated in every query below rather than factored out. A Metabase native
# question is a single statement executed on its own; there is no place to put
# a shared CTE. Copying it is the cost of the panel being self-contained, and
# the alternative -- a view -- would put the pin somewhere a reader of the
# panel cannot see it.
LATEST = "(SELECT max(computed_at) FROM kea.plan_recommendation)"
LATEST_HARDSHIP = "(SELECT max(computed_at) FROM kea.hardship_assessment)"

# The outreach rule of pipeline/scripts/decisions/create_hardship_alerts.py (rule v2),
# restated in SQL because a panel cannot import it. A test in
# pipeline/tests/test_hardship_alerts.py keeps these lists equal to the job's.
#   flagged             at least one v1 code; an outreach, as in Sprint 3
#   early warning       no v1 code, a bill well above the usual payment, and
#                       payment drift or a first shortfall; an early outreach
#   warning signs only  any other signal; shown to staff, no outreach
HARDSHIP_V1_CODES = ("direct_debit_failed", "late_payment_streak",
                     "part_payment_arrears", "payments_stopped", "usage_collapse")
HARDSHIP_EARLY_SPIKE = "bill_above_usual_payment"
HARDSHIP_EARLY_WITH = ("first_shortfall", "payment_drift")


def _text_array(codes) -> str:
    return "ARRAY[" + ", ".join(f"'{c}'" for c in codes) + "]"


def hardship_codes(assessment_id: str) -> str:
    """One assessment's signal codes, as a sorted text[]."""
    return ("array(SELECT s.signal_code::text FROM kea.hardship_signal s "
            f"WHERE s.assessment_id = {assessment_id} ORDER BY 1)")


def hardship_kind(codes: str) -> str:
    """flagged / early warning / warning signs only / clear, from a text[]."""
    return (f"CASE WHEN {codes} && {_text_array(HARDSHIP_V1_CODES)} THEN 'flagged' "
            f"WHEN '{HARDSHIP_EARLY_SPIKE}' = ANY({codes}) "
            f"AND {codes} && {_text_array(HARDSHIP_EARLY_WITH)} THEN 'early warning' "
            f"WHEN cardinality({codes}) > 0 THEN 'warning signs only' "
            "ELSE 'clear' END")


OPEN_OUTREACH = """x.kind = 'hardship_outreach' AND x.acted_at IS NULL
  AND x.status IN ('pending', 'sent', 'delivered', 'suppressed')"""

# One representative from each distinct from-plan -> to-plan movement, then
# the two strongest movements by unconditional annual saving.  It stays data
# driven: rebuilding a different compact cohort can change the accounts without
# requiring anyone to edit an account number in Metabase.
CASE_CTE = f"""
latest_recommendations AS (
    SELECT r.*,
           row_number() OVER (
               PARTITION BY r.current_plan_id, r.candidate_plan_id
               ORDER BY r.usage_saving DESC, r.account_id
           ) AS movement_example_rank
    FROM kea.plan_recommendation r
    WHERE r.computed_at = {LATEST} AND r.is_recommended
), strongest_movements AS (
    SELECT *
    FROM latest_recommendations
    WHERE movement_example_rank = 1
    ORDER BY usage_saving DESC, account_id
    LIMIT 2
), cases AS (
    SELECT row_number() OVER (ORDER BY usage_saving DESC, account_id) AS case_no,
           *
    FROM strongest_movements
)
"""

SECTIONS = [
    ("Start here — concrete customer evidence", [
        ("Why the current plan is a poor fit — two real households", "table", 24, f"""
-- Two different plan movements, not two cherry-picked months.  Discount-window
-- shares are measured from the same three completed bill periods shown next.
WITH {CASE_CTE}, evidence AS (
    SELECT e.account_id,
           sum(e.import_kwh) AS import_kwh,
           sum(e.current_discount_kwh) AS current_discount_kwh,
           sum(e.candidate_discount_kwh) AS candidate_discount_kwh,
           sum(e.current_energy_amount) AS current_energy_amount,
           sum(e.candidate_energy_amount) AS candidate_energy_amount
    FROM kea.plan_bill_example e
    WHERE e.computed_at = {LATEST}
    GROUP BY e.account_id
)
SELECT 'Case ' || c.case_no                                           AS "case",
       a.account_number                                               AS "account_number",
       customer.full_name                                             AS "full_name",
       customer.email                                                 AS "email",
       regexp_replace(cur.plan_name, ' \\(.*$', '')                    AS "current plan",
       CASE
         WHEN e.current_discount_kwh = 0 THEN
           'No free/half-price window on the current plan; '
           || round(100 * e.candidate_discount_kwh / nullif(e.import_kwh, 0), 1)
           || '% of this usage would receive one on the recommended plan. '
           || 'Energy charges on these bills fall from $'
           || round(e.current_energy_amount) || ' to $'
           || round(e.candidate_energy_amount) || '.'
         WHEN e.candidate_discount_kwh <= e.current_discount_kwh THEN
           round(100 * e.current_discount_kwh / nullif(e.import_kwh, 0), 1)
           || '% hits the current free/half-price window, but the remaining '
           || round(100 * (e.import_kwh - e.current_discount_kwh)
                    / nullif(e.import_kwh, 0), 1)
           || '% is expensive enough that energy charges total $'
           || round(e.current_energy_amount) || '. The recommended plan has '
           || round(100 * e.candidate_discount_kwh / nullif(e.import_kwh, 0), 1)
           || '% discounted and costs only $'
           || round(e.candidate_energy_amount) || ' for the same usage.'
         ELSE
           'Only ' || round(100 * e.current_discount_kwh / nullif(e.import_kwh, 0), 1)
           || '% of usage hits the current discount window, versus '
           || round(100 * e.candidate_discount_kwh / nullif(e.import_kwh, 0), 1)
           || '% on the recommended plan. Energy charges on these bills fall from $'
           || round(e.current_energy_amount) || ' to $'
           || round(e.candidate_energy_amount) || '.'
       END                                                            AS "what is wrong with the fit",
       regexp_replace(cand.plan_name, ' \\(.*$', '')                   AS "recommended plan",
       round(c.current_annual_cost)                                    AS "current annual cost $",
       round(c.candidate_annual_cost)                                  AS "recommended annual cost $",
       round(c.usage_saving)                                           AS "usage saving $ / year",
       round(c.term_credit)                                            AS "conditional term credit $",
       c.rationale                                                     AS "why"
FROM cases c
JOIN billing.account a ON a.account_id = c.account_id
JOIN billing.customer customer ON customer.customer_id = a.customer_id
JOIN catalogue.plan cur ON cur.plan_id = c.current_plan_id
JOIN catalogue.plan cand ON cand.plan_id = c.candidate_plan_id
JOIN evidence e ON e.account_id = c.account_id
ORDER BY c.case_no
"""),
        ("Same usage, different plan — three real bills each", "table", 24, f"""
-- `actual issued` proves these are real bill periods.  Do not subtract the
-- candidate from it: historical rounding/version/discount effects belong to
-- the bill.  The two re-rate columns are the fair same-engine comparison.
WITH {CASE_CTE}
SELECT 'Case ' || c.case_no                                           AS "case",
       a.account_number                                               AS "account",
       to_char(e.period_start, 'DD Mon YYYY') || ' - ' ||
       to_char(e.period_end, 'DD Mon YYYY')                            AS "real bill period",
       round(e.import_kwh)                                            AS "usage kWh",
       e.actual_bill_amount                                           AS "actual issued $",
       e.current_modelled_amount                                      AS "current plan re-rate $",
       e.candidate_modelled_amount                                    AS "recommended plan re-rate $",
       e.usage_saving                                                 AS "saving on this bill $",
       round(100 * e.candidate_discount_kwh / nullif(e.import_kwh, 0), 1)
                                                                       AS "usage in new discount window %",
       e.monthly_credit                                                AS "conditional credit $ / month",
       e.candidate_modelled_amount - e.monthly_credit                  AS "new bill after credit $"
FROM cases c
JOIN billing.account a ON a.account_id = c.account_id
JOIN kea.plan_bill_example e
  ON e.account_id = c.account_id AND e.computed_at = c.computed_at
ORDER BY c.case_no, e.period_end DESC
"""),
        ("Where the three-bill saving comes from", "bar", 24, f"""
-- The same three invoices split by charge group.  Network and fixed/other
-- charges often barely move; the energy bar makes the time-of-use fit visible.
WITH {CASE_CTE}, component_cost AS (
    SELECT c.case_no, a.account_number, v.component, v.plan, sum(v.amount) AS amount
    FROM cases c
    JOIN billing.account a ON a.account_id = c.account_id
    JOIN kea.plan_bill_example e
      ON e.account_id = c.account_id AND e.computed_at = c.computed_at
    CROSS JOIN LATERAL (VALUES
        ('energy', 'current', e.current_energy_amount),
        ('energy', 'recommended', e.candidate_energy_amount),
        ('network', 'current', e.current_network_amount),
        ('network', 'recommended', e.candidate_network_amount),
        ('other / fixed', 'current', e.current_other_amount),
        ('other / fixed', 'recommended', e.candidate_other_amount)
    ) v(component, plan, amount)
    GROUP BY c.case_no, a.account_number, v.component, v.plan
)
SELECT 'Case ' || case_no || ' / ' || component                     AS "case / cost component",
       plan                                                          AS "plan",
       round(amount)                                                 AS "three-bill cost $"
FROM component_cost
ORDER BY case_no,
         CASE component WHEN 'energy' THEN 1 WHEN 'network' THEN 2 ELSE 3 END,
         plan
"""),
    ]),
    ("What the recommender found", [
        ("Recommendation run totals", "table", 24, f"""
-- One row. Counted from the recommendations themselves rather than from the
-- job's own log, for the same reason Stage 5 counts the tables and not
-- mart_load_log: a job records what it BELIEVES it did.
--
-- "households moved" is smaller than "households compared" on purpose. A
-- household whose current plan is already the cheapest gets a full set of
-- priced candidates and no recommendation -- that is a result, not a gap.
SELECT count(DISTINCT account_id)                                   AS "households compared",
       count(*)                                                     AS "tariff / term offers priced",
       count(*) FILTER (WHERE is_recommended)                       AS "households moved",
       to_char(sum(usage_saving) FILTER (WHERE is_recommended),
               'FM$999,999,999')                                    AS "unconditional annual saving",
       to_char(sum(term_credit) FILTER (WHERE is_recommended),
               'FM$999,999,999')                                    AS "conditional term credit",
       to_char(max(computed_at) AT TIME ZONE 'Pacific/Auckland',
               'YYYY-MM-DD HH24:MI')                                AS "generation (NZ time)"
FROM   kea.plan_recommendation
WHERE  computed_at = {LATEST}
"""),
        ("How much is on the table", "bar", 12, f"""
-- The best plan for each household, bucketed by what it would save.
--
-- The first bucket is the honest one: some households are already on their
-- cheapest eligible plan. A recommender with no such bucket is not being asked
-- hard enough.
--
-- Ordered by the bucket's own minimum saving rather than alphabetically, and
-- the sort key is deliberately NOT selected: a third column would be dropped
-- from the chart without an error. That is what verify.py checks.
SELECT CASE WHEN usage_saving <= 0      THEN 'no cheaper plan'
            WHEN usage_saving <  100    THEN 'under $100'
            WHEN usage_saving <  250    THEN '$100 - $250'
            WHEN usage_saving <  500    THEN '$250 - $500'
            ELSE                             '$500 or more'
       END                    AS "annual saving",
       count(*)               AS "households"
FROM   kea.plan_recommendation
WHERE  computed_at = {LATEST} AND rank = 1
GROUP  BY 1
ORDER  BY min(usage_saving)
"""),
        ("Annual saving by current plan family", "bar", 12, f"""
-- The generated cohort now has one lines company and one user category, so a
-- network split would be a single uninformative bar. This shows which current
-- plan families contribute the switch opportunity instead.
SELECT CASE split_part(cur.plan_code, '_', 2)
           WHEN 'ANY'  THEN 'Anytime Rate'
           WHEN 'TOU'  THEN 'Time of Use'
           WHEN 'WKND' THEN 'Free Weekends'
       END                                                AS "current plan",
       round(sum(r.usage_saving))                         AS "total annual saving $"
FROM   kea.plan_recommendation r
JOIN   catalogue.plan cur ON cur.plan_id = r.current_plan_id
WHERE  r.computed_at = {LATEST} AND r.is_recommended
GROUP  BY 1
ORDER  BY 2 DESC
"""),
    ]),

    ("Which plans win", [
        ("Which plan comes out cheapest", "bar", 24, f"""
-- Counted on rank = 1, so every household votes exactly once -- including the
-- households whose best offer is their current tariff. The term is part of
-- the offer: the same tariff on Flexible and on twelve months can rank
-- differently because the latter earns a conditional credit. The chart rolls
-- that winning offer back up to the three customer-facing plan families; term
-- is shown separately in the movement table below.
--
-- If one plan family dominates, read that as a question rather than a result:
-- it is what you would expect
-- either from a genuinely well-priced weekend plan, or from a cohort whose
-- households all have the same weekday/weekend shape. The second is worth
-- ruling out before any of this reaches a customer -- LCL is real metering, so
-- the variation is real, but "real" is not the same as "varied enough to
-- discriminate between plans".
SELECT CASE split_part(p.plan_code, '_', 2)
           WHEN 'ANY'  THEN 'Anytime Rate'
           WHEN 'TOU'  THEN 'Time of Use'
           WHEN 'WKND' THEN 'Free Weekends'
       END                             AS "plan",
       count(*)                        AS "households"
FROM   kea.plan_recommendation r
JOIN   catalogue.plan p ON p.plan_id = r.candidate_plan_id
WHERE  r.computed_at = {LATEST} AND r.rank = 1
GROUP  BY 1
ORDER  BY 2 DESC
"""),
        ("Where households would move", "table", 24, f"""
-- From-plan to to-plan, for the moves the job actually recommends.
--
-- The diagonal is absent by construction: is_recommended requires the
-- candidate to differ from the current plan AND to save money, so a household
-- already on its cheapest plan produces no row here at all.
SELECT CASE split_part(cur.plan_code, '_', 2)
           WHEN 'ANY'  THEN 'Anytime Rate'
           WHEN 'TOU'  THEN 'Time of Use'
           WHEN 'WKND' THEN 'Free Weekends'
       END                                 AS "currently on",
       CASE split_part(cand.plan_code, '_', 2)
           WHEN 'ANY'  THEN 'Anytime Rate'
           WHEN 'TOU'  THEN 'Time of Use'
           WHEN 'WKND' THEN 'Free Weekends'
       END                                 AS "would move to",
       CASE r.term_months WHEN 0 THEN 'Flexible'
            ELSE r.term_months || ' months' END
                                             AS "term",
       count(*)                            AS "households",
       to_char(round(avg(r.usage_saving)), 'FM$999,999') AS "average saving each",
       to_char(round(sum(r.usage_saving)), 'FM$999,999') AS "total"
FROM   kea.plan_recommendation r
JOIN   catalogue.plan cur  ON cur.plan_id  = r.current_plan_id
JOIN   catalogue.plan cand ON cand.plan_id = r.candidate_plan_id
WHERE  r.computed_at = {LATEST} AND r.is_recommended
GROUP  BY 1, 2, 3
ORDER  BY 4 DESC
"""),
    ]),

    ("How far to trust it", [
        ("How close is second place", "bar", 12, f"""
-- THE PANEL THAT KEEPS THE FEATURE HONEST. For each household, the gap in
-- total offer between the cheapest candidate and the next cheapest. This is
-- annual usage cost less the conditional credit for honouring the chosen term.
--
-- A gap inside $50 a year is under a dollar a week. Within that band "the
-- best plan" is a claim the modelling cannot support:
-- the line-loss factor alone moves a plan's energy charge by 5%, and next
-- winter's weather will move a household's shape by more than the gap. The
-- honest presentation for those is two plans and a note, not a winner.
--
-- Ordered by the bucket's own minimum gap; the sort key is not selected.
SELECT CASE WHEN gap <  20 THEN 'under $20'
            WHEN gap <  50 THEN '$20 - $50'
            WHEN gap < 150 THEN '$50 - $150'
            WHEN gap < 400 THEN '$150 - $400'
            ELSE                '$400 or more'
       END                  AS "gap to the runner-up",
       count(*)             AS "households"
FROM (
    SELECT account_id,
           max(annual_saving) FILTER (WHERE rank = 1)
         - max(annual_saving) FILTER (WHERE rank = 2) AS gap
    FROM   kea.plan_recommendation
    WHERE  computed_at = {LATEST} AND rank <= 2
    GROUP  BY 1
) g
WHERE  gap IS NOT NULL
GROUP  BY 1
ORDER  BY min(gap)
"""),
        ("What was not recommended, and why", "table", 12, f"""
-- The counterpart to Stage 5's "what the export did not carry". Accounts with
-- no recommendation remain visible so a deliberate exclusion can be
-- distinguished from a silent failure.
--
-- The nine fixed Sprint 2 fixtures predate the current pricing year. The
-- deliberately ambiguous-register account is also excluded before pricing.
-- Zero usable readings in the pricing window means
-- no half-hourly profile, and pricing a plan against no usage would produce a
-- daily-charge-only ranking that is confidently wrong.
SELECT n.network_name                                              AS "network",
       p.plan_code                                                 AS "current plan",
       rw.reads                                                    AS "reads in window",
       (SELECT count(*) FROM catalogue.plan c
         WHERE c.network_id = i.network_id AND c.user_type = p.user_type
           AND left(c.plan_code, 4) = 'KEA_' AND c.is_active)
                                                                    AS "eligible plans",
       coalesce(ctx.quarantine_reason,
                CASE WHEN rw.reads = 0 THEN 'no usable reads in the pricing year'
                     ELSE 'no eligible comparison set' END)         AS "reason"
FROM   billing.account a
JOIN   metering.icp i     ON i.icp_id = a.icp_id
JOIN   metering.network n ON n.network_id = i.network_id
JOIN   catalogue.plan p   ON p.plan_id = a.plan_id
LEFT JOIN kea.mart_account_context ctx ON ctx.account_id = a.account_id
CROSS JOIN LATERAL (
    SELECT count(*) AS reads
    FROM metering.meter_register g
    JOIN metering.meter_read_hh h ON h.register_id = g.register_id
    WHERE g.icp_id = a.icp_id
      AND h.interval_start >= (SELECT min(usage_period_start)
                                 FROM kea.plan_recommendation
                                WHERE computed_at = {LATEST})
) rw
WHERE  a.end_date IS NULL
  AND  NOT EXISTS (SELECT 1 FROM kea.plan_recommendation r
                    WHERE r.account_id = a.account_id
                      AND r.computed_at = {LATEST})
ORDER  BY 1, 2
"""),
        ("Modelled cost against what was actually billed", "table", 24, f"""
-- Does the engine that priced the candidates reproduce a bill it did not
-- write? Nothing else on this page can be checked against reality; this can.
--
-- ⚠️ AGAINST total_charges, NOT charges_excl_gst. Recommendation amounts are
-- customer-facing GST-inclusive dollars, so comparing them against the net
-- bill creates a 15% mismatch that looks like a pricing defect and is not one.
--
-- The residual is the honest bit: it is not zero, and it is not expected to be.
-- Billed totals carry mid-period plan versions, estimated reads and per-line
-- rounding that a clean re-rate does not.
WITH me AS (
    SELECT account_id, current_annual_cost, usage_period_start s, usage_period_end e
    FROM   kea.plan_recommendation
    WHERE  computed_at = {LATEST} AND rank = 1
), billed AS (
    SELECT b.account_id, sum(b.total_charges) amt, sum(b.period_days) d
    FROM   billing.bill b JOIN me ON me.account_id = b.account_id
    WHERE  b.period_start >= me.s AND b.period_end <= me.e
    GROUP  BY 1
)
SELECT count(*)                                                     AS "households",
       to_char(round(sum(me.current_annual_cost)), 'FM$999,999,999') AS "modelled, re-rated",
       to_char(round(sum(billed.amt * 365.0 / billed.d)),
               'FM$999,999,999')                                    AS "billed, annualised",
       round(100.0 * (sum(me.current_annual_cost)
                      - sum(billed.amt * 365.0 / billed.d))
             / sum(billed.amt * 365.0 / billed.d), 2)               AS "difference %"
FROM   me JOIN billed USING (account_id)
"""),
        ("What these numbers are not", "table", 24, """
-- Three known gaps, each shown as the count that proves it rather than as a
-- sentence a reader has to take on trust. Every one of them is a number that
-- should change, so a panel that reads 0 today is a panel that will announce
-- its own fix.
SELECT 'term offers priced'                                          AS "what",
       (SELECT count(*) FROM catalogue.term_credit)::text             AS "rows behind it",
       'Every tariff is compared on Flexible, 6 months and 12 months. '
       || 'The advertised credits and all recommendation amounts are '
       || 'GST-inclusive; the credit remains separate because it is repaid if '
       || 'the customer leaves early.'                                AS "why it matters"
UNION ALL
SELECT 'solar export credited',
       (SELECT count(*) FROM catalogue.tariff_component c
         JOIN catalogue.plan p USING (plan_id)
        WHERE p.is_active AND c.direction = 'export')::text,
       'Buy-back components are priced through the same tariff engine as '
       || 'imports. All three plans use the same assumed 8c/kWh buy-back, so '
       || 'the recommendation is driven by the displayed import terms.'
UNION ALL
SELECT 'generations in the table',
       (SELECT count(DISTINCT computed_at) FROM kea.plan_recommendation)::text,
       'Append-only by design. Every panel here pins the newest; a query that '
       || 'forgets to would multiply every figure on this page by this number.'
"""),
    ]),
    ("Who may need support — latest hardship assessment", [
        ("Latest hardship run", "table", 12, f"""
-- A parent row exists even when no signals were found. That distinguishes
-- "assessed and clear" from "never assessed", which absence alone cannot do.
-- "Flagged" keeps its Sprint 3 meaning, at least one v1 code, so the Sprint 4
-- warning signs are counted apart and cannot inflate it.
WITH latest AS (
    SELECT a.signal_count, a.ruleset_version, a.assessed_on, a.computed_at,
           {hardship_kind(hardship_codes("a.assessment_id"))} AS kind,
           o.assessment_id IS NOT NULL                      AS has_outlook
    FROM kea.hardship_assessment a
    LEFT JOIN kea.hardship_outlook o ON o.assessment_id = a.assessment_id
    WHERE a.computed_at = {LATEST_HARDSHIP}
), totals AS (
    SELECT count(*)                                         AS assessed,
           count(*) FILTER (WHERE kind = 'flagged')         AS flagged,
           count(*) FILTER (WHERE kind = 'early warning')   AS early,
           count(*) FILTER (WHERE kind = 'warning signs only') AS signs_only,
           count(*) FILTER (WHERE has_outlook)              AS outlooks,
           coalesce(sum(signal_count), 0)                   AS signals,
           max(ruleset_version)                             AS ruleset,
           max(assessed_on)                                 AS assessed_on,
           max(computed_at)                                 AS generation
    FROM latest
)
SELECT m.measure AS "measure", m.value AS "value"
FROM totals t
CROSS JOIN LATERAL (VALUES
    (1, 'accounts assessed',                      t.assessed::text),
    (2, 'flagged — a Sprint 3 signal',            t.flagged::text),
    (3, 'early warning — outreach before arrears', t.early::text),
    (4, 'warning signs only — no outreach',       t.signs_only::text),
    (5, 'arrears outlooks projected',             t.outlooks::text),
    (6, 'signals found',                          t.signals::text),
    (7, 'ruleset',                                t.ruleset),
    (8, 'assessed on',                            t.assessed_on::text),
    (9, 'generation (NZ time)',
        to_char(t.generation AT TIME ZONE 'Pacific/Auckland', 'YYYY-MM-DD HH24:MI'))
) m(n, measure, value)
ORDER BY m.n
"""),
        ("Hardship signals by reason", "bar", 12, f"""
-- Closed reason vocabulary, not a score: staff can see why each household was
-- flagged and policy can be changed without rewriting historical decisions.
-- Every code is listed, zero or not, so a warning sign that never fires shows
-- as an empty bar rather than a missing one. payment_drift,
-- bill_above_usual_payment and first_shortfall are the Sprint 4 warning signs.
WITH codes AS (
    SELECT unnest(enum_range(NULL::common.hardship_signal_code))::text AS code
)
SELECT codes.code AS "signal", count(s.signal_id) AS "households"
FROM codes
LEFT JOIN kea.hardship_signal s
       ON s.signal_code::text = codes.code
      AND s.assessment_id IN (SELECT assessment_id FROM kea.hardship_assessment
                              WHERE computed_at = {LATEST_HARDSHIP})
GROUP BY codes.code ORDER BY 2 DESC, 1
"""),
        ("Households flagged for outreach", "table", 24, f"""
-- Flagged means at least one v1 code, as in Sprint 3. The reasons list every
-- code the household has, warning signs included.
SELECT ba.account_number                                   AS "account",
       array_to_string(c.codes, ', ')                      AS "reasons",
       a.signal_count                                      AS "signal count",
       a.is_medically_dependent                            AS "medically dependent",
       a.is_vulnerable                                     AS "vulnerable",
       coalesce(outreach.status::text, 'not created')      AS "outreach status"
FROM kea.hardship_assessment a
JOIN billing.account ba USING (account_id)
CROSS JOIN LATERAL (SELECT {hardship_codes("a.assessment_id")} AS codes) c
LEFT JOIN LATERAL (
    SELECT x.status
    FROM kea.alert x
    WHERE x.account_id = a.account_id AND x.kind = 'hardship_outreach'
    ORDER BY x.created_at DESC LIMIT 1
) outreach ON true
WHERE a.computed_at = {LATEST_HARDSHIP}
  AND {hardship_kind("c.codes")} = 'flagged'
ORDER BY a.signal_count DESC, ba.account_number
"""),
        ("Signal evidence — for staff only", "table", 24, f"""
-- One row per signal in the latest generation, for every household with one.
-- `explanation` is what staff must be able to justify if asked. It is never
-- shown to the customer unmediated, and nothing on this page is sent.
SELECT ba.account_number                                   AS "account",
       k.kind                                              AS "kind",
       s.signal_code::text                                 AS "signal",
       s.signal_value                                      AS "observed",
       s.first_seen_on                                     AS "first seen",
       s.last_seen_on                                      AS "last seen",
       s.explanation                                       AS "explanation"
FROM kea.hardship_assessment a
JOIN billing.account ba USING (account_id)
CROSS JOIN LATERAL (
    SELECT {hardship_kind(hardship_codes("a.assessment_id"))} AS kind
) k
JOIN kea.hardship_signal s ON s.assessment_id = a.assessment_id
WHERE a.computed_at = {LATEST_HARDSHIP}
ORDER BY CASE k.kind WHEN 'flagged' THEN 1 WHEN 'early warning' THEN 2 ELSE 3 END,
         ba.account_number, s.signal_code::text
"""),
    ]),
    ("Outreach queue — created, never sent by the batch job", [
        ("Hardship alert delivery state", "bar", 12, """
-- Include zero-valued states so "sent 0" is explicit rather than inferred
-- from a missing bar. Delivery is runtime-owned; the batch only creates
-- pending, unscheduled in-app records.
WITH states AS (
    SELECT unnest(enum_range(NULL::common.alert_status))::text AS status
)
SELECT states.status AS "status", count(a.alert_id) AS "alerts"
FROM states
LEFT JOIN kea.alert a ON a.status::text = states.status
                     AND a.kind = 'hardship_outreach'
GROUP BY states.status ORDER BY states.status
"""),
        ("Hardship outreach idempotency", "table", 12, """
-- Re-running assessment may create a new parent generation, but an account
-- with open outreach must not receive another message.
SELECT count(*)                                         AS "open outreach",
       count(DISTINCT account_id)                       AS "accounts with outreach",
       count(DISTINCT account_id) FILTER (WHERE duplicates > 1)
                                                        AS "accounts duplicated"
FROM (
    SELECT account_id,
           count(*) OVER (PARTITION BY account_id) AS duplicates
    FROM kea.alert
    WHERE kind = 'hardship_outreach'
      AND status IN ('pending','sent','delivered','suppressed')
) x
"""),
        ("Open outreach — flagged and early warning", "table", 24, f"""
-- kea.alert has no rule column. An early outreach is one whose assessment --
-- the one that created it -- has no v1 code. The outlook is that
-- assessment's projection, and `outlook basis` is its arithmetic, for staff.
SELECT ba.account_number                                   AS "account",
       k.kind                                              AS "kind",
       array_to_string(c.codes, ', ')                      AS "reasons",
       x.status::text                                      AS "status",
       to_char(x.created_at AT TIME ZONE 'Pacific/Auckland', 'YYYY-MM-DD')
                                                           AS "created (NZ)",
       o.projected_arrears_on                              AS "arrears projected on",
       o.projected_overdue_nzd                             AS "projected overdue $",
       o.basis                                             AS "outlook basis"
FROM kea.alert x
JOIN billing.account ba ON ba.account_id = x.account_id
CROSS JOIN LATERAL (SELECT {hardship_codes("x.assessment_id")} AS codes) c
CROSS JOIN LATERAL (SELECT {hardship_kind("c.codes")} AS kind) k
LEFT JOIN kea.hardship_outlook o ON o.assessment_id = x.assessment_id
WHERE {OPEN_OUTREACH}
ORDER BY k.kind = 'early warning' DESC, o.projected_arrears_on NULLS LAST,
         ba.account_number
"""),
    ]),
]


def env() -> dict[str, str]:
    if not ENV_FILE.exists():
        sys.exit(f"{ENV_FILE} not found — cp .env.example .env first")
    return dict(
        line_.split("=", 1) for line_ in ENV_FILE.read_text(encoding="utf-8").splitlines()
        if line_.strip() and not line_.startswith("#") and "=" in line_
    )


def api(path, data=None, session=None, method=None):
    req = urllib.request.Request(
        f"{MB}{path}",
        data=json.dumps(data).encode() if data is not None else None,
        method=method or ("POST" if data is not None else "GET"),
        headers={"Content-Type": "application/json",
                 **({"X-Metabase-Session": session} if session else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = r.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"{method or 'GET'} {path} -> HTTP {e.code}\n{e.read().decode()[:600]}")


def main() -> None:
    e = env()
    s = api("/api/session", {"username": e["METABASE_ADMIN_EMAIL"],
                             "password": e["METABASE_ADMIN_PASSWORD"]})["id"]

    dbs = api("/api/database", session=s)
    db = next((d["id"] for d in dbs.get("data", dbs) if d["name"] == DB_NAME), None)
    if db is None:
        sys.exit(f"database '{DB_NAME}' is not connected — run `make dashboards`, which connects first")

    cols = api("/api/collection", session=s)
    parent = next((c["id"] for c in cols if c.get("name") == COLLECTION), None)
    if parent is None:
        parent = api("/api/collection", {"name": COLLECTION, "color": "#509EE3"},
                     session=s)["id"]
    coll = next((c["id"] for c in cols
                 if c.get("name") == SUBCOLLECTION
                 and c.get("location", "").strip("/").endswith(str(parent))), None)
    if coll is None:
        coll = api("/api/collection",
                   {"name": SUBCOLLECTION, "color": "#F9CF48", "parent_id": parent,
                    "description": "Built by analysis/metabase/build_plans_dashboard.py"},
                   session=s)["id"]
        print(f"created collection '{SUBCOLLECTION}'")

    existing = {c["name"]: c["id"] for c in
                api(f"/api/collection/{coll}/items?models=card", session=s).get("data", [])}

    def upsert(name, display, sql):
        payload = {
            "name": name, "display": display,
            "visualization_settings": LAYOUT.get(name, {}).get("viz", {}),
            "collection_id": coll,
            "dataset_query": {"type": "native", "database": db,
                              "native": {"query": sql.strip(), "template-tags": {}}},
        }
        if name in existing:
            api(f"/api/card/{existing[name]}", payload, session=s, method="PUT")
            return existing[name], "updated"
        return api("/api/card", payload, session=s)["id"], "created"

    cards, made = [], {"created": 0, "updated": 0}
    for section, panels in SECTIONS:
        cards.append(("heading", section, 24, 1))
        for name, display, width, sql in panels:
            cid, what = upsert(name, display, sql)
            made[what] += 1
            spec = LAYOUT.get(name, {})
            cards.append(("card", (cid, name), spec.get("w", width), spec.get("h", 6)))
    print(f"questions: {made['created']} created, {made['updated']} updated")

    dash = next((d for d in api(f"/api/collection/{coll}/items?models=dashboard",
                                session=s).get("data", []) if d["name"] == DASHBOARD), None)
    dash_id = dash["id"] if dash else api(
        "/api/dashboard", {"name": DASHBOARD, "collection_id": coll}, session=s)["id"]

    # Shelf packing: cards in a row differ in height, so the next row starts
    # below the TALLEST in the current one. A fixed stride either overlaps them
    # or leaves a band of dead space.
    dashcards, neg = [], -1
    row = col = band = 0

    def place(**kw):
        nonlocal neg
        dashcards.append({"id": neg, "parameter_mappings": [], **kw})
        neg -= 1

    place(card_id=None, row=0, col=0, size_x=24, size_y=7,
          visualization_settings={"virtual_card": {"display": "text"}, "text": INTRO})
    row = 7

    for kind, payload, width, height in cards:
        if kind == "heading":
            row += band
            col = band = 0
            place(card_id=None, row=row, col=0, size_x=24, size_y=1,
                  visualization_settings={"virtual_card": {"display": "heading"},
                                          "text": payload})
            row += 1
            continue
        cid, _ = payload
        if col + width > 24:
            row += band
            col = band = 0
        place(card_id=cid, row=row, col=col, size_x=width, size_y=height,
              visualization_settings={})
        col += width
        band = max(band, height)
        if col >= 24:
            row += band
            col = band = 0

    api(f"/api/dashboard/{dash_id}", {"dashcards": dashcards}, session=s, method="PUT")
    print(f"dashboard: {MB}/dashboard/{dash_id}")

    # This sub-collection is code-owned. Archive renamed questions so the old
    # network/user-type presentation cannot be mistaken for a live panel.
    built = {name for _, panels in SECTIONS for name, _, _, _ in panels}
    stale = [(c["id"], c["name"]) for c in
             api(f"/api/collection/{coll}/items?models=card", session=s).get("data", [])
             if c["name"] not in built]
    for cid, cname in stale:
        api(f"/api/card/{cid}", {"archived": True}, session=s, method="PUT")
        print(f"  archived stale question: {cname}")


if __name__ == "__main__":
    main()
