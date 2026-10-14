#!/usr/bin/env python3
"""Build the demo walkthrough in Metabase: one household, every feature, in order.

    make dashboards ONLY=demo

WHY ONE HOUSEHOLD. Switching accounts in front of an audience costs the thread
of the story, and no household the cohort drew by chance shows every feature
clearly. So the generator builds one on purpose (planted.py, DEMO_PLAN): a
medically dependent household on Free Weekends, no panels, an evening EV
charger arriving before the weather record ends, and the slowest of the
payment slides. This page walks a presenter through it, top to bottom: each
step is a short script ("say") over the chart that shows it.

HOW THE HOUSEHOLD IS FOUND. Not by an id typed here, and not by a rule over
served columns: medically dependent households with an EV-shaped load are not
unique. The generator writes the demo household's account into
`gen_audit.demo_household` on the pipeline sandbox, and the builder reads it
there, finds the served account with that number, and splices the served
SURROGATE keys (account_id, icp_id) into every card before it is written. No
card joins the two databases, and no card reads an identifier: the same guard
as the Sprint 4 page refuses one.

⚠️ A REBUILD RE-NUMBERS THE ACCOUNTS. The cards hold literal surrogate keys, so
after `make publish-serving` they point at whatever now has those keys until
this builder runs again. `make rebuild-all` rebuilds every dashboard last, so
only a hand-run publish can leave the page stale; rerun
`make dashboards ONLY=demo` after one.

⚠️ STEP 6'S EARLY WARNING IS A PAST-DATED ASSESSMENT. The published run
assesses one date, the newest, when the demo household is already in arrears,
so it writes no outlook. The warning it would have given beforehand is written
with the job's own `--as-of`:

    build_hardship_assessments.py --as-of 2026-06-04 --account <account_id>

It is one more append-only row; the latest assessment is unchanged. A publish
with DEMO_RESET clears it, so `make publish-serving DEMO_RESET=1` (and with it
`make rebuild-all`) writes it again straight after the publish. A publish
without DEMO_RESET keeps it.

The numbers quoted in the text cards are read the same way, at build time, from
the same rules the cards use, so the script never quotes a figure the chart
beside it does not draw.

**This file is the record.** Metabase keeps its questions in its own
application database; the queries below are the version-controlled source.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The helpers are the Sprint 4 builder's, so a card here is checked and drawn
# exactly as a card there is. verify.py reads MB, env and api off this module.
from build_sprint4_dashboard import (  # noqa: E402
    MB, PIPELINE_DB, SERVING_DB, api, axes, card, env, fmt, guard, money, text)

COLLECTION = "Kea Energy"
SUBCOLLECTION = "Demo — one household, every feature"
DASHBOARD = "Demo walkthrough — one household, every feature"
DESCRIPTION = ("A presenter's script: one purpose-built household shows the bill "
               "explanation, the mid-cycle warning, plan advice, EV and solar what-ifs, "
               "and the hardship escalation, in that order.")

BLUE, GREEN, RED, ORANGE, GREY = "#509EE3", "#88BF4D", "#EF8C8C", "#F2A86F", "#A7ADB5"

# Placeholders spliced in at build time. {A} and {ICP} are surrogate keys; the
# rest are the figures the script quotes.
FIND_ACCOUNT = "SELECT acct_no FROM gen_audit.demo_household"
ACCOUNT_SHAPE = re.compile(r"A\d{6,12}")

PLAN_FAMILY = ("CASE split_part({col}, '_', 2) "
               "WHEN 'ANY' THEN 'Anytime Rate' "
               "WHEN 'TOU' THEN 'Time of Use' "
               "WHEN 'WKND' THEN 'Free Weekends' ELSE {col} END")

# The hardship codes in words, as the script reads them aloud.
SIGN = """CASE s.signal_code::text
             WHEN 'payment_drift' THEN 'payments drifting later'
             WHEN 'first_shortfall' THEN 'a first shortfall'
             WHEN 'bill_above_usual_payment' THEN 'a bill well above the usual payment'
             WHEN 'part_payment_arrears' THEN 'part payments, arrears growing'
             WHEN 'late_payment_streak' THEN 'late payments, repeatedly'
             WHEN 'direct_debit_failed' THEN 'a direct debit failed'
             WHEN 'payments_stopped' THEN 'payments stopped'
             WHEN 'usage_collapse' THEN 'usage collapsed'
             ELSE s.signal_code::text END"""

# The bill the script explains: the first one the explainer could break down
# in which the EV-shaped load was the biggest driver. One definition, used by
# the waterfall, its table and the facts the text quotes.
EXPLAINED_BILL = """explained AS (
    SELECT e.*
    FROM kea.mart_bill_explanation e
    WHERE e.account_id = {A}
      AND e.is_explainable
      AND e.delta_from_detected_loads > 0
      AND e.delta_from_detected_loads >= greatest(
            abs(coalesce(e.delta_from_period_length, 0)), abs(coalesce(e.delta_from_weather, 0)),
            abs(coalesce(e.delta_from_base_load, 0)), abs(coalesce(e.delta_from_tariff, 0)),
            abs(coalesce(e.delta_unexplained, 0)))
    ORDER BY e.period_start
    LIMIT 1)"""

# The cycle the warning is shown on: the latest one with a flagged forecast.
WARNED_CYCLE = """warned AS (
    SELECT max(period_start) AS period_start
    FROM kea.bill_forecast
    WHERE account_id = {A} AND is_anomalous)"""

# Append-only tables: only the latest generation counts.
LATEST = "computed_at = (SELECT max(computed_at) FROM kea.{table} WHERE account_id = {A})"

FACTS = f"""
WITH {EXPLAINED_BILL}, {WARNED_CYCLE},
ctx AS (SELECT * FROM kea.mart_account_context WHERE account_id = {{A}}),
ev AS (SELECT min(usage_date) AS first_day
       FROM kea.mart_daily_detected_load
       WHERE icp_id = {{ICP}} AND ev_half_hours > 0 AND ev_confidence = 'high'),
fc AS (SELECT min(f.days_elapsed) FILTER (WHERE f.is_anomalous) AS first_flag_day,
              max(f.variance_pct) AS max_pct,
              count(*) FILTER (WHERE f.is_anomalous) AS flagged, count(*) AS days
       FROM kea.bill_forecast f JOIN warned w USING (period_start)
       WHERE f.account_id = {{A}}),
rec AS (SELECT r.*, {PLAN_FAMILY.format(col="p.plan_code")} AS family
        FROM kea.plan_recommendation r JOIN catalogue.plan p ON p.plan_id = r.candidate_plan_id
        WHERE r.account_id = {{A}} AND r.is_recommended
          AND r.{LATEST.format(table="plan_recommendation", A="{A}")}),
evs AS (SELECT s.saving_vs_baseline_nzd AS saving, {PLAN_FAMILY.format(col="p.plan_code")} AS family
        FROM kea.ev_scenario s JOIN catalogue.plan p ON p.plan_id = s.plan_id
        WHERE s.account_id = {{A}} AND s.scenario = 'shift_detected'
          AND s.{LATEST.format(table="ev_scenario", A="{A}")}
        ORDER BY s.saving_vs_baseline_nzd DESC LIMIT 1),
sol AS (SELECT count(*) AS options, min(payback_years_low) AS best_payback
        FROM kea.solar_estimate
        WHERE account_id = {{A}} AND {LATEST.format(table="solar_estimate", A="{A}")}),
short AS (SELECT min(issue_date) AS first_short, count(*) AS bills_short
          FROM billing.bill
          WHERE account_id = {{A}} AND payments_applied < total_charges
            AND due_date < (SELECT max(issue_date) FROM billing.bill WHERE account_id = {{A}})),
latest AS (SELECT assessment_id, observed_from, observed_to FROM kea.hardship_assessment
           WHERE account_id = {{A}}
           ORDER BY assessed_on DESC, computed_at DESC LIMIT 1),
sig AS (SELECT count(*) AS signals,
               to_char(min(l.observed_from), 'FMDD Mon YYYY') AS seen_from,
               to_char(min(l.observed_to), 'FMDD Mon YYYY') AS seen_to
        FROM latest l LEFT JOIN kea.hardship_signal s USING (assessment_id)),
early AS (SELECT h.assessed_on, o.projected_arrears_on, o.projected_overdue_nzd,
                  (SELECT string_agg({SIGN}, ', ' ORDER BY s.first_seen_on)
                   FROM kea.hardship_signal s WHERE s.assessment_id = h.assessment_id) AS seen
           FROM kea.hardship_assessment h
           JOIN kea.hardship_outlook o USING (assessment_id)
           WHERE h.account_id = {{A}}
           ORDER BY h.assessed_on, h.computed_at LIMIT 1),
-- Arrears as the ruleset defines them: a bill less than 95% paid 30 days
-- after its due date. Only dates the data has reached count.
arrears AS (SELECT min(due_date + 30) AS first_on
            FROM billing.bill
            WHERE account_id = {{A}} AND payments_applied < 0.95 * total_charges
              AND due_date + 30 <= (SELECT max(issue_date) FROM billing.bill
                                    WHERE account_id = {{A}})),
al AS (SELECT count(*) AS outreach FROM kea.alert
       WHERE account_id = {{A}} AND kind = 'hardship_outreach')
SELECT {PLAN_FAMILY.format(col="ctx.plan_code")}             AS plan,
       CASE WHEN ctx.is_medically_dependent THEN 'medically dependent'
            ELSE 'not medically dependent' END                AS medical,
       ctx.billing_cycle_day                                 AS cycle_day,
       to_char(ev.first_day, 'FMDD Mon YYYY')                AS ev_day,
       to_char(x.period_start, 'FMDD Mon') || ' – ' || to_char(x.period_end, 'FMDD Mon YYYY')
                                                             AS bill_period,
       to_char(x.amount_delta_pct, 'FM990')                  AS bill_pct,
       to_char(x.delta_from_detected_loads, 'FM$990')        AS bill_ev,
       to_char(w.period_start, 'FMDD Mon YYYY')              AS cycle_start,
       fc.first_flag_day                                     AS flag_day,
       to_char(fc.max_pct, 'FM990')                          AS flag_pct,
       fc.flagged || ' of ' || fc.days                       AS flag_days,
       rec.family                                            AS rec_plan,
       to_char(rec.annual_saving, 'FM$9,990')                AS rec_saving,
       to_char(rec.usage_saving, 'FM$9,990')                 AS rec_usage,
       to_char(rec.term_credit, 'FM$9,990')                  AS rec_credit,
       rec.term_months                                       AS rec_term,
       to_char(evs.saving, 'FM$9,990')                       AS ev_saving,
       evs.family                                            AS ev_plan,
       sol.options                                           AS solar_options,
       to_char(sol.best_payback, 'FM990.0')                  AS solar_payback,
       to_char(short.first_short, 'FMDD Mon YYYY')           AS first_short,
       short.bills_short                                     AS bills_short,
       sig.signals                                           AS signals,
       sig.seen_from                                         AS seen_from,
       sig.seen_to                                           AS seen_to,
       al.outreach                                           AS outreach,
       to_char(early.assessed_on, 'FMDD Mon YYYY')           AS warn_on,
       early.seen                                            AS warn_signs,
       to_char(early.projected_arrears_on, 'FMDD Mon YYYY')  AS warn_arrears,
       to_char(early.projected_overdue_nzd, 'FM$9,990')      AS warn_overdue,
       early.projected_arrears_on - early.assessed_on        AS warn_days,
       to_char(arrears.first_on, 'FMDD Mon YYYY')            AS arrears_on
FROM ctx
CROSS JOIN ev CROSS JOIN fc CROSS JOIN warned w CROSS JOIN sol
CROSS JOIN short CROSS JOIN sig CROSS JOIN al
LEFT JOIN explained x ON true
LEFT JOIN rec ON true
LEFT JOIN evs ON true
LEFT JOIN early ON true
CROSS JOIN arrears
"""

INTRO_H = 10
INTRO = """## Demo walkthrough: one household, every feature

**How to use this page.** Present it top to bottom; it takes about ten minutes.
Each step has a short script on the left (**Say**) and the chart that proves it
beside or below it. Every number in the script is read from the same data as the
charts when the page is built, so the words and the pictures always agree.

**The household.** One home built on purpose for the demo from a real London
smart meter, moved onto New Zealand dates, plans and weather. It is on
**{plan}**, **{medical}**, has no solar panels, and is billed on day
{cycle_day} of each month. In the middle of the winter of 2026 it gets an electric car charged
in the evenings, and at the same time its payments stop keeping up.

| Step | What the audience sees | Feature |
|---|---|---|
| 1 | an EV-shaped load appears in the half-hours | S1 · detected loads |
| 2 | why the bill rose, in dollars | 1 · bill explanation |
| 3 | a warning in the middle of the cycle | 3 · mid-cycle warning |
| 4 | a cheaper plan, and why | 2 · plan comparison |
| 5 | charging at night instead, and solar | S2 · EV and solar what-ifs |
| 6 | payments falling behind, warned weeks ahead, handed to a person | 4 · hardship, S3 · early warning |
"""

STEP0 = """**Say:** "This is one of the 200 households in our test data. Nothing here is
typed in by hand: every bill, reading and payment came through the same
pipeline as the rest."

Point at the winter of 2026: "These bills are far higher than the winter
before. Something changed. Let's find out what."

*Dates:* the story happens in 2026. The data runs to August 2027, so the
customer app's "today" is later than the story.
"""

STEP1 = """**Say:** "The meter only records the whole house, every half hour. Long
blocks of steady, heavy use in the evening are shaped like a car charger. From
**{ev_day}** we see that shape on many evenings."

*Careful wording:* it is "shaped like an EV", not "your EV". The meter never
sees an appliance. The orange part is the *lower* bound of what that shape
used; the real amount is at least this much.
"""

STEP2 = """**Say:** "The bill for **{bill_period}** was **{bill_pct}%** higher than the
one before. The explainer splits the change into causes, in dollars: the
biggest is the EV-shaped load, **{bill_ev}**. The other bars show what else
moved it, and what nothing explains is shown too, not hidden."

The bars add up from the bill before (left) to this bill (total). Amounts
exclude GST, because that is how the drivers are priced.
"""

STEP3 = """**Say:** "We don't wait for the bill. During the cycle that started on
**{cycle_start}**, the daily forecast was above the same period last year from
day **{flag_day}** of the cycle, by up to **{flag_pct}%**. That is when a customer gets the
'heading unusually high' warning, weeks before the bill arrives."

The forecast (blue) sits well above last year's bill (green), and its band
(grey) never comes down to it: **{flag_days}** daily forecasts were flagged.

*Why this cycle:* a warning needs last year's bill and weather to compare
against, and the weather record ends in mid-August 2026.
"""

STEP4 = """**Say:** "With an EV, the plan matters. On the last twelve months of this
home's own half-hours, **{rec_plan}** would cost **{rec_usage}** a year less in
usage, plus a **{rec_credit}** credit for a {rec_term}-month term: **{rec_saving}**
a year in all. We only recommend a switch when it saves money."

The chart re-prices the same usage on each plan with no term credit, so the
bars compare plans, not offers.
"""

STEP5 = """**Say:** "Two what-ifs, both priced from this home's own year of data.
First, if the car charged at night instead of when it does now, on
**{ev_plan}** that saves another **{ev_saving}** a year. On Free Weekends it saves
next to nothing: that plan has no cheap nights."

"Second, solar. We priced {solar_options} options, from 3 to 7 kW, with and
without a battery. The fastest payback is about **{solar_payback} years** at best;
the bars show the range, because generation and prices are uncertain."

*These are what-ifs from an assumptions file, not measurements.*
"""

STEP6 = """**Say:** "From the bill issued on **{first_short}**, this household paid the
same amount each month while the bills grew, and fell further behind every
month: {bills_short} bills not paid in full."

"The early warning reads the pattern, not one missed payment. On
**{warn_on}** it saw {warn_signs}, and projected that the account would be in
arrears on **{warn_arrears}**: **{warn_days} days** ahead. It was, on
**{arrears_on}**."

"Nothing happens automatically. The customer gets **{outreach}** gentle in-app
message, 'Support is available', and the account goes on the staff hardship
queue, marked **{medical}**, for a person to follow up."

The signals table is the latest assessment ({seen_from} to {seen_to}). How
early the warning comes across the whole cohort is the Sprint 4 evaluation's
figure. The staff queue is on the Plans dashboard.
"""

CAVEATS = """- **On the day:** `make up`, then stop Airflow, Spark history, Grafana and
  Prometheus so Metabase has the memory; open this page. Run no rebuild, no
  `publish-serving`, no DAG and no `make clean`: after the Snowflake trial
  ended they cannot be redone. A dump of the served data is in
  `data/raw/serving-backup/2026-10-03/`.
- **Feature 5, everyday questions,** is the app's chat; until it has a login for
  this household, show it on the fixture login for the mid-cycle case.
- **The story's dates are in 2026.** The data runs to August 2027, so the app's
  "today" is later than the story. Present the steps by their dates.
- **Weather ends on 17 Aug 2026.** Bill explanations and warnings after that date
  have no weather to work with, which is why steps 2 and 3 sit before it.
- **The customer app cannot sign in as this household yet.** Its demo logins
  cover the hand-built fixture households only; a login for this one is a
  backend change. Until then, present from this page.
- **The research forecast (Botman et al., 2025) is not on this page.** It is
  measured offline, on the Forecast backtest dashboard, and is not served.
- **The household was built for the demo.** It is left out of the Sprint 4
  evaluation, so it does not flatter the measured results.
"""

SECTIONS = [
    ("Step 0 · Meet the household", [
        text(STEP0, w=8, h=9),
        card("The household's bills, month by month", "bar", 16, 9, """
-- Every bill, as the customer sees it: incl. GST.
SELECT b.period_start      AS "Bill period from",
       b.total_charges     AS "Bill, incl. GST"
FROM billing.bill b
WHERE b.account_id = {A}
ORDER BY b.period_start
""", axes(["Bill period from"], ["Bill, incl. GST"], {
            "graph.x_axis.scale": "timeseries",
            "series_settings": {"Bill, incl. GST": {"color": BLUE}},
            **money("Bill, incl. GST")})),
    ]),
    ("Step 1 · Something new in the half-hours (S1, detected loads)", [
        text(STEP1, w=8, h=9),
        card("Daily use, and the part shaped like an EV charger", "bar", 16, 9, """
-- From a month before the first high-confidence EV-shaped day to six weeks
-- after it. ev_kwh_low is the lower bound, so the stack never overstates it.
WITH first_ev AS (
    SELECT min(usage_date) AS d
    FROM kea.mart_daily_detected_load
    WHERE icp_id = {ICP} AND ev_half_hours > 0 AND ev_confidence = 'high')
SELECT u.usage_date AS "Day",
       round(u.import_kwh - coalesce(CASE WHEN d.ev_confidence = 'high' THEN d.ev_kwh_low END, 0), 1)
         AS "Everything else, kWh",
       round(coalesce(CASE WHEN d.ev_confidence = 'high' THEN d.ev_kwh_low END, 0), 1)
         AS "Shaped like an EV, kWh"
FROM first_ev f
JOIN kea.mart_daily_usage u
  ON u.icp_id = {ICP} AND u.usage_date BETWEEN f.d - 30 AND f.d + 45
LEFT JOIN kea.mart_daily_detected_load d
  ON d.icp_id = u.icp_id AND d.usage_date = u.usage_date
ORDER BY u.usage_date
""", axes(["Day"], ["Everything else, kWh", "Shaped like an EV, kWh"], {
            "stackable.stack_type": "stacked", "graph.x_axis.scale": "timeseries",
            "series_settings": {"Everything else, kWh": {"color": GREY},
                                "Shaped like an EV, kWh": {"color": ORANGE}}})),
    ]),
    ("Step 2 · Why is my bill higher? (feature 1, bill explanation)", [
        text(STEP2, w=8, h=9),
        card("Why the bill changed, in dollars", "waterfall", 16, 9, f"""
-- The bill before, then each driver; the total bar is this bill. The steps
-- add up to the change, excl. GST, as the explainer prices them.
WITH {EXPLAINED_BILL}
SELECT v.step               AS "Step",
       round(v.amount, 2)   AS "Dollars, excl. GST"
FROM explained b
CROSS JOIN LATERAL (VALUES
    (0, 'Bill before',   b.prior_total_amount),
    (1, 'Days',          b.delta_from_period_length),
    (2, 'Weather',       b.delta_from_weather),
    (3, 'Base load',     b.delta_from_base_load),
    (4, 'EV-shaped',     b.delta_from_detected_loads),
    (5, 'Tariff',        b.delta_from_tariff),
    (6, 'Not explained', b.delta_unexplained)
) v(ord, step, amount)
ORDER BY v.ord
""", axes(["Step"], ["Dollars, excl. GST"], {
            "graph.show_values": True, "waterfall.show_total": True,
            "waterfall.increase_color": RED, "waterfall.decrease_color": GREEN,
            "waterfall.total_color": BLUE,
            **money("Dollars, excl. GST")})),
    ]),
    ("Step 3 · A warning before the bill arrives (feature 3, mid-cycle warning)", [
        text(STEP3, w=8, h=10),
        card("The forecast during the warned cycle, against last year", "line", 16, 10, f"""
-- One forecast a day. Baseline = the same period a year earlier.
WITH {WARNED_CYCLE}
SELECT f.days_elapsed      AS "Day of the cycle",
       f.projected_amount  AS "Forecast",
       f.projected_low     AS "Band low",
       f.projected_high    AS "Band high",
       f.baseline_amount   AS "Last year"
FROM kea.bill_forecast f
JOIN warned w USING (period_start)
WHERE f.account_id = {{A}}
ORDER BY 1
""", axes(["Day of the cycle"], ["Forecast", "Band low", "Band high", "Last year"], {
            "graph.y_axis.auto_split": False,
            "graph.x_axis.title_text": "Day of the billing cycle",
            "graph.y_axis.title_text": "NZD, excl. GST",
            "series_settings": {
                "Forecast": {"color": BLUE},
                "Band low": {"color": GREY, "line.style": "dashed"},
                "Band high": {"color": GREY, "line.style": "dashed"},
                "Last year": {"color": GREEN}}})),
    ]),
    ("Step 4 · Is there a better plan? (feature 2, plan comparison)", [
        text(STEP4, w=8, h=8),
        card("A year of this home's use, priced on each plan", "bar", 16, 8, f"""
-- The latest comparison, without term credits: one row per plan. The current
-- plan is priced on the same usage, so the bars differ only by plan.
SELECT {PLAN_FAMILY.format(col="p.plan_code")}
         || CASE WHEN r.candidate_plan_id = r.current_plan_id THEN ' (now)' ELSE '' END
                                AS "Plan",
       round(r.candidate_annual_cost) AS "A year, incl. GST"
FROM kea.plan_recommendation r
JOIN catalogue.plan p ON p.plan_id = r.candidate_plan_id
WHERE r.account_id = {{A}} AND r.term_months = 0
  AND r.{LATEST.format(table="plan_recommendation", A="{A}")}
ORDER BY r.candidate_annual_cost DESC
""", axes(["Plan"], ["A year, incl. GST"], {
            "graph.show_values": True,
            "series_settings": {"A year, incl. GST": {"color": BLUE}},
            **money("A year, incl. GST")})),
    ]),
    ("Step 5 · What if? Charging at night, and solar (S2)", [
        text(STEP5, w=8, h=11),
        card("The year's bill, charging as now or at night", "bar", 16, 11, f"""
-- The EV-shaped load as detected, moved to the night hours. Only plans the
-- what-if priced; the household's year, with its own charging.
SELECT {PLAN_FAMILY.format(col="p.plan_code")} AS "Plan",
       round(max(s.annual_bill_nzd) FILTER (WHERE s.charging_pattern = 'as_detected'))
         AS "Charging as now",
       round(max(s.annual_bill_nzd) FILTER (WHERE s.charging_pattern = 'night'))
         AS "Charging at night"
FROM kea.ev_scenario s
JOIN catalogue.plan p ON p.plan_id = s.plan_id
WHERE s.account_id = {{A}} AND s.scenario = 'shift_detected'
  AND s.{LATEST.format(table="ev_scenario", A="{A}")}
GROUP BY 1
ORDER BY 1
""", axes(["Plan"], ["Charging as now", "Charging at night"], {
            "graph.show_values": True,
            "series_settings": {"Charging as now": {"color": GREY},
                                "Charging at night": {"color": GREEN}},
            **money("Charging as now", "Charging at night")})),
        card("Solar: years to pay back, best and worst case", "bar", 24, 8, f"""
-- Every option priced: system size by battery. low = the best case.
-- Short labels, so nine bars sit level at full width.
SELECT round(e.system_kw) || ' kW'
         || CASE WHEN coalesce(e.battery_kwh, 0) = 0 THEN ''
                 WHEN e.battery_dispatch = 'grid_charge' THEN ' + grid battery'
                 ELSE ' + battery' END  AS "Option",
       e.payback_years_low              AS "Best case, years",
       e.payback_years_high             AS "Worst case, years"
FROM kea.solar_estimate e
WHERE e.account_id = {{A}}
  AND e.{LATEST.format(table="solar_estimate", A="{A}")}
ORDER BY e.system_kw, e.battery_kwh, e.battery_dispatch
""", axes(["Option"], ["Best case, years", "Worst case, years"], {
            "graph.show_values": True,
            "series_settings": {"Best case, years": {"color": GREEN},
                                "Worst case, years": {"color": ORANGE}}})),
    ]),
    ("Step 6 · Falling behind, and a person steps in (feature 4, S3)", [
        text(STEP6, w=8, h=16),
        card("Billed, paid, and what is still owed", "combo", 16, 16, """
-- Per bill, by the month it starts (two can be issued in one month): what it charged and what was paid against it, incl. GST; the line
-- is the running total left unpaid. The last bills are not yet due.
SELECT to_char(b.period_start, 'YYYY-MM')             AS "Bill for",
       b.total_charges                                AS "Billed",
       b.payments_applied                             AS "Paid",
       sum(b.total_charges - b.payments_applied)
         OVER (ORDER BY b.period_start)               AS "Owed so far"
FROM billing.bill b
WHERE b.account_id = {A}
ORDER BY b.period_start
""", axes(["Bill for"], ["Billed", "Paid", "Owed so far"], {
            "graph.x_axis.scale": "ordinal",
            "series_settings": {
                "Billed": {"display": "bar", "color": GREY},
                "Paid": {"display": "bar", "color": GREEN},
                "Owed so far": {"display": "line", "color": RED, "axis": "right"}},
            **money("Billed", "Paid", "Owed so far")})),
        card("When the early warning saw it coming", "table", 24, 4, f"""
-- Every assessment of the household that projected arrears: the date it ran,
-- what it saw, and the arrears it projected. The rows before the latest one
-- are the past-dated assessments the module docstring describes.
SELECT to_char(h.assessed_on, 'FMDD Mon YYYY')            AS "Assessed on",
       (SELECT string_agg({SIGN}, ', ' ORDER BY s.first_seen_on)
        FROM kea.hardship_signal s WHERE s.assessment_id = h.assessment_id)
                                                        AS "Warning signs it saw",
       to_char(o.projected_arrears_on, 'FMDD Mon YYYY')  AS "Arrears projected for",
       o.projected_overdue_nzd                           AS "Projected overdue"
FROM kea.hardship_assessment h
JOIN kea.hardship_outlook o USING (assessment_id)
WHERE h.account_id = {{A}}
ORDER BY h.assessed_on
""", {**fmt({"Warning signs it saw": {"text_wrapping": True},
            "Projected overdue": {"prefix": "$", "decimals": 2}}),
       "table.column_widths": [120, 430, 160, 150]}),
        card("What the early warning found", "table", 24, 6, """
-- The latest assessment's signals, in the ruleset's own words.
SELECT s.signal_code::text  AS "Signal",
       s.signal_value       AS "What it saw",
       to_char(s.first_seen_on, 'FMDD Mon YYYY') AS "First seen",
       s.explanation        AS "Why it matters"
FROM kea.hardship_signal s
WHERE s.assessment_id = (SELECT assessment_id FROM kea.hardship_assessment
                         WHERE account_id = {A}
                         ORDER BY assessed_on DESC, computed_at DESC LIMIT 1)
ORDER BY s.first_seen_on
""", {**fmt({"What it saw": {"text_wrapping": True},
            "Why it matters": {"text_wrapping": True}}),
       "table.column_widths": [170, 290, 110, 300]}),
        card("What the customer is sent", "table", 24, 4, """
-- The outreach, as written. Medically dependent: a person follows up.
SELECT a.channel::text  AS "Channel",
       a.subject        AS "Subject",
       a.body           AS "Message"
FROM kea.alert a
WHERE a.account_id = {A} AND a.kind = 'hardship_outreach'
ORDER BY a.created_at
""", {**fmt({"Message": {"text_wrapping": True}}),
       "table.column_widths": [90, 170, 600]}),
    ]),
    ("Before you present", [
        text(CAVEATS, w=24, h=8),
    ]),
]


def dataset(session: str, db_id: int, sql: str) -> tuple[list[str], list[list]]:
    r = api("/api/dataset", {"type": "native", "database": db_id,
                             "native": {"query": sql.strip(), "template-tags": {}}},
            session=session)
    if r.get("error"):
        sys.exit(f"query failed: {r['error']}")
    return [c["name"] for c in r["data"]["cols"]], r["data"]["rows"]


def find_demo(session: str, ids: dict[str, int]) -> dict[str, str]:
    """The demo household's served surrogate keys, from the generator's marker.

    A Metabase with no pipeline connection (the AWS demo serves only the backend
    database) derives the account number the way load_source.sh writes the
    marker: planted.account_number(planted.DEMO_LCLID).
    """
    if PIPELINE_DB in ids:
        _, rows = dataset(session, ids[PIPELINE_DB], FIND_ACCOUNT)
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                               / "pipeline/scripts/gen_source"))
        import planted
        rows = [[planted.account_number(planted.DEMO_LCLID)]]
    if len(rows) != 1 or not ACCOUNT_SHAPE.fullmatch(str(rows[0][0])):
        sys.exit(f"gen_audit.demo_household should hold one account, found {len(rows)}; "
                 "rebuild the sources with the demo household (planted.DEMO_PLAN)")
    _, rows = dataset(session, ids[SERVING_DB],
                      "SELECT account_id, icp_id FROM billing.account "
                      f"WHERE account_number = '{rows[0][0]}'")
    if len(rows) != 1:
        sys.exit("the demo household is not in serving; run make publish-serving first")
    return {"A": str(int(rows[0][0])), "ICP": str(int(rows[0][1]))}


def splice(keys: dict[str, str], facts: dict[str, str]) -> None:
    """Put the keys into every query and the facts into every script."""
    for _, items in SECTIONS:
        for it in items:
            if it["kind"] == "card":
                it["sql"] = it["sql"].replace("{A}", keys["A"]).replace("{ICP}", keys["ICP"])
            else:
                it["text"] = it["text"].format_map(facts)


def main() -> None:
    global INTRO
    cards = [it for _, items in SECTIONS for it in items if it["kind"] == "card"]
    names = [c["name"] for c in cards]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        sys.exit(f"card names are the match key and must be unique: {dupes}")
    for c in cards:
        guard(c["name"], c["sql"])
    guard("facts", FACTS)
    if "--check" in sys.argv[1:]:
        print(f"{len(cards)} cards: names unique, no identifiers read")
        return

    e = env()
    s = api("/api/session", {"username": e["METABASE_ADMIN_EMAIL"],
                             "password": e["METABASE_ADMIN_PASSWORD"]})["id"]
    dbs = api("/api/database", session=s)
    ids = {d["name"]: d["id"] for d in dbs.get("data", dbs)}
    missing = [n for n in (SERVING_DB,) if n not in ids]
    if missing:
        sys.exit(f"not connected: {missing} — run `make dashboards`, which connects first")

    keys = find_demo(s, ids)
    cols, rows = dataset(s, ids[SERVING_DB],
                         FACTS.replace("{A}", keys["A"]).replace("{ICP}", keys["ICP"]))
    if len(rows) != 1:
        sys.exit("could not read the demo household's figures")
    facts = {c: ("—" if v is None else str(v)) for c, v in zip(cols, rows[0])}
    gaps = sorted(c for c, v in facts.items() if v == "—")
    if gaps:
        print(f"  ⚠️ no value for {', '.join(gaps)}; the script shows a dash there")
    splice(keys, facts)
    INTRO = INTRO.format_map(facts)

    cols_ = api("/api/collection", session=s)
    parent = next((c["id"] for c in cols_ if c.get("name") == COLLECTION), None)
    if parent is None:
        parent = api("/api/collection", {"name": COLLECTION, "color": "#509EE3"},
                     session=s)["id"]
    coll = next((c["id"] for c in cols_
                 if c.get("name") == SUBCOLLECTION
                 and c.get("location", "").strip("/").endswith(str(parent))), None)
    if coll is None:
        coll = api("/api/collection",
                   {"name": SUBCOLLECTION, "color": "#F2A86F", "parent_id": parent,
                    "description": "Built by analysis/metabase/build_demo_dashboard.py"},
                   session=s)["id"]
        print(f"created collection '{SUBCOLLECTION}'")

    existing = {c["name"]: c["id"] for c in
                api(f"/api/collection/{coll}/items?models=card", session=s).get("data", [])}

    def upsert(c):
        payload = {
            "name": c["name"], "display": c["display"],
            "visualization_settings": c["viz"], "collection_id": coll,
            "dataset_query": {"type": "native", "database": ids[c["db"]],
                              "native": {"query": c["sql"].strip(), "template-tags": {}}},
        }
        if c["name"] in existing:
            api(f"/api/card/{existing[c['name']]}", payload, session=s, method="PUT")
            return existing[c["name"]], "updated"
        return api("/api/card", payload, session=s)["id"], "created"

    card_ids, made = {}, {"created": 0, "updated": 0}
    for c in cards:
        card_ids[c["name"]], what = upsert(c)
        made[what] += 1
    print(f"questions: {made['created']} created, {made['updated']} updated")

    dash = next((d for d in api(f"/api/collection/{coll}/items?models=dashboard",
                                session=s).get("data", []) if d["name"] == DASHBOARD), None)
    dash_id = dash["id"] if dash else api(
        "/api/dashboard", {"name": DASHBOARD, "collection_id": coll,
                           "description": DESCRIPTION},
        session=s)["id"]

    # Shelf packing, as on the Sprint 4 page: the next row starts below the
    # tallest card in the current one.
    dashcards, neg = [], -1
    row = col = band = 0

    def place(**kw):
        nonlocal neg
        dashcards.append({"id": neg, "parameter_mappings": [], **kw})
        neg -= 1

    def note(markdown, display="text"):
        return {"virtual_card": {"display": display}, "text": markdown}

    place(card_id=None, row=0, col=0, size_x=24, size_y=INTRO_H,
          visualization_settings=note(INTRO))
    row = INTRO_H

    for heading, items in SECTIONS:
        row += band
        col = band = 0
        place(card_id=None, row=row, col=0, size_x=24, size_y=1,
              visualization_settings=note(heading, "heading"))
        row += 1
        for it in items:
            w, h = it["w"], it["h"]
            if col + w > 24:
                row += band
                col = band = 0
            if it["kind"] == "text":
                place(card_id=None, row=row, col=col, size_x=w, size_y=h,
                      visualization_settings=note(it["text"]))
            else:
                place(card_id=card_ids[it["name"]], row=row, col=col, size_x=w, size_y=h,
                      visualization_settings={})
            col += w
            band = max(band, h)
            if col >= 24:
                row += band
                col = band = 0

    api(f"/api/dashboard/{dash_id}", {"dashcards": dashcards, "description": DESCRIPTION},
        session=s, method="PUT")
    print(f"dashboard: {MB}/dashboard/{dash_id}")

    stale = [(c["id"], c["name"]) for c in
             api(f"/api/collection/{coll}/items?models=card", session=s).get("data", [])
             if c["name"] not in card_ids]
    for cid, cname in stale:
        api(f"/api/card/{cid}", {"archived": True}, session=s, method="PUT")
        print(f"  archived stale question: {cname}")


if __name__ == "__main__":
    main()
