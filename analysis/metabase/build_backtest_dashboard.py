#!/usr/bin/env python3
"""Build the forecast backtest dashboard in Metabase.

    make dashboards ONLY=backtest
    python3 analysis/metabase/build_backtest_dashboard.py --check   # offline, in CI

WHAT IT SHOWS. The offline backtest of the kNN quantile forecaster from Botman
et al. (2025) against the bill forecast's own model, `hdd-linear-v2` refitted as
at each date: the gate that was fixed before the runs, how the two bands behave
through a bill cycle (Stage 2), the reproduction of the paper day-ahead
(Stage 1), and the settings that did not help (Stage 3). Those stage numbers are
the backtest's own, not the pipeline's build stages.

WHERE THE NUMBERS COME FROM. Only the committed results in
analysis/forecast_backtest/results/, read when this page is built. The backtest
writes files, not tables, so nothing in either database holds them, and landing
them somewhere just for Metabase would be a second copy to keep in agreement.
Each card's SQL is therefore a VALUES list generated from those files: it reads
no table at all, runs anywhere, and a re-run of the backtest reaches the page
the next time it is built. A figure that exists only in a report's Markdown --
Stage 1's scores, the gate's rows, the like-for-like pool curve -- is parsed
from that report's own table, and a table that has moved or changed shape stops
the build rather than drawing something else.

NOTHING HERE IS SERVED. The backtest recommends kNN as a fallback for accounts
the model turns down; acting on that is an open product decision (design §12).

**This file is the record.** A Metabase question lives in Metabase's own
application database, not in git; the queries are generated here, from files
that are. Idempotent: questions and the dashboard are matched by name and
updated in place, so the URL stays stable, and questions no longer built are
archived.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

MB = "http://localhost:3002"
DB_NAME = "Kea pipeline"
COLLECTION = "Kea Energy"
SUBCOLLECTION = "Forecast backtest — Botman et al. 2025"
DASHBOARD = "Forecast backtest — kNN quantiles against hdd-linear-v2"

#: Former names, found by name and renamed in place -- see
#: build_dashboard.py's SUBCOLLECTION_WAS. Append, never replace.
SUBCOLLECTION_WAS: tuple[str, ...] = ()
DASHBOARD_WAS: tuple[str, ...] = ()
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
RESULTS = Path(__file__).resolve().parents[1] / "forecast_backtest" / "results"
#: The run this page shows: Stages 1-3 on the 200-household cohort, export sealed
#: 2 Oct 2026 (design/kea-cohort-selection.md). The 12 and 17 Sep runs on 49
#: households stay in results/ beside it. The look-ahead card still reads the
#: 12 Sep metrics.csv: that comparison needs the shipped v2 fit, which no export
#: since Sprint 4 publishes (`--shipped-fit skip`).
CURRENT = "cohort-200-2026-10-02"

#: The report's days-elapsed buckets, in cycle order. The CSVs write them with
#: an en dash, so they are matched as written rather than re-typed.
DAYS = ("1–6", "7–9", "10–14", "15–19", "20+")

INTRO = """### Forecast backtest — can kNN quantiles stand in where the bill forecast can't?

An offline backtest of the **kNN quantile method of Botman et al. (2025)**
against the bill forecast's own model, `hdd-linear-v2`, **refitted as at each
date** so that it never sees the future. The question it answers: could kNN give
a forecast, with an honest range, for the accounts the model turns down today?

**Nothing on this page is served.** Today's bills use `hdd-linear-v3`; using kNN
as a fallback is an open product decision (design §12), not something the
pipeline does.

Every number is read from the committed results in
`analysis/forecast_backtest/results/` when this page is built; no table holds
them. Stages 1-3 ran on the **200-household** cohort, export sealed **2 Oct**;
the look-ahead panel is the earlier 49-household run on the export sealed
**12 Sep**, and figures are not comparable across the two. The stage numbers are the backtest's own. Method,
gate and verdict: `analysis/forecast_backtest/README.md`."""


# --------------------------------------------------------------- the results

def read_csv(name: str) -> list[dict[str, str]]:
    with open(RESULTS / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def one(rows: list[dict[str, str]], **want: str) -> dict[str, str]:
    """The single row matching every column in `want`; anything else stops the build."""
    hits = [r for r in rows if all(r.get(k) == v for k, v in want.items())]
    if len(hits) != 1:
        raise ValueError(f"expected one row for {want}, found {len(hits)}")
    return hits[0]


def md_table(name: str, column: str, after: str | None = None) -> list[dict[str, str]]:
    """The first Markdown table in results/<name> with `column` in its header,
    searched from the heading containing `after`. Cells are stripped of `**`."""
    lines = (RESULTS / name).read_text(encoding="utf-8").splitlines()
    start = 0
    if after is not None:
        start = next((i for i, ln in enumerate(lines) if ln.startswith("#") and after in ln), -1)
        if start < 0:
            raise ValueError(f"{name}: no heading containing {after!r}")

    def cells(ln: str) -> list[str]:
        return [c.strip().replace("**", "") for c in ln.strip().strip("|").split("|")]

    for i in range(start, len(lines) - 1):
        if lines[i].startswith("|") and lines[i + 1].startswith("|---"):
            header = cells(lines[i])
            if column in header:
                rows = []
                for ln in lines[i + 2:]:
                    if not ln.startswith("|"):
                        break
                    rows.append(dict(zip(header, cells(ln))))
                return rows
    raise ValueError(f"{name}: no table with a {column!r} column" + (f" after {after!r}" if after else ""))


def num(text: str) -> float:
    """A figure as the reports print it -- '+0.9%', '1.84 ×', '27,882', '$74.01' -- as a float."""
    m = re.search(r"[-+]?\d[\d,]*\.?\d*", text)
    if m is None:
        raise ValueError(f"no number in {text!r}")
    return float(m.group().replace(",", ""))


def pct(fraction: str) -> float:
    return round(float(fraction) * 100, 1)


# ------------------------------------------------------------ SQL from data

def literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else repr(round(float(v), 4))
    return "'" + str(v).replace("'", "''") + "'"


def values(source: str, columns: list[str], rows: list[list]) -> str:
    """A card's SQL: the rows as a VALUES list, in the order given. `ord` sorts
    them without being selected, so the verifier sees only plotted columns."""
    if not rows:
        raise ValueError(f"{source}: no rows")
    quoted = ", ".join(f'"{c}"' for c in columns)
    body = ",\n       ".join(
        "(" + ", ".join([str(i)] + [literal(v) for v in r]) + ")" for i, r in enumerate(rows, 1))
    return (f"-- From analysis/forecast_backtest/results/{source}, generated by\n"
            f"-- build_backtest_dashboard.py when the page is built. It reads no table.\n"
            f"SELECT {quoted}\nFROM (VALUES {body})\n     AS t(ord, {quoted})\nORDER BY ord")


# ------------------------------------------------------------------- charts

#: One colour per method on every chart: kNN purple, v2 yellow, the paper grey.
#: Left to Metabase, the same method changes colour from one panel to the next.
KNN, V2, PAPER = "#A989C5", "#F9D45C", "#B8BBC3"


def bar(dim, *metrics, series=None, goal=None, goal_label=None, colors=()):
    v = {"graph.dimensions": [dim] + ([series] if series else []), "graph.metrics": list(metrics)}
    if colors:
        v["series_settings"] = {m: {"color": c} for m, c in zip(metrics, colors)}
    if goal is not None:
        v.update({"graph.show_goal": True, "graph.goal_value": goal,
                  "graph.goal_label": goal_label or str(goal)})
    return v


def line(dim, *metrics, goal=None, goal_label=None, colors=()):
    return bar(dim, *metrics, goal=goal, goal_label=goal_label, colors=colors)


# ----------------------------------------------------------------- the page

def sections() -> tuple[list, dict]:
    """(SECTIONS, LAYOUT), built from the committed results. Raises if a source
    is missing or has changed shape, so neither the build nor --check can draw
    the wrong figure quietly."""
    s2 = read_csv(f"{CURRENT}/metrics.csv")
    s2_12sep = read_csv("metrics.csv")
    s3 = read_csv(f"{CURRENT}/stage3_metrics.csv")

    def s2row(population: str, days: str) -> dict[str, str]:
        return one(s2, champion_fit="asat", cycles="held_out", population=population, days_elapsed=days)

    h2h = [s2row("h2h", d) for d in DAYS]
    fallback = [s2row("fallback", d) for d in DAYS]
    h2h_all = s2row("h2h", "all")

    # The gate, as the Stage 3 report states it for each variant on the 2 Oct export.
    base_gate = md_table(f"{CURRENT}/stage3_extras.md", "Criterion", after="The baseline on this export")
    seas_gate = md_table(f"{CURRENT}/stage3_extras.md", "Criterion", after="Seasonal neighbours")
    if [r["Criterion"] for r in base_gate] != [r["Criterion"] for r in seas_gate]:
        raise ValueError("stage3_extras.md: the two gates list different criteria")
    verdict_col = [c for c in base_gate[0] if c not in ("Criterion", "Result")][0]
    limit = num(re.search(r"= ([\d.]+)%", base_gate[1]["Result"]).group(1))

    def criterion(text: str) -> str:      # the APE limit is a number, so say it
        return text.replace("≤ v2's own + 5 points", f"≤ v2's own + 5 points ({limit:g}%)")

    def result(row: dict[str, str]) -> str:
        return f"{row['Result'].split(' against ')[0]} — {row[verdict_col].upper()}"

    stage1_scores = md_table(f"{CURRENT}/stage1_reproduce.md", "Model")
    stage1_gains = md_table(f"{CURRENT}/stage1_reproduce.md", "kNN gain over")
    pool = md_table(f"{CURRENT}/stage3_extras.md", "Whole cohort, same snapshots", after="Library size")
    seasonal = md_table(f"{CURRENT}/stage3_extras.md", "Snapshots served", after="Seasonal neighbours")
    grid = [r for r in s3 if r["population"] == "knn" and r["run"].startswith("k=")]

    COVER = ("kNN P10–P90", "v2 ±15%")
    layout = {
        "How often the band catches the final bill":
            {"viz": bar("days elapsed", *COVER, goal=80, goal_label="80% target", colors=(KNN, V2)),
             "w": 12, "h": 7},
        "kNN's CRPS against v2's":
            {"viz": line("days elapsed", "kNN over v2", goal=1, goal_label="level", colors=(KNN,)),
             "w": 12, "h": 7},
        "The price of honesty: band width":
            {"viz": bar("days elapsed", "kNN P10–P90 (kWh)", "v2 ±15% (kWh)", colors=(KNN, V2)),
             "w": 12, "h": 7},
        "Median error, head to head":
            {"viz": bar("days elapsed", "kNN P50 (%)", "v2 point (%)", colors=(KNN, V2)), "w": 12, "h": 7},
        "Where v2 can't forecast: kNN's median error":
            {"viz": bar("days elapsed", "kNN median APE (%)", goal=limit,
                        goal_label=f"gate limit {limit:g}%", colors=(KNN,)), "w": 12, "h": 7},
        "kNN's gain over the paper's two baselines, in the paper's CRPS form":
            {"viz": bar("baseline", "here (%)", "in the paper (%)", colors=(KNN, PAPER)), "w": 12, "h": 7},
        "Library size, like for like":
            {"viz": bar("library", "CRPS over the whole cohort's", goal=1, goal_label="whole cohort",
                        colors=(KNN,)), "w": 12, "h": 7},
        "The gate, for the paper's setting and the seasonal variant": {"w": 24, "h": 5},
        "Why v2 is refitted as at each date (export sealed 12 Sep)": {"w": 12, "h": 7},
        "Day-ahead scores (absolute CRPS is not comparable with the paper's)": {"w": 12, "h": 7},
        "Seasonal neighbours against the paper's setting": {"w": 12, "h": 7},
        "k and normalisation, in-sample (never picks the headline)": {"w": 24, "h": 10},
    }

    secs = [
        ("The verdict — a gate fixed before the runs", [
            ("kNN's P10–P90 band catches the final bill", "scalar", 8, values(
                f"{CURRENT}/metrics.csv", ["share of snapshots"],
                [[f"{pct(h2h_all['knn_cover'])}%"]])),
            ("Today's ±15% band catches it", "scalar", 8, values(
                f"{CURRENT}/metrics.csv", ["share of snapshots"],
                [[f"{pct(h2h_all['champ_cover'])}%"]])),
            ("Snapshots both forecast, held out", "scalar", 8, values(
                f"{CURRENT}/metrics.csv", ["snapshots"], [[int(h2h_all["n"])]])),
            ("The gate, for the paper's setting and the seasonal variant", "table", 24, values(
                f"{CURRENT}/stage3_extras.md", ["criterion", "paper's setting (k = 100, week)", "seasonal (±30 days)"],
                [[criterion(b["Criterion"]), result(b), result(s)] for b, s in zip(base_gate, seas_gate)])),
        ]),
        ("Stage 2 — month-end against v2, held-out cycles (200 households, export sealed 2 Oct)", [
            ("How often the band catches the final bill", "bar", 12, values(
                f"{CURRENT}/metrics.csv", ["days elapsed", *COVER],
                [[r["days_elapsed"], pct(r["knn_cover"]), pct(r["champ_cover"])] for r in h2h])),
            ("kNN's CRPS against v2's", "line", 12, values(
                f"{CURRENT}/metrics.csv", ["days elapsed", "kNN over v2"],
                [[r["days_elapsed"], round(float(r["ratio"]), 3)] for r in h2h])),
            ("The price of honesty: band width", "bar", 12, values(
                f"{CURRENT}/metrics.csv", ["days elapsed", "kNN P10–P90 (kWh)", "v2 ±15% (kWh)"],
                [[r["days_elapsed"], round(float(r["knn_width_kwh"])), round(float(r["champ_width_kwh"]))]
                 for r in h2h])),
            ("Median error, head to head", "bar", 12, values(
                f"{CURRENT}/metrics.csv", ["days elapsed", "kNN P50 (%)", "v2 point (%)"],
                [[r["days_elapsed"], round(float(r["knn_mdape"]), 1), round(float(r["champ_mdape"]), 1)]
                 for r in h2h])),
            ("Where v2 can't forecast: kNN's median error", "bar", 12, values(
                f"{CURRENT}/metrics.csv", ["days elapsed", "kNN median APE (%)"],
                [[r["days_elapsed"], round(float(r["knn_mdape"]), 1)] for r in fallback])),
            ("Why v2 is refitted as at each date (export sealed 12 Sep)", "table", 12, values(
                "metrics.csv", ["v2's fit", "kNN CRPS over v2", "kNN cover", "v2 cover"],
                [[label, round(float(r["ratio"]), 2), f"{pct(r['knn_cover'])}%", f"{pct(r['champ_cover'])}%"]
                 for label, fit in (("as at each date", "asat"), ("as shipped (sees the future)", "shipped"))
                 for r in [one(s2_12sep, champion_fit=fit, cycles="held_out",
                               population="h2h", days_elapsed="all")]])),
        ]),
        ("Stage 1 — reproducing the paper, day-ahead (200 households, export sealed 2 Oct)", [
            ("kNN's gain over the paper's two baselines, in the paper's CRPS form", "bar", 12, values(
                f"{CURRENT}/stage1_reproduce.md", ["baseline", "here (%)", "in the paper (%)"],
                [[r["kNN gain over"], num(r["Here (quantile form)"]), num(r["Paper"])]
                 for r in stage1_gains])),
            ("Day-ahead scores (absolute CRPS is not comparable with the paper's)", "table", 12, values(
                f"{CURRENT}/stage1_reproduce.md",
                ["model", "CRPS", "paper's CRPS", "cover"],
                [[r["Model"].replace(", k=100, week normalisation", " (k=100)"),
                  num(r["CRPS (paper's quantile form)"]), num(r["Paper's CRPS (LCL)"]),
                  r["P10–P90 coverage"]] for r in stage1_scores])),
        ]),
        ("Stage 3 — the settings that did not help (200 households, export sealed 2 Oct)", [
            ("Library size, like for like", "bar", 12, values(
                f"{CURRENT}/stage3_extras.md", ["library", "CRPS over the whole cohort's"],
                [[f"{r['Households']} households", num(r["Ratio"])] for r in pool])),
            ("Seasonal neighbours against the paper's setting", "table", 12, values(
                f"{CURRENT}/stage3_extras.md",
                ["setting", "served", "CRPS", "cover", "too few weeks"],
                [[r[""].replace(" (all weeks)", ""), int(num(r["Snapshots served"])), num(r["kNN CRPS"]),
                  r["kNN cover"], int(num(r["Too few weeks"]))] for r in seasonal])),
            ("k and normalisation, in-sample (never picks the headline)", "table", 24, values(
                f"{CURRENT}/stage3_metrics.csv", ["k", "normalisation", "mean CRPS", "median APE (%)", "cover"],
                [[int(r["k"]), r["norm"], round(float(r["knn_crps"]), 1), round(float(r["knn_mdape"]), 1),
                  f"{pct(r['knn_cover'])}%"] for r in grid])),
        ]),
    ]
    return secs, layout


def check() -> None:
    """Offline: every source parses, every card has rows and a unique name, and
    no card reads a table. What only a live Metabase can say -- that each card
    runs and uses every column -- is verify.py's."""
    secs, layout = sections()
    names = [name for _, panels in secs for name, *_ in panels]
    problems = [f"duplicate card name: {n}" for n in sorted(set(names)) if names.count(n) > 1]
    problems += [f"layout names a card that is not built: {n}" for n in layout if n not in names]
    for _, panels in secs:
        for name, _display, _w, sql in panels:
            if "FROM (VALUES" not in sql or re.search(r"\b(src_\w+|kea|billing|metering|lake_audit)\.", sql):
                problems.append(f"{name}: reads a table")
    if problems:
        sys.exit("\n".join(problems))
    print(f"{len(names)} cards: names unique, every one built from the committed results, none reads a table")


# ------------------------------------------------------------------ Metabase

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
    secs, layout = sections()          # parse first: a bad source stops before Metabase is touched
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
        parent = api("/api/collection", {"name": COLLECTION, "color": "#509EE3"}, session=s)["id"]
    coll = next((c["id"] for c in cols
                 if c.get("name") == SUBCOLLECTION
                 and c.get("location", "").strip("/").endswith(str(parent))), None)
    if coll is None:
        previous = next((c for c in cols if c.get("name") in SUBCOLLECTION_WAS
                         and c.get("location", "").strip("/").endswith(str(parent))), None)
        if previous:
            api(f"/api/collection/{previous['id']}", {"name": SUBCOLLECTION}, session=s, method="PUT")
            coll = previous["id"]
            print(f"renamed collection '{previous['name']}' -> '{SUBCOLLECTION}'")
    if coll is None:
        coll = api("/api/collection",
                   {"name": SUBCOLLECTION, "color": "#F2A86F", "parent_id": parent,
                    "description": "Built by analysis/metabase/build_backtest_dashboard.py"},
                   session=s)["id"]
        print(f"created collection '{SUBCOLLECTION}'")

    existing = {c["name"]: c["id"] for c in
                api(f"/api/collection/{coll}/items?models=card", session=s).get("data", [])}

    def upsert(name, display, sql):
        payload = {
            "name": name, "display": display,
            "visualization_settings": layout.get(name, {}).get("viz", {}),
            "collection_id": coll,
            "dataset_query": {"type": "native", "database": db,
                              "native": {"query": sql, "template-tags": {}}},
        }
        if name in existing:
            api(f"/api/card/{existing[name]}", payload, session=s, method="PUT")
            return existing[name], "updated"
        return api("/api/card", payload, session=s)["id"], "created"

    cards, made, built = [], {"created": 0, "updated": 0}, set()
    for section, panels in secs:
        cards.append(("heading", section, 24, 1))
        for name, display, width, sql in panels:
            cid, what = upsert(name, display, sql)
            made[what] += 1
            built.add(name)
            spec = layout.get(name, {})
            cards.append(("card", (cid, name), spec.get("w", width), spec.get("h", 3 if display == "scalar" else 6)))
    print(f"questions: {made['created']} created, {made['updated']} updated")

    dash = next((d for d in api(f"/api/collection/{coll}/items?models=dashboard",
                                session=s).get("data", []) if d["name"] == DASHBOARD), None)
    if dash is None:
        previous = next((d for d in api(f"/api/collection/{coll}/items?models=dashboard",
                                        session=s).get("data", []) if d["name"] in DASHBOARD_WAS), None)
        if previous:
            api(f"/api/dashboard/{previous['id']}", {"name": DASHBOARD}, session=s, method="PUT")
            dash = previous
            print(f"renamed dashboard '{previous['name']}' -> '{DASHBOARD}'")
    dash_id = dash["id"] if dash else api(
        "/api/dashboard", {"name": DASHBOARD, "collection_id": coll}, session=s)["id"]

    # Shelf packing, as in build_lake_dashboard.py: the next row starts below the
    # TALLEST card in the current one.
    dashcards, neg = [], -1
    row = col = band = 0

    def place(**kw):
        nonlocal neg
        dashcards.append({"id": neg, "parameter_mappings": [], **kw})
        neg -= 1

    place(card_id=None, row=0, col=0, size_x=24, size_y=8,
          visualization_settings={"virtual_card": {"display": "text"}, "text": INTRO})
    row = 8
    for kind, payload, width, height in cards:
        if kind == "heading":
            row += band
            col = band = 0
            place(card_id=None, row=row, col=0, size_x=24, size_y=1,
                  visualization_settings={"virtual_card": {"display": "heading"}, "text": payload})
            row += 1
            continue
        cid, _ = payload
        if col + width > 24:
            row += band
            col = band = 0
        place(card_id=cid, row=row, col=col, size_x=width, size_y=height, visualization_settings={})
        col += width
        band = max(band, height)
        if col >= 24:
            row += band
            col = band = 0

    api(f"/api/dashboard/{dash_id}", {"dashcards": dashcards}, session=s, method="PUT")
    print(f"dashboard: {MB}/dashboard/{dash_id}")

    # The collection is code-owned: a question this file no longer builds is
    # archived, so a renamed card does not leave its old self behind.
    for cname, cid in existing.items():
        if cname not in built:
            api(f"/api/card/{cid}", {"archived": True}, session=s, method="PUT")
            print(f"  archived stale question: {cname}")


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        check()
    else:
        main()
