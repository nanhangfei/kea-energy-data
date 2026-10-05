#!/usr/bin/env python3
"""Build the Sprint 4 dashboard in Metabase: detected loads, solar and EV.

    make dashboards ONLY=sprint4

WHAT IS NEW HERE, AND WHY IT NEEDED ITS OWN PAGE.

The Sprint 3 page plots what the pipeline DECIDED -- a recommendation, an
assessment, an outreach. Sprint 4 publishes two things that are neither an
observation nor a decision, and each needs its own framing:

  * EVIDENCE.  `kea.mart_daily_detected_load` and `kea.mart_charging_session`
    say what a run of half-hours LOOKED LIKE, as a pair of bounds and a
    confidence. They do not say what appliance it was, because the meter
    never saw an appliance. Every heading here says "shaped like", and the
    two panels in section 3 exist to stop a reader hearing "your EV".
  * WHAT-IFS.  `kea.solar_estimate` and `kea.ev_scenario` price a year that
    never happened, from an assumptions file. A panel that showed them
    without their assumptions would be a forecast wearing a measurement's
    clothes, so the assumptions are a panel, not a footnote.

The third stretch goal, the hardship early warning, is a STAFF QUEUE and is
built by build_plans_dashboard.py. Section 8 here shows only what the ruleset
found, and points at that page rather than repeating it: an outreach list has
names on it, and this page has none.

⚠️ SECTION 7 IS THE ONE THAT SAYS WHAT WE CANNOT SEE. A meter records import
and export on two separate registers and NEVER records generation, so on a
household with panels the import register is blind while they export and
short by an unknown amount while they generate. detect-v2 marks those
half-hours and withholds the claims they make unsafe; the section exists
because a withheld class reads as "no EV found" to anyone not told otherwise.

⚠️ NO CUSTOMER IDENTIFIERS, BUT THE HOUSEHOLDS ARE NAMED. Every query is still
checked against IDENTIFIERS before it is sent, so no ICP number, customer
name, address, email, phone or account number reaches a cell. What this page
now does show is `icp_id` -- the surrogate key of `metering.icp`, NOT the
`icp_number` printed on a bill -- because it is the key every card here is
keyed on, and the one thing a data engineer needs in order to go and look at
the household a card is about. That is a deliberate difference from the demo
walkthrough, which shows no identifier: THIS PAGE IS AN INTERNAL ONE. An icp_id on
it reaches a named customer in one join, so it is not a page to open in front
of an audience.

The five households shown up close are still picked by a rule inside the
query, and every one of those rules ends its ORDER BY on an id, so two builds
of the same data land on the same households. Each rule has ONE definition --
the PICK_* constants -- spliced both into the cards that draw it and into
RESOLVE_HOUSEHOLDS, which reads the id into the card's title before any card
is written. A rebuilt cohort therefore renames the cards, and cannot leave a
heading naming a household the chart below it no longer draws.

⚠️ ONE HOUSEHOLD IS PINNED BY HAND, AND IT IS THE EXCEPTION THAT PROVES THE
RULE ABOVE. The four-register panel that closes section 7 is the picture the
customer-facing site is to be built from, so it must keep drawing the same
household the site does -- DEMO_ICP -- rather than whatever a rule ranks first
after the next rebuild. Because a pinned id CAN go stale where a rule cannot,
PICK_DEMO re-checks the three properties the panel depends on instead of
trusting the number, and the panel renders EMPTY rather than drawing a
different household under an unchanged title.

⚠️ EVERY APPEND-ONLY TABLE IS PINNED. `solar_estimate`, `ev_scenario` and
`hardship_assessment` keep every generation; a panel that forgets
`computed_at = (SELECT max(...))` does not error and does not look wrong, it
just multiplies as further generations arrive. The pin is repeated in every
query rather than shared, because a Metabase native question is one statement
with nowhere to keep a CTE.

⚠️ TWO DATABASES, AND NO CARD JOINS THEM. What was PUBLISHED is in the
serving database; what was PLANTED when the source data was generated is in
`gen_audit` on the pipeline sandbox. Section 3 puts the two counts side by
side and says so. Matching them household by household is
`make evaluate-sprint4`, whose report is the place for precision and recall;
a dashboard that appeared to do it across two connections would be inventing
a join Metabase never ran.

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
PIPELINE_DB = "Kea pipeline"    # kea-source-db: the source systems and gen_audit
COLLECTION = "Kea Energy"
SUBCOLLECTION = "Sprint 4 — detected loads, solar and EV"
DASHBOARD = "Sprint 4 — what the meter can see, and two what-ifs"
DESCRIPTION = ("Sprint 4's three stretch goals: detected loads as bounded evidence, "
               "solar payback, EV charging what-ifs, and where the early warning lives.")
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# The list build_demo_dashboard.py also guards against. A card that reads one
# of these is refused rather than published. ⚠️ `icp_id` is deliberately NOT on
# it: it is a surrogate key with no meaning outside our own database, and
# naming it is the whole point of the titles below. `icp_number` -- the one
# printed on the bill -- and `account_number` stay refused.
IDENTIFIERS = re.compile(
    r"\b(icp_number|icp_identifier|customer_name|full_name|email|phone"
    r"|address_display|account_number|billing\.customer)\b", re.IGNORECASE)

# ⚠️ The one exemption. `gen_audit.planted_load` is keyed by icp_identifier, so
# counting the households that were given an EV cannot avoid naming the column.
# A COUNT DISTINCT of it is a count of households, not an identifier: nothing
# reaches a cell. Anything else -- selecting it, grouping by it, joining on it
# -- still trips the guard, and test_sprint4_dashboard.py holds that line.
COUNTED = re.compile(r"count\(\s*distinct\s+[a-z_.]*(icp_identifier|icp_number)\s*\)",
                     re.IGNORECASE)

# Generated source ids start here; the nine hand-built fixtures sit below it.
FIRST_GENERATED = 1_000_000

# One population behind every "N of M" on this page: the generated households
# still on supply. A household the detector found nothing
# in is counted, not dropped.
COHORT = f"""cohort AS (
    SELECT DISTINCT icp_id FROM billing.account
    WHERE end_date IS NULL AND account_id >= {FIRST_GENERATED} AND icp_id IS NOT NULL
)"""

# ⚠️ The serving database keeps 13 months of half-hour readings; the detection
# marts go back further, to households that joined the trial early. Mixing the
# two makes "N of M days" mean one thing at the top of the page and another at
# the bottom, so every panel here reads the same last 12 months -- the window
# in which the whole cohort has readings, and the only window the half-hourly
# panels in section 2 could draw at all.
LAST_YEAR = "(SELECT max(usage_date) - 365 FROM kea.mart_daily_usage)"

LATEST_SOLAR = "(SELECT max(computed_at) FROM kea.solar_estimate)"
# One label per system: panels alone ("5 kW"), with a battery storing only
# the surplus ("5 kW + 13.5 kWh battery") or one also charged from the grid
# ("5 kW + 13.5 kWh battery, grid-charged"). Every solar card groups by it,
# because a publication writes all three for a size and a card grouped by size
# alone would blend them.
SOLAR_SYSTEM = """(rtrim(rtrim(system_kw::text, '0'), '.') || ' kW'
        || CASE WHEN battery_kwh > 0
                THEN ' + ' || rtrim(rtrim(battery_kwh::text, '0'), '.') || ' kWh battery'
                ELSE '' END
        || CASE WHEN battery_dispatch = 'grid_charge' THEN ', grid-charged'
                ELSE '' END)"""
# Panels alone, then the surplus-only battery, then the grid-charged one.
SOLAR_ORDER = ("min(system_kw), min(battery_kwh), "
               "bool_or(battery_dispatch = 'grid_charge')")
LATEST_EV = "(SELECT max(computed_at) FROM kea.ev_scenario)"
LATEST_HARDSHIP = "(SELECT max(computed_at) FROM kea.hardship_assessment)"
LATEST_PLANT = "(SELECT max(run_id) FROM gen_audit.planted_load)"

# ⚠️ AN EXPORT REGISTER IS THE ONLY THING SERVING KNOWS ABOUT PANELS. Import
# and export are two separate, non-negative registers and generation is never
# metered at all, so there is no "kWp" column anywhere below -- array size is a
# generator fact, it lives in cohort.json, and it belongs in the evaluation
# report where its source is visible. Section 7 shows measured export instead.
GENERATING = """generating AS (
    SELECT DISTINCT g.icp_id FROM metering.meter_register g
    WHERE g.direction = 'export'
)"""

# ⚠️ ONE DEFINITION PER HOUSEHOLD. Each rule below is spliced into every card
# that draws that household AND into RESOLVE_ACCOUNTS, which reads the account
# id for the card's title. Two copies of a rule is two different households the
# day one copy is edited -- and the failure is silent, because both copies
# return exactly one row either way.
PICK_A = f"""household_a AS (
    -- The most wallbox-shaped days in the last 12 months.
    SELECT d.icp_id
    FROM kea.mart_daily_detected_load d
    JOIN cohort c ON c.icp_id = d.icp_id
    WHERE d.ev_charger_class = 'high_power' AND d.usage_date > {LAST_YEAR}
    GROUP BY d.icp_id
    ORDER BY count(*) DESC, d.icp_id
    LIMIT 1
)"""

PICK_B = f"""household_b AS (
    -- The most night-block days in the last 12 months.
    SELECT d.icp_id
    FROM kea.mart_daily_detected_load d
    JOIN cohort c ON c.icp_id = d.icp_id
    WHERE d.timed_night_half_hours > 0 AND d.usage_date > {LAST_YEAR}
    GROUP BY d.icp_id
    ORDER BY count(*) DESC, d.icp_id
    LIMIT 1
)"""

# Section 7's three are all picked from the same shape: every published run of
# every generating household in the cohort, with the half-hours the panels hid.
RUNS = """runs AS (
    SELECT s.icp_id, s.half_hours, coalesce(s.masked_half_hours, 0) AS masked,
           s.charger_class, s.confidence
    FROM kea.mart_charging_session s
    JOIN cohort c ON c.icp_id = s.icp_id
    JOIN generating g ON g.icp_id = s.icp_id
)"""

PICK_C = """household_c AS (
    -- The control: most runs the panels never touched.
    SELECT r.icp_id FROM runs r GROUP BY r.icp_id
    ORDER BY count(*) FILTER (WHERE r.masked = 0) DESC, r.icp_id LIMIT 1
)"""

PICK_D = """household_d AS (
    -- The split one: as close to half its runs masked as the cohort offers.
    SELECT r.icp_id FROM runs r GROUP BY r.icp_id
    ORDER BY abs(0.5 - avg(CASE WHEN r.masked > 0 THEN 1.0 ELSE 0.0 END)),
             count(*) DESC, r.icp_id LIMIT 1
)"""

PICK_E = """household_e AS (
    -- The hidden one: the largest share of run half-hours behind the panels.
    SELECT r.icp_id FROM runs r GROUP BY r.icp_id
    HAVING sum(r.half_hours) > 0
    ORDER BY sum(r.masked)::numeric / sum(r.half_hours) DESC, count(*) DESC,
             r.icp_id LIMIT 1
)"""

# ⚠️ THE ONE HOUSEHOLD ON THIS PAGE THAT IS PINNED BY HAND, AND WHY.
# Every other household here is picked by a rule, so a rebuilt cohort picks its
# own. This one is not: the four-register panel in section 7 is the picture the
# customer-facing site will be built from, and it has to keep drawing the
# household that demo was agreed on rather than whatever a rule ranks first the
# next time the data moves.
#
# The cost is real and is paid for below. icp_id is GENERATED ALWAYS AS
# IDENTITY: `make rebuild-all` renumbers it, and a stale number would draw a
# *different* household under a title that still says this one -- wrong, not
# empty, which is the worse of the two failures. So PICK_DEMO re-checks every
# property the panel depends on instead of trusting the number: in the cohort,
# it exports, and it has at least one charging run the panels never hid. A
# household that stops being solar+EV drops out and the panel renders EMPTY,
# which is the signal to come and look.
# Re-pinned 2 Oct 2026, when the cohort grew to 200 and renumbered every ICP:
# the SOLAR_PLAN "evening" host. On its demo day (21 Dec 2026) export ends by
# 19:00 and the wallbox block starts at 19:30, so the whole charge is on the
# import register -- what 1027 showed before.
DEMO_ICP = 1086
PICK_DEMO = f"""demo_household AS (
    SELECT c.icp_id
    FROM cohort c
    JOIN generating g ON g.icp_id = c.icp_id
    WHERE c.icp_id = {DEMO_ICP}
      AND EXISTS (SELECT 1 FROM kea.mart_charging_session s
                  WHERE s.icp_id = c.icp_id
                    AND coalesce(s.masked_half_hours, 0) = 0)
)"""

# The day is picked by rule even though the household is not, and it gets the
# same one-definition treatment: this constant is spliced into the card that
# draws the day AND into RESOLVE_HOUSEHOLDS, which reads the date into the
# card's title. A second copy would let the heading name a day the chart below
# it does not draw, and both copies would go on returning exactly one row.
PICK_DEMO_DAY = f"""demo_day AS (
    SELECT d.icp_id, d.usage_date
    FROM kea.mart_daily_detected_load d
    JOIN demo_household h ON h.icp_id = d.icp_id
    JOIN kea.mart_daily_usage u ON u.icp_id = d.icp_id AND u.usage_date = d.usage_date
    WHERE d.ev_half_hours > 0 AND d.usage_date > {LAST_YEAR}
      AND u.export_kwh > 0
    ORDER BY u.export_kwh DESC, d.ev_kwh_low DESC, d.usage_date
    LIMIT 1
)"""

# A card title is a plain Python string and cannot query anything, so it carries
# a placeholder until resolve_households() has read the real id. The letter is
# never meant to reach Metabase; if it does, the household it stands for had no
# rows, and seeing `ICP <C>` on the page is the signal to go and look.
#
# ⚠️ icp_id, not account_id. Everything this page measures -- the detection
# marts, mart_daily_usage and metering.meter_read_hh -- is keyed on icp_id, so
# an icp_id in the heading is the key the card's own query already uses. An
# account id would have to be resolved through billing.account before any of
# those tables could be read at all.
HOUSEHOLD = {letter: f"ICP <{letter}>" for letter in "ABCDE"}
NAMED = "ICP {icp_id}"

# The demo panel's heading names its day for the same reason the others name
# their household: the chart's x-axis is a clock, so without this nothing on
# the card says WHICH day, or that it is a summer one -- and the solar arc on
# the same household in June is visibly shorter. The date is a query result, so
# it carries a placeholder here and is read at build time like the ids.
DAY_KEY = "day"
DEMO_DAY = "<the day>"
PLACEHOLDER = {**HOUSEHOLD, DAY_KEY: DEMO_DAY}

# One round trip, built from the same PICK_* constants the cards splice in, so
# the id in a heading is by construction the id that heading's own card draws.
RESOLVE_HOUSEHOLDS = f"""
WITH {COHORT}, {GENERATING}, {RUNS}, {PICK_A}, {PICK_B}, {PICK_C}, {PICK_D}, {PICK_E},
     {PICK_DEMO}, {PICK_DEMO_DAY}
-- Cast to text because the last row is a date and the rest are ids. What the
-- title says is decided in Python, except for the date's wording, which is
-- decided here so that one to_char is the only place it lives.
          SELECT 'A' AS key, icp_id::text AS value FROM household_a
UNION ALL SELECT 'B', icp_id::text FROM household_b
UNION ALL SELECT 'C', icp_id::text FROM household_c
UNION ALL SELECT 'D', icp_id::text FROM household_d
UNION ALL SELECT 'E', icp_id::text FROM household_e
UNION ALL SELECT '{DAY_KEY}', to_char(usage_date, 'FMDD FMMonth YYYY') FROM demo_day
ORDER BY 1
"""

# dbt vars detect_cold_months and detect_warm_months, restated for the one
# panel that needs them (section 2's winter-against-summer night profile).
# test_sprint4_dashboard.py reads dbt_project.yml and fails if they drift.
COLD_MONTHS = (6, 7, 8)
WARM_MONTHS = (12, 1, 2)

# The Sprint 3 codes, and the three Sprint 4 warning signs. Restated in SQL
# because a panel cannot import the job; test_hardship_alerts.py keeps these
# equal to pipeline/scripts/decisions/create_hardship_alerts.py's own lists.
V1_CODES = ("direct_debit_failed", "late_payment_streak", "part_payment_arrears",
            "payments_stopped", "usage_collapse")
EARLY_SPIKE = "bill_above_usual_payment"
EARLY_WITH = ("first_shortfall", "payment_drift")


def _arr(codes) -> str:
    return "ARRAY[" + ", ".join(f"'{c}'" for c in codes) + "]"


CODES_OF = """array(SELECT s.signal_code::text FROM kea.hardship_signal s
                    WHERE s.assessment_id = a.assessment_id ORDER BY 1)"""
RULE_SAYS = (f"CASE WHEN c.codes && {_arr(V1_CODES)} THEN 'flagged' "
             f"WHEN '{EARLY_SPIKE}' = ANY(c.codes) AND c.codes && {_arr(EARLY_WITH)} "
             "THEN 'early warning' "
             "WHEN cardinality(c.codes) > 0 THEN 'warning signs only' ELSE 'clear' END")

# Both classes in one column, in the words the page uses everywhere else. The
# meter sees a shape; the class names in the table are what the shape matched.
# detect-v2 withholds the charger class when solar masks part of a detected
# charging run, so NULL must remain visibly unknown rather than becoming a
# portable charger or a timer-shaped night block.
SESSION_SHAPE = """CASE
    WHEN s.charger_class = 'high_power' THEN 'wallbox-shaped (5 kW and up)'
    WHEN s.charger_class = 'low_power' THEN 'portable-shaped (under 5 kW)'
    WHEN s.charger_class IS NULL THEN 'charger class unknown (solar-masked)'
    ELSE 'charger class unknown'
END"""

SHAPE = """CASE
    WHEN d.ev_charger_class = 'high_power' THEN 'charging, wallbox-shaped'
    WHEN d.ev_charger_class = 'low_power' THEN 'charging, portable-shaped'
    WHEN d.ev_charger_class IS NULL AND d.ev_half_hours > 0
        THEN 'charging, class unknown (solar-masked)'
    WHEN d.timed_night_half_hours > 0 THEN 'night block, timer-shaped'
    ELSE 'detection, shape unknown'
END"""

# A plan code is KEA_<family>_<network>_<user type>; the family is the part a
# customer would recognise, and the rest is the same across the whole generated
# cohort. The EV panels compare families, so they all read this.
SIGN_SAYS = """CASE c.code
    WHEN 'direct_debit_failed'      THEN 'a direct debit failed'
    WHEN 'late_payment_streak'      THEN 'late payments, repeatedly'
    WHEN 'part_payment_arrears'     THEN 'part payments, arrears growing'
    WHEN 'payments_stopped'         THEN 'payments stopped'
    WHEN 'usage_collapse'           THEN 'usage collapsed'
    WHEN 'bill_above_usual_payment' THEN 'a bill well above the usual payment'
    WHEN 'payment_drift'            THEN 'payments drifting later'
    WHEN 'first_shortfall'          THEN 'a first shortfall'
    ELSE c.code END"""

PLAN_FAMILY = ("CASE split_part(p.plan_code, '_', 2) "
               "WHEN 'ANY' THEN 'Anytime Rate' "
               "WHEN 'TOU' THEN 'Time of Use' "
               "WHEN 'WKND' THEN 'Free Weekends' ELSE p.plan_code END")


def axes(dims, metrics, more=None):
    """Axis assignments are declared, never guessed. verify.py fails a card whose
    query returns a column that is neither a dimension nor a metric, because
    Metabase drops such a column from the chart without an error."""
    return {"graph.dimensions": list(dims), "graph.metrics": list(metrics), **(more or {})}


def stacked(dims, metrics):
    return axes(dims, metrics, {"stackable.stack_type": "stacked"})


def fmt(settings: dict) -> dict:
    """Per-column formatting, and on a chart also what the value labels use: a
    y-axis title is dropped on a narrow card without warning, and the label is
    then the only place the unit survives. One call per card -- a second
    column_settings key would replace the first."""
    return {"column_settings": {
        json.dumps(["name", column], separators=(",", ":")): spec
        for column, spec in settings.items()}}


def money(*columns: str, decimals: int = 0) -> dict:
    return fmt({c: {"prefix": "$", "decimals": decimals} for c in columns})


def card(name, display, w, h, sql, viz=None, db=SERVING_DB):
    return {"kind": "card", "name": name, "display": display, "w": w, "h": h,
            "sql": sql, "viz": viz or {}, "db": db}


def text(markdown, w=24, h=4):
    return {"kind": "text", "text": markdown.strip(), "w": w, "h": h}


# Heights are sized for the narrowest grid Metabase still
# draws in columns (an iPad in portrait, 772px), where a text card needs about
# 1.4 times its desktop rows and a short one cuts its words off silently.
INTRO_H = 14
INTRO = """## Sprint 4: what a half-hour meter can see, and two what-ifs

Three stretch goals, in the order they change what a customer is told:
**1-3 · detected loads** (evidence about where the power went),
**4 · what that changes** on the bill and in the forecast,
**5 · solar payback** and **6 · EV charging** (two priced what-ifs),
**7 · the households whose panels are in the way**, closing with one
household whose bought, sold, car and house numbers are all readable at
once, and
**8 · the hardship early warning**, whose queue lives on the Sprint 3 page.
Every number here is a live query; none is typed into this text.

⚠️ **A detection is a pattern, not an appliance.** The meter records one total
every half hour. A run that draws 7 kW from midnight is *shaped like* a
wallbox; it could be a spa pool, a workshop or a kiln. Nothing on this page
says "your EV", and section 3 shows exactly how far the shape is from the
thing: far more households have EV-shaped days than were ever given an EV.

⚠️ **Evidence, not a breakdown.** Each detected day publishes a **band**, not
a number: the low bound subtracts the level either side of the run, the high
bound subtracts the day's smallest half-hour. The truth is inside the band,
and the band is often half a kWh wide. Nothing is published for a run that
only appears in winter -- that is heating, and it belongs to the heating
slope, not to an appliance.

⚠️ **Sections 5 and 6 are what-ifs.** No panel has ever happened: no household
has installed solar or moved its charging. Both price a year of the
household's own real readings under an assumption, and both name the
assumptions file that produced them.

**Where the numbers come from.** Sections 1-2 and 4-8 read the serving
database, what the backend serves. The two right-hand counts in section 3
read the pipeline sandbox, where the generator records what it planted.
No card joins the two: `make evaluate-sprint4` is what matches them household
by household, and its report is where precision and recall belong."""

SECTIONS = [
    ("1 · Detected loads — what a half-hour meter can see", [
        card("Households with a detected load", "scalar", 8, 3, f"""
-- Coverage against one population: generated households still on supply, over
-- the last 12 months. A household with nothing detected is counted here, not
-- dropped, so the fraction means coverage rather than success.
WITH {COHORT}
SELECT count(DISTINCT d.icp_id) FILTER (
           WHERE d.ev_half_hours > 0 OR d.timed_night_half_hours > 0)::text
       || ' of ' || (SELECT count(*) FROM cohort)::text
       AS "Households with a detected load"
FROM kea.mart_daily_detected_load d
JOIN cohort c ON c.icp_id = d.icp_id
WHERE d.usage_date > {LAST_YEAR}
"""),
        card("Days carrying a detection", "scalar", 8, 3, f"""
-- The same 12 months, by day rather than by household: most days of most
-- households carry nothing, which is what a detector that only speaks when
-- it is sure looks like.
WITH {COHORT}
SELECT to_char(count(*) FILTER (
           WHERE d.ev_half_hours > 0 OR d.timed_night_half_hours > 0), 'FM999,999')
       || ' of ' || to_char(count(*), 'FM999,999') AS "Days carrying a detection"
FROM kea.mart_daily_detected_load d
JOIN cohort c ON c.icp_id = d.icp_id
WHERE d.usage_date > {LAST_YEAR}
"""),
        card("The rules these detections were found by", "scalar", 8, 3, """
-- Every row carries the version of the rules that produced it. A changed
-- threshold is a new version, so two versions on this card means the mart
-- holds a rebuild that did not finish, and no panel on this page is comparing
-- like with like.
SELECT string_agg(DISTINCT method_version, ' + ')
       AS "The rules these detections were found by"
FROM kea.mart_daily_detected_load
"""),
        card("When a charging run starts", "bar", 12, 8, f"""
-- Local time, because a timer is set in local time: a run that starts at 23:00
-- NZDT and 23:00 NZST is the same habit, and in UTC it is two.
WITH {COHORT}
SELECT to_char(s.session_start AT TIME ZONE 'Pacific/Auckland', 'HH24:00')
           AS "start of the run",
       {SESSION_SHAPE} AS "shape",
       count(*) AS "runs"
FROM kea.mart_charging_session s
JOIN cohort c ON c.icp_id = s.icp_id
WHERE (s.session_start AT TIME ZONE 'Pacific/Auckland')::date > {LAST_YEAR}
GROUP BY 1, 2
ORDER BY 1, 2
""", stacked(["start of the run", "shape"], ["runs"])),
        card("How long a charging run lasts", "bar", 12, 8, f"""
-- A run is consecutive half-hours on the continuous timeline, so a missing
-- read ends it: the short bars are partly gaps in the meter data, not short
-- charges. Length in absolute time is exactly half-hours x 30 minutes, across
-- a daylight-saving change too.
WITH {COHORT}
SELECT CASE WHEN s.half_hours <= 2 THEN 'up to 1 h'
            WHEN s.half_hours <= 4 THEN '1 to 2 h'
            WHEN s.half_hours <= 8 THEN '2 to 4 h'
            WHEN s.half_hours <= 12 THEN '4 to 6 h'
            ELSE 'over 6 h' END AS "how long it ran",
       {SESSION_SHAPE} AS "shape",
       count(*) AS "runs"
FROM kea.mart_charging_session s
JOIN cohort c ON c.icp_id = s.icp_id
WHERE (s.session_start AT TIME ZONE 'Pacific/Auckland')::date > {LAST_YEAR}
GROUP BY 1, 2
ORDER BY min(s.half_hours), 2
""", stacked(["how long it ran", "shape"], ["runs"])),
        card("How sure the detector is", "row", 12, 7, f"""
-- Confidence is a label the rules assign, not a probability: high means the
-- run matched every part of its rule, medium that it matched a weaker part of
-- it. A portable-shaped charge is never higher than medium, because at 2 kW
-- for six hours an oven and a dehumidifier look the same.
WITH {COHORT}, d AS (
    SELECT d.* FROM kea.mart_daily_detected_load d
    JOIN cohort c ON c.icp_id = d.icp_id
    WHERE d.usage_date > {LAST_YEAR}
)
SELECT {SHAPE} AS "what it was shaped like",
       coalesce(d.ev_confidence, d.timed_night_confidence)::text AS "confidence",
       count(*) AS "days"
FROM d
WHERE d.ev_half_hours > 0 OR d.timed_night_half_hours > 0
GROUP BY 1, 2
ORDER BY 1, 2
""", stacked(["what it was shaped like", "confidence"], ["days"])),
        card("How much of the day the detection claims", "bar", 12, 7, f"""
-- The low bound only: the least the run can have used. On most detected days
-- it is a fifth to a half of everything the household imported, which is the
-- honest size of what "where the power went" can answer -- the rest is the
-- base load, the heating and everything the meter cannot separate.
WITH {COHORT}, d AS (
    SELECT d.ev_kwh_low + d.timed_night_kwh_low AS detected_kwh, u.import_kwh
    FROM kea.mart_daily_detected_load d
    JOIN cohort c ON c.icp_id = d.icp_id
    JOIN kea.mart_daily_usage u ON u.icp_id = d.icp_id AND u.usage_date = d.usage_date
    WHERE d.usage_date > {LAST_YEAR}
      AND (d.ev_half_hours > 0 OR d.timed_night_half_hours > 0)
      AND u.import_kwh > 0
)
SELECT CASE WHEN detected_kwh / import_kwh < 0.1 THEN 'under 10%'
            WHEN detected_kwh / import_kwh < 0.2 THEN '10 to 20%'
            WHEN detected_kwh / import_kwh < 0.3 THEN '20 to 30%'
            WHEN detected_kwh / import_kwh < 0.5 THEN '30 to 50%'
            ELSE 'over 50%' END AS "share of the day's import",
       count(*) AS "days"
FROM d
GROUP BY 1
ORDER BY min(detected_kwh / import_kwh)
""", axes(["share of the day's import"], ["days"])),
    ]),
    ("2 · Two households, up close", [
        text("""
**Both accounts are picked by a rule inside the query, not by hand.** The
first is the account with the most wallbox-shaped days in the last 12 months;
the day drawn is its *median* such day, not its biggest, so the shape is the
usual one and not the outlier. The second is the account with the most
night-block days. A rebuilt cohort picks its own two, and the headings are
re-read from the database on every build -- so a title cannot go stale and
name an account the chart below it no longer draws.

The number in each heading is the internal `icp_id` -- the key the card's own
query is written on, not the ICP number printed on a bill. It is there so a
figure on this page can be taken straight back to the rows it came from.

**What the two bounds mean.** In A's day the floor is the household's
smallest half-hour: everything it draws around the clock. The published
**high** bound is the run minus that floor; the **low** bound subtracts the
higher level either side of the run instead, on the grounds that whatever was
already running probably kept running. The charge is somewhere between them.

**Why B's chart is the test that matters.** A block at the same time every
night *in both seasons* is a timer. A block that is only there in winter is
heating, and the rules publish nothing for it -- it belongs to the heating
slope, where the bill explanation and the forecast already account for it.
""", w=24, h=7),
        card(f"{HOUSEHOLD['A']}: a typical charging day, half-hour by half-hour",
             "bar", 24, 9, f"""
-- Each half-hour is split into what the day's floor accounts for and what sits
-- above it, and the part above is labelled by whether it fell inside a
-- published run. The dark band across the bottom is the floor: the high bound
-- is everything above it during the run, which is why the high bound can never
-- double-count a kWh the base-load driver already counted.
WITH {COHORT}, {PICK_A}, days AS (
    SELECT d.* FROM kea.mart_daily_detected_load d
    JOIN household_a h ON h.icp_id = d.icp_id
    WHERE d.ev_charger_class = 'high_power' AND d.usage_date > {LAST_YEAR}
), typical AS (
    SELECT * FROM days
    WHERE ev_kwh_high = (SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY ev_kwh_high)
                         FROM days)
    ORDER BY usage_date
    LIMIT 1
), hh AS (
    SELECT (r.interval_start AT TIME ZONE 'Pacific/Auckland') AS ts, sum(r.kwh) AS kwh
    FROM metering.meter_read_hh r
    JOIN metering.meter_register g ON g.register_id = r.register_id
    JOIN typical t ON t.icp_id = r.icp_id
    WHERE g.direction = 'import' AND NOT g.is_controlled
      AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date = t.usage_date
    GROUP BY 1
), parts AS (
    SELECT hh.ts,
           least(hh.kwh, t.floor_kwh_per_hh) AS floor_kwh,
           greatest(hh.kwh - t.floor_kwh_per_hh, 0) AS above_kwh,
           EXISTS (SELECT 1 FROM kea.mart_charging_session s
                   WHERE s.icp_id = t.icp_id
                     AND hh.ts >= (s.session_start AT TIME ZONE 'Pacific/Auckland')
                     AND hh.ts <  (s.session_end   AT TIME ZONE 'Pacific/Auckland')) AS in_run
    FROM hh CROSS JOIN typical t
)
SELECT to_char(ts, 'HH24:MI') AS "half-hour",
       'the day''s floor' AS "part of the half-hour",
       round(floor_kwh, 3) AS "kWh"
FROM parts
UNION ALL
SELECT to_char(ts, 'HH24:MI'),
       CASE WHEN in_run THEN 'above the floor, inside the detected run'
            ELSE 'above the floor, rest of the day' END,
       round(above_kwh, 3)
FROM parts
ORDER BY 1, 2
""", stacked(["half-hour", "part of the half-hour"], ["kWh"])),
        card(f"{HOUSEHOLD['A']}: what that day publishes", "table", 12, 9, f"""
-- The row a customer's answer would be built from. Note what is not here:
-- no appliance, no make of car, and no single number for the charge.
WITH {COHORT}, {PICK_A}, days AS (
    SELECT d.* FROM kea.mart_daily_detected_load d
    JOIN household_a h ON h.icp_id = d.icp_id
    WHERE d.ev_charger_class = 'high_power' AND d.usage_date > {LAST_YEAR}
), typical AS (
    SELECT * FROM days
    WHERE ev_kwh_high = (SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY ev_kwh_high)
                         FROM days)
    ORDER BY usage_date
    LIMIT 1
)
SELECT 'the day drawn on the left' AS "what",
       t.usage_date::text AS "value"
FROM typical t
UNION ALL SELECT 'wallbox-shaped days in the last 12 months',
       (SELECT count(*)::text FROM days)
UNION ALL SELECT 'half-hours inside the run that day', t.ev_half_hours::text FROM typical t
UNION ALL SELECT 'charge, at least (low bound)',
       to_char(t.ev_kwh_low, 'FM999,990.0') || ' kWh' FROM typical t
UNION ALL SELECT 'charge, at most (high bound)',
       to_char(t.ev_kwh_high, 'FM999,990.0') || ' kWh' FROM typical t
UNION ALL SELECT 'everything the household imported that day',
       to_char(u.import_kwh, 'FM999,990.0') || ' kWh'
       FROM typical t JOIN kea.mart_daily_usage u
         ON u.icp_id = t.icp_id AND u.usage_date = t.usage_date
UNION ALL SELECT 'the day''s floor, per half-hour',
       to_char(t.floor_kwh_per_hh, 'FM990.000') || ' kWh' FROM typical t
UNION ALL SELECT 'how sure', t.ev_confidence::text FROM typical t
UNION ALL SELECT 'found by', t.method_version FROM typical t
"""),
        card(f"{HOUSEHOLD['B']}: the same block every night, winter and summer",
             "line", 12, 9, f"""
-- The published class is called timed_night for a reason: the run has to start
-- in the same half-hour slot on most nights ALL YEAR. Winter and summer are
-- drawn from the same 12 months, so the two lines are the same household in
-- the same window. A block in both is a timer; a block only in the winter line
-- would be heating, and nothing is published for it.
WITH {COHORT}, {PICK_B}, reads AS (
    SELECT (r.interval_start AT TIME ZONE 'Pacific/Auckland') AS ts, sum(r.kwh) AS kwh
    FROM metering.meter_read_hh r
    JOIN metering.meter_register g ON g.register_id = r.register_id
    JOIN household_b h ON h.icp_id = r.icp_id
    WHERE g.direction = 'import' AND NOT g.is_controlled
      AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date > {LAST_YEAR}
    GROUP BY 1
)
SELECT to_char(ts, 'HH24:MI') AS "half-hour",
       CASE WHEN extract(month FROM ts) IN {COLD_MONTHS} THEN 'winter nights (Jun-Aug)'
            ELSE 'summer nights (Dec-Feb)' END AS "season",
       round(avg(kwh), 3) AS "average kWh"
FROM reads
WHERE extract(month FROM ts) IN {COLD_MONTHS + WARM_MONTHS}
GROUP BY 1, 2
ORDER BY 1, 2
""", axes(["half-hour", "season"], ["average kWh"])),
    ]),
    ("3 · Reasons to doubt it", [
        text("""
**The shape is not the thing.** The two counts on the left are what the
detector published; the two on the right are what the generator actually
planted when it built the source data. They are not close, and they are not
meant to be: a household with a spa pool, a workshop, a kiln or a big
dehumidifier on a timer draws exactly what a charger draws. This is the
reason every heading on this page says *shaped like*, and the reason the
customer-facing wording asks a question rather than making a claim.

**The counts come from two different databases and no card joins them.**
Left: the serving database. Right: `gen_audit` on the pipeline sandbox, where
the generator records what it planted. Matching them household by household
-- which planted load was found, which was missed, how wide the bounds were
around the truth -- is `make evaluate-sprint4`, and its report is the only
place precision, recall and the bound coverage should be read from.

**Three of the planted households have panels as well as a car.** They were
added with `detect-v2` and they are counted on the right like any other, so
the right-hand numbers are not the same as Sprint 4's first run. Section 7 is
about them, and about why the detector is quieter on a roof than off one.
""", w=24, h=7),
        card("Households with an EV-shaped day, published", "scalar", 6, 3, f"""
WITH {COHORT}
SELECT count(DISTINCT d.icp_id)::text || ' of ' || (SELECT count(*) FROM cohort)::text
       AS "Households with an EV-shaped day, published"
FROM kea.mart_daily_detected_load d
JOIN cohort c ON c.icp_id = d.icp_id
WHERE d.ev_half_hours > 0 AND d.usage_date > {LAST_YEAR}
"""),
        card("Households actually given an EV", "scalar", 6, 3, f"""
-- The pipeline sandbox: what the generator planted, in its own audit trail.
-- No fraction, because the two databases count slightly different populations
-- and this number is not the denominator of the one on its left.
SELECT count(DISTINCT icp_identifier)::text || ' households'
       AS "Households actually given an EV"
FROM gen_audit.planted_load
WHERE run_id = {LATEST_PLANT} AND load_kind LIKE 'ev_%'
""", db=PIPELINE_DB),
        card("Households with a night-block day, published", "scalar", 6, 3, f"""
WITH {COHORT}
SELECT count(DISTINCT d.icp_id)::text || ' of ' || (SELECT count(*) FROM cohort)::text
       AS "Households with a night-block day, published"
FROM kea.mart_daily_detected_load d
JOIN cohort c ON c.icp_id = d.icp_id
WHERE d.timed_night_half_hours > 0 AND d.usage_date > {LAST_YEAR}
"""),
        card("Households actually given a timed water heater", "scalar", 6, 3, f"""
SELECT count(DISTINCT icp_identifier)::text || ' households'
       AS "Households actually given a timed water heater"
FROM gen_audit.planted_load
WHERE run_id = {LATEST_PLANT} AND load_kind = 'hot_water_3kw'
""", db=PIPELINE_DB),
        card("What the detector published, by shape", "row", 12, 7, f"""
-- Days, not households: the same household can look like a wallbox one week
-- and like nothing the next, and that is a property of the rules worth seeing.
WITH {COHORT}, d AS (
    SELECT d.* FROM kea.mart_daily_detected_load d
    JOIN cohort c ON c.icp_id = d.icp_id
    WHERE d.usage_date > {LAST_YEAR}
)
SELECT {SHAPE} AS "what it was shaped like", count(*) AS "days"
FROM d
WHERE d.ev_half_hours > 0 OR d.timed_night_half_hours > 0
GROUP BY 1
ORDER BY 2 DESC
""", axes(["what it was shaped like"], ["days"])),
        card("What the generator actually planted", "bar", 12, 7, f"""
-- The same 12-month window is not available here: the generator plants over
-- the whole span it built, and this is every planted run in the latest run.
SELECT CASE load_kind WHEN 'ev_wallbox_7kw' THEN 'EV, wallbox'
                      WHEN 'ev_portable_2kw' THEN 'EV, portable charger'
                      ELSE 'water heater on a timer' END AS "what was planted",
       CASE pattern WHEN 'timer' THEN 'on a timer' ELSE 'when they got home' END
           AS "when it ran",
       count(*) AS "runs planted"
FROM gen_audit.planted_load
WHERE run_id = {LATEST_PLANT}
GROUP BY 1, 2
ORDER BY 3 DESC
""", stacked(["what was planted", "when it ran"], ["runs planted"]), db=PIPELINE_DB),
    ]),
    ("4 · What it changes, on the bill and in the forecast", [
        text("""
**A detected load is now a driver of its own.** The bill explanation attributes
a change in six steps, in this order: *the period was longer or shorter → the
weather → the base load → **a detected load** → a rate change → unexplained.*
Each step is priced at the previous period's own rate, so a bigger bill and a
dearer bill stay separate. A new charger used to land in the weather term or
in the unexplained remainder; it now has its own line.

**The forecast was refitted around it.** `hdd-linear-v3` fits the heating
slope *net* of detected loads and then adds the detected load back as a flat
daily amount, so a week of assessed detection days is now a condition of
forecasting at all -- a stricter rule than v2's, and the price of not
attributing a charger to the weather. That rule alone is the
first bar of *What the forecast adds back each day*; the rest of the gap in
*Forecast snapshots the model can serve* is v2's own conditions, unchanged: a
usable heating fit, weather covering every elapsed day of the period, and a
prior bill to price against.
""", w=24, h=6),
        card("Bills with a detected-load driver", "scalar", 8, 3, """
-- Of the bills the explainer can break down at all: how many had a detected
-- load that moved between this period and the last one. A zero is a real
-- answer -- nothing detected, or the same amount both periods.
SELECT to_char(count(*) FILTER (WHERE delta_from_detected_loads <> 0), 'FM999,999')
       || ' of ' || to_char(count(*), 'FM999,999') AS "Bills with a detected-load driver"
FROM kea.mart_bill_explanation
WHERE is_explainable AND delta_from_detected_loads IS NOT NULL
"""),
        card("Forecast snapshots the model can serve", "scalar", 8, 3, """
-- v3 needs a week of assessed detection days in the trailing 28 before it will
-- forecast at all, so this fraction is the cost and the benefit of the change
-- in one number.
SELECT to_char(count(*) FILTER (WHERE is_forecastable), 'FM999,999')
       || ' of ' || to_char(count(*), 'FM999,999')
       AS "Forecast snapshots the model can serve"
FROM kea.mart_forecast_input
"""),
        card("The forecast model published", "scalar", 8, 3, """
-- Two versions here means a publication that did not replace the last one, and
-- the panels above are then mixing two models.
SELECT string_agg(DISTINCT model_version, ' + ') AS "The forecast model published"
FROM kea.bill_forecast
"""),
        card("How big the detected-load driver is", "bar", 12, 7, """
-- Absolute dollars, because a charger arriving and a charger leaving are the
-- same evidence. Priced at the prior period's rate, like every other driver.
SELECT CASE WHEN abs(delta_from_detected_loads) = 0 THEN 'nothing to attribute'
            WHEN abs(delta_from_detected_loads) < 5 THEN 'under $5'
            WHEN abs(delta_from_detected_loads) < 20 THEN '$5 to $20'
            WHEN abs(delta_from_detected_loads) < 50 THEN '$20 to $50'
            ELSE 'over $50' END AS "size of the detected-load driver",
       count(*) AS "bills"
FROM kea.mart_bill_explanation
WHERE is_explainable AND delta_from_detected_loads IS NOT NULL
GROUP BY 1
ORDER BY min(abs(delta_from_detected_loads))
""", axes(["size of the detected-load driver"], ["bills"])),
        card("Which driver moved the bill most", "bar", 12, 7, """
-- The largest of the six by absolute dollars, one vote per bill. `unexplained`
-- is on the chart on purpose: it is the remainder after the five named drivers,
-- and a page that hid it would be claiming the attribution is complete.
WITH e AS (
    SELECT delta_from_period_length AS pl, delta_from_weather AS w,
           delta_from_base_load AS bl, delta_from_detected_loads AS dl,
           delta_from_tariff AS tf, delta_unexplained AS un
    FROM kea.mart_bill_explanation
    WHERE is_explainable AND delta_from_detected_loads IS NOT NULL
), largest AS (
    SELECT (SELECT x.name FROM (VALUES
                ('the period was longer or shorter', abs(e.pl)),
                ('the weather', abs(e.w)),
                ('the base load', abs(e.bl)),
                ('a detected load', abs(e.dl)),
                ('a rate change', abs(e.tf)),
                ('unexplained', abs(e.un))) AS x(name, size)
            ORDER BY x.size DESC LIMIT 1) AS driver
    FROM e
)
SELECT driver AS "the biggest driver on the bill", count(*) AS "bills"
FROM largest
GROUP BY 1
ORDER BY 2 DESC
""", axes(["the biggest driver on the bill"], ["bills"])),
        card("What the forecast adds back each day", "bar", 12, 7, """
-- The flat daily amount v3 adds to a baseline it fitted without it: the mean of
-- the low bounds over the assessed days in the trailing 28. Snapshots with no
-- detection are the first bar, and they are most of them.
SELECT CASE WHEN detected_load_kwh_day IS NULL THEN 'not enough assessed days'
            WHEN detected_load_kwh_day = 0 THEN 'nothing detected'
            WHEN detected_load_kwh_day < 2 THEN 'under 2 kWh a day'
            WHEN detected_load_kwh_day < 5 THEN '2 to 5 kWh a day'
            WHEN detected_load_kwh_day < 10 THEN '5 to 10 kWh a day'
            ELSE 'over 10 kWh a day' END AS "detected load added back",
       count(*) AS "forecast snapshots"
FROM kea.mart_forecast_input
GROUP BY 1
ORDER BY min(coalesce(detected_load_kwh_day, -1))
""", axes(["detected load added back"], ["forecast snapshots"])),
        text("""
**Reading *Which driver moved the bill most*.** A detected load is the biggest
driver on a minority of bills, and `unexplained` is still the biggest on many.
That is the honest state of the attribution: the six drivers are priced,
ordered and checked, and what is left over is reported rather than spread
across the named five.

**The band, again.** The driver uses the **low** bound of each day -- the
least the run can have used -- so the detected-load line understates rather
than overstates, and the remainder it leaves behind is counted as unexplained
instead of being quietly attributed to a charger.
""", w=12, h=8),
    ]),
    ("5 · Solar payback — a what-if, not a measurement", [
        text("""
**Nobody priced here has solar, and that is a rule, not a coincidence.**
`build_solar_estimates.py` skips every account that already has an export
register: a payback for panels is only a question for a household that has
none. Some households in the cohort do have them, and section 7 is about
those -- so read this section as "what installing would be worth", not as
"nobody in the cohort generates". Each estimate replays a year of the
household's own half-hourly imports against a regional yield table -- low and
high kWh per kW per day, by month -- spread over the daylight half-hours,
netted off what the household was already importing at that moment, and
priced on its current plan. What it could not use is exported at the export
rate.

**Everything is a band, and the band is the answer.** Low is the best case
(most generation, lowest install cost); high is the worst. A payback of
"7 to 15 years" is not a failure to decide: it is the width of the assumption.
The payback is simple -- no panel or battery degradation, no price rises --
and every row names the assumptions file it came from.

**Each size is priced with and without a home battery.** The battery stores
what the panels would otherwise export and gives it back when the household
next imports, losing about a tenth on the way. On a plan with a cheap window
(Time of Use, Weekend) there is a third row: the same battery also filled from
the grid in that window and emptied when power costs more. Flat plans get no
such row, because there it would only lose the tenth. Only the solar share of
what a battery gives back counts as "used at home". Its cost is added to the
panels', so read a battery row against the same size without one: more saved
each year is not the same as paid back sooner.
""", w=24, h=8),
        card("What a year of generation is worth", "bar", 12, 7, f"""
-- The median household of the estimate set, per system size. Saving means the
-- import bill avoided plus what the export earns, on the plan the household is
-- on today.
SELECT {SOLAR_SYSTEM} AS "system size",
       round((percentile_cont(0.5) WITHIN GROUP (
           ORDER BY annual_saving_nzd_low))::numeric) AS "worst case, a year",
       round((percentile_cont(0.5) WITHIN GROUP (
           ORDER BY annual_saving_nzd_high))::numeric) AS "best case, a year"
FROM kea.solar_estimate
WHERE computed_at = {LATEST_SOLAR}
GROUP BY 1
ORDER BY {SOLAR_ORDER}
""", axes(["system size"], ["worst case, a year", "best case, a year"],
             money("worst case, a year", "best case, a year"))),
        card("How long it takes to pay for itself", "bar", 12, 7, f"""
-- Best case = most generation against the lowest install cost; worst case is
-- the other corner. A household whose best case has no payback at all is
-- counted in the panel below, not hidden here.
SELECT {SOLAR_SYSTEM} AS "system size",
       round((percentile_cont(0.5) WITHIN GROUP (
           ORDER BY payback_years_low))::numeric, 1) AS "best case, years",
       round((percentile_cont(0.5) WITHIN GROUP (
           ORDER BY payback_years_high))::numeric, 1) AS "worst case, years"
FROM kea.solar_estimate
WHERE computed_at = {LATEST_SOLAR}
GROUP BY 1
ORDER BY {SOLAR_ORDER}
""", axes(["system size"], ["best case, years", "worst case, years"])),
        card("The assumptions behind every estimate", "table", 24, 7, f"""
-- The panel that keeps the two charts honest: a payback is only as good as the
-- yield table, the install cost and the share of generation the household can
-- actually use at the moment it is generated.
SELECT {SOLAR_SYSTEM} AS "system size",
       count(*)::text AS "households",
       string_agg(DISTINCT weather_region, ', ') AS "yield table",
       min(basis_from)::text || ' to ' || max(basis_to)::text AS "readings replayed",
       to_char(min(annual_generation_kwh_low), 'FM999,999') || ' to '
           || to_char(max(annual_generation_kwh_high), 'FM999,999') || ' kWh'
           AS "a year's generation",
       to_char(percentile_cont(0.5) WITHIN GROUP (ORDER BY self_used_pct_low),
               'FM990') || ' to '
           || to_char(percentile_cont(0.5) WITHIN GROUP (ORDER BY self_used_pct_high),
               'FM990') || '%' AS "used at home, median household",
       '$' || to_char(min(install_cost_nzd_low), 'FM999,999') || ' to $'
           || to_char(max(install_cost_nzd_high), 'FM999,999')
           AS "install cost, battery included",
       count(*) FILTER (WHERE payback_years_high IS NULL)::text
           AS "never pays for itself",
       string_agg(DISTINCT assumptions_version, ', ') AS "assumptions"
FROM kea.solar_estimate
WHERE computed_at = {LATEST_SOLAR}
GROUP BY 1
ORDER BY {SOLAR_ORDER}
"""),
    ]),
    ("6 · EV charging — moving it, or adding it", [
        text("""
**Two different questions.** *Adding an EV* takes every household in the
cohort and gives it a year of charging from an assumed annual distance,
starting at 18:00 (home from work, the baseline) or at 23:00 (overnight).
*Moving the charging that already happens* takes the wallbox-shaped runs the
detector found and replays each one at 23:00 the same night. Both price the
whole household's year, so the plans compare directly.

**The two charts price different households, and say so.** Adding an EV is a
question anyone can ask, so every household in the cohort is priced on all
three plan families. Moving charging already detected can only be asked of a
household with wallbox-shaped runs to move, and those are priced on the plan
they are on plus Time of Use -- the only plan on which moving anything can
change the bill. The bracket after each plan name is how many households went
into that bar.

**The honest result is that the plan decides.** On a flat plan, moving power
from the evening to the night saves nothing: a kWh costs the same at both
times, and what little difference appears is the charge landing in a
different billing period. The saving is real only on a plan that prices time
-- and on Free Weekends, charging at night can *cost* more, because it moves
power out of the free window. **And for a household with panels, so does the
sun:** the cheapest kWh of the day is the one its own roof is making, which
is a third pattern, `solar_soak`, priced in section 7.
""", w=24, h=9),
        card("Adding an EV: what charging overnight saves", "bar", 12, 7, f"""
-- Median household, by plan. Positive means the overnight pattern costs less
-- over the year than the same kWh charged from 18:00 on the same plan.
SELECT {PLAN_FAMILY} || ' (' || count(*) || ')' AS "plan (households priced)",
       round((percentile_cont(0.5) WITHIN GROUP (
           ORDER BY s.saving_vs_baseline_nzd))::numeric) AS "median saving a year"
FROM kea.ev_scenario s
JOIN catalogue.plan p ON p.plan_id = s.plan_id
WHERE s.computed_at = {LATEST_EV}
  AND s.scenario = 'add_ev' AND s.charging_pattern = 'night'
GROUP BY {PLAN_FAMILY}
ORDER BY 2 DESC
""", axes(["plan (households priced)"], ["median saving a year"],
                  money("median saving a year"))),
        card("Moving charging that already happens", "bar", 12, 7, f"""
-- Only households with wallbox-shaped runs to move, so this is a smaller set
-- than the panel on the left -- and the runs are the detector's, bounds and
-- all, replayed at their low bound.
SELECT {PLAN_FAMILY} || ' (' || count(*) || ')' AS "plan (households priced)",
       round((percentile_cont(0.5) WITHIN GROUP (
           ORDER BY s.saving_vs_baseline_nzd))::numeric) AS "median saving a year"
FROM kea.ev_scenario s
JOIN catalogue.plan p ON p.plan_id = s.plan_id
WHERE s.computed_at = {LATEST_EV}
  AND s.scenario = 'shift_detected' AND s.charging_pattern = 'night'
GROUP BY {PLAN_FAMILY}
ORDER BY 2 DESC
""", axes(["plan (households priced)"], ["median saving a year"],
                  money("median saving a year"))),
        card("What each scenario assumed", "table", 24, 7, f"""
-- Four rows, because each scenario has a baseline pattern and a moved one, and
-- the baseline is what the saving is measured against. A baseline row saves
-- nothing by definition, and the table says so rather than leaving a zero to
-- be read as a result.
SELECT CASE s.scenario WHEN 'add_ev' THEN 'adding an EV'
                       ELSE 'moving charging already detected' END AS "scenario",
       CASE s.charging_pattern WHEN 'evening' THEN 'from 18:00 (the baseline)'
                               WHEN 'as_detected' THEN 'when it happened (the baseline)'
                               ELSE 'from 23:00' END AS "charging pattern",
       count(DISTINCT s.account_id)::text AS "households",
       count(*)::text AS "priced rows",
       to_char(percentile_cont(0.5) WITHIN GROUP (ORDER BY s.annual_ev_kwh),
               'FM999,999') || ' kWh' AS "charging a year, median",
       '$' || to_char(percentile_cont(0.5) WITHIN GROUP (ORDER BY s.annual_ev_cost_nzd),
               'FM999,999') AS "what the charging costs, median",
       min(s.basis_from)::text || ' to ' || max(s.basis_to)::text AS "readings replayed",
       string_agg(DISTINCT s.assumptions_version, ', ') AS "assumptions"
FROM kea.ev_scenario s
WHERE s.computed_at = {LATEST_EV}
GROUP BY 1, 2
ORDER BY 1, 2
"""),
    ]),
    ("7 · When the panels are in the way", [
        text("""
**A meter never records what a roof generates.** It records two things, on two
separate registers, neither of which can go negative: what the household
**imported** from us, and what it **exported** to us. Everything on this page
about detected load is read from the import register -- so on a household with
panels there are two half-hours we cannot read straight:

* **While it exports**, import reads exactly 0. Whatever the house was doing in
  that half-hour is *invisible*, not small.
* **In daylight when it does not export**, import is load *minus* whatever the
  panels covered -- understated by an amount nothing on the meter reveals.

A 7 kW wallbox under a generating array can therefore present as 2 kW of
import, below the threshold that says "wallbox". Until this release we
published that as a portable charger. Now the half-hours the panels hid are
counted, and where they make the distinction unsafe the class is **withheld**.

⚠️ **A class we cannot state is not "no EV found."** The session is still
published, with its kWh and its hours; only the wallbox-against-portable
distinction is left blank, and blank here means *the meter could not tell*.

⚠️ **This cohort has no half-masked household, and that is a result, not a
gap.** A 7 kW wallbox outdraws every array here, so a charge big enough to
hide behind the panels is also big enough to *stop the export that would have
hidden it* — the meter reads the charge in full and the run is never marked.
Only a charge that begins while the panels are still ahead, in the middle of
the day, is hidden at all. The rule below that looks for a half-and-half
household therefore lands on the same ICP as the rule that looks for the
most-hidden one, and the table says so rather than printing one household
twice under two headings.
""", w=24, h=8),
        card(f"{HOUSEHOLD['E']}: a charging day the meter barely sees", "bar", 24, 7, f"""
-- The account is picked by rule, not by hand: the generating household with
-- the largest share of its detected run half-hours hidden by its own panels.
-- The day is that household's most-hidden detected day. Both tiebreaks end on
-- an id, so a rebuilt cohort picks its own household, the heading is re-read
-- from the same rule, and the page stays reproducible.
WITH {COHORT}, {GENERATING}, {RUNS}, {PICK_E}, day AS (
    SELECT d.icp_id, d.usage_date
    FROM kea.mart_daily_detected_load d
    JOIN household_e h ON h.icp_id = d.icp_id
    WHERE d.ev_half_hours > 0 AND d.usage_date > {LAST_YEAR}
    ORDER BY coalesce(d.masked_intervals, 0) DESC, d.usage_date
    LIMIT 1
), hh AS (
    -- The two registers side by side, for the same half-hour of the same day.
    SELECT (r.interval_start AT TIME ZONE 'Pacific/Auckland') AS ts,
           coalesce(sum(r.kwh) FILTER (
               WHERE g.direction = 'import' AND NOT g.is_controlled), 0) AS import_kwh,
           coalesce(sum(r.kwh) FILTER (WHERE g.direction = 'export'), 0) AS export_kwh
    FROM metering.meter_read_hh r
    JOIN metering.meter_register g ON g.register_id = r.register_id
    JOIN day t ON t.icp_id = r.icp_id
    WHERE (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date = t.usage_date
    GROUP BY 1
), parts AS (
    -- The same EXISTS test household A's card uses, against the runs the
    -- detector published for this household.
    SELECT hh.ts, hh.import_kwh, hh.export_kwh,
           EXISTS (SELECT 1 FROM kea.mart_charging_session s
                   WHERE s.icp_id = t.icp_id
                     AND hh.ts >= (s.session_start AT TIME ZONE 'Pacific/Auckland')
                     AND hh.ts <  (s.session_end AT TIME ZONE 'Pacific/Auckland')) AS in_run
    FROM hh CROSS JOIN day t
)
SELECT to_char(ts, 'HH24:MI') AS "half-hour",
       CASE WHEN in_run THEN 'imported, inside the detected run'
            ELSE 'imported, the rest of the day' END AS "which register, and when",
       round(import_kwh, 3) AS "kWh"
FROM parts
UNION ALL
SELECT to_char(ts, 'HH24:MI'),
       'exported -- the panels covered the house and more',
       round(export_kwh, 3)
FROM parts
ORDER BY 1, 2
""", stacked(["half-hour", "which register, and when"], ["kWh"])),
        card("The three generating households: what each one publishes", "table", 24, 6, f"""
-- Three generating households, each picked by a rule about masking and each
-- ending its ORDER BY on an id. The id in each row is the icp_id those rules
-- select on, so a row here names the same household as the headings above it.
WITH {COHORT}, {GENERATING}, {RUNS}, {PICK_C}, {PICK_D}, {PICK_E}, picked AS (
              SELECT icp_id, 'the control: panels change nothing at 23:00' AS label,
                     1 AS ord FROM household_c
    UNION ALL SELECT icp_id, 'the charge the meter can barely see', 2 FROM household_e
    UNION ALL SELECT icp_id, 'the nearest thing here to a half-masked household',
                     3 FROM household_d
), labelled AS (
    -- ⚠️ GROUPED BY HOUSEHOLD, NOT BY RULE. Two rules can land on the same ICP
    -- -- and in this cohort they do, because a maskable wallbox is a rare shape
    -- rather than a common one. Grouping makes that visible as one row wearing
    -- two reasons; a row per rule would have shown the same household twice and
    -- read like two.
    SELECT icp_id, string_agg(label, '; ' ORDER BY ord) AS label, min(ord) AS ord
    FROM picked GROUP BY icp_id
)
SELECT 'ICP ' || l.icp_id || ' — ' || l.label AS "the household, and why it is here",
       count(*)::text AS "runs published",
       count(*) FILTER (WHERE r.masked > 0)::text AS "…with half-hours the panels hid",
       to_char(100.0 * sum(r.masked) / nullif(sum(r.half_hours), 0), 'FM990')
           || '%' AS "share of run half-hours hidden",
       count(*) FILTER (WHERE r.charger_class = 'high_power')::text
           AS "published as wallbox-shaped",
       count(*) FILTER (WHERE r.charger_class = 'low_power')::text
           AS "published as portable-shaped",
       count(*) FILTER (WHERE r.charger_class IS NULL)::text
           AS "cannot be told apart",
       string_agg(DISTINCT r.confidence::text, ', ' ORDER BY r.confidence::text)
           AS "how sure the detector is"
FROM labelled l JOIN runs r ON r.icp_id = l.icp_id
GROUP BY l.icp_id, l.label, l.ord
ORDER BY l.ord
"""),
        card("Detections against masking", "bar", 24, 7, f"""
-- The evaluation report's "49 detections a household-year without panels,
-- 17 with them" line, made live. The gap cannot close -- the meter is still
-- blind while the house exports -- but the buckets now say how blind, which
-- is the whole difference between a low number and an unexplained one.
WITH {COHORT}, {GENERATING}, days AS (
    SELECT d.icp_id, coalesce(d.masked_intervals, 0) AS masked
    FROM kea.mart_daily_detected_load d
    JOIN cohort c ON c.icp_id = d.icp_id
    WHERE d.usage_date > {LAST_YEAR}
), per_household AS (
    SELECT y.icp_id, (g.icp_id IS NOT NULL) AS generates, count(*) AS days,
           sum(y.masked)::numeric / (count(*) * 48) AS masked_share
    FROM days y LEFT JOIN generating g ON g.icp_id = y.icp_id
    GROUP BY 1, 2
), runs AS (
    SELECT s.icp_id, count(*) AS runs
    FROM kea.mart_charging_session s
    JOIN cohort c ON c.icp_id = s.icp_id
    WHERE (s.session_start AT TIME ZONE 'Pacific/Auckland')::date > {LAST_YEAR}
    GROUP BY 1
), bucketed AS (
    SELECT CASE WHEN NOT h.generates THEN 0 WHEN h.masked_share = 0 THEN 1
                WHEN h.masked_share <= 0.25 THEN 2
                WHEN h.masked_share <= 0.50 THEN 3 ELSE 4 END AS ord,
           h.days, coalesce(r.runs, 0) AS runs
    FROM per_household h LEFT JOIN runs r ON r.icp_id = h.icp_id
)
SELECT CASE ord WHEN 0 THEN 'no panels (the reference)'
                WHEN 1 THEN 'panels, nothing hidden'
                WHEN 2 THEN 'panels, up to 25% of the year hidden'
                WHEN 3 THEN 'panels, 26-50% hidden'
                ELSE 'panels, over half the year hidden' END
           || ' (' || count(*) || ')' AS "how much of the year the panels hid",
       round(sum(runs) * 365.25 / nullif(sum(days), 0), 1)
           AS "detected runs per household-year"
FROM bucketed
GROUP BY ord
ORDER BY ord
""", axes(["how much of the year the panels hid"],
          ["detected runs per household-year"])),
        card("Charging into your own sun", "bar", 24, 7, f"""
-- solar_soak against the night rate, for the households that have something
-- to soak. ⚠️ A NEGATIVE BAR IS A RESULT, NOT A BUG: on Time of Use, midday
-- import that the panels did not cover sits in the most expensive window, and
-- the honest answer is sometimes "your night rate still wins". A losing row is
-- published, not suppressed.
WITH offered AS (
    SELECT DISTINCT account_id FROM kea.ev_scenario
    WHERE computed_at = {LATEST_EV} AND charging_pattern = 'solar_soak'
), priced AS (
    SELECT s.account_id, s.scenario, s.charging_pattern, s.saving_vs_baseline_nzd,
           {PLAN_FAMILY} AS family
    FROM kea.ev_scenario s
    JOIN catalogue.plan p ON p.plan_id = s.plan_id
    JOIN offered o ON o.account_id = s.account_id
    WHERE s.computed_at = {LATEST_EV}
      AND s.charging_pattern IN ('solar_soak', 'night')
), labels AS (
    SELECT family, scenario,
           family || ' — ' || CASE scenario WHEN 'add_ev' THEN 'adding an EV'
                                            ELSE 'moving charging' END
                  || ' (' || count(DISTINCT account_id) || ')' AS label,
           min(CASE scenario WHEN 'add_ev' THEN 1 ELSE 2 END) AS ord
    FROM priced GROUP BY family, scenario
)
SELECT l.label AS "plan, and the question (households priced)",
       CASE pr.charging_pattern WHEN 'solar_soak' THEN 'charging into your own sun'
                                ELSE 'charging from 23:00' END AS "pattern",
       round(percentile_cont(0.5) WITHIN GROUP
             (ORDER BY pr.saving_vs_baseline_nzd)::numeric)
           AS "median saving a year"
FROM priced pr JOIN labels l ON l.family = pr.family AND l.scenario = pr.scenario
GROUP BY l.label, l.ord, 2
ORDER BY l.ord, l.label, 2
""", axes(["plan, and the question (households priced)", "pattern"],
             ["median saving a year"], money("median saving a year"))),
        card("How much of the year the meter cannot see", "table", 24, 6, f"""
-- Measured, both columns: the import register and the export register, as the
-- household's own meter recorded them over the last 12 months. No array size
-- appears here -- kWp is a generator fact, it is not in this database, and the
-- evaluation report is where it belongs.
WITH {COHORT}, {GENERATING}, reads AS (
    SELECT r.icp_id,
           coalesce(sum(r.kwh) FILTER (
               WHERE g.direction = 'import' AND NOT g.is_controlled), 0) AS import_kwh,
           coalesce(sum(r.kwh) FILTER (WHERE g.direction = 'export'), 0) AS export_kwh
    FROM metering.meter_read_hh r
    JOIN metering.meter_register g ON g.register_id = r.register_id
    JOIN generating e ON e.icp_id = r.icp_id
    JOIN cohort c ON c.icp_id = r.icp_id
    WHERE (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date > {LAST_YEAR}
    GROUP BY 1
), hidden AS (
    SELECT d.icp_id, sum(coalesce(d.masked_intervals, 0)) AS hidden_hh,
           count(*) * 48 AS read_hh,
           count(*) FILTER (WHERE coalesce(d.masked_intervals, 0) > 0) AS days_touched,
           count(*) AS days
    FROM kea.mart_daily_detected_load d
    JOIN generating e ON e.icp_id = d.icp_id
    JOIN cohort c ON c.icp_id = d.icp_id
    WHERE d.usage_date > {LAST_YEAR}
    GROUP BY 1
), joined AS (
    SELECT r.icp_id, h.hidden_hh AS sort_hh,
           r.import_kwh, r.export_kwh, h.hidden_hh, h.read_hh,
           h.days_touched, h.days
    FROM reads r JOIN hidden h ON h.icp_id = r.icp_id
)
SELECT 'ICP ' || icp_id AS "household",
       to_char(import_kwh, 'FM999,999') || ' kWh' AS "bought from us, 12 months",
       to_char(export_kwh, 'FM999,999') || ' kWh' AS "sold back to us, 12 months",
       to_char(hidden_hh, 'FM999,999') AS "half-hours the meter could not read",
       to_char(100.0 * hidden_hh / nullif(read_hh, 0), 'FM990.0')
           || '%' AS "…of the whole year",
       to_char(days_touched, 'FM999,999') || ' of ' || to_char(days, 'FM999,999')
           AS "days with at least one"
FROM joined
ORDER BY sort_hh DESC, icp_id
"""),
        text(f"""
### And the day they are not: one household, all four numbers at once

Everything above is about what the panels **hide**. This last pair is the
opposite case, and it is here because it is the one a customer could be shown.
**ICP {DEMO_ICP}** charges in the evening, after its array has stopped, so on a
single summer day all four of the numbers a household would ask about are
readable from its own two registers:

* what it **bought** from us, half-hour by half-hour -- the import register;
* what it **sold** back to us -- the export register;
* what the **car** took, as the detector's bounded claim;
* what the **rest of the house** used, which is simply the import that was not
  inside the charging run.

⚠️ **"The rest of the house" means the rest of what it bought, not the rest of
what it used.** Between roughly 06:30 and 19:00 the import register on the chart
below reads a flat zero. The house was not idle; the panels were covering it,
and a meter that records only `max(0, load - pv)` cannot say by how much.
Generation is never metered. Any wording built on this panel has to say *what
you bought from us*, never *what you used* -- the same rule the class-withheld
sessions above are published under.

⚠️ **This is the only household on the page named by hand rather than by rule**,
so that the site and this panel keep showing the same one. The query re-checks
that it is still in the cohort, still exporting and still charging in the clear;
if it ever stops being any of those, both cards go blank rather than quietly
drawing somebody else.
""", w=24, h=9),
        card(f"ICP {DEMO_ICP} on {DEMO_DAY}: bought, sold, the car and the house",
             "bar", 24, 8, f"""
-- The day is picked by rule even though the household is not: the most
-- exporting day in the last 12 months that also carries a detected run, so
-- both registers and the car are all doing something in the same picture.
-- The tiebreaks end on a date, so the same data always yields the same day.
-- The heading names it, read at build time out of this same CTE: the x-axis
-- is a clock, so without it nothing on the card says which day, or that it is
-- a summer one.
WITH {COHORT}, {GENERATING}, {PICK_DEMO}, {PICK_DEMO_DAY}, hh AS (
    -- Both registers for the same half-hour, exactly as the meter recorded
    -- them. Nothing here is modelled.
    SELECT (r.interval_start AT TIME ZONE 'Pacific/Auckland') AS ts,
           coalesce(sum(r.kwh) FILTER (
               WHERE g.direction = 'import' AND NOT g.is_controlled), 0) AS import_kwh,
           coalesce(sum(r.kwh) FILTER (WHERE g.direction = 'export'), 0) AS export_kwh
    FROM metering.meter_read_hh r
    JOIN metering.meter_register g ON g.register_id = r.register_id
    JOIN demo_day t ON t.icp_id = r.icp_id
    WHERE (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date = t.usage_date
    GROUP BY 1
), parts AS (
    SELECT hh.ts, hh.import_kwh, hh.export_kwh,
           EXISTS (SELECT 1 FROM kea.mart_charging_session s
                   WHERE s.icp_id = t.icp_id
                     AND hh.ts >= (s.session_start AT TIME ZONE 'Pacific/Auckland')
                     AND hh.ts <  (s.session_end   AT TIME ZONE 'Pacific/Auckland')) AS in_run
    FROM hh CROSS JOIN demo_day t
)
-- Three series that stack to the whole day: the two parts of what was bought,
-- and what was sold. Import split by the run is the car against the house.
SELECT to_char(ts, 'HH24:MI') AS "half-hour",
       'bought — the car charging' AS "which register, and what it was",
       round(CASE WHEN in_run THEN import_kwh ELSE 0 END, 3) AS "kWh"
FROM parts
UNION ALL
SELECT to_char(ts, 'HH24:MI'),
       'bought — the rest of the house',
       round(CASE WHEN in_run THEN 0 ELSE import_kwh END, 3)
FROM parts
UNION ALL
SELECT to_char(ts, 'HH24:MI'),
       'sold back — the panels covered the house and more',
       round(export_kwh, 3)
FROM parts
ORDER BY 1, 2
""", stacked(["half-hour", "which register, and what it was"], ["kWh"])),
        card(f"ICP {DEMO_ICP}: the same four numbers over twelve months", "table", 24, 5, f"""
-- The annual version of the chart above, from the daily mart rather than the
-- half-hourly reads. The car is a range, not a number, because that is what the
-- detector publishes; "the rest of the house" is the import the run did not
-- claim, and carries the same bound in reverse.
WITH {COHORT}, {GENERATING}, {PICK_DEMO}, year AS (
    SELECT u.icp_id,
           count(*) AS days,
           sum(u.import_kwh) AS import_kwh,
           sum(coalesce(u.export_kwh, 0)) AS export_kwh,
           sum(coalesce(d.ev_kwh_low, 0)) AS ev_low,
           sum(coalesce(d.ev_kwh_high, 0)) AS ev_high,
           sum(coalesce(d.masked_intervals, 0)) AS hidden_hh
    FROM kea.mart_daily_usage u
    JOIN demo_household h ON h.icp_id = u.icp_id
    LEFT JOIN kea.mart_daily_detected_load d
           ON d.icp_id = u.icp_id AND d.usage_date = u.usage_date
    WHERE u.usage_date > {LAST_YEAR}
    GROUP BY 1
)
SELECT to_char(import_kwh, 'FM999,999') || ' kWh' AS "bought from us",
       to_char(export_kwh, 'FM999,999') || ' kWh' AS "sold back to us",
       to_char(ev_low, 'FM999,999') || ' – ' || to_char(ev_high, 'FM999,999')
           || ' kWh' AS "the car took",
       to_char(import_kwh - ev_high, 'FM999,999') || ' – '
           || to_char(import_kwh - ev_low, 'FM999,999')
           || ' kWh' AS "the rest of the house bought",
       to_char(hidden_hh, 'FM999,999') || ' of ' || to_char(days * 48, 'FM999,999')
           AS "half-hours the panels hid",
       'generation is not metered' AS "what the house actually used"
FROM year
"""),
    ]),
    ("8 · Hardship — the early warning, and where it is acted on", [
        text("""
**What Sprint 4 added.** Three warning signs -- a bill well above the usual
payment, payments drifting later, a first shortfall -- and, when a household
shows the spike *together with* drift or a shortfall and none of the five
Sprint 3 codes, an **early warning**: a reason to make contact a few weeks
before the arrears, with a projected date and the arithmetic behind it. Still
no score, deliberately. A staff member can check every step of a date; nobody
can check a weight.

**The bars below include the signs nobody raised.** A warning sign that fires
for no household in this run is why the early-warning count is what it is, and
dropping it would leave that unexplained.
""", w=24, h=6),
        card("Warning signs in the latest assessment", "bar", 24, 7, f"""
-- Every code the schema defines gets a bar, including the ones nobody raised:
-- a sign that fires for nobody today is the reason the early-warning count
-- below is what it is, and a chart that dropped it would hide that.
-- One household can raise several signs, so the bars add to more than the
-- households assessed.
WITH latest AS (
    SELECT assessment_id, account_id FROM kea.hardship_assessment
    WHERE computed_at = {LATEST_HARDSHIP}
), codes AS (
    SELECT unnest(enum_range(NULL::common.hardship_signal_code))::text AS code
)
SELECT {SIGN_SAYS} AS "warning sign",
       CASE WHEN c.code IN {V1_CODES} THEN 'Sprint 3'
            ELSE 'added in Sprint 4' END AS "added",
       count(DISTINCT a.account_id) AS "households"
FROM codes c
LEFT JOIN kea.hardship_signal s ON s.signal_code::text = c.code
LEFT JOIN latest a ON a.assessment_id = s.assessment_id
GROUP BY 1, 2
ORDER BY 3 DESC, 1
""", stacked(["warning sign", "added"], ["households"])),
        card("What the rule says about each household", "bar", 12, 7, f"""
-- The outreach rule itself, restated: a Sprint 3 code is an outreach as
-- before; the spike together with drift or a shortfall and no Sprint 3 code is
-- an early warning; any other warning sign is shown to staff and creates
-- nothing.
WITH a AS (
    SELECT * FROM kea.hardship_assessment WHERE computed_at = {LATEST_HARDSHIP}
), c AS (
    SELECT a.account_id, {CODES_OF} AS codes FROM a
), said AS (
    SELECT {RULE_SAYS} AS says FROM c
), outcomes(says, ord) AS (
    VALUES ('flagged', 1), ('early warning', 2), ('warning signs only', 3), ('clear', 4)
)
-- The four outcomes are listed, not discovered, and ordered by how urgent they
-- are rather than by how many. An empty bar is the honest answer for a rule
-- that fired for nobody in this run.
SELECT o.says AS "what the rule says", count(said.says) AS "households"
FROM outcomes o
LEFT JOIN said ON said.says = o.says
GROUP BY o.says, o.ord
ORDER BY o.ord
""", axes(["what the rule says"], ["households"])),
        text("""
**An empty early-warning count is a result, not a bug.** The rule fires in the
weeks *before* a household misses a payment. Assessed on a single date, a
cohort can easily have none open: the households that were sliding have since
tripped one of the five Sprint 3 codes, and are counted as flagged rather than
early. How much warning the rule actually buys was measured by replaying it
week by week -- `make evaluate-sprint4` -- and not from this page.

**This section is not the queue.** The outreach list, the staff-only
explanation behind each sign and the projected arrears date live on the
Sprint 3 page, *Plan analysis & recommendation*, because they carry names.
Nothing is sent from anywhere during a demo.
""", w=12, h=10),
        card("Households assessed", "scalar", 8, 3, f"""
SELECT to_char(count(*), 'FM999,999') || ' on ' || max(assessed_on)::text
       AS "Households assessed"
FROM kea.hardship_assessment
WHERE computed_at = {LATEST_HARDSHIP}
"""),
        card("Early warnings with a projected date", "scalar", 8, 3, f"""
-- The outlook row is written only when the early-warning rule fires, so this
-- counts the same households the rule panel calls an early warning. The date
-- and the arithmetic behind it are staff evidence and stay on the Sprint 3
-- page.
SELECT count(*)::text AS "Early warnings with a projected date"
FROM kea.hardship_outlook o
JOIN kea.hardship_assessment a ON a.assessment_id = o.assessment_id
WHERE a.computed_at = {LATEST_HARDSHIP}
"""),
        card("The ruleset that assessed them", "scalar", 8, 3, f"""
SELECT string_agg(DISTINCT ruleset_version, ' + ') AS "The ruleset that assessed them"
FROM kea.hardship_assessment
WHERE computed_at = {LATEST_HARDSHIP}
"""),
    ]),
]


def guard(name: str, sql: str) -> None:
    """Refuse a card that reads an identifier, with one exemption.

    Comments go first: they talk about ICPs and households in prose, and a page
    is not unsafe because a comment says the word. Then the counted form --
    `count(DISTINCT icp_identifier)` -- is blanked, because it puts a number of
    households in a cell and never a household. Whatever the guard still finds
    after that would have reached a reader."""
    body = re.sub(r"--[^\n]*", "", sql)
    body = COUNTED.sub("count(*)", body)
    hit = IDENTIFIERS.search(body)
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


def resolve_households(session: str, db_id: int) -> dict[str, str]:
    """Read the icp_id behind each lettered selector, and the date behind the
    demo panel, once, before any card is written.

    The query is built from the same PICK_* constants the cards splice in, so
    the ids in a heading are by construction the household that heading's own
    card draws, and the date in one is by construction the day its own chart
    plots. It runs through Metabase's own /api/dataset rather than a Postgres
    driver: the session is already open, and this file has no database
    dependency to gain.

    A key that comes back with no row keeps its placeholder. An empty cohort
    is a reason to see `ICP <C>` on the page and go looking, not a reason to
    fail a build the other thirty cards of which are sound."""
    result = api("/api/dataset",
                 {"type": "native", "database": db_id,
                  "native": {"query": RESOLVE_HOUSEHOLDS.strip(), "template-tags": {}}},
                 session=session)
    if result.get("error"):
        print(f"  ⚠️ could not resolve the household ids ({result['error']}); "
              "the headings keep their placeholders")
        return {}
    rows = result.get("data", {}).get("rows", [])
    # Every row arrives as text so the UNION types agree. The date was worded
    # by the query and goes straight in; an id still gets its "ICP " here,
    # because that wording is what the placeholders are written against.
    found = {str(key): value if str(key) == DAY_KEY else NAMED.format(icp_id=value)
             for key, value in rows if value is not None}
    missing = sorted(set(PLACEHOLDER) - set(found))
    if missing:
        print(f"  ⚠️ nothing matched {', '.join(missing)}; "
              "those headings keep their placeholders")
    return found


def name_the_cards(cards: list[dict], named: dict[str, str]) -> None:
    """Swap each placeholder for the text just read. Card names are the match
    key this builder upserts on, so a rebuilt cohort -- or a demo day that moves
    -- renames rather than rewrites: the new questions are created and the stale
    sweep at the end of main() archives the ones that named the old household or
    the old day. That is the intended behaviour -- a heading that survived a
    rebuild would be naming an account, or a date, its chart no longer draws."""
    for c in cards:
        for key, text in named.items():
            c["name"] = c["name"].replace(PLACEHOLDER[key], text)
    names = [c["name"] for c in cards]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        sys.exit(f"two cards resolved to the same name: {dupes}")


def main() -> None:
    cards = [it for _, items in SECTIONS for it in items if it["kind"] == "card"]
    names = [c["name"] for c in cards]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        sys.exit(f"card names are the match key and must be unique: {dupes}")
    for c in cards:
        guard(c["name"], c["sql"])
    # CI stops here: no .env, no network, no Metabase.
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
        sys.exit(f"not connected: {missing} — run `make dashboards`, which connects first")

    # Before anything is written: the headings name the households they draw.
    name_the_cards(cards, resolve_households(s, ids[SERVING_DB]))

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
                   {"name": SUBCOLLECTION, "color": "#88BF4D", "parent_id": parent,
                    "description": "Built by analysis/metabase/build_sprint4_dashboard.py"},
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
