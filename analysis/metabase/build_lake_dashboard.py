#!/usr/bin/env python3
"""Build the Stage 3 lake dashboard in Metabase.

    make dashboards ONLY=lake  # refreshes the rollups, then runs this

WHAT THIS DASHBOARD IS FOR, and what it deliberately is not. The Stage 2
dashboard already plots the metering data itself from `src_metering` — kWh,
seasons, plan mix. Re-plotting those from the lake would be a second view of
the same numbers and a second thing to keep in agreement.

What Stage 3 newly makes visible is the PIPELINE'S OWN BEHAVIOUR: what landed,
when it arrived, how late a correction can be, and whether parse lost anything
on the way through. None of that is answerable from the source tables, because
none of it exists until something extracts them.

Metabase talks JDBC and the lake is Parquet on MinIO, so every panel here reads
the `lake_audit` rollups written by summarise_lake.py — not the lake directly.
That schema is analysis, not pipeline output; Stage 5 is what lands the real
thing in the serving store.

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
DB_NAME = "Kea pipeline"
COLLECTION = "Kea Energy"
SUBCOLLECTION = "Lake — bronze and parsed"
DASHBOARD = "Lake — extract, bronze, parsed"

#: What these were called before, when the dashboards were named for the
#: build stage rather than the step. Found by name, so an instance built
#: under the old name is adopted and renamed in place -- see
#: build_dashboard.py's SUBCOLLECTION_WAS. Append, never replace.
SUBCOLLECTION_WAS = ("Stage 3 — the lake",)
DASHBOARD_WAS = ("Stage 3 — extract, bronze, parsed",)
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

INTRO = """### Stage 3 — what the pipeline saw, and what it did with it

Every panel reads `lake_audit`, rebuilt whole from the Parquet in
`s3://kea-lake/` by `analysis/metabase/summarise_lake.py`. Metabase talks JDBC
and the lake does not, so these are rollups rather than the lake itself.

**The metering data is not here.** It is on the Sources dashboard, read from
`src_metering`. This one is about the *pipeline*: arrival lag, corrections,
partitions, and whether parse conserved what extract landed.

**Weather is the exception, and deliberately so.** It arrived after the Stage 2
dashboard was built, so nothing anywhere plots it. A series that peaks in July
or sits one day off would be invisible until it moved a bill explanation.

The panel that matters most is **How late a reading arrives**. Everything
beyond two days is a correction, and the tail past 21 days is what an extract
watermarked on reading time would silently lose — design §4.1."""


# `series` is Metabase's SECOND dimension, not a third metric. One measure
# broken out into a line per region is a different shape from several measures
# sharing an axis, and passing climate_region as a metric silently plots
# nothing -- it is a string.
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


# Axis assignments are declared, never left to Metabase. Without them it
# guesses which column is the dimension and guesses wrong on every panel with
# more than one measure -- originals against corrections, rows against lag.
LAYOUT = {
    "Rows landed per load date": {"viz": line("load_date", "rows"), "w": 12, "h": 6},
    "How late a reading arrives": {"viz": bar("lag_bucket", "rows"), "w": 12, "h": 7},
    "Late arrivals are corrections": {"viz": bar("lag_bucket", "corrections", "originals",
                                                 stack="stacked"), "w": 12, "h": 7},
    "Corrections per load date": {"viz": line("load_date", "originals", "corrections",
                                              split=True), "w": 12, "h": 6},
    "What an interval_start watermark would lose": {"viz": bar("lag_bucket", "rows"),
                                                    "w": 12, "h": 6},
    "Read quality per load date": {"viz": bar("load_date", "actual", "estimated", "final",
                                              stack="stacked"), "w": 12, "h": 6},
    "Households seen per load date": {"viz": line("load_date", "icps"), "w": 12, "h": 6},
    "Worst lag per load date": {"viz": line("load_date", "max_lag_days", "mean_lag_days"),
                                "w": 12, "h": 6},
    # Four lines over roughly two years, so it needs the full width and the extra height
    # -- a squashed seasonal curve is exactly the chart that hides a one-day
    # shift, which is the thing this panel is here to catch.
    "Mean temperature by region, weekly": {"viz": line("week", "temp_mean_c",
                                                      series="climate_region"),
                                          "w": 18, "h": 8},
    "Heating degree days per month": {"viz": bar("month", "hdd", series="climate_region"),
                                      "w": 12, "h": 7},
}

SECTIONS = [
    ("The lake at a glance", [
        ("Lake totals", "scalar", 6, """
-- One row, read as four numbers. `layer='parsed'` on purpose: raw and parsed
-- hold the same rows, so summing both would double every count.
SELECT sum(row_count)                        AS "rows in parsed",
       count(*)                              AS "partitions",
       count(DISTINCT entity)                AS "entities",
       count(DISTINCT source_system)         AS "source systems"
FROM   lake_audit.partitions WHERE layer = 'parsed'
"""),
        ("Parse conserved every row", "table", 18, """
-- Design §5: parse RESHAPES, so every row extracted comes out somewhere. A row
-- that is neither parsed nor quarantined has been silently dropped, and that
-- looks like success from every angle. Any non-zero "unaccounted for" is a bug.
--
-- JOINED ON source_system AS WELL AS entity. The fourteen entity names happen
-- to be unique across the four systems today, so entity alone gives the right
-- answer -- but it is right by luck, and the first duplicate name would
-- cross-multiply two sources into a row count that still looks plausible.
SELECT r.source_system, r.entity,
       sum(r.row_count)                                     AS "extracted",
       sum(p.row_count)                                     AS "parsed",
       sum(p.quarantined_rows)                              AS "quarantined",
       sum(r.row_count) - sum(p.row_count)
         - sum(p.quarantined_rows)                          AS "unaccounted for"
FROM       lake_audit.partitions r
INNER JOIN lake_audit.partitions p
        ON p.source_system = r.source_system AND p.entity = r.entity
       AND p.load_date = r.load_date AND p.layer = 'parsed'
WHERE  r.layer = 'raw'
GROUP  BY r.source_system, r.entity ORDER BY 3 DESC
"""),
    ]),
    ("How late data arrives — the reason the watermark is `received_at`", [
        ("How late a reading arrives", "bar", 12, """
-- Elapsed days, not a calendar difference: datediff() truncates, and the two
-- disagree by 9 rows on a single load date. The shape is the point -- a mean
-- would be meaningless against a distribution this skewed.
SELECT lag_bucket AS "lag_bucket", rows AS "rows"
FROM   lake_audit.arrival_lag ORDER BY min_lag_days
"""),
        ("Late arrivals are corrections", "bar", 12, """
-- The claim design §4.1 rests on, as data. Nothing arriving more than a week
-- late is a first delivery -- it is a revision of a reading already extracted.
SELECT lag_bucket             AS "lag_bucket",
       corrections            AS "corrections",
       rows - corrections     AS "originals"
FROM   lake_audit.arrival_lag ORDER BY min_lag_days
"""),
        ("What an interval_start watermark would lose", "bar", 12, """
-- Every one of these rows corrects a reading whose interval_start is older
-- than any watermark an extract would hold, so an extract keyed on reading
-- time never sees them: the customer's bill is then explained from superseded
-- data, and nothing anywhere reports an error.
SELECT lag_bucket AS "lag_bucket", rows AS "rows"
FROM   lake_audit.arrival_lag
WHERE  min_lag_days >= 8 ORDER BY min_lag_days
"""),
        ("Worst lag per load date", "line", 12, """
-- The 36-day-late revision Stage 3 was built to catch is one point on this
-- line, not an outlier: corrections routinely arrive three to six weeks out.
SELECT load_date AS "load_date", max_lag_days AS "max_lag_days",
       mean_lag_days AS "mean_lag_days"
FROM   lake_audit.reads_daily ORDER BY load_date
"""),
    ]),
    ("What each nightly run contained", [
        ("Rows landed per load date", "line", 12, """
-- In the compact profile these dates come from one range extract, but remain
-- grouped by each row's received_at date. A low day normally carries late
-- corrections for a subset rather than a disappearing household cohort.
SELECT load_date AS "load_date", rows AS "rows"
FROM   lake_audit.reads_daily ORDER BY load_date
"""),
        ("Corrections per load date", "line", 12, """
SELECT load_date AS "load_date", original AS "originals", corrections AS "corrections"
FROM   lake_audit.reads_daily ORDER BY load_date
"""),
        ("Read quality per load date", "bar", 12, """
-- 'final' is the interesting one: it is the third CHECK value the source
-- allows, it appears when an account closes, and common.read_quality in the
-- serving schema has no such symbol. SETTLED IN STAGE 5: the export maps
-- final -> validated rather than widening the enum, because both mean
-- "confirmed, not an estimate" and only the source's spelling is lost.
-- The mapping is currently unexercised -- the 1,010 'F' rows sit in bronze
-- partitions that were sealed before the generator wrote them.
SELECT load_date AS "load_date", actual AS "actual",
       estimated AS "estimated", final AS "final"
FROM   lake_audit.reads_daily ORDER BY load_date
"""),
        ("Households seen per load date", "line", 12, """
-- Compare with the source dashboard's current cohort count rather than a
-- hard-coded scale. Fewer normally means this date carried corrections for a
-- subset, not that households disappeared.
SELECT load_date AS "load_date", icps AS "icps"
FROM   lake_audit.reads_daily ORDER BY load_date
"""),
    ]),
    ("Weather — the fourth source system, and the only plot of it anywhere", [
        ("Mean temperature by region, weekly", "line", 18, """
-- THE SANITY CHECK THAT NOTHING ELSE PERFORMS. No other dashboard plots
-- temperature: the Sources dashboard predates the source entirely. A
-- wrong-hemisphere series peaks in July instead of January, and nothing else
-- in this project would notice until it moved a bill explanation.
--
-- WEEKLY, NOT DAILY, AND NOT BY PREFERENCE. Metabase caps a card at 2,000
-- rows and the daily series is 2,791, so a daily version returned exactly
-- 2,000 and stopped mid-2026 -- a chart that looks complete and is not, which
-- is the failure this whole panel exists to catch. ~100 weeks x 4 regions has
-- room to spare as the span grows.
--
-- What this CANNOT show is the one-day shift from reading NIWA's 9am UTC
-- stamp as a UTC date: at any resolution the curve looks right. That one is
-- settled in load_niwa_weather.py against DataHub's own labelling at both ends of
-- the Auckland series, not by eye.
--
-- Read from the parsed lake, not src_weather, so it is the pipeline's copy
-- being checked rather than the loader's.
SELECT date_trunc('week', obs_date)::date        AS week,
       climate_region,
       round(avg(temp_mean_c)::numeric, 1)       AS temp_mean_c
FROM   lake_audit.weather_daily
WHERE  temp_mean_c IS NOT NULL
GROUP  BY 1, 2 ORDER BY 1
"""),
        ("Heating degree days per month", "bar", 12, """
-- GREATEST(16 - mean, 0), design §8.4, summed by month. This is the driver
-- behind mart_daily_usage.heating_degree_days and every weather figure the
-- agent quotes, so the winter peak is the number a bill explanation stands on.
--
-- Christchurch should sit highest and Auckland lowest. If they cross, the
-- station-to-region mapping is wrong.
SELECT date_trunc('month', obs_date)::date AS month,
       climate_region,
       round(sum(hdd16))                   AS hdd
FROM   lake_audit.weather_daily
GROUP  BY 1, 2 ORDER BY 1
"""),
        ("Days with no temperature", "table", 12, """
-- The gaps the fill rule exists for. A null mean is a day that station cannot
-- explain; the dictionary's rule is to fill from the nearest of the other
-- three and mark the row, and to leave heating_degree_days null only when NO
-- station covers the day.
--
-- "covered elsewhere" is what makes nearest-station fill sufficient. If it
-- ever stops equalling "missing days", an inner day is uncovered and the rule
-- has to change rather than merely widen.
WITH gap AS (
  SELECT climate_region, obs_date
  FROM   lake_audit.weather_daily WHERE temp_mean_c IS NULL
), covered AS (
  SELECT obs_date FROM lake_audit.weather_daily
  WHERE  temp_mean_c IS NOT NULL GROUP BY obs_date
)
SELECT g.climate_region,
       count(*)                                              AS "missing days",
       count(c.obs_date)                                     AS "covered elsewhere",
       count(*) - count(c.obs_date)                          AS "covered nowhere",
       min(g.obs_date)                                       AS "first",
       max(g.obs_date)                                       AS "last"
FROM   gap g LEFT JOIN covered c ON c.obs_date = g.obs_date
GROUP  BY g.climate_region ORDER BY 2 DESC
"""),
        ("Stations, and what each one covers", "table", 12, """
-- Four stations, one per climate_region carrying a meaningful share of the
-- cohort. A station is NOT a region -- Kelburn is 140 m up and windier than
-- most of Wellington -- and that assumption is recorded in the dictionary
-- rather than presented as measurement.
SELECT climate_region, station_name,
       count(*)                                  AS "days",
       count(temp_mean_c)                        AS "with a mean",
       round(avg(temp_mean_c)::numeric, 1)       AS "mean °C",
       round(min(temp_min_c)::numeric, 1)        AS "coldest",
       round(max(temp_max_c)::numeric, 1)        AS "warmest"
FROM   lake_audit.weather_daily
GROUP  BY 1, 2 ORDER BY 5
"""),
    ]),
    ("Provenance — what landed, and whether it moved", [
        ("Partitions by entity", "table", 12, """
-- One row per entity in the lake. `interval_reads` is the only incremental metering one and
-- the only one Spark reads; `invoices` is incremental on a naive ISSUE_DT, so
-- its window is NZ wall clock rather than an instant. The rest are snapshots.
--
-- The watermark is taken with max() rather than grouped on: only the EXTRACT
-- manifest records one, so grouping by it splits every entity in two -- one
-- row for the extract and a phantom one for the parse. It did, until this
-- panel returned one row more than there were entities.
SELECT entity,
       max(mode) FILTER (WHERE layer = 'raw')                   AS "mode",
       coalesce(max(watermark_column) FILTER (WHERE layer = 'raw'), '—') AS "watermark",
       count(*) FILTER (WHERE layer = 'parsed')                 AS "partitions",
       sum(row_count) FILTER (WHERE layer = 'parsed')           AS "rows"
FROM   lake_audit.partitions
GROUP  BY entity ORDER BY 5 DESC NULLS LAST
"""),
        ("Sealed partitions and their hashes", "table", 12, """
-- The content hash is what "re-running a load date changes nothing" MEANS: an
-- order-independent, duplicate-preserving digest over every column but
-- _extracted_at. A hash that moves without the source moving is a bug.
SELECT load_date, layer, row_count AS "rows",
       left(content_hash, 18) || '…' AS "content hash",
       extracted_at AS "extracted"
FROM   lake_audit.partitions
WHERE  entity = 'interval_reads'
ORDER  BY load_date DESC, layer LIMIT 40
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
        # Adopt a former name, renamed in place: the collection keeps its id,
        # so every card and link inside it survives, where creating a new one
        # would strand them beside it.
        previous = next((c for c in cols if c.get("name") in SUBCOLLECTION_WAS
                         and c.get("location", "").strip("/").endswith(str(parent))), None)
        if previous:
            api(f"/api/collection/{previous['id']}",
                {"name": SUBCOLLECTION, "description": "Built by analysis/metabase/build_lake_dashboard.py"},
                session=s, method="PUT")
            coll = previous["id"]
            print(f"renamed collection '{previous['name']}' -> '{SUBCOLLECTION}'")
    if coll is None:
        coll = api("/api/collection",
                   {"name": SUBCOLLECTION, "color": "#A989C5", "parent_id": parent,
                    "description": "Built by analysis/metabase/build_lake_dashboard.py"},
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
    if dash is None:
        previous = next((d for d in api(f"/api/collection/{coll}/items?models=dashboard",
                                        session=s).get("data", [])
                         if d["name"] in DASHBOARD_WAS), None)
        if previous:
            api(f"/api/dashboard/{previous['id']}", {"name": DASHBOARD},
                session=s, method="PUT")
            dash = previous
            print(f"renamed dashboard '{previous['name']}' -> '{DASHBOARD}'")
    dash_id = dash["id"] if dash else api(
        "/api/dashboard", {"name": DASHBOARD, "collection_id": coll}, session=s)["id"]

    # Shelf packing: cards in a row differ in height, so the next row starts
    # below the TALLEST in the current one. A fixed stride either overlaps
    # them or leaves a band of dead space.
    dashcards, neg = [], -1
    row = col = band = 0

    def place(**kw):
        nonlocal neg
        dashcards.append({"id": neg, "parameter_mappings": [], **kw})
        neg -= 1

    place(card_id=None, row=0, col=0, size_x=24, size_y=5,
          visualization_settings={"virtual_card": {"display": "text"}, "text": INTRO})
    row = 5

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


if __name__ == "__main__":
    main()
