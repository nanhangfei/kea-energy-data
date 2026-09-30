#!/usr/bin/env python3
"""Check that every dashboard actually renders what its queries return.

A Metabase panel fails quietly in several ways that all look like working
panels, so publishing is not evidence that anything works. This asserts what
the eye would have to catch:

  * every card executes without error
  * every card returns rows
  * every declared axis name exists in that card's own result columns --
    a renamed alias otherwise draws an empty chart and reports nothing
  * NO RESULT COLUMN IS SILENTLY IGNORED. This is the one that hurt. A
    query returning hour / month / kWh with only `hour` declared as a
    dimension drops the month breakout and draws one averaged line: a
    shape neither a weekday nor a weekend has, and no error anywhere.
    Every column must be a dimension, a measure, or removed from the
    SELECT.
  * no two cards overlap on the grid

    python3 analysis/metabase/verify.py                          # all seven, as `make dashboards` runs it
    python3 analysis/metabase/verify.py build_story_dashboard.py # only these (`make dashboards ONLY=story`)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Resolve by the names held in each builder. Metabase ids belong to its own
# application database and change after a volume reset; hard-coding them made
# this verifier fail against dashboards the same code had just recreated.
DASHBOARDS = ["build_dashboard.py", "build_source_dashboard.py",
              "build_lake_dashboard.py", "build_serving_dashboard.py",
              "build_plans_dashboard.py", "build_story_dashboard.py",
              "build_sprint4_dashboard.py", "build_backtest_dashboard.py"]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(mod) -> list[str]:
    e = mod.env()
    s = mod.api("/api/session", {"username": e["METABASE_ADMIN_EMAIL"],
                                 "password": e["METABASE_ADMIN_PASSWORD"]})["id"]
    collections = mod.api("/api/collection", session=s)
    collection = next((c for c in collections if c.get("name") == mod.SUBCOLLECTION), None)
    if collection is None:
        return [f"{mod.DASHBOARD}: collection {mod.SUBCOLLECTION!r} does not exist"]
    dashboards = mod.api(
        f"/api/collection/{collection['id']}/items?models=dashboard", session=s
    ).get("data", [])
    dashboard = next((d for d in dashboards if d.get("name") == mod.DASHBOARD), None)
    if dashboard is None:
        return [f"{mod.DASHBOARD}: dashboard does not exist"]
    dash_id = dashboard["id"]
    d = mod.api(f"/api/dashboard/{dash_id}", session=s)
    cards = sorted(d["dashcards"], key=lambda x: (x["row"], x["col"]))
    problems: list[str] = []

    grid: dict[tuple[int, int], str] = {}
    for c in cards:
        nm = (c.get("card") or {}).get("name", "text")
        for r in range(c["row"], c["row"] + c["size_y"]):
            for col in range(c["col"], c["col"] + c["size_x"]):
                if (r, col) in grid:
                    problems.append(f"overlap: {nm} over {grid[(r, col)]}")
                grid[(r, col)] = nm

    print(f"\n=== {mod.DASHBOARD} — {mod.MB}/dashboard/{dash_id} ===")
    for c in cards:
        cid = c.get("card_id")
        if not cid:
            continue
        card = c.get("card") or {}
        name = card.get("name", str(cid))
        r = mod.api(f"/api/card/{cid}/query", {}, session=s)
        if r.get("status") != "completed":
            problems.append(f"{name}: query failed")
            print(f"  FAIL  {name}")
            continue
        rows = len(r["data"]["rows"])
        cols = [x["display_name"] for x in r["data"]["cols"]]
        viz = card.get("visualization_settings") or {}
        dims = viz.get("graph.dimensions", [])
        mets = viz.get("graph.metrics", [])
        notes = []
        if rows == 0:
            problems.append(f"{name}: returns no rows")
            notes.append("EMPTY")
        if card.get("display") in ("bar", "row", "line", "area", "scatter", "combo", "waterfall"):
            missing = [x for x in dims + mets if x not in cols]
            ignored = [x for x in cols if x not in dims and x not in mets]
            if missing:
                problems.append(f"{name}: axis names not in result: {missing}")
                notes.append(f"MISSING {missing}")
            if ignored:
                problems.append(f"{name}: result columns ignored by the chart: {ignored}")
                notes.append(f"IGNORED {ignored}")
        print(f"  {card.get('display','?'):<6} {name[:50]:<52}{rows:>6} rows"
              + ("  " + " ".join(notes) if notes else ""))
    return problems


def main() -> None:
    # Builders named on the command line, or every one. An unknown name is an
    # error rather than a skip: a verify that checked nothing would pass.
    wanted = sys.argv[1:] or DASHBOARDS
    unknown = [w for w in wanted if w not in DASHBOARDS]
    if unknown:
        sys.exit(f"not a dashboard builder: {unknown}; one of {DASHBOARDS}")
    failures: list[str] = []
    for fn in wanted:
        failures += check(load(HERE / fn, fn[:-3]))
    print()
    if failures:
        print(f"{len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"{len(wanted)} dashboard(s): every card runs, returns rows, "
          "and uses every column it selects")


if __name__ == "__main__":
    main()
