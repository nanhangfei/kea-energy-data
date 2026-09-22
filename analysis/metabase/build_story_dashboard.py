#!/usr/bin/env python3
"""Build "Story": one long page, the platform's four features and the pipeline behind them.

    make story-dashboard
    make ci-story-guard     # the checks alone, as CI runs them: no .env, no Metabase

WHO IT IS FOR. Someone seeing the platform for the first time. The page is read
top to bottom in five sections: what the platform does for a household --
explain a bill, forecast the next one, recommend a plan, notice hardship -- and
then the pipeline that feeds all four. Each feature section says how it works
today and then shows the figures that say whether it works. The other
dashboards are working tools, one per stage; this one borrows from each and is
read in order, so it is a single page with headings rather than tabs.

⚠️ NO IDENTIFIER REACHES THE SCREEN. The page is shown to people outside the
project, and an ICP can be looked up in the public registry. `guard()` refuses
any query that names an identifying column before anything is sent to
Metabase. The walkthrough household is "Household A", picked by a rule inside
each query (HOUSEHOLD_A) rather than by an id typed here, and the rule starts
at the first generated id, so none of the nine hand-built fixtures -- one of
which carries a real ICP -- can be picked. The hardship section shows counts
only: no household is shown up close there.

⚠️ NO NUMBER IS TYPED INTO THE TEXT. Every figure comes from a card that runs
against the live platform. The text says what to look at, never what the value
is, so a rebuild cannot leave the words disagreeing with the charts. The rules'
own constants (the ±15% band, a 16 °C degree-day base) are part of the method,
not results, and are named where they explain it.

**This file is the record.** A Metabase question lives in Metabase's own
application database: not in git, not reviewable in a PR, and gone after a
`docker compose down -v`. The queries below are the version-controlled source.

Idempotent: questions and the dashboard are matched by name and updated in
place, so the URL stays stable.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

MB = "http://localhost:3002"
SERVING_DB = "Kea serving"      # the backend's database, as kea_app
PIPELINE_DB = "Kea pipeline"    # kea-source-db: the four source systems and lake_audit
COLLECTION = "Kea Energy"
SUBCOLLECTION = "Story — the work end to end"
DASHBOARD = "Story"
DESCRIPTION = ("One long page: bill explanation, bill forecast, plan recommendation "
               "and hardship, then the pipeline behind them.")
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

BLUE, GREEN, RED, ORANGE, GREY = "#509EE3", "#88BF4D", "#EF8C8C", "#F2A86F", "#A7ADB5"

# Columns that identify a customer or a connection point. SQL comments are
# stripped before the check, so a query may say what it avoids.
IDENTIFIERS = re.compile(
    r"\b(icp_number|icp_identifier|customer_name|full_name|email|phone"
    r"|address_display|account_number|billing\.customer)\b", re.IGNORECASE)

# Generated source ids start here. The nine hand-built fixtures sit below it,
# so `>= FIRST_GENERATED` is the cohort filter everywhere on this page.
FIRST_GENERATED = 1_000_000
COHORT_ICPS = f"(SELECT icp_id FROM billing.account WHERE account_id >= {FIRST_GENERATED})"

# ⚠️ One population behind every "N of M" in sections 2-4: the generated
# households still on supply. Each feature used to count against whatever table
# it reads, which put 50, 44 and 45 on one page for what a reader takes to be
# the same cohort. Against a single base the three numbers mean coverage, and a
# household a feature can't serve is counted rather than dropped.
OPEN_COHORT = f"""open_cohort AS (
    SELECT account_id FROM billing.account
    WHERE end_date IS NULL AND account_id >= {FIRST_GENERATED}
)"""

# Pinned in every query that reads an append-only table. Without the pin a
# panel doesn't error; it multiplies as further generations arrive.
LATEST = "(SELECT max(computed_at) FROM kea.plan_recommendation)"
LATEST_EXAMPLES = "(SELECT max(computed_at) FROM kea.plan_bill_example)"
LATEST_HARDSHIP = "(SELECT max(computed_at) FROM kea.hardship_assessment)"

# ⚠️ Repeated in every Household A query rather than shared: a Metabase native
# question is one statement with nowhere to keep a CTE. Copying it keeps each
# panel self-contained and the rule visible to anyone who opens the question.
HOUSEHOLD_A = f"""household_a AS (
    -- The lowest-numbered generated account with everything sections 1-3
    -- show: a recommended move that saves money, a bill the explainer can
    -- break down, and a forecast that ran to the last day of a cycle whose
    -- bill has arrived. A rule, not a chosen account: a rebuilt cohort picks
    -- its own.
    SELECT min(r.account_id) AS account_id
    FROM kea.plan_recommendation r
    WHERE r.computed_at = {LATEST}
      AND r.is_recommended AND r.usage_saving > 0
      AND r.account_id >= {FIRST_GENERATED}
      AND EXISTS (SELECT 1 FROM kea.mart_bill_explanation e
                  WHERE e.account_id = r.account_id AND e.is_explainable)
      AND EXISTS (SELECT 1 FROM kea.bill_forecast f
                  JOIN billing.bill b ON b.account_id = f.account_id
                   AND b.period_start = f.period_start
                   AND b.period_end = f.period_end
                  WHERE f.account_id = r.account_id
                    AND f.days_elapsed >= b.period_days)
)"""

LAST_EXPLAINED_BILL = """last_bill AS (
    SELECT e.*
    FROM kea.mart_bill_explanation e
    JOIN household_a h ON h.account_id = e.account_id
    WHERE e.is_explainable
    ORDER BY e.period_end DESC
    LIMIT 1
)"""

LAST_FULL_CYCLE = """last_cycle AS (
    -- The most recent cycle that can be scored start to finish: forecasts up
    -- to its last day, and its bill issued.
    SELECT f.period_start
    FROM kea.bill_forecast f
    JOIN household_a h ON h.account_id = f.account_id
    JOIN billing.bill b ON b.account_id = f.account_id
     AND b.period_start = f.period_start AND b.period_end = f.period_end
    WHERE f.days_elapsed >= b.period_days
    ORDER BY f.period_start DESC
    LIMIT 1
)"""

# Every forecast snapshot against the bill for the same cycle. Both sides are
# GST-exclusive: the forecast prices kWh at the previous bill's rate.
FORECAST_AND_BILL = """FROM kea.bill_forecast f
JOIN billing.bill b ON b.account_id = f.account_id
 AND b.period_start = f.period_start AND b.period_end = f.period_end"""

CYCLE_STAGE = """CASE WHEN f.days_elapsed <= 6  THEN 'days 1–6'
            WHEN f.days_elapsed <= 13 THEN 'days 7–13'
            WHEN f.days_elapsed <= 20 THEN 'days 14–20'
            ELSE 'day 21 on' END"""

LAKE = """p AS (
    -- Incremental extracts add up across loads; a snapshot counts once, from
    -- its newest load.
    SELECT *, rank() OVER (PARTITION BY source_system, entity, layer
                           ORDER BY load_date DESC) AS newest
    FROM lake_audit.partitions
)"""


def family(code: str) -> str:
    return (f"CASE split_part({code}, '_', 2) WHEN 'ANY' THEN 'Anytime Rate' "
            f"WHEN 'TOU' THEN 'Time of Use' WHEN 'WKND' THEN 'Free Weekends' "
            f"ELSE {code} END")


def axes(dims, metrics, more=None):
    """Axis assignments are declared, never guessed. verify.py fails a card
    whose query returns a column that is neither a dimension nor a metric,
    because Metabase drops such a column from the chart without an error."""
    return {"graph.dimensions": list(dims), "graph.metrics": list(metrics), **(more or {})}


def fmt(settings: dict) -> dict:
    """Per-column formatting. On a chart this is also what the value labels use,
    and the labels are the only place a unit is certain to survive: Metabase
    drops a y-axis title on a narrow card without saying so, which left `63`
    and `71.1` on the screen meaning percentages.

    One call per card. A second `column_settings` key would replace the first,
    so mixed formats go in one `fmt({...})` rather than two spreads."""
    return {"column_settings": {
        json.dumps(["name", column], separators=(",", ":")): spec
        for column, spec in settings.items()}}


def wrap(column: str) -> dict:
    """Wrap one long text column instead of cutting it off."""
    return fmt({column: {"text_wrapping": True}})


def money(*columns: str, decimals: int = 2) -> dict:
    """Dollars, to a fixed number of places: a bare 4.5 beside an 18.31 reads as
    a different kind of number."""
    return fmt({c: {"prefix": "$", "decimals": decimals} for c in columns})


def pct(*columns: str) -> dict:
    """Percentages, always to one place: a bare 63 beside a 71.1 reads as a
    count."""
    return fmt({c: {"suffix": "%", "decimals": 1} for c in columns})


def card(name, display, w, h, sql, viz=None, db=SERVING_DB):
    return {"kind": "card", "name": name, "display": display, "w": w, "h": h,
            "sql": sql, "viz": viz or {}, "db": db}


def text(markdown, w=24, h=4):
    return {"kind": "text", "text": markdown.strip(), "w": w, "h": h}


# ⚠️ Heights and widths are sized for the narrowest grid Metabase still draws
# in columns: an iPad in portrait, 772px wide against the page's 1048. A text
# card there needs about 1.4 times its desktop rows, and a card that is too
# short cuts its text off with nothing to say more is there, so every note
# below is as tall as the iPad needs and leaves room to spare at full width.
# Tables are held to what a 772px grid gives them: a third of the page is too
# narrow for two columns of words. Phones get Metabase's own one-column layout
# and are not designed for.
INTRO_H = 12
INTRO = """## Story: from half-hour meter readings to a bill a customer can understand

Four things the platform does for a household, then the pipeline behind them:
**1 · explain a bill → 2 · forecast the next one → 3 · recommend a cheaper plan →
4 · notice hardship early → 5 · the pipeline.** Each feature section says how it
works today, then shows whether it works. Every number is a live query against
the running platform; none is typed into this text.

**What's real and what's generated.** The half-hour readings are real
households from the Low Carbon London smart-meter trial, moved onto the New
Zealand calendar by whole days so that their winters fall in the NZ winter. The
weather is real NIWA station data. The customers, accounts, bills and plans are
generated: nobody on this page is a real person. Sections 2, 3 and 4 all count
against the same population — the generated households still on supply — so
every *N of M* on this page means coverage, and a household a feature cannot
serve is counted rather than left out of both sides of the fraction.

**Household A** is the one household shown up close in sections 1–3. It is
picked by a rule, not by hand: the lowest-numbered generated account with a bill
the explainer can break down, a forecast that ran to the end of a cycle, and a
cheaper plan. The rule sits inside every query, so a rebuilt cohort picks its
own, and no identifier is shown.

**GST.** Sections 1 and 2 are excl. GST, priced as the bill's own lines are;
section 3 includes it, as a quote would."""

SECTIONS = [
    ("1 · Bill explanation: why this bill differs from the last one", [
        card("Bills 20%+ above the last one", "scalar", 12, 3, f"""
-- Generated households only: the nine fixtures are hand-built edge cases,
-- chosen to be unusual. Compared on the GST-inclusive total, the figure the
-- customer sees.
WITH b AS (
    SELECT total_charges,
           lag(total_charges) OVER (PARTITION BY account_id ORDER BY period_end) AS prior
    FROM billing.bill
    WHERE account_id >= {FIRST_GENERATED}
)
SELECT to_char(100.0 * count(*) FILTER (WHERE total_charges >= prior * 1.2)
               / nullif(count(prior), 0), 'FM990.0') || '%'
       AS "Bills 20%+ above the last one"
FROM b
"""),
        card("Bills the explainer can break down", "scalar", 12, 3, f"""
-- Generated households only, as every other card in this section is: side by
-- side, two scalars counting different populations invite the reader to divide
-- one by the other.
SELECT to_char(count(*) FILTER (WHERE is_explainable), 'FM999,999')
       || ' of ' || to_char(count(*), 'FM999,999') AS "Bills the explainer can break down"
FROM kea.mart_bill_explanation
WHERE account_id >= {FIRST_GENERATED}
"""),
        text("""
**How it works today.** For every bill, `mart_bill_explanation` compares it
with the same account's previous bill and splits the change into four drivers,
**in a fixed order**. Each is measured after the ones before it are taken out,
so nothing is counted twice:

1. **Days in the period.** A longer bill costs more with nothing else changed.
   It goes first, so a long bill is never blamed on the weather.
2. **Weather.** The change in heating degree days (how far each day's mean
   temperature fell below 16 °C) × the household's own heating slope.
3. **Base load.** The change in always-on use per day, × the days.
4. **Tariff.** Zero when the plan didn't change.

Each driver is priced at the previous bill's own rate per kWh, so the dollars
can be checked against a bill the customer already holds; excl. GST. **Not
explained** is whatever the four leave over, and it is always shown.

A bill is broken down **only when every input exists**: an earlier bill, a
heating fit good enough to price weather, weather and readings for every day of
both periods, and no change of plan. Otherwise the agent is told not to present
it as an explanation.""", w=12, h=17),
        card("How much each bill changed from the one before", "bar", 12, 17, f"""
-- The problem the explainer answers. Buckets are ordered by their own
-- smallest change; the sort key is not selected, because a third column
-- would be dropped from the chart without an error.
WITH b AS (
    SELECT 100.0 * (total_charges - lag(total_charges) OVER w)
           / nullif(lag(total_charges) OVER w, 0) AS pct
    FROM billing.bill
    WHERE account_id >= {FIRST_GENERATED}
    WINDOW w AS (PARTITION BY account_id ORDER BY period_end)
)
SELECT CASE WHEN pct <= -20 THEN 'down 20% or more'
            WHEN pct <  -5  THEN 'down 5–20%'
            WHEN pct <=  5  THEN 'within 5%'
            WHEN pct <  20  THEN 'up 5–20%'
            WHEN pct <  50  THEN 'up 20–50%'
            ELSE                 'up 50% or more'
       END      AS "Change on the previous bill",
       count(*) AS "Bills"
FROM b
WHERE pct IS NOT NULL
GROUP BY 1
ORDER BY min(pct)
""", axes(["Change on the previous bill"], ["Bills"], {"graph.show_values": True})),
        card("Household A: why its bill changed", "waterfall", 12, 8, f"""
-- The explainer's four drivers and its residual. They add up to the change
-- on the bill before, which is the total bar.
WITH {HOUSEHOLD_A}, {LAST_EXPLAINED_BILL}
SELECT v.step              AS "Driver",
       round(v.amount, 2)  AS "Change $, excl. GST"
FROM last_bill b
CROSS JOIN LATERAL (VALUES
    -- Short names on purpose: six long ones tilt to 45° and cost the bars
    -- half their height. The note above names each driver in full.
    (1, 'Days',          b.delta_from_period_length),
    (2, 'Weather',       b.delta_from_weather),
    (3, 'Base load',     b.delta_from_base_load),
    (4, 'Tariff',        b.delta_from_tariff),
    (5, 'Not explained', b.delta_unexplained)
) v(ord, step, amount)
ORDER BY v.ord
""", axes(["Driver"], ["Change $, excl. GST"], {
            "graph.show_values": True, "waterfall.show_total": True,
            "waterfall.increase_color": RED, "waterfall.decrease_color": GREEN,
            "waterfall.total_color": BLUE, "graph.x_axis.labels_enabled": False,
            **money("Change $, excl. GST")})),
        card("Household A: the bill being explained", "table", 12, 8, f"""
WITH {HOUSEHOLD_A}, {LAST_EXPLAINED_BILL}
SELECT v.what  AS "The bill",
       v.value AS "This (before)"
FROM last_bill b
CROSS JOIN LATERAL (VALUES
    (1, 'Period', to_char(b.period_start, 'FMDD Mon') || ' – '
                  || to_char(b.period_end, 'FMDD Mon YYYY')),
    (2, 'Days', b.period_days || ' (' || b.prior_period_days || ')'),
    (3, 'Amount, excl. GST', to_char(b.total_amount, 'FM$999,990.00') || ' ('
                             || to_char(b.prior_total_amount, 'FM$999,990.00') || ')'),
    (4, 'Change', CASE WHEN b.amount_delta < 0 THEN '−' ELSE '+' END
                  || to_char(abs(b.amount_delta), 'FM$999,990.00')),
    (5, 'Degree days', round(b.hdd_this_period) || ' (' || round(b.hdd_prior_period) || ')'),
    (6, 'Always-on use, kWh a day', round(b.base_load_kwh_day_this, 1) || ' ('
                                    || round(b.base_load_kwh_day_prior, 1) || ')')
) v(ord, what, value)
ORDER BY v.ord
""", {
            # Fixed widths, both wrapping: measured at half an iPad's width, the
            # two columns asked for more than the card has and scrolled sideways.
            **fmt({"The bill": {"text_wrapping": True},
                   "This (before)": {"text_wrapping": True}}),
            "table.column_widths": [175, 175]}),
        text("""
**The same two periods, day by day.** Every day of both bill periods as kWh,
with the mean temperature beside it. This is what the explainer reduces to five
numbers: use rising as the temperature falls is the weather driver, and what
moves for any other reason — a different routine, visitors, a heater left on —
is what lands in **Not explained**.""", h=4),
        card("Household A: daily use and temperature, both periods", "combo", 24, 7, f"""
-- The two periods the waterfall compares, day by day. Cycles run back to back,
-- so the earlier one begins period_start - prior_period_days. The two kWh
-- columns are complementary, which gives each period its own colour.
WITH {HOUSEHOLD_A}, {LAST_EXPLAINED_BILL}
SELECT u.usage_date AS "Day",
       round((CASE WHEN u.usage_date <  b.period_start THEN u.import_kwh END)::numeric, 1)
         AS "kWh, the bill before",
       round((CASE WHEN u.usage_date >= b.period_start THEN u.import_kwh END)::numeric, 1)
         AS "kWh, this bill",
       round(u.temp_mean_c::numeric, 1) AS "Mean temperature °C"
FROM last_bill b
JOIN kea.mart_daily_usage u ON u.icp_id = b.icp_id
WHERE u.usage_date >= b.period_start - (b.prior_period_days || ' days')::interval
  AND u.usage_date <= b.period_end
ORDER BY u.usage_date
""", axes(["Day"], ["kWh, the bill before", "kWh, this bill", "Mean temperature °C"], {
            # No y-axis title: Metabase puts `graph.y_axis.title_text` on BOTH
            # axes, which labels the temperature side kWh. Left untouched, each
            # axis is titled by the series on it.
            "graph.x_axis.scale": "timeseries", "graph.x_axis.labels_enabled": False,
            # Complementary columns never share a day, so stacking is what puts
            # both at full width on one slot per day.
            "stackable.stack_type": "stacked",
            "series_settings": {
                "kWh, the bill before": {"display": "bar", "color": GREY, "axis": "left"},
                "kWh, this bill": {"display": "bar", "color": BLUE, "axis": "left"},
                "Mean temperature °C": {"display": "line", "color": ORANGE,
                                        "axis": "right"}}})),
        text("""
*What moves a bill* adds up, over every bill that could be broken down, how many
dollars each driver moved. *Why a bill can't be broken down* counts the rest,
each under the first check it fails: *weather missing* is where the readings'
NZ calendar runs past the end of the weather record; *other reasons* means
partial weather, missing half-hours, a change of plan, or a weak heating fit.""",
             h=4),
        card("What moves a bill", "row", 12, 7, f"""
-- Every bill the explainer could break down, excl. GST. Summed as sizes, not
-- signs: a driver that raised one bill by $20 and lowered another by $20
-- still moved $40.
SELECT v.driver                   AS "Driver",
       round(sum(abs(v.amount)))  AS "Dollars moved, excl. GST"
FROM kea.mart_bill_explanation e
CROSS JOIN LATERAL (VALUES
    (1, 'Days in the period', e.delta_from_period_length),
    (2, 'Weather',            e.delta_from_weather),
    (3, 'Base load',          e.delta_from_base_load),
    (4, 'Tariff',             e.delta_from_tariff),
    (5, 'Not explained',      e.delta_unexplained)
) v(ord, driver, amount)
WHERE e.is_explainable AND e.account_id >= {FIRST_GENERATED}
GROUP BY v.ord, v.driver
ORDER BY v.ord
-- Bars run across, not up: as a column chart the five driver names tilted to
-- 45° and took half the plot's height with them. Across, each name is written
-- out flat and the lengths are still what's being compared.
""", axes(["Driver"], ["Dollars moved, excl. GST"], {
            "graph.show_values": True, **money("Dollars moved, excl. GST", decimals=0)})),
        card("Why a bill can't be broken down", "row", 12, 7, f"""
-- Each bill is counted once, under the first of these it fails. Across rather
-- than up for the same reason as its neighbour, which also buys room to name
-- each outcome in full instead of abbreviating it to fit an axis.
SELECT CASE WHEN is_explainable                         THEN 'broken down'
            WHEN prior_bill_id IS NULL                   THEN 'first bill for the account'
            WHEN hdd_this_period IS NULL
              OR hdd_prior_period IS NULL                THEN 'weather missing'
            WHEN heating_sensitivity_kwh_per_hdd IS NULL THEN 'no heating fit'
            ELSE 'other reasons'
       END      AS "Outcome",
       count(*) AS "Bills"
FROM kea.mart_bill_explanation
WHERE account_id >= {FIRST_GENERATED}
GROUP BY 1
ORDER BY 2 DESC
""", axes(["Outcome"], ["Bills"], {"graph.show_values": True})),
        text("""
**Is it right?** *Not explained* is what the four drivers leave over, and the
chart below measures it against the change it was meant to account for: the
further down the list a bill falls, the less of its own movement the explainer
accounted for. Only bills that moved more than 5% are counted — on a bill that
barely moved, a couple of dollars of leftover swamps the comparison without
saying anything about the method. The leftover is never folded into a driver to
make the split look tidier.""", h=4),
        card("How much of the change the four drivers account for", "row", 24, 6, f"""
-- The section's own test, on bills that actually moved. `share` is the
-- residual as a fraction of the change, so a small share is a good breakdown
-- and a share above 1 means the leftover outweighs what the bill did.
WITH e AS (
    SELECT abs(delta_unexplained) / nullif(abs(amount_delta), 0) AS share
    FROM kea.mart_bill_explanation
    WHERE is_explainable
      AND account_id >= {FIRST_GENERATED}
      AND abs(amount_delta) >= 0.05 * abs(prior_total_amount)
)
-- Labelled as percentages of the change, the same scale the residual is
-- measured on. "three quarters of it or more" read as a judgement; 75% or more
-- is the number itself, and a reader can put two bands side by side without
-- translating either.
SELECT CASE WHEN share <= 0.25 THEN '75% or more'
            WHEN share <= 0.50 THEN '50–75%'
            WHEN share <= 1.00 THEN 'under 50%'
            -- Not a percentage, because there isn't one: past a share of 1 the
            -- leftover is bigger than the move it was meant to explain, and the
            -- part accounted for has gone negative.
            ELSE                    'none of it: the leftover is bigger than the change'
       END      AS "The four drivers account for"
     , count(*) AS "Bills"
FROM e
GROUP BY 1
ORDER BY min(share)
""", axes(["The four drivers account for"], ["Bills"], {"graph.show_values": True})),
    ]),

    ("2 · Bill forecast: what this bill will be, before it arrives", [
        card("Households with a bill forecast", "scalar", 12, 3, f"""
-- Counted against every household still on supply, not against the ones the
-- forecaster could use: an account without a good enough heating fit belongs
-- in this denominator, because from the household's side it simply has no
-- forecast.
WITH {OPEN_COHORT}
SELECT count(*) FILTER (WHERE EXISTS (SELECT 1 FROM kea.bill_forecast f
                                       WHERE f.account_id = o.account_id))
       || ' of ' || count(*) AS "Households with a bill forecast"
FROM open_cohort o
"""),
        card("Billing cycles forecast, one forecast a day", "scalar", 12, 3, """
SELECT to_char(count(DISTINCT (account_id, period_start)), 'FM999,999') || ' cycles, '
       || to_char(count(*), 'FM999,999') || ' daily forecasts'
       AS "Billing cycles forecast, one forecast a day"
FROM kea.bill_forecast
"""),
        text("""
**How it works today** (`hdd-linear-v2`). Every day of a billing cycle the
warehouse writes a snapshot of what is known so far. Snapshots are added, never
overwritten, so any past forecast can be replayed exactly as it was made. The
forecast job then adds the rest of the cycle:

- **kWh still to come** = the days left × the household's base use per day +
  the degree days still expected × its heating slope. The expected degree days
  come from climate normals for its region, fixed once for the cycle, so each
  day's real weather replaces part of the estimate rather than adding to it.
- **Base use and heating slope** come from one straight-line fit per household
  on its monthly averages, the same fit the explainer uses, so the two never
  disagree about a household. Monthly, because the readings are London's and
  the weather New Zealand's: the seasons line up, single days don't. *Colder
  months, more power* is the relationship being fitted.""", w=12, h=13),
        card("Colder months, more power", "combo", 12, 13, f"""
-- Generated households, on the days that have weather. Both measures come
-- from the same days, so a month with a gap in the weather is not set against
-- a fuller month of usage.
SELECT date_trunc('month', usage_date)::date AS "Month",
       round(avg(import_kwh), 2)             AS "kWh per household per day",
       round(avg(heating_degree_days), 2)    AS "Degree days per day"
FROM kea.mart_daily_usage
WHERE icp_id IN {COHORT_ICPS} AND heating_degree_days IS NOT NULL
GROUP BY 1
ORDER BY 1
""", axes(["Month"], ["kWh per household per day", "Degree days per day"], {
            "series_settings": {
                "kWh per household per day": {"display": "bar", "color": BLUE},
                "Degree days per day": {"display": "line", "axis": "right", "color": ORANGE}}})),
        text("""
**From kWh to a forecast bill,** shown for Household A beside this:

- **Dollars**: kWh priced at the previous bill's rate, plus the cycle's fixed
  daily charges, known on day one. Excl. GST.
- **The band** is ±15% of the cost still to come. What has been metered is not
  uncertain, so the band narrows as the cycle runs.
- **The warning**: a forecast 15% or more away from the same period last year
  is flagged, so the household hears early.
- **No guess.** A forecast is made only with a heating fit good enough to use,
  an earlier bill to price from, and readings and weather for every day so far.
  An account missing any of these gets no forecast.""", w=12, h=11),
        card("Household A: the forecast through its last full cycle", "line", 12, 11, f"""
-- One forecast per day of the cycle, each made with what was known that day,
-- against the bill that was issued at the end. Series names carry no `$`: at
-- half the page's width four legend entries only fit short, and the axis
-- title already says NZD.
WITH {HOUSEHOLD_A}, {LAST_FULL_CYCLE}
SELECT f.days_elapsed     AS "Day of the cycle",
       f.projected_amount AS "Forecast",
       f.projected_low    AS "Band low",
       f.projected_high   AS "Band high",
       b.charges_excl_gst AS "Final bill"
{FORECAST_AND_BILL}
JOIN household_a h ON h.account_id = f.account_id
JOIN last_cycle c ON c.period_start = f.period_start
ORDER BY 1
""", axes(["Day of the cycle"], ["Forecast", "Band low", "Band high", "Final bill"], {
            # ⚠️ One axis, always. Left to itself Metabase moved "Band low" to
            # a second axis, which drew the band's low edge above its high one.
            "graph.y_axis.auto_split": False,
            # ⚠️ The axis starts at zero and the four lines sit in the top third
            # of it. It stays that way. `graph.y_axis.auto_range: False` is the
            # only setting that would change it, and turning it off without a
            # min and a max leaves Metabase on its 0-100 default: the card drew
            # "Every data point is out of range" and no lines at all. Pinning a
            # min and max instead would fit this cohort and clip the next one,
            # since Household A is chosen by a rule and rebuilt with the data.
            "graph.x_axis.title_text": "Day of the billing cycle",
            "graph.y_axis.title_text": "NZD, excl. GST",
            "series_settings": {
                "Forecast": {"color": BLUE},
                "Band low": {"color": GREY, "line.style": "dashed"},
                "Band high": {"color": GREY, "line.style": "dashed"},
                "Final bill": {"color": GREEN}}})),
        text("""
**Is it right? Measured on every forecast whose bill has since arrived, in-sample.**
The heating fit behind each forecast saw every month, including the ones being
forecast, so the three panels below are a best case, not a result. *Did day 10
warn of a big rise?* counts only cycles with the same period a year earlier to
compare against.

**The known weakness is the rate.** Every kWh is priced at the previous bill's
average rate, and that average moves whenever the mix of hours does. It moves
most on Free Weekends, where weekend daytime use is free, so a cycle with a
different share of it can miss its bill even when fully metered; less on Time
of Use; hardly at all on Anytime Rate. The band allows only for usage still to
come, not for the rate, so as the cycle runs it shrinks faster than the error
and holds the final bill less often.""", h=6),
        card("Did day 10 warn of a big rise?", "table", 8, 7, f"""
-- Cycles whose bill has arrived and that have last year's bill for the same
-- period. Warned: some forecast by day 10 of the cycle was 15% or more above
-- last year's bill, the job's own warning line. Excl. GST on both sides.
-- A table, not a chart: this is a 2x2 of a couple of dozen cycles, and as
-- grouped bars the reader had to pair a legend colour with an axis label to
-- work out which cell was which. Spelled out, each row says what happened.
WITH c AS (
    SELECT bool_or(f.variance_pct >= 15 AND f.days_elapsed <= 10)    AS warned,
           max(b.charges_excl_gst) >= 1.15 * max(f.baseline_amount) AS rose
    {FORECAST_AND_BILL}
    WHERE f.baseline_amount IS NOT NULL
    GROUP BY f.account_id, f.period_start
), t AS (
    SELECT CASE WHEN rose AND warned THEN 1 WHEN rose THEN 2
                WHEN warned          THEN 3 ELSE 4 END AS ord,
           -- Short labels: a third of an iPad's width has room for about
           -- twenty characters beside the count.
           CASE WHEN rose AND warned THEN 'rose 15%+, warned'
                WHEN rose            THEN 'rose 15%+, not warned'
                WHEN warned          THEN 'warned, did not rise'
                ELSE                      'no rise, no warning'
           END AS label
    FROM c
)
-- A short header: at a third of the page's width the long one pushed "Cycles"
-- off the card and left the counts behind a scrollbar. What the column is
-- counted against is in the note above these three panels, where it applies to
-- all of them.
SELECT label    AS "What happened",
       count(*) AS "Cycles"
FROM t
GROUP BY ord, label
ORDER BY ord
"""),
        card("Median forecast error", "bar", 8, 7, f"""
-- Every forecast snapshot whose bill has arrived: the median gap between the
-- forecast and that bill, as a share of the bill. The title is short enough
-- for a third of an iPad's width; the axis says what it is broken down by.
SELECT {CYCLE_STAGE} AS "Days into the cycle",
       round((percentile_cont(0.5) WITHIN GROUP (
           ORDER BY abs(f.projected_amount - b.charges_excl_gst)
                    / nullif(b.charges_excl_gst, 0)) * 100)::numeric, 1)
           AS "Median error %"
{FORECAST_AND_BILL}
GROUP BY 1
ORDER BY min(f.days_elapsed)
""", axes(["Days into the cycle"], ["Median error %"], {
            # ⚠️ The y-axis title below does not render: a card this narrow
            # drops it, with no error and nothing in its place, which put a
            # bare `6.4` on the screen meaning 6.4%. The unit belongs on the
            # bar labels, where width can't take it away; the title stays for
            # the question view, which is wide enough to show it.
            "graph.show_values": True,
            "graph.y_axis.title_text": "Median error %, in-sample",
            **pct("Median error %")})),
        card("Final bill inside the band", "bar", 8, 7, f"""
-- The band is ±15% of the cost still to come, so it narrows as the cycle
-- runs. This measures what that does, snapshot by snapshot.
SELECT {CYCLE_STAGE} AS "Days into the cycle",
       round(100.0 * avg((b.charges_excl_gst BETWEEN f.projected_low
                                                 AND f.projected_high)::int), 1)
           AS "Final bill inside the band %"
{FORECAST_AND_BILL}
GROUP BY 1
ORDER BY min(f.days_elapsed)
""", axes(["Days into the cycle"], ["Final bill inside the band %"], {
            "graph.show_values": True,
            "graph.y_axis.title_text": "% of forecasts, in-sample",
            **pct("Final bill inside the band %")})),
    ]),

    ("3 · Plan recommendation: the cheapest plan for how a household uses power", [
        card("Households with a cheaper plan", "scalar", 8, 3, f"""
-- Counted against every household still on supply, the same base as sections 2
-- and 4, not against the ones the latest run managed to price. A household
-- whose readings can't be attributed gets no recommendation, and that belongs
-- in the denominator. A household counts once however many offers beat its
-- plan.
WITH {OPEN_COHORT}
SELECT count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM kea.plan_recommendation r
           WHERE r.account_id = o.account_id
             AND r.computed_at = {LATEST} AND r.is_recommended))
       || ' of ' || count(*) AS "Households with a cheaper plan"
FROM open_cohort o
"""),
        card("Total saving a year, incl. GST", "scalar", 8, 3, f"""
-- What the households with a cheaper plan would save between them, on usage
-- alone. A term credit is conditional on staying, so it is not counted.
SELECT to_char(sum(usage_saving), 'FM$999,999,990')
       AS "Total saving a year, incl. GST"
FROM kea.plan_recommendation
WHERE computed_at = {LATEST} AND is_recommended
"""),
        card("Bills re-priced to within $1", "scalar", 8, 3, f"""
-- The real bills behind every recommendation, re-priced on the household's
-- own plan and compared with what was actually issued. Incl. GST. The title is
-- short enough for a third of an iPad's width.
SELECT count(*) FILTER (WHERE abs(actual_bill_amount - current_modelled_amount) <= 1)
       || ' of ' || count(*) AS "Bills re-priced to within $1"
FROM kea.plan_bill_example
WHERE computed_at = {LATEST_EXAMPLES}
"""),
        text("""
**How it works today.** A batch job inside every publish, not a calculation made
while the customer waits:

- It takes each open account's last year of half-hour readings and prices them
  on every plan the household could move to (same lines company, same kind of
  user), on every term: flexible, 6 or 12 months.
- The pricing is the app's own tariff engine, imported rather than rewritten,
  so there is one implementation of the rates. A year of readings is first
  summed into a weekday × half-hour profile; that is exact, not an
  approximation, because every price band here is a weekday and a clock time.
- The current plan is re-priced by the same engine rather than read off past
  bills, so the saving compares like with like.""", w=12, h=11),
        card("Every household: what switching would save", "bar", 12, 11, f"""
-- The best offer for each household, bucketed by its saving on usage alone.
-- Every household still on supply appears exactly once, so the bars add up to
-- the scalar above: the two honest buckets are the households already on
-- their cheapest plan, and the ones the run couldn't price at all.
WITH {OPEN_COHORT}
SELECT CASE WHEN r.account_id IS NULL    THEN 'no recommendation'
            WHEN r.usage_saving <= 0     THEN 'already cheapest'
            WHEN r.usage_saving <  100   THEN 'under $100'
            WHEN r.usage_saving <  250   THEN '$100–250'
            WHEN r.usage_saving <  500   THEN '$250–500'
            ELSE                              '$500 or more'
       END      AS "Saving a year, incl. GST",
       count(*) AS "Households"
FROM open_cohort o
LEFT JOIN kea.plan_recommendation r ON r.account_id = o.account_id
     AND r.computed_at = {LATEST} AND r.rank = 1
GROUP BY 1
-- Ordered by the bucket's own smallest saving; the unpriced bucket has none,
-- so it is sorted before them all rather than falling wherever NULL lands.
ORDER BY min(coalesce(r.usage_saving, -1e9))
""", axes(["Saving a year, incl. GST"], ["Households"], {"graph.show_values": True})),
        text("""
**What makes it safe to act on.**

- A term credit can change the ranking, but a move is recommended **only if
  usage alone costs less**: a credit never manufactures a saving. Prices
  include GST, as a quote would.
- **Proof before advice.** The engine must reproduce issued bills to within $1
  before any comparison ships, and every recommendation carries the household's
  last three real bills re-priced on both plans.
- Accounts whose readings can't be attributed with confidence get no
  recommendation, and every run is kept, so *what did we tell this customer in
  August* always has an answer.""", w=12, h=9),
        card("Where the recommended moves go", "table", 12, 9, f"""
-- One row per move between plan families; one recommendation per household.
-- Column widths are fixed so the three fit half an iPad's width, the move
-- wrapping onto a second line rather than pushing the others off the side.
SELECT {family('c.plan_code')} || ' → ' || {family('n.plan_code')} AS "Move",
       count(*)                                                AS "Households",
       to_char(avg(r.usage_saving), 'FM$999,990')              AS "Avg saving a year"
FROM kea.plan_recommendation r
JOIN catalogue.plan c ON c.plan_id = r.current_plan_id
JOIN catalogue.plan n ON n.plan_id = r.candidate_plan_id
WHERE r.computed_at = {LATEST} AND r.is_recommended
GROUP BY 1
ORDER BY 2 DESC, 1
""", {**wrap("Move"), "table.column_widths": [130, 100, 130]}),
        card("Household A: what each plan would save it", "bar", 12, 8, f"""
-- Flexible term only, so the bars compare tariffs; term credits are in the
-- rationale below. GST-inclusive, as a quote would be.
--
-- The difference, not the level. Three plans cost this household within a few
-- per cent of each other, so as absolute annual costs on an axis that starts
-- at zero the bars were the same height and the recommendation -- the point of
-- the whole section -- was invisible. The axis can't be pinned instead:
-- Household A is chosen by a rule, so a rebuilt cohort brings its own numbers.
-- Against the current plan, zero is a real baseline and each bar is the saving
-- or the loss itself.
WITH {HOUSEHOLD_A}
SELECT {family('p.plan_code')}
       || CASE WHEN r.candidate_plan_id = r.current_plan_id THEN ' (current)' ELSE '' END
                                     AS "Plan",
       round(r.current_annual_cost - r.candidate_annual_cost)
                                     AS "A year cheaper (or dearer) by, incl. GST"
FROM kea.plan_recommendation r
JOIN household_a h ON h.account_id = r.account_id
JOIN catalogue.plan p ON p.plan_id = r.candidate_plan_id
WHERE r.computed_at = {LATEST} AND r.term_months = 0
ORDER BY 2 DESC
""", axes(["Plan"], ["A year cheaper (or dearer) by, incl. GST"], {
            "graph.show_values": True,
            # No axis title. A name this long doesn't fit the card's height and
            # was cut to "incl. GS", which is worse than nothing: the bars carry
            # the dollar sign themselves, the card's own title says what they are
            # measured against, and the section's note says prices include GST.
            "graph.y_axis.labels_enabled": False,
            **money("A year cheaper (or dearer) by, incl. GST", decimals=0)})),
        card("Household A: when it uses power", "line", 12, 8, f"""
-- Why one plan beats another: the hours a time-of-use plan prices
-- differently.
WITH {HOUSEHOLD_A}
SELECT p.hour_of_day AS "Hour of day",
       CASE WHEN extract(month FROM p.profile_month) IN (6, 7, 8) THEN 'Winter (Jun–Aug)'
            ELSE 'Summer (Dec–Feb)' END AS "Season",
       round(sum(p.avg_import_kwh * p.sample_days) / nullif(sum(p.sample_days), 0), 3)
           AS "kWh in the hour"
FROM kea.mart_hourly_profile p
JOIN billing.account a ON a.icp_id = p.icp_id
JOIN household_a h ON h.account_id = a.account_id
WHERE extract(month FROM p.profile_month) IN (6, 7, 8, 12, 1, 2)
GROUP BY 1, 2
ORDER BY 1, 2
""", axes(["Hour of day", "Season"], ["kWh in the hour"], {
            "series_settings": {"Winter (Jun–Aug)": {"color": BLUE},
                                "Summer (Dec–Feb)": {"color": ORANGE}}})),
        card("Household A: its last three bills, re-priced", "table", 24, 5, f"""
-- Real, issued bills. The engine re-prices each on the household's own plan,
-- which should land within $1 of what was issued, and on the recommended
-- plan. Incl. GST, as issued.
-- The four amounts are formatted, not selected raw: straight out of the mart
-- they printed as 189.23 next to 10.2, which reads as two kinds of number and
-- as nothing like money. The dollar sign moves from the header to the values,
-- where it belongs.
WITH {HOUSEHOLD_A}
SELECT to_char(e.period_start, 'FMDD Mon') || ' – '
       || to_char(e.period_end, 'FMDD Mon YYYY') AS "Bill period",
       e.actual_bill_amount                      AS "Issued",
       e.current_modelled_amount                 AS "Re-priced on its plan",
       e.candidate_modelled_amount               AS "Recommended plan",
       e.usage_saving                            AS "Saving"
FROM kea.plan_bill_example e
JOIN household_a h ON h.account_id = e.account_id
WHERE e.computed_at = {LATEST_EXAMPLES}
ORDER BY e.period_end
""", money("Issued", "Re-priced on its plan", "Recommended plan", "Saving")),
        card("Household A: the recommendation, in its own words", "table", 24, 8, f"""
-- One row per field rather than one column: as a column the rationale ran off
-- the side of the card; as a row it wraps across the page's full width. The
-- widths are fixed: measured, the wrapped column asked for more than an iPad's
-- page has and pushed the table into a sideways scroll.
WITH {HOUSEHOLD_A}
SELECT v.what  AS "The recommendation",
       v.value AS "Value"
FROM kea.plan_recommendation r
JOIN household_a h ON h.account_id = r.account_id
JOIN catalogue.plan p ON p.plan_id = r.candidate_plan_id
CROSS JOIN LATERAL (VALUES
    (1, 'Plan', {family('p.plan_code')}),
    (2, 'Term', CASE r.term_months WHEN 0 THEN 'Flexible'
                ELSE r.term_months || ' months' END),
    (3, 'Saving from usage, a year, incl. GST', '$' || to_char(r.usage_saving, 'FM999,990')),
    (4, 'Conditional term credit', '$' || to_char(r.term_credit, 'FM999,990')),
    (5, 'Rationale, as the job wrote it', r.rationale)
) v(ord, what, value)
WHERE r.computed_at = {LATEST} AND r.is_recommended
ORDER BY v.ord
""", {**wrap("Value"), "table.column_widths": [200, 540]}),
    ]),

    ("4 · Hardship: noticing a household that is struggling, before it has to ask", [
        card("Households flagged in the latest assessment", "scalar", 12, 3, f"""
-- Every open account is assessed on every run; a clean result is recorded too.
-- Counted against the same base as sections 2 and 3, so a household the run
-- somehow missed would show in the denominator rather than disappear from both
-- sides of it.
WITH {OPEN_COHORT}
SELECT count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM kea.hardship_assessment a
           WHERE a.account_id = o.account_id
             AND a.computed_at = {LATEST_HARDSHIP} AND a.signal_count > 0))
       || ' of ' || count(*) AS "Households flagged in the latest assessment"
FROM open_cohort o
"""),
        card("Flagged with more than one signal", "scalar", 12, 3, f"""
SELECT count(*) FILTER (WHERE signal_count > 1) AS "Flagged with more than one signal"
FROM kea.hardship_assessment
WHERE computed_at = {LATEST_HARDSHIP}
"""),
        card("Flagged, and medically dependent or vulnerable", "scalar", 12, 3, f"""
-- Care obligations, recorded on the assessment beside the signals, never as
-- signals.
SELECT count(*) FILTER (WHERE signal_count > 0
                          AND (is_medically_dependent OR is_vulnerable))
       AS "Flagged, and medically dependent or vulnerable"
FROM kea.hardship_assessment
WHERE computed_at = {LATEST_HARDSHIP}
"""),
        card("Outreach messages created, and sent", "scalar", 12, 3, """
-- The job only creates a pending in-app message. Sending is a separate,
-- authorised step, and 'sent' counts what that step has done.
SELECT count(*) || ' created, ' || count(sent_at) || ' sent'
       AS "Outreach messages created, and sent"
FROM kea.alert
WHERE kind = 'hardship_outreach'
"""),
        text("""
**How it works today.** An identification job: not a score, and not a
collection process. Every open account is assessed on every run, and a clean
result is recorded too, because *we looked and found nothing* is a different
answer from *we never looked*.

**Five signals, a closed list** (adding one takes a schema change), each a rule
that can be explained to the household it was applied to:

- **Direct debit failed** in the last 180 days.
- **Late payment streak**: two or more bills in a row paid after the due date.
- **Part-payment arrears**: two or more bills in a row only partly paid.
- **Payments stopped**: none for 60 days after an established history, with two
  or more bills unpaid.
- **Usage collapse**: the last seven days' use 40% or more below the three weeks
  before. It is checked only when the readings are complete, so a meter gap
  can't pass for a household that has stopped heating.""", w=12, h=13),
        card("Care signals in the latest assessment", "row", 12, 9, f"""
-- A closed list of reasons, not a score, so staff can see why a household
-- was flagged. Counts only.
--
-- Bars run across: five signal names this long tilted to 45° as columns and
-- took the plot's height with them. Across, each name is written out flat.
-- Shorter than the list of rules beside it, and deliberately: a row chart
-- stretches its bars to fill whatever height it is given, and at the list's
-- full height five counts became five slabs. Sentence case, not initcap, so
-- each row reads as the rule is named in that list.
SELECT upper(left(replace(s.signal_code::text, '_', ' '), 1))
       || lower(substr(replace(s.signal_code::text, '_', ' '), 2)) AS "Signal",
       count(*)                                                    AS "Households"
FROM kea.hardship_signal s
JOIN kea.hardship_assessment a USING (assessment_id)
WHERE a.computed_at = {LATEST_HARDSHIP}
GROUP BY 1
ORDER BY 2 DESC, 1
""", axes(["Signal"], ["Households"], {"graph.show_values": True})),
        text("""
**Medically dependent** and **vulnerable** are recorded beside the signals,
never as signals: they are care obligations, not evidence of difficulty.

The evidence is for staff. A flagged household would see one gentle in-app
message, with no amount, no reason and no diagnosis. The job only creates it:
nothing is sent until a separate, authorised step sends it, and a household
never has more than one open.""", w=12, h=8),
        card("What a flagged household would see", "table", 12, 8, """
-- The message itself, the same for every household. The evidence behind a
-- flag stays with staff. One row per part rather than one column each: at half
-- the page's width the message wraps down the card instead of off its side.
SELECT v.what AS "Part", v.body AS "What it says"
FROM (SELECT DISTINCT subject, body FROM kea.alert
      WHERE kind = 'hardship_outreach') a
CROSS JOIN LATERAL (VALUES (1, 'Subject', a.subject),
                           (2, 'Message', a.body)) v(ord, what, body)
ORDER BY v.ord
""", {**wrap("What it says"), "table.column_widths": [90, 270]}),
    ]),

    ("5 · The pipeline behind all four", [
        text("""
**Four source systems → S3 lake → Snowflake → the backend's Postgres → the app
and its agent.**

- **Extract.** Metering, billing, catalogue and weather land in the lake twice:
  raw, exactly as sent, then parsed to Parquet by Spark, with rows that fail
  parsing quarantined and counted. MinIO stands in for S3 locally.
- **Transform.** dbt builds staging → silver → gold → marts in Snowflake. A
  correction arrives as a new version of a reading, not an edit: silver keeps
  the highest version of each half-hour, and the lake keeps every version. The
  heating fit, the bill explanation and the forecast snapshots are built here.
- **Publish.** The marts are exported to the lake as sealed files. One database
  transaction then loads them into the backend's Postgres, runs the feature
  jobs above (forecast, plan pricing with its $1 bill check, hardship and
  outreach), validates the result and records the release. If any step fails,
  the previous release stays served, unchanged.
- **Run and check.** Airflow schedules it, Soda checks the warehouse and the
  served data, and Marquez records lineage. Secrets stay out of git, in an
  untracked `.env`.""", h=8),
        card("Readings landed in the lake", "scalar", 12, 3, f"""
-- Every version of every reading, as delivered.
WITH {LAKE}
SELECT sum(row_count) AS "Readings landed in the lake"
FROM p
WHERE entity = 'interval_reads' AND layer = 'raw'
  AND (mode = 'incremental' OR newest = 1)
""", db=PIPELINE_DB),
        card("Rows quarantined by the parser", "scalar", 12, 3, f"""
-- Rows Spark could not parse, across every table. Counted on the parsed
-- layer, which is where quarantine happens.
WITH {LAKE}
SELECT coalesce(sum(quarantined_rows), 0) AS "Rows quarantined by the parser"
FROM p
WHERE layer = 'parsed' AND (mode = 'incremental' OR newest = 1)
""", db=PIPELINE_DB),
        card("Releases published to serving", "scalar", 12, 3, """
SELECT count(*) AS "Releases published to serving" FROM data_ops.serving_publication
"""),
        card("Marts whose last load worked", "scalar", 12, 3, """
-- The latest attempt per mart. Earlier failures stay in the log; this asks
-- whether any mart is failing now.
SELECT count(*) FILTER (WHERE status::text = 'succeeded') || ' of ' || count(*)
       AS "Marts whose last load worked"
FROM (SELECT DISTINCT ON (mart_name) mart_name, status
      FROM kea.mart_load_log
      ORDER BY mart_name, started_at DESC) latest
"""),
        card("What the lake holds, by source system", "table", 24, 6, f"""
WITH {LAKE}
SELECT replace(source_system, 'src_', '')                    AS "Source",
       count(DISTINCT entity)                                AS "Tables",
       sum(row_count) FILTER (WHERE layer = 'raw')           AS "Raw rows",
       sum(row_count) FILTER (WHERE layer = 'parsed')        AS "Parsed rows",
       coalesce(sum(quarantined_rows) FILTER (WHERE layer = 'parsed'), 0)
                                                             AS "Quarantined"
FROM p
WHERE mode = 'incremental' OR newest = 1
GROUP BY 1
ORDER BY 3 DESC
""", db=PIPELINE_DB),
        card("How late half-hour readings arrive", "table", 14, 8, """
-- How long after the half-hour it measures each reading reached the source,
-- and how many of those arrivals were corrections to a reading already sent.
-- The share is a formatted percentage, not a bare number with a % in the
-- header: beside a count of readings, `1.1` next to `1.35` reads as two kinds
-- of quantity. Short headers: the four columns share a card just over half
-- an iPad's width.
SELECT a.lag_bucket                                   AS "Arrived after",
       a.rows                                         AS "Readings",
       100.0 * a.rows / sum(a.rows) OVER ()           AS "Share",
       a.corrections                                  AS "Corrections"
FROM lake_audit.arrival_lag a
ORDER BY a.min_lag_days
""", pct("Share"), db=PIPELINE_DB),
        text("""
**Late data.** Most readings arrive within a day, and a tail arrives weeks
later, including corrections to readings already loaded. So the metering
extract is incremental on arrival time (`received_at`), not on the half-hour a
reading measures: a watermark on the reading's own time would never pick up a
late correction.""", w=10, h=8),
    ]),
]


def guard(name: str, sql: str) -> None:
    hit = IDENTIFIERS.search(re.sub(r"--[^\n]*", "", sql))
    if hit:
        sys.exit(f"refusing '{name}': it reads {hit.group(0)}, and this page shows no identifiers")


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
    cards = [it for _, items in SECTIONS for it in items if it["kind"] == "card"]
    names = [c["name"] for c in cards]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        sys.exit(f"card names are the match key and must be unique: {dupes}")
    for c in cards:
        guard(c["name"], c["sql"])
    # CI stops here (`make ci-story-guard`): no .env, no network, no Metabase.
    if "--check" in sys.argv[1:]:
        print(f"{len(cards)} cards: names unique, no identifiers read")
        return

    e = env()
    s = api("/api/session", {"username": e["METABASE_ADMIN_EMAIL"],
                             "password": e["METABASE_ADMIN_PASSWORD"]})["id"]

    dbs = api("/api/database", session=s)
    ids = {d["name"]: d["id"] for d in dbs.get("data", dbs)}
    missing = [n for n in (SERVING_DB, PIPELINE_DB) if n not in ids]
    if missing:
        sys.exit(f"not connected: {missing} — run `make metabase-setup` first")

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
                    "description": "Built by analysis/metabase/build_story_dashboard.py"},
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

    # Shelf packing: cards in a row differ in height, so the next row starts
    # below the TALLEST in the current one. Text notes pack like cards, so a
    # note sits next to what it describes.
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

    # The description goes with the layout so an existing dashboard's text
    # follows the page it describes.
    api(f"/api/dashboard/{dash_id}", {"dashcards": dashcards, "description": DESCRIPTION},
        session=s, method="PUT")
    print(f"dashboard: {MB}/dashboard/{dash_id}")

    # This sub-collection is code-owned: archive questions no longer built, so
    # a renamed panel can't linger and be mistaken for a live one.
    stale = [(c["id"], c["name"]) for c in
             api(f"/api/collection/{coll}/items?models=card", session=s).get("data", [])
             if c["name"] not in card_ids]
    for cid, cname in stale:
        api(f"/api/card/{cid}", {"archived": True}, session=s, method="PUT")
        print(f"  archived stale question: {cname}")


if __name__ == "__main__":
    main()
