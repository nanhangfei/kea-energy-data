#!/usr/bin/env python3
"""Build the Stage 2 source-data dashboard in Metabase.

The nine fixtures have their own dashboard (build_dashboard.py, over the
serving marts). This one describes the generated households in
src_metering / src_billing / src_catalogue. The normal demo profile has 50;
the cards calculate the actual cohort size so the full profile remains valid.

**This file is the record.** A Metabase question lives in Metabase's own
application database: not in git, not reviewable in a PR, and gone after a
`docker compose down -v`. The queries below are the version-controlled source;
Metabase is only where they are read.

Heavy panels read the materialised rollups in gen_audit (see
source_views.sql), not interval_reads directly. Re-aggregating millions of
rows once per panel makes the dashboard unusable and the filter unresponsive.

Idempotent: questions and the dashboard are matched by name and updated in
place, so the URL stays stable.

    make source-dashboard        # refreshes the views, then runs this
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
# Its own sub-collection, so a renamed panel can be cleaned up without
# touching the fixture dashboard's questions in the parent.
SUBCOLLECTION = "Stage 2 — source data"
DASHBOARD = "Stage 2 — generated source data"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# A solar household, so the export panels show something.
#
# ⚠️ STABLE ACROSS A REGENERATION, NOT ACROSS A WINDOW CHANGE. The cohort is
# deterministic, so re-running `make init-data` reproduces it exactly -- but
# the ICP number carries the household's INDEX in the cohort, so anything that
# reorders the cohort renumbers almost everyone. Moving uk_to_nz.LCL_START on
# 2026-09-04 kept only part of the earlier cohort and left few on their
# old ICP; this one is the same house (MAC004882, AURA, Waikato) that used to
# be ...AU05. The compact one-network profile changes its suffix to VE.
#
# If a panel comes back empty after a window change, this is the line.
DEFAULT_ICP = "0927296872VE04"

TAG = {
    "icp": {
        "id": "c7a4b2d3-0000-4000-8000-000000000002",
        "name": "icp",
        "display-name": "ICP",
        "type": "text",
        "default": DEFAULT_ICP,
        "required": True,
    }
}

COHORT = [
    ("Cohort at a glance", "table", 24, """
SELECT (SELECT count(*) FROM src_metering.connection_points WHERE icp_identifier LIKE '09%')  AS "households",
       (SELECT SUM(intervals) FROM gen_audit.mv_daily)                                        AS "half-hours (latest version)",
       (SELECT count(*) FROM src_billing.invoices i JOIN src_billing.accounts a
          ON a.ACCT_ID = i.ACCT_ID WHERE a.ICP_NUMBER LIKE '09%')                             AS "invoices",
       (SELECT count(*) FROM src_catalogue.price_plans WHERE plan_id >= 1000000)              AS "plan versions",
       (SELECT count(DISTINCT icp_identifier) FROM gen_audit.mv_daily
         WHERE register_code = 'EX')                                                          AS "with solar",
       (SELECT min(nz_date) FROM gen_audit.mv_daily)                                          AS "from",
       (SELECT max(nz_date) FROM gen_audit.mv_daily)                                          AS "to"
"""),
    ("Households by the three plan families", "bar", 12, """
SELECT CASE split_part(a.PLAN_CODE, '_', 2)
           WHEN 'ANY'  THEN 'Anytime Rate'
           WHEN 'TOU'  THEN 'Time of Use'
           WHEN 'WKND' THEN 'Free Weekends'
       END AS "plan", count(*) AS "households"
FROM   src_billing.accounts a
WHERE  a.ICP_NUMBER LIKE '09%'
  AND  split_part(a.PLAN_CODE, '_', 2) IN ('ANY', 'TOU', 'WKND')
GROUP  BY 1 ORDER BY 1
"""),
    ("Households by payment method", "bar", 12, """
SELECT a.PAY_METHOD AS "pay method", count(*) AS "households"
FROM   src_billing.accounts a
WHERE  a.ICP_NUMBER LIKE '09%'
GROUP  BY 1 ORDER BY 1
"""),
    ("Completeness — share of expected half-hours held", "bar", 24, """
-- Measured against what Pacific/Auckland says each day actually is, so a
-- 46-hour day counts as complete at 46. Anything short is a real gap.
-- Two levels: completeness PER HOUSEHOLD, then a histogram of that.
-- One level gives a row per household, which is a list rather than a shape.
SELECT pct AS "% complete", count(*) AS "households"
FROM  (SELECT icp_identifier,
              ROUND(100.0 * SUM(intervals) / SUM(expected), 0) AS pct
       FROM  (SELECT icp_identifier, intervals,
                     (EXTRACT(epoch FROM
                        ((nz_date + 1)::timestamp AT TIME ZONE 'Pacific/Auckland')
                      - ( nz_date     ::timestamp AT TIME ZONE 'Pacific/Auckland')) / 1800)::int
                       AS expected
              FROM   gen_audit.mv_daily WHERE register_code = 'UN') t
       GROUP  BY icp_identifier) h
GROUP  BY 1 ORDER BY 1
"""),
]

USAGE = [
    ("Mean daily kWh by month — the season flip", "line", 12, """
SELECT date_trunc('month', nz_date)::date AS "month",
       ROUND(AVG(kwh), 2) AS "mean kWh/day"
FROM   gen_audit.mv_daily
WHERE  register_code = 'UN' AND intervals >= 46
GROUP  BY 1 ORDER BY 1
"""),
    ("Average load shape — winter vs summer", "line", 12, """
SELECT half_hour / 2.0 AS "hour of day",
       ROUND(AVG(avg_kwh) FILTER (WHERE nz_month IN (6,7,8)),  4) AS "winter (Jun-Aug)",
       ROUND(AVG(avg_kwh) FILTER (WHERE nz_month IN (12,1,2)), 4) AS "summer (Dec-Feb)"
FROM   gen_audit.mv_profile
WHERE  register_code = 'UN'
GROUP  BY 1 ORDER BY 1
"""),
    ("Daily consumption distribution", "bar", 12, """
SELECT LEAST((kwh / 5)::int * 5, 100) AS "kWh/day (bucket)", count(*) AS "household-days"
FROM   gen_audit.mv_daily
WHERE  register_code = 'UN' AND intervals >= 46
GROUP  BY 1 ORDER BY 1
"""),
    ("Solar export by month", "line", 12, """
SELECT date_trunc('month', nz_date)::date AS "month",
       ROUND(AVG(kwh) FILTER (WHERE register_code = 'EX'), 3) AS "mean export kWh/day",
       ROUND(AVG(kwh) FILTER (WHERE register_code = 'UN'), 3) AS "mean import kWh/day"
FROM   gen_audit.mv_daily
WHERE  icp_identifier IN (SELECT DISTINCT icp_identifier FROM gen_audit.mv_daily
                          WHERE register_code = 'EX')
GROUP  BY 1 ORDER BY 1
"""),
    ("The four daylight-saving days", "table", 12, """
-- Filtered on what the ZONE says the day is, not on the count held.
-- Matching intervals IN (46,50) instead catches every gap day that
-- happens to land on 46 -- 128 dates rather than four.
SELECT d.nz_date AS "NZ date", x.expected AS "true half-hours",
       count(*) FILTER (WHERE d.intervals = x.expected) AS "households complete",
       count(*)                                         AS "households with data"
FROM   gen_audit.mv_daily d
CROSS  JOIN LATERAL (SELECT (EXTRACT(epoch FROM
              ((d.nz_date + 1)::timestamp AT TIME ZONE 'Pacific/Auckland')
            - ( d.nz_date     ::timestamp AT TIME ZONE 'Pacific/Auckland')) / 1800)::int
              AS expected) x
WHERE  d.register_code = 'UN' AND x.expected <> 48
GROUP  BY 1, 2 ORDER BY 1
"""),
    ("Heaviest households", "table", 12, """
SELECT icp_identifier AS "ICP", ROUND(AVG(kwh), 1) AS "mean kWh/day",
       ROUND(MAX(kwh), 1) AS "biggest day", count(*) AS "days"
FROM   gen_audit.mv_daily WHERE register_code = 'UN' AND intervals >= 46
GROUP  BY 1 ORDER BY 2 DESC LIMIT 20
"""),
]

BILLING = [
    ("Average bill and effective rate by plan", "table", 12, """
SELECT plan AS "plan", count(*) AS "invoices",
       ROUND(AVG(TOTAL_AMT), 2)                                        AS "avg bill",
       ROUND(AVG(TOTAL_IMPORT_KWH), 0)                                 AS "avg kWh",
       ROUND(AVG(TOTAL_AMT) / NULLIF(AVG(TOTAL_IMPORT_KWH), 0), 4)     AS "eff. $/kWh",
       ROUND(AVG(EXPORT_CREDIT), 2)                                    AS "avg export credit"
FROM   gen_audit.mv_bills
WHERE  plan IN ('ANY', 'TOU', 'WKND')
GROUP BY 1 ORDER BY 5 DESC
"""),
    ("Bill size distribution", "bar", 12, """
SELECT LEAST((TOTAL_AMT / 50)::int * 50, 800) AS "bill ($, bucket)", count(*) AS "invoices"
FROM   gen_audit.mv_bills WHERE TOTAL_AMT >= 0 GROUP BY 1 ORDER BY 1
"""),
    ("BILL SHOCK — biggest month-on-month increases", "table", 24, """
-- The project's whole question: this bill is much larger, why?
-- The three candidate causes are side by side, so the answer is visible
-- rather than inferred. A longer period is the one customers never
-- suspect and src_billing.invoices has no period_days column to show it.
SELECT icp_identifier AS "ICP", plan, period_from AS "from", period_to AS "to",
       prev_amt AS "previous $", TOTAL_AMT AS "this $",
       ROUND(TOTAL_AMT - prev_amt, 2)                                    AS "increase $",
       ROUND(100 * (TOTAL_AMT - prev_amt) / NULLIF(prev_amt, 0))         AS "increase %",
       prev_kwh AS "previous kWh", TOTAL_IMPORT_KWH AS "this kWh",
       ROUND(100 * (TOTAL_IMPORT_KWH - prev_kwh) / NULLIF(prev_kwh, 0))  AS "volume %",
       prev_days AS "prev days", period_days AS "days"
FROM   gen_audit.mv_bills
WHERE  prev_amt IS NOT NULL AND prev_amt > 0
ORDER  BY (TOTAL_AMT - prev_amt) DESC LIMIT 40
"""),
    ("Increases explained by a longer billing period", "table", 12, """
-- Fixture 07's trap, at cohort scale. Same daily usage, bigger bill,
-- purely because the period ran long.
SELECT icp_identifier AS "ICP", period_from AS "from", period_days AS "days",
       prev_days AS "prev days", prev_amt AS "previous $", TOTAL_AMT AS "this $",
       ROUND(TOTAL_AMT / NULLIF(period_days, 0), 2)   AS "$/day this",
       ROUND(prev_amt / NULLIF(prev_days, 0), 2)      AS "$/day previous"
FROM   gen_audit.mv_bills
WHERE  prev_days IS NOT NULL AND period_days > prev_days + 8
ORDER  BY (TOTAL_AMT - prev_amt) DESC LIMIT 25
"""),
    ("Solar export credits applied", "table", 12, """
SELECT icp_identifier AS "ICP", plan, period_from AS "from",
       TOTAL_IMPORT_KWH AS "import kWh", TOTAL_EXPORT_KWH AS "export kWh",
       EXPORT_CREDIT AS "credit $", TOTAL_AMT AS "bill $"
FROM   gen_audit.mv_bills WHERE EXPORT_CREDIT > 0
ORDER  BY EXPORT_CREDIT DESC LIMIT 25
"""),
    ("Billed volume and revenue by month", "line", 24, """
SELECT date_trunc('month', period_from)::date AS "month",
       ROUND(SUM(TOTAL_AMT), 0)        AS "billed $",
       ROUND(SUM(TOTAL_IMPORT_KWH), 0) AS "import kWh"
FROM   gen_audit.mv_bills GROUP BY 1 ORDER BY 1
"""),
]

QUALITY = [
    ("Injected defects by kind", "bar", 12, """
SELECT replace(defect_kind, 'src:', '') AS "defect", count(*) AS "rows"
FROM   gen_audit.defects WHERE defect_kind LIKE 'src:%'
GROUP  BY 1 ORDER BY 2 DESC
"""),
    ("Read status", "bar", 12, """
SELECT read_status AS "status", count(*) AS "reads"
FROM   src_metering.interval_reads WHERE source_file LIKE 'lcl%'
GROUP  BY 1 ORDER BY 2 DESC
"""),
    ("Incomplete household-days", "table", 12, """
-- Gaps, in runs. A day short of its true length is a comms outage, not
-- a clock change: the DST days are complete at 46 and 50.
SELECT nz_date AS "NZ date", count(*) AS "households short",
       ROUND(AVG(intervals), 1) AS "avg half-hours held"
FROM   gen_audit.mv_daily d
WHERE  register_code = 'UN'
  AND  intervals < (EXTRACT(epoch FROM ((nz_date + 1)::timestamp AT TIME ZONE 'Pacific/Auckland')
                                     - ( nz_date     ::timestamp AT TIME ZONE 'Pacific/Auckland')) / 1800)::int
GROUP  BY 1 HAVING count(*) > 1 ORDER BY 2 DESC LIMIT 25
"""),
    ("Stuck meters — a whole day of zeros", "table", 12, """
-- Passes the non-negative CHECK, passes completeness, passes any range
-- check. Only a distribution rule finds it.
SELECT d.icp_identifier AS "ICP", d.nz_date AS "NZ date", d.intervals AS "half-hours",
       d.kwh AS "kWh that day",
       ROUND((SELECT AVG(kwh) FROM gen_audit.mv_daily x
              WHERE x.icp_identifier = d.icp_identifier AND x.register_code = 'UN'), 2)
         AS "their usual kWh/day"
FROM   gen_audit.mv_daily d
WHERE  d.register_code = 'UN' AND d.kwh = 0 AND d.intervals >= 46
ORDER  BY 5 DESC LIMIT 25
"""),
]

REVISIONS = [
    ("Rows by version", "bar", 8, """
SELECT version_no AS "version", count(*) AS "rows"
FROM   src_metering.interval_reads WHERE source_file LIKE 'lcl%'
GROUP  BY 1 ORDER BY 1
"""),
    ("Arrival lag — reading date to delivery", "bar", 8, """
-- The extract watermarks on received_at, not interval_start, and this is
-- why: corrections arrive weeks after the reading they correct.
SELECT LEAST((received_at::date - (interval_start AT TIME ZONE 'Pacific/Auckland')::date), 60)
         AS "days late", count(*) AS "reads"
FROM   src_metering.interval_reads
WHERE  source_file LIKE 'lcl%'
GROUP  BY 1 ORDER BY 1
"""),
    ("The late correction Stage 3 must find", "table", 8, """
-- A five-week-old reading delivered three days before the span ends.
-- An extract filtered on interval_start will never see it.
SELECT icp_identifier AS "ICP",
       (interval_start AT TIME ZONE 'Pacific/Auckland')::date AS "reading date",
       received_at::date AS "delivered",
       received_at::date - (interval_start AT TIME ZONE 'Pacific/Auckland')::date AS "days late",
       version_no AS "version", count(*) AS "intervals"
FROM   src_metering.interval_reads WHERE source_file = 'lcl-rev-late.csv'
GROUP  BY 1, 2, 3, 4, 5
"""),
]

HOUSEHOLD = [
    ("Daily use — one household", "line", 24, """
SELECT nz_date AS "NZ date",
       ROUND(SUM(kwh) FILTER (WHERE register_code = 'UN'), 3) AS "import kWh",
       ROUND(SUM(kwh) FILTER (WHERE register_code = 'EX'), 3) AS "export kWh"
FROM   gen_audit.mv_daily WHERE icp_identifier = {{icp}}
GROUP  BY 1 ORDER BY 1
"""),
    ("Their bills", "table", 12, """
SELECT INV_NO AS "invoice", period_from AS "from", period_to AS "to",
       period_days AS "days", TOTAL_IMPORT_KWH AS "import kWh",
       TOTAL_EXPORT_KWH AS "export kWh", CHARGES_EXGST AS "ex GST",
       GST_AMT AS "GST", TOTAL_AMT AS "total $"
FROM   gen_audit.mv_bills WHERE icp_identifier = {{icp}} ORDER BY period_from
"""),
    ("Their most recent bill, line by line", "table", 12, """
SELECT l.CHG_GROUP AS "group", l.FLOW_DIR AS "flow", l.LINE_DESC AS "line",
       l.TOU_BAND AS "band", l.QTY AS "qty", l.UOM AS "uom",
       l.UNIT_RATE AS "rate", l.LOSS_QTY AS "losses", l.LINE_AMT AS "amount"
FROM   src_billing.invoice_lines l
JOIN   src_billing.invoices i ON i.INV_ID = l.INV_ID
JOIN   src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
WHERE  a.ICP_NUMBER = {{icp}}
  AND  i.INV_ID = (SELECT i2.INV_ID FROM src_billing.invoices i2
                   JOIN src_billing.accounts a2 ON a2.ACCT_ID = i2.ACCT_ID
                   WHERE a2.ICP_NUMBER = {{icp}}
                   ORDER BY i2.PERIOD_FROM DESC LIMIT 1)
ORDER  BY l.SORT_SEQ, l.LINE_ID
"""),
    ("Their load shape, busiest week", "line", 24, """
SELECT (r.interval_start AT TIME ZONE 'Pacific/Auckland') AS "NZ time",
       r.kwh AS "kWh"
FROM   src_metering.interval_reads r
WHERE  r.icp_identifier = {{icp}} AND r.register_code = 'UN' AND r.version_no = 1
  AND  (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date BETWEEN
       (SELECT nz_date - 3 FROM gen_audit.mv_daily
         WHERE icp_identifier = {{icp}} AND register_code = 'UN'
         ORDER BY kwh DESC LIMIT 1)
       AND
       (SELECT nz_date + 3 FROM gen_audit.mv_daily
         WHERE icp_identifier = {{icp}} AND register_code = 'UN'
         ORDER BY kwh DESC LIMIT 1)
ORDER  BY 1
"""),
    ("Their plan, as the catalogue holds it", "table", 24, """
SELECT p.code AS "plan", p.valid_from AS "from", p.valid_to AS "to",
       rc.charge_scope AS "group", rc.component_desc AS "component",
       w.band_label AS "band", w.days_csv AS "days", w.time_from AS "start",
       w.time_to AS "end", rc.rate_uom AS "uom", rc.flow_direction AS "flow",
       rc.unit_rate AS "rate"
FROM   src_billing.accounts a
JOIN   src_catalogue.price_plans p ON p.code = a.PLAN_CODE
JOIN   src_catalogue.rate_components rc ON rc.plan_id = p.plan_id
LEFT   JOIN src_catalogue.tou_windows w ON w.window_id = rc.window_id
WHERE  a.ICP_NUMBER = {{icp}}
ORDER  BY p.valid_from, rc.charge_scope, rc.flow_direction, rc.unit_rate DESC
"""),
]

# ---------------------------------------------------------------------
# Presentation, kept apart from the SQL above so a layout change cannot
# disturb a query that has been verified against the database.
#
#   w, h  -- Metabase's 24-column grid, height in grid rows
#   viz   -- axis assignments. WITHOUT THESE Metabase guesses which
#            column is the dimension and which are measures, and it
#            guesses wrong on every multi-series panel here: winter
#            against summer, import against export, dollars against kWh.
#            The names must match the SQL aliases exactly.
# ---------------------------------------------------------------------
def bar(dim, *metrics, stack=None):
    v = {"graph.dimensions": [dim], "graph.metrics": list(metrics)}
    if stack:
        v["stackable.stack_type"] = stack
    return v


def line(dim, *metrics, split=False):
    v = {"graph.dimensions": [dim], "graph.metrics": list(metrics)}
    if split:
        # Dollars and kWh differ by an order of magnitude; on one axis
        # the smaller series flattens to the baseline and reads as zero.
        v["graph.y_axis.auto_split"] = True
    return v


LAYOUT = {
    # The cohort
    "Cohort at a glance":                     dict(w=24, h=3),
    "Households by the three plan families":  dict(
        w=12, h=6, viz=bar("plan", "households")),
    "Households by payment method":           dict(
        w=12, h=6, viz=bar("pay method", "households")),
    "Completeness — share of expected half-hours held":
                                              dict(w=24, h=5, viz=bar("% complete", "households")),
    # Consumption and seasonality
    "Mean daily kWh by month — the season flip":
                                              dict(w=12, h=6, viz=line("month", "mean kWh/day")),
    "Average load shape — winter vs summer":  dict(w=12, h=6, viz=line("hour of day", "winter (Jun-Aug)", "summer (Dec-Feb)")),
    "Daily consumption distribution":         dict(w=12, h=6, viz=bar("kWh/day (bucket)", "household-days")),
    "Solar export by month":                  dict(w=12, h=6, viz=line("month", "mean export kWh/day", "mean import kWh/day", split=True)),
    "The four daylight-saving days":          dict(w=10, h=5),
    "Heaviest households":                    dict(w=14, h=7),
    # Billing and bill shock
    "Average bill and effective rate by plan":dict(w=10, h=5),
    "Bill size distribution":                 dict(w=14, h=5, viz=bar("bill ($, bucket)", "invoices")),
    "BILL SHOCK — biggest month-on-month increases":
                                              dict(w=24, h=10),
    "Increases explained by a longer billing period":
                                              dict(w=12, h=8),
    "Solar export credits applied":
                                              dict(w=12, h=8),
    "Billed volume and revenue by month":     dict(w=24, h=6, viz=line("month", "billed $", "import kWh", split=True)),
    # Data quality
    "Injected defects by kind":               dict(w=12, h=6, viz=bar("defect", "rows")),
    "Read status":                            dict(w=12, h=6, viz=bar("status", "reads")),
    "Incomplete household-days":              dict(w=12, h=7),
    "Stuck meters — a whole day of zeros":    dict(w=12, h=7),
    # Revisions and the extract watermark
    "Rows by version":                        dict(w=6,  h=5, viz=bar("version", "rows")),
    "Arrival lag — reading date to delivery": dict(w=12, h=5, viz=bar("days late", "reads")),
    "The late correction Stage 3 must find":  dict(w=6,  h=5),
    # One household
    "Daily use — one household":              dict(w=24, h=6, viz=line("NZ date", "import kWh", "export kWh")),
    "Their bills":                            dict(w=12, h=7),
    "Their most recent bill, line by line":   dict(w=12, h=7),
    "Their load shape, busiest week":         dict(w=24, h=6, viz=line("NZ time", "kWh")),
    "Their plan, as the catalogue holds it":  dict(w=24, h=8),
}

INTRO = (
    "### Stage 2 — the generated source systems\n\n"
    "The default compact profile contains **50 households** derived from the Low "
    "Carbon London trial, shifted onto the New Zealand calendar from 2025-08-01 "
    "to 2027-08-27. All generated households are Standard User customers on "
    "Vector; the choice being demonstrated is the three Sprint 3 plan families. "
    "The first card reports the actual size, so this dashboard "
    "also remains correct when the optional 500-household profile is loaded.\n\n"
    "Every panel deliberately reads `src_metering`, `src_billing` and "
    "`src_catalogue` directly, or the refreshed rollups in `gen_audit`; it does "
    "not use the warehouse copy. "
    "The **ICP filter** above drives the last section only.\n\n"
    "Queries are version-controlled in `analysis/metabase/build_source_dashboard.py`; "
    "rebuild with `make source-dashboard`."
)


SECTIONS = [
    ("The cohort", COHORT),
    ("Consumption and seasonality", USAGE),
    ("Billing and bill shock", BILLING),
    ("Data quality — what was injected, and whether you can see it", QUALITY),
    ("Revisions and the extract watermark", REVISIONS),
    ("One household — set the ICP filter above", HOUSEHOLD),
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
        sys.exit(f"database '{DB_NAME}' is not connected — run `make metabase-setup` first")

    cols = api("/api/collection", session=s)
    parent = next((c["id"] for c in cols if c.get("name") == COLLECTION), None)
    if parent is None:
        parent = api("/api/collection", {"name": COLLECTION, "color": "#509EE3"},
                     session=s)["id"]
    coll = next((c["id"] for c in cols
                 if c.get("name") == SUBCOLLECTION and c.get("location", "").strip("/").endswith(str(parent))),
                None)
    if coll is None:
        coll = api("/api/collection",
                   {"name": SUBCOLLECTION, "color": "#88BF4D", "parent_id": parent,
                    "description": "Built by analysis/metabase/build_source_dashboard.py"},
                   session=s)["id"]
        print(f"created collection '{SUBCOLLECTION}'")

    existing = {c["name"]: c["id"] for c in
                api(f"/api/collection/{coll}/items?models=card", session=s).get("data", [])}

    def upsert(name, display, sql):
        uses_filter = "{{icp}}" in sql
        payload = {
            "name": name, "display": display,
            "visualization_settings": LAYOUT.get(name, {}).get("viz", {}),
            "collection_id": coll,
            "dataset_query": {"type": "native", "database": db,
                              "native": {"query": sql.strip(),
                                         "template-tags": TAG if uses_filter else {}}},
        }
        if name in existing:
            api(f"/api/card/{existing[name]}", payload, session=s, method="PUT")
            return existing[name], "updated", uses_filter
        return api("/api/card", payload, session=s)["id"], "created", uses_filter

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
    dash_id = dash["id"] if dash else api(
        "/api/dashboard", {"name": DASHBOARD, "collection_id": coll}, session=s)["id"]

    param = {"id": "icpS2001", "name": "ICP", "slug": "icp",
             "type": "string/=", "sectionId": "string", "default": [DEFAULT_ICP]}

    # Shelf packing. Cards in a row differ in height, so the next row
    # starts below the TALLEST card in the current one -- a fixed stride
    # either overlaps them or leaves a band of dead space.
    dashcards, neg = [], -1
    row = col = band = 0

    def place(**kw):
        nonlocal neg
        dashcards.append({"id": neg, "parameter_mappings": [], **kw})
        neg -= 1

    place(card_id=None, row=0, col=0, size_x=24, size_y=4,
          visualization_settings={"virtual_card": {"display": "text"}, "text": INTRO})
    row = 4

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
              # Only cards that declare the tag get the mapping; mapping a
              # filter onto a card without it makes Metabase reject the save.
              parameter_mappings=([{"parameter_id": param["id"], "card_id": cid,
                                    "target": ["variable", ["template-tag", "icp"]]}]
                                  if uses else []))
        col += width
        band = max(band, height)
        if col >= 24:
            row += band
            col = band = 0

    api(f"/api/dashboard/{dash_id}", {"parameters": [param], "dashcards": dashcards},
        session=s, method="PUT")
    print(f"dashboard: {len(dashcards)} cards -> {MB}/dashboard/{dash_id}")

    # Renaming a panel would otherwise leave the old question behind,
    # unreferenced and indistinguishable from a live one. Safe because
    # this sub-collection holds nothing but what this file builds.
    built = {name for _, panels in SECTIONS for name, _, _, _ in panels}
    stale = [(c["id"], c["name"]) for c in
             api(f"/api/collection/{coll}/items?models=card", session=s).get("data", [])
             if c["name"] not in built]
    for cid, cname in stale:
        api(f"/api/card/{cid}", {"archived": True}, session=s, method="PUT")
        print(f"  archived stale question: {cname}")


if __name__ == "__main__":
    main()
