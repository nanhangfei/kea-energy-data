#!/usr/bin/env python3
"""Build the Stage 5 serving-store dashboard in Metabase.

    make stage5-dashboard

WHAT IS NEW HERE, AND WHY IT COULD NOT EXIST BEFORE.

Everything Stage 4 built lives in Snowflake, which Metabase is not connected
to, so for the whole of Stage 4 the warehouse was invisible to BI: `dbt build`
could go green and no panel anywhere would move. Stage 5 is the hop that
changes that. The served tables live in the backend's database, and this
dashboard reads them there as `kea_app` -- the application's own read-only
role -- so every row it shows is pipeline output, as the product sees it.

ONE CARD READS ELSEWHERE. The four source systems stay in the pipeline's own
Postgres (`kea-source-db`, Metabase's "Kea pipeline"), a different database,
so "what crossed" cannot be one query. It is two cards side by side, rows in
the same order: what the sources hold, and what was served.

So this dashboard answers two questions no earlier one could:

  * **did the load work, and how fresh is the replica** -- `mart_load_log` and
    `synced_at` exist precisely so that is answerable without inspecting the
    data, and until Stage 5 nothing had ever written either
  * **what did NOT cross, and why** -- the export drops rows on purpose, and a
    silent drop is indistinguishable from a source that never had them

It also carries the first BI view of the marts themselves. That is Stage 4's
content, not Stage 5's, but it arrives here because this is the hop that made
it reachable -- and a load that "succeeded" while landing unusable data would
look identical in the log.

**Not here, deliberately:** the source metering data (Stage 2's dashboard, from
`src_metering`) and the lake's own behaviour (Stage 3's, from `lake_audit`).
Re-plotting either would be a second view of the same numbers and a second
thing to keep in agreement.

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
SERVING_DB = "Kea serving"      # the backend's database, as kea_app
PIPELINE_DB = "Kea pipeline"    # kea-source-db: the four source systems
# Every card reads SERVING_DB except these.
READS_PIPELINE = {"What the sources hold"}
COLLECTION = "Kea Energy"
SUBCOLLECTION = "Stage 5 — the serving store"
DASHBOARD = "Stage 5 — export, load, and what crossed"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

INTRO = """### Stage 5 — the warehouse and the serving store finally meet

Every panel but one reads the served tables in the **backend's database**, as
the application's read-only role, and **every row in them is pipeline
output**. Until this stage the serving schema was filled by hand from
`sample/`, and everything the warehouse built lived in Snowflake where Metabase
could not see it. The exception, *What the sources hold*, counts the source
systems in the pipeline's own Postgres.

Two things are newly answerable. **Did the load work, and how fresh is this
replica** — `kea.mart_load_log` and `synced_at` exist for exactly that, and
nothing had ever written either. And **what did not cross**: the export drops
rows deliberately, and a silent drop looks identical to a source that never had
them.

⚠️ **`COPY INTO` cannot work and the design said `COPY INTO`.** Snowflake is
SaaS in ap-southeast-2, MinIO is local-only, so the export **pulls** —
`export_warehouse_to_lake.py` runs the query and writes the Parquet itself.

The panel that matters most is **What the export did not carry**. Everything in
it is a deliberate exclusion with a named cause; a number moving there without
the source moving is the bug this dashboard exists to surface."""


def bar(dim, *metrics, stack=None, series=None):
    v = {"graph.dimensions": [dim] + ([series] if series else []),
         "graph.metrics": list(metrics)}
    if stack:
        v["stackable.stack_type"] = stack
    return v


def line(dim, *metrics, split=False, series=None):
    v = {"graph.dimensions": [dim] + ([series] if series else []),
         "graph.metrics": list(metrics)}
    if split:
        v["graph.y_axis.auto_split"] = True
    return v


# Axis assignments are declared, never guessed. verify.py enforces the harder
# half of this: every column a query returns must be a dimension or a metric,
# because a column Metabase does not know about is silently dropped from the
# chart and the result still renders.
LAYOUT = {
    "Rows per monthly partition": {"viz": bar("month", "rows"), "w": 24, "h": 7},
    "Usage against heating degree days, weekly":
        {"viz": line("week", "avg import kWh", "avg HDD", split=True), "w": 24, "h": 7},
    "Where a bill's change comes from": {"viz": bar("driver", "total $"), "w": 12, "h": 6},
    "The average day, weekday vs weekend":
        {"viz": line("hour", "avg import kWh", series="day type"), "w": 12, "h": 6},
}

SECTIONS = [
    ("Did the load work", [
        ("Serving store totals", "scalar", 8, """
-- One row, read as four numbers. Counted from the tables themselves rather
-- than from mart_load_log: the log records what the loader BELIEVES it did,
-- and a dashboard that reads only the log cannot tell a successful load from
-- a successful-looking one.
SELECT (SELECT count(*) FROM metering.meter_read_hh)                AS "half-hourly reads",
       (SELECT count(*) FROM metering.icp)                          AS "households",
       (SELECT count(*) FROM billing.bill)                          AS "bills",
       (SELECT max(synced_at)::timestamp(0) FROM billing.account)   AS "replica as at"
"""),
        ("Every mart's last load", "table", 16, """
-- kea.mart_load_log, which nothing had ever written before Stage 5. One row
-- per mart per run; this shows the most recent of each.
--
-- `status` is the point. A mart that failed still has its PREVIOUS rows
-- sitting in the table looking perfectly healthy, so "is this data good" is
-- not answerable by querying the data -- only by asking what the last load did.
--
-- `sample_*` rows are filtered out: Stage 1's fixtures logging their own
-- hand-seeding, not pipeline loads. Only a database the fixtures' served half
-- was once seeded into holds them, and nothing truncates this table --
-- deliberately, because a load log that a load can erase is not a log.
SELECT DISTINCT ON (mart_name)
       mart_name                                       AS "mart",
       status                                          AS "status",
       row_count                                       AS "rows",
       round(extract(epoch FROM completed_at - started_at)::numeric, 1) AS "seconds",
       completed_at::timestamp(0)                      AS "completed",
       coalesce(left(error_message, 60), '—')          AS "error"
FROM   kea.mart_load_log
WHERE  mart_name NOT LIKE 'sample!_%' ESCAPE '!'
ORDER  BY mart_name, load_id DESC
"""),
        ("Replica freshness, table by table", "table", 12, """
-- synced_at is set ONCE PER TABLE at commit time, not DEFAULT now() per row.
-- A slow load would otherwise spread timestamps across a single table and
-- "how fresh is this replica" would stop having an answer -- so `distinct
-- stamps` must be 1 on every row here. Anything else means the loader
-- regressed to per-row defaults.
SELECT 'metering.network'           AS "table", count(*) AS "rows",
       count(DISTINCT synced_at)    AS "distinct stamps",
       max(synced_at)::timestamp(0) AS "synced" FROM metering.network
UNION ALL SELECT 'metering.icp',      count(*), count(DISTINCT synced_at), max(synced_at)::timestamp(0) FROM metering.icp
UNION ALL SELECT 'metering.meter_register', count(*), count(DISTINCT synced_at), max(synced_at)::timestamp(0) FROM metering.meter_register
UNION ALL SELECT 'catalogue.plan',    count(*), count(DISTINCT synced_at), max(synced_at)::timestamp(0) FROM catalogue.plan
UNION ALL SELECT 'catalogue.tou_band', count(*), count(DISTINCT synced_at), max(synced_at)::timestamp(0) FROM catalogue.tou_band
UNION ALL SELECT 'catalogue.tariff_component', count(*), count(DISTINCT synced_at), max(synced_at)::timestamp(0) FROM catalogue.tariff_component
UNION ALL SELECT 'billing.customer', count(*), count(DISTINCT synced_at), max(synced_at)::timestamp(0) FROM billing.customer
UNION ALL SELECT 'billing.account',  count(*), count(DISTINCT synced_at), max(synced_at)::timestamp(0) FROM billing.account
UNION ALL SELECT 'billing.bill',     count(*), count(DISTINCT synced_at), max(synced_at)::timestamp(0) FROM billing.bill
UNION ALL SELECT 'billing.bill_line', count(*), count(DISTINCT synced_at), max(synced_at)::timestamp(0) FROM billing.bill_line
ORDER  BY 2 DESC
"""),
    ]),
    ("What crossed, and what did not", [
        ("What the sources hold", "table", 8, """
-- THE MEASUREMENT ONLY WORKS FROM OUTSIDE. An earlier version of this panel
-- counted serving's own gaps -- "bills whose period no plan version covers" --
-- and returned 0 for a reason that proves nothing: the export already excluded
-- those, so serving cannot contain any. It was a tautology wearing a check's
-- clothing.
--
-- So count what the source holds, count what landed, and name the difference.
-- The two are in different databases -- the sources here in the pipeline's
-- Postgres, the served tables in the backend's -- so this card is one half and
-- the card beside it the other, rows in the same order. A gap is only correct
-- if its cause is stated there.
SELECT '1 ICPs'                   AS "what", 'src_metering.connection_points' AS "source table",
       (SELECT count(*) FROM src_metering.connection_points)                  AS "rows"
UNION ALL SELECT '2 accounts',          'src_billing.accounts',        (SELECT count(*) FROM src_billing.accounts)
UNION ALL SELECT '3 bills',             'src_billing.invoices',        (SELECT count(*) FROM src_billing.invoices)
UNION ALL SELECT '4 half-hourly reads', 'src_metering.interval_reads', (SELECT count(*) FROM src_metering.interval_reads)
UNION ALL SELECT '5 plan versions',     'src_catalogue.price_plans',   (SELECT count(*) FROM src_catalogue.price_plans)
ORDER  BY 1
"""),
        ("What was served, and why it differs", "table", 16, """
-- The other half of the card beside it, from the backend's database: the same
-- rows in the same order, so the difference reads straight across.
SELECT '1 ICPs'                   AS "what", 'metering.icp' AS "served table",
       (SELECT count(*) FROM metering.icp)                  AS "rows",
       'every one crosses; the ICP is the spine of the model' AS "why it differs"
UNION ALL SELECT '2 accounts', 'billing.account', (SELECT count(*) FROM billing.account),
       'unrepresentable source accounts are quarantined upstream; a resolvable ambiguity is served with its reason'
UNION ALL SELECT '3 bills', 'billing.bill', (SELECT count(*) FROM billing.bill),
       'dropped: no plan version covers the period (2.1.2), or the account itself did not cross'
UNION ALL SELECT '4 half-hourly reads', 'metering.meter_read_hh', (SELECT count(*) FROM metering.meter_read_hh),
       'superseded revisions collapse to one row (4.1), then the 13-month retention window applies'
UNION ALL SELECT '5 plan versions', 'catalogue.plan', (SELECT count(*) FROM catalogue.plan),
       'one row per plan VERSION, not per marketing plan code'
ORDER  BY 1
"""),
        ("Bill lines that found their tariff component", "scalar", 8, """
-- Gold records what was charged but not which component priced it, so the
-- export recovers component_id by matching plan version, charge group,
-- direction, basis, rate AND band. `unmatched` is expected to be small and is
-- currently zero; a line that matches nothing keeps a NULL rather than being
-- dropped or guessed.
--
-- The other number is the one that would fail loudly: the match is a LEFT JOIN
-- with no tie-break, so if two components ever matched one line the row count
-- would GROW. It did, by 3,057, until the band was added to the match --
-- which is why `duplicated` is here and not assumed away.
SELECT count(*)                                        AS "bill lines",
       count(*) FILTER (WHERE component_id IS NULL)    AS "unmatched",
       count(*) - count(DISTINCT line_id)              AS "duplicated"
FROM   billing.bill_line
"""),
        ("Quarantine reaches the serving decision boundary", "table", 24, """
-- Design 6.3 says a quarantined household must make the product hedge or
-- decline rather than answer confidently. The ambiguous-register fixture can
-- still be represented as an account, so it now crosses with its reason and
-- the recommendation job excludes it rather than choosing a physical meter.
--
-- Orphan ICPs and unknown plans remain absent from billing.account because
-- serving them would require inventing the missing foreign key. How many is
-- the accounts row of the two cards above, read across them: the sources are
-- in another database, so the difference cannot be computed here.
SELECT 'accounts served'                                            AS "what",
       count(*)::text                                               AS "value",
       'the product-visible account context'                         AS "meaning"
FROM   kea.mart_account_context
UNION ALL
SELECT 'of those, flagged is_quarantined',
       count(*) FILTER (WHERE is_quarantined)::text,
       'visible to the agent and excluded from automated plan advice'
FROM   kea.mart_account_context
UNION ALL
SELECT 'carrying a quarantine_reason',
       count(*) FILTER (WHERE quarantine_reason IS NOT NULL)::text,
       'the machine-readable reason for hedging or declining'
FROM   kea.mart_account_context
"""),
        ("The nine fixture households, reproduced", "table", 24, """
-- THE ACCEPTANCE TARGET, as data. These nine were hand-seeded in Stage 1 as a
-- stand-in for pipeline output; every row below is now the pipeline's own,
-- matched by icp_number because the warehouse mints its own surrogates and the
-- fixtures' hand-assigned ids no longer exist.
--
-- `0165841877LCC54` is the printed invoice. The DEMO ones are generated
-- scenarios -- note their read spans: about two months each, which is why no
-- heating slope fits them and three of their bill explanations come back
-- unexplainable. That is the model declining to name a cause it cannot
-- measure, not a defect.
SELECT i.icp_number                                   AS "ICP",
       count(DISTINCT b.bill_id)                      AS "bills",
       (SELECT count(*) FROM metering.meter_read_hh r
         WHERE r.icp_id = i.icp_id)                   AS "reads",
       (SELECT (max(interval_start AT TIME ZONE 'Pacific/Auckland')::date
              - min(interval_start AT TIME ZONE 'Pacific/Auckland')::date) + 1
          FROM metering.meter_read_hh r WHERE r.icp_id = i.icp_id) AS "days spanned",
       (SELECT count(*) FROM kea.mart_daily_usage d
         WHERE d.icp_id = i.icp_id)                   AS "daily rows",
       round(sum(b.total_charges), 2)                 AS "billed $"
FROM   metering.icp i
LEFT   JOIN billing.account a ON a.icp_id = i.icp_id
LEFT   JOIN billing.bill    b ON b.account_id = a.account_id
WHERE  i.icp_number NOT LIKE '09%'
GROUP  BY i.icp_number, i.icp_id
ORDER  BY 1
"""),
    ]),
    ("meter_read_hh — the partition swap", [
        ("The partition set", "scalar", 8, """
-- Design 7.3 builds each month beside the table and swaps it in. The DDL
-- declared three example partitions and said "generate the full set from the
-- loader"; the loader now does, one per month present in the data.
--
-- `rows in default` MUST BE ZERO. The default partition is the catch-all, and
-- anything landing there is a reading whose month the loader failed to create
-- -- which is how 48 rows once vanished between a 9,732,560-row export and a
-- 9,732,512-row table, when the bounds were resolved in UTC instead of NZ.
SELECT (SELECT count(*) FROM pg_inherits
         WHERE inhparent = 'metering.meter_read_hh'::regclass)     AS "partitions",
       (SELECT count(*) FROM metering.meter_read_hh_default)       AS "rows in default",
       (SELECT min(interval_start AT TIME ZONE 'Pacific/Auckland')::date
          FROM metering.meter_read_hh)                             AS "earliest NZ reading",
       (SELECT max(interval_start AT TIME ZONE 'Pacific/Auckland')::date
          FROM metering.meter_read_hh)                             AS "latest NZ reading"
"""),
        ("Rows per monthly partition", "bar", 24, """
-- The two blocks and the gap between them are the retention rule made visible.
-- The export applies a PER-HOUSEHOLD window: 13 months for the generated
-- cohort, and the ENTIRE span for the nine fixture households. A single global
-- window would have excluded the fixtures' 2023-24 reads entirely and taken
-- Stage 5's acceptance target with them.
--
-- So the tall block on the right is the cohort, the low one on the left is
-- nine households, and the empty middle is simply months in which neither has
-- readings. Grouped in NZ, because that is where the month boundary is.
SELECT to_char(date_trunc('month', interval_start AT TIME ZONE 'Pacific/Auckland'),
               'YYYY-MM')            AS "month",
       count(*)                      AS "rows"
FROM   metering.meter_read_hh
GROUP  BY 1 ORDER BY 1
"""),
    ]),
    ("The printed invoice, rebuilt by the pipeline", [
        ("Bill B, line by line", "table", 24, """
-- data/sample/How_to_read_your_invoice_page_2 -- ICP 0165841877LCC54, 08-31
-- August 2024, $50.54. The only numbers in this whole stack that come from
-- paper rather than from a generator.
--
-- Stage 4 reproduced this total inside Snowflake. This is the same eleven
-- lines after a round trip through Parquet and a Postgres load, which is what
-- Stage 5 had to not break. Network 29.22 + Energy 21.10 + Other 0.22 = 50.54.
SELECT l.charge_group                       AS "group",
       l.direction                          AS "direction",
       l.line_label                         AS "line",
       coalesce(l.band_name, '—')           AS "band",
       l.quantity                           AS "quantity",
       l.unit                               AS "unit",
       l.rate                               AS "rate",
       coalesce(l.loss_kwh, 0)              AS "line loss kWh",
       l.amount                             AS "amount"
FROM   billing.bill_line l
JOIN   billing.bill b    ON b.bill_id    = l.bill_id
JOIN   billing.account a ON a.account_id = b.account_id
JOIN   metering.icp i    ON i.icp_id     = a.icp_id
WHERE  i.icp_number = '0165841877LCC54'
  AND  b.period_start = DATE '2024-08-08'
ORDER  BY l.sort_order
"""),
    ]),
    ("The marts, reachable for the first time", [
        ("Usage against heating degree days, weekly", "line", 24, """
-- WEEKLY, NOT DAILY, AND THE REASON IS NOT AESTHETIC. Metabase caps a result
-- at 2,000 rows and says nothing when it truncates; mart_daily_usage is larger
-- than that even in the compact profile. A daily chart would silently plot the first few days of the cohort
-- and look entirely reasonable.
--
-- The two series are the relationship design 8.4 rests on: consumption should
-- rise with heating degree days. On this cohort it does at the seasonal scale
-- and barely at the daily one -- the households are LCL, real London metering
-- date-shifted onto NZ dates, while the weather is real NZ, so there is no
-- mechanism by which the two correlate day to day. Split axes: kWh and HDD do
-- not share a scale.
SELECT to_char(date_trunc('week', usage_date), 'YYYY-MM-DD') AS "week",
       round(avg(import_kwh), 2)                             AS "avg import kWh",
       round(avg(heating_degree_days), 2)                    AS "avg HDD"
FROM   kea.mart_daily_usage
WHERE  heating_degree_days IS NOT NULL
GROUP  BY 1 ORDER BY 1
"""),
        ("Where a bill's change comes from", "bar", 12, """
-- The five measurable drivers, plus what is left. Design 8.5 decomposes a
-- bill-over-bill change into period length, weather, base load and tariff,
-- and §15 adds detected loads after base load; it refuses to attribute the
-- remainder -- `unexplained` is a first-class answer, not a rounding bucket.
--
-- Summed as absolute values: a driver that pushes some bills up and others
-- down would otherwise cancel to nothing and look like it never mattered.
SELECT d.driver AS "driver", round(d.amount, 0) AS "total $"
FROM  (SELECT 'period length' AS driver, sum(abs(coalesce(delta_from_period_length, 0))) AS amount FROM kea.mart_bill_explanation
       UNION ALL SELECT 'weather',   sum(abs(coalesce(delta_from_weather, 0)))   FROM kea.mart_bill_explanation
       UNION ALL SELECT 'base load', sum(abs(coalesce(delta_from_base_load, 0))) FROM kea.mart_bill_explanation
       UNION ALL SELECT 'detected loads', sum(abs(coalesce(delta_from_detected_loads, 0))) FROM kea.mart_bill_explanation
       UNION ALL SELECT 'tariff',    sum(abs(coalesce(delta_from_tariff, 0)))    FROM kea.mart_bill_explanation
       UNION ALL SELECT 'unexplained', sum(abs(coalesce(delta_unexplained, 0)))  FROM kea.mart_bill_explanation) d
ORDER BY 2 DESC
"""),
        ("The average day, weekday vs weekend", "line", 12, """
-- Design 8.7 argues these are two different curves, and mart_hourly_profile
-- doubles its row count to hold that claim.
--
-- ON THE FIXTURES THE TWO CURVES SIT ON TOP OF EACH OTHER: generate_scenarios
-- applies one hourly shape to every day, and the largest weekday/weekend gap
-- in any hour is 0.0001 kWh. The cohort is different -- LCL is real metering,
-- so its day-of-week variation is real. This panel reads the whole serving
-- store, which is now overwhelmingly cohort, so a visible separation here is
-- the column earning its keep.
SELECT hour_of_day                                        AS "hour",
       CASE WHEN is_weekday THEN 'weekday' ELSE 'weekend' END AS "day type",
       round(avg(avg_import_kwh), 4)                      AS "avg import kWh"
FROM   kea.mart_hourly_profile
GROUP  BY 1, 2 ORDER BY 1, 2
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
    ids = {d["name"]: d["id"] for d in dbs.get("data", dbs)}
    missing = [n for n in (SERVING_DB, PIPELINE_DB) if n not in ids]
    if missing:
        sys.exit(f"not connected: {', '.join(missing)} — run `make metabase-setup` first")

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
                    "description": "Built by analysis/metabase/build_stage5_dashboard.py"},
                   session=s)["id"]
        print(f"created collection '{SUBCOLLECTION}'")

    existing = {c["name"]: c["id"] for c in
                api(f"/api/collection/{coll}/items?models=card", session=s).get("data", [])}

    def upsert(name, display, sql):
        payload = {
            "name": name, "display": display,
            "visualization_settings": LAYOUT.get(name, {}).get("viz", {}),
            "collection_id": coll,
            "dataset_query": {"type": "native",
                              "database": ids[PIPELINE_DB if name in READS_PIPELINE else SERVING_DB],
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

    place(card_id=None, row=0, col=0, size_x=24, size_y=6,
          visualization_settings={"virtual_card": {"display": "text"}, "text": INTRO})
    row = 6

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

    # A renamed panel must not leave an apparently current saved question in
    # this code-owned collection.
    built = {name for _, panels in SECTIONS for name, _, _, _ in panels}
    for card in api(f"/api/collection/{coll}/items?models=card", session=s).get("data", []):
        if card["name"] not in built:
            api(f"/api/card/{card['id']}", {"archived": True}, session=s, method="PUT")
            print(f"  archived stale question: {card['name']}")


if __name__ == "__main__":
    main()
