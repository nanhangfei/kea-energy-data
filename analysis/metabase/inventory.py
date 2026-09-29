#!/usr/bin/env python3
"""Emit the data-bearing inventory of a dashboard builder: for every card, the
source tables it reads, the population it draws from, the window it covers, the
release generation it is pinned to, and the units its own output columns name.

    python3 analysis/metabase/inventory.py                     # dashboard 11
    python3 analysis/metabase/inventory.py build_dashboard.py  # any builder

This exists because a validation that transcribes the inventory by hand goes
stale the first time a card changes, and a stale inventory is worse than none:
it reads like a record of what the page does. Everything below is derived from
the builder's own SQL at run time, so it cannot disagree with what was built.
It needs no database and no Metabase session -- the same reason `--check` runs
in CI.

⚠️ WHAT THIS DOES NOT DO. It reports what each card *reads*, never whether the
number is right. Recomputing the aggregates is a separate job against a named
serving release, because only the database can answer that.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# A population is a named CTE the cards share, so it is the same set everywhere
# it appears. The text is what the card must splice for the claim to hold.
POPULATIONS = {
    "cohort": "the 44 generated demo households",
    "generating": "of those, the ones with an export register",
    "runs": "detected charging runs on the cohort",
    "demo_household": "one household, pinned by hand",
    "demo_day": "one day of that household",
}


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def tables(sql: str) -> list[str]:
    """Schema-qualified sources only. An unqualified name is a CTE defined in
    the same query, which is reported as a population instead."""
    found = re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+\.[a-z_]+)", sql, re.I)
    return sorted(set(found))


def populations(sql: str) -> list[str]:
    """A population counts only where its CTE is actually DEFINED in this card.
    Matching the bare word would credit a card for a population it never
    splices -- `runs` also appears in column aliases, and a card that merely
    says the word draws from everything."""
    return [name for name in POPULATIONS
            if re.search(rf"\b{name}\s+AS\s*\(", sql, re.I)]


def pins(mod, sql: str) -> list[str]:
    out = []
    for const in ("LATEST_SOLAR", "LATEST_EV", "LATEST_HARDSHIP", "LATEST_PLANT"):
        text = getattr(mod, const, None)
        if text and " ".join(text.split()) in " ".join(sql.split()):
            out.append(const)
    return out


def window(mod, sql: str) -> str:
    flat = " ".join(sql.split())
    if " ".join(getattr(mod, "LAST_YEAR", "\0").split()) in flat:
        return "last 365 days of the mart"
    for pattern, label in (
        (r"usage_date\s*>=?\s*'(\d{4}-\d{2}-\d{2})'", "from {0}"),
        (r"interval_start", "half-hourly, the day the card selects"),
        (r"basis_from|basis_to", "the scenario's own basis period"),
        (r"assessed_on", "the assessment date"),
        (r"period_start|period_end", "the billing period"),
    ):
        hit = re.search(pattern, flat, re.I)
        if hit:
            return label.format(*hit.groups()) if hit.groups() else label
    return "all rows in the release"


# A unit lives in one of three places, and reading only the column header gets
# it wrong. On a chart it is a `$` prefix in column_settings, because a header
# that said NZD would repeat itself on every bar. On a table it is concatenated
# into the value in SQL (`|| ' kWh'`), because a table has no per-column prefix
# to hang it on -- and on a banded dimension it is inside the band labels
# (`'under 2 kWh a day'`), which is why the literals are read, not just the
# headers. Only what is left gets read off the header wording.
#
# Scanned over SQL string literals, so these stay to tokens no prose would carry
# by accident: 'not enough assessed days' is a band NAME, not a measure in days.
LITERALS = [(r"kwh", "kWh"), (r"\$", "NZD"), (r"%", "%"), (r"\bkw\b", "kW"),
            # A dimension has a unit too, and it is the one a header never says:
            # both of these are x-axes, declared only by the format string that
            # builds them (`HH24:00`) or by the band names (`up to 1 h`).
            (r"hh24", "hour of day"), (r"\d+\s*h\b", "hours")]

# Header rules, money first: "a year" in `median saving a year` is a RATE on a
# money measure, not a measure in years, and "of the year" is a window. Only the
# PLURAL names years as the thing measured -- `best case, years` is the payback
# card, and it is the one card on the page whose bars really are years.
UNITS = [
    (r"kwh", "kWh"), (r"\$|nzd|dollar|saving|cost|worth|spend", "NZD"),
    (r"%|pct|share|percent", "%"), (r"\bdays?\b", "days"),
    (r"half-hours?|intervals?", "half-hours"), (r"\bkw\b", "kW"),
    (r"(?:^|,\s*)years\b|\byears$", "years"),
    (r"households?|bills?|runs?|sessions?|accounts?|nights?|rows?|"
     r"snapshots?|warnings?|detections?", "count"),
]


def units(columns: list[str], sql: str = "", viz: dict | None = None,
          display: str = "", name: str = "") -> list[str]:
    """⚠️ This reports the unit the card DECLARES, not that the number is in it.
    A column whose unit appears nowhere -- no prefix, no literal, no word in the
    header -- lands in `label only`, which on a measure is a finding: the reader
    is left to guess. A scalar is the exception, and says so: it has one column
    named after the card, so the title carries the unit and there is nowhere
    else for it to go."""
    if display == "scalar" and columns == [name]:
        return ["the title is the label"]
    out = []
    for spec in (viz or {}).get("column_settings", {}).values():
        if spec.get("prefix") == "$" and "NZD" not in out:
            out.append("NZD")
    # Double-quoted identifiers go first: one alias is `a year's generation`, and
    # its apostrophe would pair with the next real quote and shift every literal
    # after it -- silently, reporting the units of a card that is not this one.
    bare = re.sub(r'"[^"]*"', "", sql)
    literals = " | ".join(re.findall(r"'([^']*)'", bare))
    for unit_pattern, label in LITERALS:
        if re.search(unit_pattern, literals, re.I) and label not in out:
            out.append(label)
    for unit_pattern, label in UNITS:
        if any(re.search(unit_pattern, c, re.I) for c in columns) and label not in out:
            out.append(label)
    return out or ["label only"]


def columns(sql: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r'AS\s+"([^"]+)"', sql)))


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "build_sprint4_dashboard.py"
    mod = load(HERE / name)
    print(f"# Card inventory — {mod.DASHBOARD}\n")
    print(f"Collection **{mod.SUBCOLLECTION}**, generated by "
          f"`analysis/metabase/{name}` via `analysis/metabase/inventory.py`.\n")
    print("| # | card | kind | database | source tables | population | window | "
          "release pin | units |")
    print("|---|---|---|---|---|---|---|---|---|")
    n = 0
    for heading, items in mod.SECTIONS:
        section = heading.strip().lstrip("#").strip()
        print(f"| | **{section}** | | | | | | | |")
        for it in items:
            if it["kind"] != "card":
                continue
            n += 1
            sql = strip_comments(it["sql"])
            cols = columns(sql)
            pop = populations(sql)
            print(f"| {n} | {it['name']} | {it['display']} | {it['db']} "
                  f"| {'<br>'.join(f'`{t}`' for t in tables(sql)) or '—'} "
                  f"| {', '.join(pop) if pop else 'every account in the release'} "
                  f"| {window(mod, sql)} "
                  f"| {', '.join(pins(mod, sql)) or 'none needed'} "
                  f"| {', '.join(units(cols, sql, it.get('viz'), it['display'], it['name']))} |")
    print(f"\n**{n} data-bearing cards.**\n")
    print("## Populations\n")
    for key, meaning in POPULATIONS.items():
        print(f"* `{key}` — {meaning}")


if __name__ == "__main__":
    main()
