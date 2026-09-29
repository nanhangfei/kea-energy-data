#!/usr/bin/env python3
"""Build the Kea serving dashboard in Metabase.

The schema explorer over the serving database: a question per table plus the
forecast panels, all driven by one `icp_id` filter.

⚠️ **The nine households it explores are no longer hand-seeded.** Stage 5's
load replaced every fixture row with pipeline output, so the ids this file used
to hard-code are gone and the households are named by `icp_number` instead,
resolved at build time. The dashboard's subject did not change — the same nine
households, the same nine scenarios — only where their rows come from.

Started life as a Grafana dashboard; that pass is over and Grafana now carries
pipeline health only, so this is where the panels live.

**This file is the record.** A Metabase question lives in Metabase's own
application database — not in git, not reviewable in a PR, and gone after a
`docker compose down -v`. The queries below are the version-controlled source;
Metabase is where they are read. Re-run to rebuild after a wipe, or after
editing a query here.

Idempotent: questions and the dashboard are matched by name and updated in
place, so the dashboard URL stays stable.

    python3 analysis/metabase/build_dashboard.py      # or: make metabase-dashboard
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
SUBCOLLECTION = "Sprint 2 — The nine households"
DASHBOARD = "Sprint 2 — why this bill changed, one household at a time"

#: WHAT THESE WERE CALLED BEFORE, AND WHY THE LIST HAS TO EXIST. Both objects
#: are found by name, so on an instance built by an older revision the current
#: name matches nothing: the build would leave that instance's populated
#: collection sitting there and create a new empty one beside it. Adopting the
#: former name and renaming it in place is what makes this script idempotent on
#: ANY instance rather than only on a fresh one -- and it removes the manual
#: "rename the cards first" prerequisite that used to be a release note.
#: Append, never replace: an instance can be any number of renames behind.
SUBCOLLECTION_WAS = ("Stage1 - The nine households",)
DASHBOARD_WAS = ("Kea serving — data explorer",)
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# THE NINE HOUSEHOLDS, BY BUSINESS KEY -- NOT BY SURROGATE.
#
# This file used to name them as literal ids: 131001 through 139001, and
# account 229001. That worked for exactly as long as the fixtures were the only
# thing in the serving store. Stage 5 replaced every row with pipeline output
# carrying the warehouse's OWN key_map surrogates, the hand-assigned ids ceased
# to exist, and all twelve cards went quietly empty -- each one a valid query
# returning nothing, which is the failure mode `verify.py` was written for.
#
# icp_number and account_number are the same on both sides because they come
# from the same source, so they are what this dashboard names its households
# by. The ids are looked up at build time and can change under it freely.
HOUSEHOLDS = [
    ("0165841877LCC54",  "The printed invoice — TOU with solar, reconciles to $50.54"),
    ("DEMO0000000201AA", "A full house — more people at home"),
    ("DEMO0000000301AA", "A cold snap — a cold month after a mild one"),
    ("DEMO0000000401AA", "Something is broken — a cylinder element stuck on"),
    ("DEMO0000000501AA", "A new appliance on a timer"),
    ("DEMO0000000601AA", "Estimated reads in the period"),
    ("DEMO0000000701AA", "A longer billing period — the one nobody suspects"),
    ("DEMO0000000801AA", "Thirteen months of history"),
    ("DEMO0000000901AA", "A mid-cycle forecast, ten days in"),
]

#: The six scenario households, the ones built to be compared with each other.
SCENARIOS = HOUSEHOLDS[1:7]

#: Fixture 08 — thirteen months of bills, the richest household to land on.
DEFAULT_HOUSEHOLD = "DEMO0000000801AA"

#: Fixture 09's account is preferred when it has a generated forecast. The
#: generated cohort normally supplies the richer weather-overlapping history.
FORECAST_ACCOUNT_NUMBER = "DEMO-ACCT-229001"

# Substituted in upsert() once the lookup has run. Both input and output panels
# deliberately stay on the same account and the same latest cycle.
FORECAST_INPUT_TOKEN = "__FORECAST_INPUT_ACCOUNT__"

TAG_TEMPLATE = {
    "icp_id": {
        "id": "b6f3a1c2-0000-4000-8000-000000000001",
        "name": "icp_id",
        "display-name": "ICP",
        "type": "number",
        "required": True,
    }
}

# (name, display, width, sql). Layout and axis settings live in LAYOUT.

WHICH = [
    ("The nine fixtures — which ICP to filter to", "table", 24, """
-- The ICP filter above takes a number, and nothing else on this dashboard
-- says which numbers are valid or what each one demonstrates. Start here.
--
-- The ids are LOOKED UP from icp_number, never written down: the pipeline
-- mints its own surrogates on every load, so a literal here would be right
-- until the next one and then silently empty.
SELECT i.icp_id AS "ICP",
       a.account_number AS "account_number",
       c.full_name AS "full_name",
       c.email AS "email",
       f.scenario AS "what it demonstrates",
       count(DISTINCT b.bill_id)  AS "bills",
       count(DISTINCT e.bill_id)  AS "explanations",
       count(DISTINCT x.forecast_date) AS "forecasts"
FROM  (VALUES
        ('0165841877LCC54',  'The printed invoice — TOU with solar, reconciles to $50.54'),
        ('DEMO0000000201AA', 'A full house — more people at home'),
        ('DEMO0000000301AA', 'A cold snap — a cold month after a mild one'),
        ('DEMO0000000401AA', 'Something is broken — a cylinder element stuck on'),
        ('DEMO0000000501AA', 'A new appliance on a timer'),
        ('DEMO0000000601AA', 'Estimated reads in the period'),
        ('DEMO0000000701AA', 'A longer billing period — the one nobody suspects'),
        ('DEMO0000000801AA', 'Thirteen months of history'),
        ('DEMO0000000901AA', 'A mid-cycle forecast, ten days in')
      ) AS f(icp_number, scenario)
JOIN   metering.icp     i ON i.icp_number = f.icp_number
JOIN   billing.account  a ON a.icp_id     = i.icp_id
JOIN   billing.customer c ON c.customer_id = a.customer_id
LEFT   JOIN billing.bill b ON b.account_id = a.account_id
LEFT   JOIN kea.mart_bill_explanation e ON e.account_id = a.account_id
LEFT   JOIN kea.bill_forecast x ON x.account_id = a.account_id
GROUP  BY i.icp_id, a.account_number, c.full_name, c.email, f.scenario
ORDER  BY i.icp_id
"""),
    ("Account context — the selected ICP", "table", 24,
     "SELECT * FROM kea.mart_account_context WHERE icp_id = {{icp_id}}"),
]

WHY = [
    ("Every scenario side by side — what actually drove each increase", "bar", 24, """
-- The comparison the fixtures exist for, and the one panel that ignores the
-- ICP filter on purpose: a scenario set is only meaningful next to the others.
-- Each bar is one bill's increase, split into the causes the mart attributes
-- it to. A tall "unexplained" would mean the decomposition is not working.
SELECT CASE i.icp_number
         WHEN 'DEMO0000000201AA' THEN 'Full house'
         WHEN 'DEMO0000000301AA' THEN 'Cold snap'
         WHEN 'DEMO0000000401AA' THEN 'Broken element'
         WHEN 'DEMO0000000501AA' THEN 'New appliance'
         WHEN 'DEMO0000000601AA' THEN 'Estimated reads'
         WHEN 'DEMO0000000701AA' THEN 'Longer period'
         ELSE i.icp_number END                         AS "scenario",
       e.delta_from_period_length                      AS "period length",
       e.delta_from_weather                            AS "weather",
       e.delta_from_base_load                          AS "base load",
       e.delta_from_detected_loads                     AS "detected loads",
       e.delta_from_tariff                             AS "tariff",
       e.delta_unexplained                             AS "unexplained"
FROM   kea.mart_bill_explanation e
JOIN   metering.icp i ON i.icp_id = e.icp_id
WHERE  i.icp_number IN ('DEMO0000000201AA','DEMO0000000301AA','DEMO0000000401AA',
                        'DEMO0000000501AA','DEMO0000000601AA','DEMO0000000701AA')
  AND  e.amount_delta IS NOT NULL
ORDER  BY e.amount_delta DESC
"""),
    ("The same six, as numbers", "table", 24, """
SELECT CASE i.icp_number
         WHEN 'DEMO0000000201AA' THEN 'Full house'
         WHEN 'DEMO0000000301AA' THEN 'Cold snap'
         WHEN 'DEMO0000000401AA' THEN 'Broken element'
         WHEN 'DEMO0000000501AA' THEN 'New appliance'
         WHEN 'DEMO0000000601AA' THEN 'Estimated reads'
         WHEN 'DEMO0000000701AA' THEN 'Longer period'
         ELSE i.icp_number END                AS "scenario",
       e.prior_total_amount                   AS "previous $",
       e.total_amount                         AS "this $",
       e.amount_delta                         AS "increase $",
       e.amount_delta_pct                     AS "increase %",
       e.prior_period_days || ' -> ' || e.period_days AS "days",
       e.prior_total_kwh                      AS "previous kWh",
       e.total_kwh                            AS "this kWh",
       e.temp_mean_prior                      AS "prev temp",
       e.temp_mean_this                       AS "temp",
       e.hdd_prior_period                     AS "prev HDD",
       e.hdd_this_period                      AS "HDD",
       e.estimated_read_pct                   AS "estimated %",
       e.delta_unexplained                    AS "unexplained $"
FROM   kea.mart_bill_explanation e
JOIN   metering.icp i ON i.icp_id = e.icp_id
WHERE  i.icp_number IN ('DEMO0000000201AA','DEMO0000000301AA','DEMO0000000401AA',
                        'DEMO0000000501AA','DEMO0000000601AA','DEMO0000000701AA')
  AND  e.amount_delta IS NOT NULL
ORDER  BY e.amount_delta DESC
"""),
    ("This household's bills, explained", "table", 24, """
-- Empty only where there is no prior bill to compare against.
SELECT e.period_start AS "from", e.period_end AS "to", e.period_days AS "days",
       e.prior_total_amount AS "previous $", e.total_amount AS "this $",
       e.amount_delta AS "delta $", e.amount_delta_pct AS "delta %",
       e.delta_from_period_length AS "period", e.delta_from_weather AS "weather",
       e.delta_from_base_load AS "base load",
       e.delta_from_detected_loads AS "detected loads", e.delta_from_tariff AS "tariff",
       e.delta_unexplained AS "unexplained",
       e.estimated_read_pct AS "estimated %", e.missing_interval_count AS "missing"
FROM   kea.mart_bill_explanation e
WHERE  e.icp_id = {{icp_id}}
ORDER  BY e.period_start
"""),
    ("Bill and volume over time", "line", 24, """
-- billing.bill names it total_charges; total_amount is the mart's name
-- for the same money. Different layer, different vocabulary.
SELECT b.period_end AS "period end",
       b.total_charges AS "bill $", b.total_import_kwh AS "kWh"
FROM   billing.bill b
WHERE  b.account_id IN (SELECT account_id FROM billing.account WHERE icp_id = {{icp_id}})
ORDER  BY b.period_end
"""),
]

USAGE = [
    ("Daily usage", "bar", 24, """
SELECT usage_date AS "day", import_kwh AS "kWh"
FROM   kea.mart_daily_usage WHERE icp_id = {{icp_id}} ORDER BY usage_date
"""),
    ("Usage against temperature", "line", 12, """
-- The weather driver, visible rather than asserted: as mean temperature
-- falls, consumption rises. HDD is the same fact on a base-16 scale.
SELECT usage_date AS "day", import_kwh AS "kWh",
       temp_mean_c AS "mean temp C", heating_degree_days AS "HDD"
FROM   kea.mart_daily_usage WHERE icp_id = {{icp_id}} ORDER BY usage_date
"""),
    ("Average day, weekday vs weekend", "line", 12, """
-- The printed bill's "Average Weekday Breakdown", made queryable. Weekday
-- and weekend are two different curves (design doc 8.7), so they stay
-- separate rather than being averaged into a shape neither day has.
--
-- PIVOTED INTO TWO COLUMNS on purpose. Returning a label column and letting
-- the chart break out by it needs that column named as a second dimension,
-- and if it is not, Metabase drops it silently and draws ONE averaged line
-- -- a shape neither a weekday nor a weekend has, with nothing to say it is
-- wrong. Two explicit measures cannot fail that way.
--
-- Averaged across every month the household has. mart_hourly_profile is
-- per month, so a 13-month household would otherwise give 26 curves.
--
-- THE TWO CURVES WILL SIT ALMOST ON TOP OF EACH OTHER, and that is the
-- sample data rather than the chart. generate_scenarios.py applies one
-- hourly shape to every day and varies only the daily total, so the
-- largest weekday/weekend difference across all nine fixtures is 0.0001
-- kWh in any hour. Daily totals differ by up to 7% purely because
-- weekends land on days with different weather.
--
-- Design 8.6 argues the opposite -- "weekday and weekend are two
-- different curves, and a household that is empty 9-to-5 on weekdays has
-- no such trough on Saturday" -- so is_weekday doubles this mart's row
-- count to carry a contrast the fixtures do not contain. Filed as an
-- open item; the LCL-derived cohort has real day-of-week variation,
-- because the source data does.
SELECT hour_of_day AS "hour",
       ROUND(AVG(avg_import_kwh) FILTER (WHERE is_weekday), 4)     AS "weekday",
       ROUND(AVG(avg_import_kwh) FILTER (WHERE NOT is_weekday), 4) AS "weekend"
FROM   kea.mart_hourly_profile WHERE icp_id = {{icp_id}}
GROUP  BY hour_of_day ORDER BY hour_of_day
"""),
    ("Half-hourly meter reads", "line", 24, """
SELECT interval_start AS "time", kwh AS "kWh"
FROM   metering.meter_read_hh WHERE icp_id = {{icp_id}} ORDER BY interval_start
"""),
]

ACC_IN = "WHERE  account_id = " + FORECAST_INPUT_TOKEN

FORECAST = [
    ("Forecast pipeline status", "table", 24,
     "-- Inputs and outputs have different owners, joined here as one status.\n"
     "SELECT a.account_number                                AS \"input account\",\n"
     "       count(*)                                       AS \"input snapshots\",\n"
     "       count(DISTINCT f.period_start)                  AS \"periods\",\n"
     "       count(*) FILTER (WHERE f.is_forecastable)\n"
     "                                                      AS \"forecast-ready inputs\",\n"
     "       (SELECT count(*) FROM kea.bill_forecast x\n"
     f"         {ACC_IN.replace('account_id', 'x.account_id')}) AS \"forecast outputs\",\n"
     "       CASE WHEN (SELECT count(*) FROM kea.bill_forecast x\n"
     f"                         {ACC_IN.replace('account_id', 'x.account_id')}) > 0\n"
     "              THEN 'forecast output available'\n"
     "            WHEN count(*) FILTER (WHERE f.is_forecastable) = 0\n"
     "              THEN 'inputs retained; model features are not yet sufficient'\n"
     "            ELSE 'forecast-ready inputs await the Stage 6 output job' END\n"
     "                                                      AS \"status\"\n"
     "FROM kea.mart_forecast_input f JOIN billing.account a USING (account_id)\n"
     f"{ACC_IN}\nGROUP BY a.account_number"),
    ("Latest cycle — observed kWh and cost", "line", 12,
     "SELECT as_at_date AS \"as at\", kwh_to_date AS \"observed kWh\",\n"
     "       cost_to_date AS \"observed cost $\"\n"
     f"FROM kea.mart_forecast_input\n{ACC_IN}\n"
     "  AND period_start = (SELECT max(period_start) FROM kea.mart_forecast_input\n"
     f"                      {ACC_IN})\nORDER BY as_at_date"),
    ("Latest cycle — measured and expected HDD", "bar", 12,
     "-- Fact displaces the expected remainder as the cycle progresses.\n"
     "SELECT as_at_date AS \"as at\",\n"
     "       hdd_to_date AS \"measured\",\n"
     "       hdd_expected_remaining AS \"expected remaining\"\n"
     f"FROM kea.mart_forecast_input\n{ACC_IN}\n"
     "  AND period_start = (SELECT max(period_start) FROM kea.mart_forecast_input\n"
     f"                      {ACC_IN})\nORDER BY as_at_date"),
    ("Latest cycle — projected bill and confidence band", "line", 24,
     "SELECT forecast_date AS \"as at\", projected_low AS \"low $\",\n"
     "       projected_amount AS \"projected $\", projected_high AS \"high $\"\n"
     f"FROM kea.bill_forecast\n{ACC_IN}\n"
     "  AND period_start = (SELECT max(period_start) FROM kea.bill_forecast\n"
     f"                      {ACC_IN})\nORDER BY forecast_date"),
]

TABLES = [
    ("billing.customer", "table", 8, "SELECT * FROM billing.customer LIMIT 50"),
    ("billing.account", "table", 8, "SELECT * FROM billing.account WHERE icp_id = {{icp_id}}"),
    ("metering.icp", "table", 8, "SELECT * FROM metering.icp WHERE icp_id = {{icp_id}}"),
    ("billing.bill", "table", 12,
     "SELECT * FROM billing.bill\nWHERE account_id IN (SELECT account_id FROM billing.account WHERE icp_id = {{icp_id}})"),
    ("billing.bill_line", "table", 12,
     "SELECT * FROM billing.bill_line\nWHERE bill_id IN (SELECT bill_id FROM billing.bill\n"
     "  WHERE account_id IN (SELECT account_id FROM billing.account WHERE icp_id = {{icp_id}}))"),
    ("catalogue.plan", "table", 8,
     "SELECT * FROM catalogue.plan\nWHERE plan_id IN (SELECT plan_id FROM billing.account WHERE icp_id = {{icp_id}})"),
    ("catalogue.tou_band", "table", 8,
     "SELECT * FROM catalogue.tou_band\nWHERE plan_id IN (SELECT plan_id FROM billing.account WHERE icp_id = {{icp_id}})"),
    ("catalogue.tariff_component", "table", 8,
     "SELECT * FROM catalogue.tariff_component\nWHERE plan_id IN (SELECT plan_id FROM billing.account WHERE icp_id = {{icp_id}})"),
    ("metering.meter_register", "table", 8,
     "SELECT * FROM metering.meter_register WHERE icp_id = {{icp_id}}"),
    ("metering.property", "table", 8, "SELECT * FROM metering.property LIMIT 50"),
    ("metering.network", "table", 8, "SELECT * FROM metering.network LIMIT 50"),
    ("kea.mart_forecast_input", "table", 24,
     f"SELECT * FROM kea.mart_forecast_input\n{ACC_IN}\n"
     "  AND period_start = (SELECT max(period_start) FROM kea.mart_forecast_input\n"
     f"                      {ACC_IN})\nORDER BY as_at_date"),
    ("kea.bill_forecast", "table", 24,
     f"SELECT * FROM kea.bill_forecast\n{ACC_IN}\n"
     "  AND period_start = (SELECT max(period_start) FROM kea.bill_forecast\n"
     f"                      {ACC_IN})\nORDER BY forecast_date"),
]


def bar(dim, *metrics, stack=None):
    v = {"graph.dimensions": [dim], "graph.metrics": list(metrics)}
    if stack:
        v["stackable.stack_type"] = stack
    return v


def line(dim, *metrics, split=False):
    v = {"graph.dimensions": [dim], "graph.metrics": list(metrics)}
    if split:
        v["graph.y_axis.auto_split"] = True
    return v


# Axis settings matter more here than anywhere: without them Metabase picks
# the columns itself, and the decomposition chart -- six measures against one
# dimension -- is exactly the shape it gets wrong.
#
# ⚠️ AND A DECLARED AXIS IS A LIST THAT HAS TO BE KEPT. Sprint 4 added
# `delta_from_detected_loads` to kea.mart_bill_explanation and to the query
# below; this list was not updated, so Metabase dropped the column from the
# chart without an error and the stacked bars quietly stopped summing to the
# increase they decompose. The one panel that exists to show nothing is
# unexplained was hiding a cause. `make verify-dashboards` is what catches
# this -- it fails any card returning a column the chart ignores -- so a new
# measure means a new entry here, checked by running it.
LAYOUT = {
    "The nine fixtures — which ICP to filter to": dict(w=24, h=6),
    "Account context — the selected ICP":         dict(w=24, h=4),
    "Every scenario side by side — what actually drove each increase":
        dict(w=24, h=7, viz=bar("scenario", "period length", "weather", "base load",
                                "detected loads", "tariff", "unexplained",
                                stack="stacked")),
    "The same six, as numbers":                   dict(w=24, h=6),
    "This household's bills, explained":          dict(w=24, h=9),
    "Bill and volume over time":                  dict(w=24, h=6,
        viz=line("period end", "bill $", "kWh", split=True)),
    "Daily usage":                                dict(w=24, h=6, viz=bar("day", "kWh")),
    "Usage against temperature":                  dict(w=12, h=6,
        viz=line("day", "kWh", "mean temp C", "HDD", split=True)),
    "Average day, weekday vs weekend":            dict(w=12, h=6,
        viz=line("hour", "weekday", "weekend")),
    "Half-hourly meter reads":                    dict(w=24, h=6, viz=line("time", "kWh")),
    "Forecast pipeline status":                   dict(w=24, h=3),
    "Latest cycle — observed kWh and cost":       dict(w=12, h=6,
        viz=line("as at", "observed kWh", "observed cost $", split=True)),
    "Latest cycle — measured and expected HDD":  dict(w=12, h=6,
        viz=bar("as at", "measured", "expected remaining", stack="stacked")),
    "Latest cycle — projected bill and confidence band": dict(w=24, h=6,
        viz=line("as at", "low $", "projected $", "high $")),
}

INTRO = (
    "### The nine households — as the pipeline now produces them\n\n"
    "Nine households, each demonstrating one reason a bill goes up, and one of them "
    "reconciling to a real printed invoice at **$50.54**.\n\n"
    "⚠️ **These rows are not the fixtures in `data/sample/`.** They are the pipeline's "
    "output for the same households, published into the backend's database; the fixtures "
    "now seed only the source systems, and their expected-output half is what "
    "`make assert-serving` checks this against. The households are "
    "the same and their `icp_number`s are unchanged; what differs is where the rows "
    "came from.\n\n"
    "⚠️ **So the ICP ids here will not match `sample/*.sql`, and that is correct.** "
    "The fixtures hand-assign 131001–139001; the pipeline mints its own surrogates from "
    "`key_map_icp`. A surrogate is whatever the load "
    "assigns, so it can change again; `icp_number` is the stable handle, and this "
    "dashboard resolves every household by it at build time rather than naming an id. "
    "Hard-coding ids is exactly what broke all twelve of these cards when Stage 5 first "
    "ran, and what `make verify-dashboards` caught.\n\n"
    "They are no longer *self-asserting* ground truth either. Each fixture used to check "
    "its own inserted figures; the pipeline's output is checked instead by "
    "`make assert-serving` — **56 checks, 50 passing, 6 known divergences** whose causes "
    "are recorded in that file.\n\n"
    "Set the **ICP filter** above — the first panel says which ids are currently valid "
    "and what each household shows. The *scenario comparison* and *forecast-input* sections "
    "deliberately ignore the filter: the first is only meaningful across all six at once, "
    "and forecast panels use one forecast-ready account with enough weather history.\n\n"
    "The generated cohort has its own dashboard, **Stage 2 — generated "
    "source data**. Queries here are version-controlled in "
    "`analysis/metabase/build_dashboard.py`; rebuild with `make metabase-dashboard`."
)

SECTIONS = [
    ("Which fixture am I looking at?", WHICH),
    ("Why is this bill higher?", WHY),
    ("Usage", USAGE),
    ("Mid-cycle bill forecast — one forecast-ready account", FORECAST),
    ("The tables underneath", TABLES),
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
        with urllib.request.urlopen(req, timeout=120) as r:
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
        sys.exit(f"database '{DB_NAME}' is not connected — run `make metabase-setup` first")

    cols = api("/api/collection", session=s)
    parent = next((c["id"] for c in cols if c.get("name") == COLLECTION), None)
    if parent is None:
        parent = api("/api/collection", {"name": COLLECTION, "color": "#509EE3"},
                     session=s)["id"]
    def _under_parent(c):
        return c.get("location", "").strip("/").endswith(str(parent))

    coll = next((c["id"] for c in cols
                 if c.get("name") == SUBCOLLECTION and _under_parent(c)), None)
    if coll is None:
        # Adopt a former name before creating anything. Renaming in place keeps
        # the collection's id, so every card, dashboard and bookmark inside it
        # survives; creating instead would strand them.
        previous = next((c for c in cols
                         if c.get("name") in SUBCOLLECTION_WAS and _under_parent(c)), None)
        if previous:
            api(f"/api/collection/{previous['id']}", {"name": SUBCOLLECTION},
                session=s, method="PUT")
            coll = previous["id"]
            print(f"renamed collection '{previous['name']}' -> '{SUBCOLLECTION}'")
    if coll is None:
        coll = api("/api/collection",
                   {"name": SUBCOLLECTION, "color": "#A989C5", "parent_id": parent,
                    "description": "Built by analysis/metabase/build_dashboard.py"},
                   session=s)["id"]
        print(f"created collection '{SUBCOLLECTION}'")

    # THE IDS ARE LOOKED UP, NEVER WRITTEN DOWN. Two of them have to reach
    # Metabase as literal numbers -- the ICP filter's default, and the forecast
    # account the forecast panels select directly -- and both used to be
    # hard-coded fixture surrogates that Stage 5's load made meaningless.
    #
    # The query runs through Metabase's own /api/dataset rather than a second
    # database connection: this script already holds a session and knows the
    # database id, and a psycopg2 dependency here would be a second place for
    # the connection details to drift out of step with metabase_setup.py.
    def scalar(sql):
        r = api("/api/dataset", {"type": "native", "database": db,
                                 "native": {"query": sql}}, session=s)
        rows = r.get("data", {}).get("rows") or []
        return rows[0][0] if rows else None

    default_icp = scalar("SELECT icp_id FROM metering.icp "
                         f"WHERE icp_number = '{DEFAULT_HOUSEHOLD}'")
    # Prefer an account with real outputs, then one whose warehouse safety gate
    # is open. This keeps every panel coherent and prevents a rich but
    # unforecastable fixture history from winning on raw row count alone.
    forecast_input_account = scalar(
        "SELECT i.account_id FROM kea.mart_forecast_input i "
        "LEFT JOIN kea.bill_forecast f ON f.account_id = i.account_id "
        " AND f.period_start = i.period_start AND f.forecast_date = i.as_at_date "
        "GROUP BY i.account_id "
        "ORDER BY (count(f.forecast_id) > 0) DESC, "
        "         count(*) FILTER (WHERE i.is_forecastable) DESC, "
        "         (i.account_id = (SELECT account_id FROM billing.account "
        f"          WHERE account_number = '{FORECAST_ACCOUNT_NUMBER}')) DESC NULLS LAST, "
        "         count(*) DESC, i.account_id LIMIT 1")
    if default_icp is None or forecast_input_account is None:
        sys.exit(f"{DEFAULT_HOUSEHOLD} or forecast inputs are not in the served "
                 "tables — publish first: `make demo-refresh`, or "
                 "`make publish-serving RELEASE=<id>` over a current export")
    print(f"resolved {DEFAULT_HOUSEHOLD} -> icp_id {default_icp}, "
          f"forecast inputs -> account_id {forecast_input_account}")

    tag = json.loads(json.dumps(TAG_TEMPLATE))
    tag["icp_id"]["default"] = default_icp

    existing = {c["name"]: c["id"] for c in
                api(f"/api/collection/{coll}/items?models=card", session=s).get("data", [])}

    def upsert(name, display, sql):
        sql = sql.replace(FORECAST_INPUT_TOKEN, str(forecast_input_account))
        uses = "{{icp_id}}" in sql
        payload = {
            "name": name, "display": display,
            "visualization_settings": LAYOUT.get(name, {}).get("viz", {}),
            "collection_id": coll,
            "dataset_query": {"type": "native", "database": db,
                              "native": {"query": sql.strip(),
                                         "template-tags": tag if uses else {}}},
        }
        if name in existing:
            api(f"/api/card/{existing[name]}", payload, session=s, method="PUT")
            return existing[name], "updated", uses
        return api("/api/card", payload, session=s)["id"], "created", uses

    cards, made = [], {"created": 0, "updated": 0}
    for section, panels in SECTIONS:
        cards.append(("heading", section, 24, False, 1))
        for name, display, width, sql in panels:
            cid, what, uses = upsert(name, display, sql)
            made[what] += 1
            spec = LAYOUT.get(name, {})
            cards.append(("card", (cid, name), spec.get("w", width), uses,
                          spec.get("h", 6)))
    print(f"questions: {made['created']} created, {made['updated']} updated")

    def _dashboards(collection_id):
        return api(f"/api/collection/{collection_id}/items?models=dashboard",
                   session=s).get("data", [])

    dash = next((d for d in _dashboards(coll) if d["name"] == DASHBOARD), None)
    if dash is None:
        # It lived in the parent collection before this file grew a
        # sub-collection; move it rather than orphaning the URL.
        dash = next((d for d in _dashboards(parent) if d["name"] == DASHBOARD), None)
        if dash:
            api(f"/api/dashboard/{dash['id']}", {"collection_id": coll},
                session=s, method="PUT")
    if dash is None:
        # Same adoption as the collection above, and for the same reason: a
        # dashboard found by a former name is renamed in place, so its id, its
        # cards and any link to it survive. Both locations are searched because
        # an old instance may be behind on the move AND the rename.
        previous = next((d for d in _dashboards(coll) if d["name"] in DASHBOARD_WAS), None)
        where = coll
        if previous is None:
            previous = next((d for d in _dashboards(parent) if d["name"] in DASHBOARD_WAS), None)
            where = parent
        if previous:
            api(f"/api/dashboard/{previous['id']}",
                {"name": DASHBOARD, "collection_id": coll}, session=s, method="PUT")
            dash = previous
            moved = " and moved" if where == parent else ""
            print(f"renamed dashboard '{previous['name']}' -> '{DASHBOARD}'{moved}")
    dash_id = dash["id"] if dash else api(
        "/api/dashboard", {"name": DASHBOARD, "collection_id": coll}, session=s)["id"]

    param = {"id": "icp0001", "name": "ICP", "slug": "icp_id",
             "type": "number/=", "sectionId": "number", "default": [default_icp]}

    # Shelf packing: the next row starts below the TALLEST card in the
    # current one. A fixed stride overlaps cards or leaves dead bands.
    dashcards, neg = [], -1
    row = col = band = 0

    def place(**kw):
        nonlocal neg
        dashcards.append({"id": neg, "parameter_mappings": [], **kw})
        neg -= 1

    place(card_id=None, row=0, col=0, size_x=24, size_y=5,
          visualization_settings={"virtual_card": {"display": "text"}, "text": INTRO})
    row = 5

    for kind, payload, width, uses, height in cards:
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
              visualization_settings={},
              parameter_mappings=([{"parameter_id": param["id"], "card_id": cid,
                                    "target": ["variable", ["template-tag", "icp_id"]]}]
                                  if uses else []))
        col += width
        band = max(band, height)
        if col >= 24:
            row += band
            col = band = 0

    api(f"/api/dashboard/{dash_id}", {"parameters": [param], "dashcards": dashcards},
        session=s, method="PUT")
    print(f"dashboard: {len(dashcards)} cards -> {MB}/dashboard/{dash_id}")

    built = {name for _, panels in SECTIONS for name, _, _, _ in panels}
    for c in api(f"/api/collection/{coll}/items?models=card", session=s).get("data", []):
        if c["name"] not in built:
            api(f"/api/card/{c['id']}", {"archived": True}, session=s, method="PUT")
            print(f"  archived stale question: {c['name']}")


if __name__ == "__main__":
    main()
