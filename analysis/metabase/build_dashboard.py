#!/usr/bin/env python3
"""Build the Kea serving dashboard in Metabase.

The schema explorer over the serving database: a question per table plus the
forecast panels, all driven by one `icp_id` filter.

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
SUBCOLLECTION = "Sample data — the nine fixtures"
DASHBOARD = "Kea serving — data explorer"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# Fixture 08 — thirteen months of bills and thirteen explanations, the
# richest household to land on. The forecast panels do NOT use this filter:
# only fixture 09 has forecast snapshots, so they select that account
# directly. Otherwise whichever ICP is chosen leaves one section empty, and
# an empty panel reads as a broken one.
DEFAULT_ICP = 138001

# Fixture 09's account. Named once, here, rather than buried in six queries.
FORECAST_ACCOUNT = 229001

TAG = {
    "icp_id": {
        "id": "b6f3a1c2-0000-4000-8000-000000000001",
        "name": "icp_id",
        "display-name": "ICP",
        "type": "number",
        "default": DEFAULT_ICP,
        "required": True,
    }
}

# (name, display, width, sql). Layout and axis settings live in LAYOUT.

WHICH = [
    ("The nine fixtures — which ICP to filter to", "table", 24, """
-- The ICP filter above takes a number, and nothing else on this dashboard
-- says which numbers are valid or what each one demonstrates. Start here.
SELECT f.icp_id AS "ICP", f.scenario AS "what it demonstrates",
       count(DISTINCT b.bill_id)  AS "bills",
       count(DISTINCT e.bill_id)  AS "explanations",
       count(DISTINCT x.forecast_date) AS "forecasts"
FROM  (VALUES
        (131001, 'The printed invoice — TOU with solar, reconciles to $50.54'),
        (132001, 'A full house — more people at home'),
        (133001, 'A cold snap — a cold month after a mild one'),
        (134001, 'Something is broken — a cylinder element stuck on'),
        (135001, 'A new appliance on a timer'),
        (136001, 'Estimated reads in the period'),
        (137001, 'A longer billing period — the one nobody suspects'),
        (138001, 'Thirteen months of history'),
        (139001, 'A mid-cycle forecast, ten days in')
      ) AS f(icp_id, scenario)
JOIN   billing.account a ON a.icp_id = f.icp_id
LEFT   JOIN billing.bill b ON b.account_id = a.account_id
LEFT   JOIN kea.mart_bill_explanation e ON e.account_id = a.account_id
LEFT   JOIN kea.bill_forecast x ON x.account_id = a.account_id
GROUP  BY 1, 2 ORDER BY 1
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
SELECT CASE e.icp_id
         WHEN 132001 THEN 'Full house'      WHEN 133001 THEN 'Cold snap'
         WHEN 134001 THEN 'Broken element'  WHEN 135001 THEN 'New appliance'
         WHEN 136001 THEN 'Estimated reads' WHEN 137001 THEN 'Longer period'
         ELSE e.icp_id::text END                       AS "scenario",
       e.delta_from_period_length                      AS "period length",
       e.delta_from_weather                            AS "weather",
       e.delta_from_base_load                          AS "base load",
       e.delta_from_tariff                             AS "tariff",
       e.delta_unexplained                             AS "unexplained"
FROM   kea.mart_bill_explanation e
WHERE  e.icp_id BETWEEN 132001 AND 137001 AND e.amount_delta IS NOT NULL
ORDER  BY e.amount_delta DESC
"""),
    ("The same six, as numbers", "table", 24, """
SELECT CASE e.icp_id
         WHEN 132001 THEN 'Full house'      WHEN 133001 THEN 'Cold snap'
         WHEN 134001 THEN 'Broken element'  WHEN 135001 THEN 'New appliance'
         WHEN 136001 THEN 'Estimated reads' WHEN 137001 THEN 'Longer period'
         ELSE e.icp_id::text END              AS "scenario",
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
WHERE  e.icp_id BETWEEN 132001 AND 137001 AND e.amount_delta IS NOT NULL
ORDER  BY e.amount_delta DESC
"""),
    ("This household's bills, explained", "table", 24, """
-- Empty only where there is no prior bill to compare against.
SELECT e.period_start AS "from", e.period_end AS "to", e.period_days AS "days",
       e.prior_total_amount AS "previous $", e.total_amount AS "this $",
       e.amount_delta AS "delta $", e.amount_delta_pct AS "delta %",
       e.delta_from_period_length AS "period", e.delta_from_weather AS "weather",
       e.delta_from_base_load AS "base load", e.delta_from_tariff AS "tariff",
       e.delta_unexplained AS "unexplained",
       e.estimated_read_pct AS "estimated %", e.missing_interval_count AS "missing"
FROM   kea.mart_bill_explanation e
WHERE  e.icp_id = {{icp_id}}
ORDER  BY e.period_start
"""),
    ("Bill and volume over time", "line", 24, """
-- billing.bill names it total_charges; total_amount is the mart's name
-- for the same money. Different layer, different vocabulary.
SELECT b.period_end AS "period end", b.period_days AS "days",
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
-- and weekend are two different curves (design doc 8.6), so they stay
-- separate rather than being averaged into a shape neither day has.
SELECT hour_of_day AS "hour",
       to_char(profile_month, 'Mon YYYY')
         || CASE WHEN is_weekday THEN ' weekday' ELSE ' weekend' END AS "month",
       avg_import_kwh AS "kWh"
FROM   kea.mart_hourly_profile WHERE icp_id = {{icp_id}}
ORDER  BY profile_month, is_weekday, hour_of_day
"""),
    ("Half-hourly meter reads", "line", 24, """
SELECT interval_start AS "time", kwh AS "kWh"
FROM   metering.meter_read_hh WHERE icp_id = {{icp_id}} ORDER BY interval_start
"""),
]

ACC = "WHERE  account_id = " + str(FORECAST_ACCOUNT)

FORECAST = [
    ("As at the latest forecast", "table", 24,
     "-- The sentence the agent has to be able to say, and the evidence for it.\n"
     "-- Fixture 09 only, so this section ignores the ICP filter.\n"
     "SELECT projected_amount               AS \"projected bill\",\n"
     "       projected_low                  AS \"low\",\n"
     "       projected_high                 AS \"high\",\n"
     "       projected_high - projected_low AS \"band width\",\n"
     "       baseline_amount                AS \"same period last year\",\n"
     "       variance_pct                   AS \"variance\"\n"
     f"FROM   kea.bill_forecast\n{ACC}\nORDER  BY forecast_date DESC\nLIMIT  1"),
    ("The band narrows because there is less period left to be wrong about", "line", 24,
     "-- x is days_elapsed, not a timestamp: the fixture is June 2024 and would\n"
     "-- fall outside any dashboard time range.\n"
     "SELECT days_elapsed     AS \"day of cycle\",\n"
     "       projected_low    AS \"low\",\n"
     "       projected_amount AS \"projected bill\",\n"
     "       projected_high   AS \"high\",\n"
     "       baseline_amount  AS \"same period last year\"\n"
     f"FROM   kea.bill_forecast\n{ACC}\n"
     "  AND  period_start = (SELECT max(period_start) FROM kea.bill_forecast\n"
     f"                      {ACC})\n"
     "ORDER  BY days_elapsed"),
    ("Where the projected kWh comes from", "bar", 24,
     "-- projected_kwh, taken apart. Metered is fact; the other two are the split\n"
     "-- the design doc argues for -- baseline and weather forecast separately, so\n"
     "-- a warm spell can pull the projection down on its own.\n"
     "SELECT to_char(as_at_date, 'DD Mon')                              AS \"as at\",\n"
     "       ROUND(kwh_to_date, 1)                                      AS \"metered so far\",\n"
     "       ROUND((days_total - days_elapsed) * baseline_daily_kwh, 1) AS \"baseline, remaining\",\n"
     "       ROUND(hdd_expected_remaining * heating_sensitivity_kwh_per_hdd, 1) AS \"weather, remaining\"\n"
     f"FROM   kea.mart_forecast_input\n{ACC}\nORDER  BY as_at_date"),
    ("Heating degree days: fact displacing forecast over a fixed total", "bar", 12,
     "-- The invariant the fixture asserts: fact displaces forecast, day by day,\n"
     "-- over a total that never moves (282.42).\n"
     "SELECT to_char(as_at_date, 'DD Mon') AS \"as at\",\n"
     "       hdd_to_date                   AS \"measured\",\n"
     "       hdd_expected_remaining        AS \"forecast\"\n"
     f"FROM   kea.mart_forecast_input\n{ACC}\nORDER  BY as_at_date"),
    ("Band width, in dollars", "bar", 12,
     "SELECT to_char(forecast_date, 'DD Mon')  AS \"as at\",\n"
     "       projected_high - projected_low    AS \"band width\"\n"
     f"FROM   kea.bill_forecast\n{ACC}\nORDER  BY forecast_date"),
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
     f"SELECT * FROM kea.mart_forecast_input\n{ACC}\nORDER BY as_at_date"),
    ("kea.bill_forecast", "table", 24,
     f"SELECT * FROM kea.bill_forecast\n{ACC}\nORDER BY forecast_date"),
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
# the columns itself, and the decomposition chart -- five measures against one
# dimension -- is exactly the shape it gets wrong.
LAYOUT = {
    "The nine fixtures — which ICP to filter to": dict(w=24, h=6),
    "Account context — the selected ICP":         dict(w=24, h=4),
    "Every scenario side by side — what actually drove each increase":
        dict(w=24, h=7, viz=bar("scenario", "period length", "weather", "base load",
                                "tariff", "unexplained", stack="stacked")),
    "The same six, as numbers":                   dict(w=24, h=6),
    "This household's bills, explained":          dict(w=24, h=9),
    "Bill and volume over time":                  dict(w=24, h=6,
        viz=line("period end", "bill $", "kWh", split=True)),
    "Daily usage":                                dict(w=24, h=6, viz=bar("day", "kWh")),
    "Usage against temperature":                  dict(w=12, h=6,
        viz=line("day", "kWh", "mean temp C", "HDD", split=True)),
    "Average day, weekday vs weekend":            dict(w=12, h=6,
        viz=line("hour", "kWh")),
    "Half-hourly meter reads":                    dict(w=24, h=6, viz=line("time", "kWh")),
    "As at the latest forecast":                  dict(w=24, h=3),
    "The band narrows because there is less period left to be wrong about":
        dict(w=24, h=7, viz=line("day of cycle", "low", "projected bill", "high",
                                 "same period last year")),
    "Where the projected kWh comes from":         dict(w=24, h=6,
        viz=bar("as at", "metered so far", "baseline, remaining", "weather, remaining",
                stack="stacked")),
    "Heating degree days: fact displacing forecast over a fixed total":
        dict(w=12, h=6, viz=bar("as at", "measured", "forecast", stack="stacked")),
    "Band width, in dollars":                     dict(w=12, h=6, viz=bar("as at", "band width")),
}

INTRO = (
    "### The nine fixtures — hand-built sample data\n\n"
    "Nine households seeded by `data/sample/*.sql`, each demonstrating one reason "
    "a bill goes up. They are **ground truth**: every figure is asserted by the "
    "fixture that creates it, and one of them reconciles to a real printed invoice.\n\n"
    "Set the **ICP filter** above — the first panel says which numbers are valid and "
    "what each one shows. The *scenario comparison* and *forecast* sections deliberately "
    "ignore the filter: the first is only meaningful across all six at once, and only "
    "fixture 09 has forecasts.\n\n"
    "The generated 500-household cohort has its own dashboard, **Stage 2 — generated "
    "source data**. Queries here are version-controlled in "
    "`analysis/metabase/build_dashboard.py`; rebuild with `make metabase-dashboard`."
)

SECTIONS = [
    ("Which fixture am I looking at?", WHICH),
    ("Why is this bill higher?", WHY),
    ("Usage", USAGE),
    ("The mid-cycle forecast — fixture 09, regardless of the filter", FORECAST),
    ("The tables underneath", TABLES),
]


def env() -> dict[str, str]:
    if not ENV_FILE.exists():
        sys.exit(f"{ENV_FILE} not found — cp .env.example .env first")
    return dict(
        l.split("=", 1) for l in ENV_FILE.read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.startswith("#") and "=" in l
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
    coll = next((c["id"] for c in cols
                 if c.get("name") == SUBCOLLECTION
                 and c.get("location", "").strip("/").endswith(str(parent))), None)
    if coll is None:
        coll = api("/api/collection",
                   {"name": SUBCOLLECTION, "color": "#A989C5", "parent_id": parent,
                    "description": "Built by analysis/metabase/build_dashboard.py"},
                   session=s)["id"]
        print(f"created collection '{SUBCOLLECTION}'")

    existing = {c["name"]: c["id"] for c in
                api(f"/api/collection/{coll}/items?models=card", session=s).get("data", [])}

    def upsert(name, display, sql):
        uses = "{{icp_id}}" in sql
        payload = {
            "name": name, "display": display,
            "visualization_settings": LAYOUT.get(name, {}).get("viz", {}),
            "collection_id": coll,
            "dataset_query": {"type": "native", "database": db,
                              "native": {"query": sql.strip(),
                                         "template-tags": TAG if uses else {}}},
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

    dash = next((d for d in api(f"/api/collection/{coll}/items?models=dashboard",
                                session=s).get("data", []) if d["name"] == DASHBOARD), None)
    if dash is None:
        # It lived in the parent collection before this file grew a
        # sub-collection; move it rather than orphaning the URL.
        dash = next((d for d in api(f"/api/collection/{parent}/items?models=dashboard",
                                    session=s).get("data", []) if d["name"] == DASHBOARD), None)
        if dash:
            api(f"/api/dashboard/{dash['id']}", {"collection_id": coll},
                session=s, method="PUT")
    dash_id = dash["id"] if dash else api(
        "/api/dashboard", {"name": DASHBOARD, "collection_id": coll}, session=s)["id"]

    param = {"id": "icp0001", "name": "ICP", "slug": "icp_id",
             "type": "number/=", "sectionId": "number", "default": [DEFAULT_ICP]}

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
