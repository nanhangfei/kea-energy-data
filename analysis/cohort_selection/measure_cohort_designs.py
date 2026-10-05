"""Measure the LCL households a cohort can be drawn from, and compare cohort designs.

The evidence behind design/kea-cohort-selection.md. Every figure that document quotes
is printed by this script, so it can be re-measured rather than trusted.

Read-only. It streams the half-hourly blocks out of the Kaggle zip (nothing is
extracted), reads the coverage cache cohort.py keeps, and -- with --serving -- runs
read-only SELECTs against this machine's kea-db. Aggregates only: no LCL id, ICP or
account number is printed.

    cd data
    python3 analysis/cohort_selection/measure_cohort_designs.py              # designs.md
    python3 analysis/cohort_selection/measure_cohort_designs.py --serving    # + serving_now.md

Stdlib only, like cohort.py.

⚠️ THE SHAPE MEASURES ARE PROXIES, AND EACH SAYS WHICH RULE IT STANDS IN FOR.
  * heating   R² of a household's 12 calendar-month means against a cosine that
              peaks in January. It stands in for the monthly heating fit
              (int_metering__heating_sensitivity, R² >= 0.30 against Auckland HDD),
              which needs the warehouse. --serving checks the proxy against the
              accounts that fit actually made forecastable.
  * EV-shaped the day's largest excess over its floor held for two half-hours in a
              row. A day counts when that excess, scaled, is >= 2.5 kWh: detect-v1's
              high-power rule (planted.DETECT_V1), not the whole screen() --
              screen() also applies the low-power and timer rules, so it is stricter.
  * plan      the cheapest of the three Kea plans from the household's band shares
              at displayed prices: no solar, no term credit, no line losses.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import statistics
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

GEN = Path("pipeline/scripts/gen_source")
sys.path.insert(0, str(GEN))
import cohort as co  # noqa: E402
import planted  # noqa: E402
import uk_to_nz as tz  # noqa: E402

ZIP = Path("raw/lcl/smart-meters-in-london.zip")
EXTRACTED = Path("raw/lcl/extracted")
BLOCK = "halfhourly_dataset/halfhourly_dataset/{}.csv"
OUT = Path(__file__).resolve().parent / "results"

MIN_COVERAGE = 0.99                  # cohort.py's --min-coverage default
HEAT_PROXY_CUT = 0.5                 # see the calibration in serving_now.md
EV_EXCESS_KWH = planted.DETECT_V1["detect_high_power_excess_kwh"]
NZ_TARGET = co.NZ_TARGET_MEAN_KWH_DAY
SOLAR_SHARE = co.SOLAR_SHARE

#: Displayed, GST-inclusive Kea prices (docs/sprints/sprint-3/plans-and-terms.md §2).
ANYTIME = (1.45, 0.325)
TOU = (1.50, 0.390, 0.270, 0.180)    # daily, peak, off-peak, night
FREE_WEEKENDS = (1.55, 0.365)


def _h(*parts) -> int:
    """cohort.py's and gen_billing.py's hash: sha256 of the parts joined by '|'."""
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


def account_number(lcl: str) -> str:
    """gen_billing.py's account number, which 08_payments.sql hashes for the profile."""
    return f"A{_h(lcl, 'acct') % 10**8:08d}"


def payment_profile(lcl: str) -> str:
    """08_payments.sql's hash bucket, before its ensure-steps fill an empty one."""
    r = int(hashlib.md5((account_number(lcl) + "payprofile").encode()).hexdigest()[:7], 16) % 100
    return ("PAY_ON_TIME" if r < 85 else "LATE_STREAK" if r < 91 else
            "DD_FAILURE" if r < 95 else "PART_PAYMENT" if r < 98 else "STOPPED")


def acorn(h: dict) -> str:
    a = h["Acorn_grouped"]
    return "ACORN-U" if a.startswith("ACORN-") else a


def group(h: dict) -> tuple[str, str]:
    return (h["stdorToU"], acorn(h))


# --------------------------------------------------------------------------- inputs

def load_households() -> list[dict]:
    with zipfile.ZipFile(ZIP) as z, z.open("informations_households.csv") as f:
        return list(csv.DictReader(io.TextIOWrapper(f, newline="")))


def load_coverage() -> tuple[dict[str, float], dict[str, str]]:
    """Each household's share of the window, from cohort.py's cache.

    Refuses rather than rescans: cohort.scan_coverage() reads the EXTRACTED blocks,
    and a partial extraction would silently shrink the pool.
    """
    if not co.CACHE.exists():
        sys.exit(f"{co.CACHE} is missing: run `python3 {GEN}/cohort.py --rescan` "
                 "with every block extracted first")
    masks, blocks, _, ndays = co.load_coverage()
    band, width = co.window_band(ndays)
    frac = {k: bin(m & band).count("1") / width for k, m in masks.items()}
    return frac, blocks


def _bst(year: int) -> tuple[str, str]:
    def last_sunday(month: int) -> dt.date:
        d = dt.date(year, month, 31)
        return d - dt.timedelta(days=(d.weekday() + 1) % 7)
    return f"{last_sunday(3)} 01:00", f"{last_sunday(10)} 01:00"


def profile(eligible: set[str], blocks: dict[str, str]) -> dict[str, dict]:
    """One pass over the blocks the eligible households live in.

    `tstp` is UTC (probe_tstp.py); the band shares need London clock time, so the
    BST hour is added back. A day with fewer than 46 half-hours is left out.
    """
    bst = {y: _bst(y) for y in range(2011, 2015)}
    out: dict[str, dict] = {}

    def flush(lcl, day, hh):
        if day is None or len(hh) < 46:
            return
        d = dt.date.fromisoformat(day)
        if not tz.LCL_START <= d <= tz.LCL_END:
            return
        s = out.setdefault(lcl, {"days": 0, "kwh": 0.0, "night": 0.0, "peak": 0.0,
                                 "day": 0.0, "wkfree": 0.0, "month": [0.0] * 12,
                                 "mdays": [0] * 12, "excess": []})
        vals = [hh[k] for k in sorted(hh)]
        floor = min(vals)
        s["days"] += 1
        s["kwh"] += sum(vals)
        s["month"][d.month - 1] += sum(vals)
        s["mdays"][d.month - 1] += 1
        s["excess"].append(round(max(min(a, b) for a, b in zip(vals, vals[1:])) - floor, 3))
        for slot, v in hh.items():
            hour = slot / 2
            if hour >= 23 or hour < 7:
                s["night"] += v
            elif 7 <= hour < 11 or 17 <= hour < 21:
                s["peak"] += v
            if 9 <= hour < 17:
                s["day"] += v
                if d.weekday() >= 5:
                    s["wkfree"] += v

    with zipfile.ZipFile(ZIP) as z:
        for b in sorted({blocks[k] for k in eligible}):
            with z.open(BLOCK.format(b)) as f:
                r = csv.reader(io.TextIOWrapper(f, newline=""))
                next(r, None)
                cur, hh = (None, None), {}
                for row in r:
                    if row[0] not in eligible:
                        continue
                    ts = row[1][:16]
                    t = dt.datetime.fromisoformat(ts)
                    on, off = bst[t.year]
                    if on <= ts < off:
                        t += dt.timedelta(hours=1)
                    key = (row[0], t.date().isoformat())
                    if key != cur:
                        flush(*cur, hh)
                        cur, hh = key, {}
                    try:
                        slot = t.hour * 2 + (t.minute >= 30)
                        hh[slot] = hh.get(slot, 0.0) + float(row[2])
                    except ValueError:
                        pass          # 'Null': missing, not zero
                flush(*cur, hh)
    return out


# --------------------------------------------------------------------------- measures

COS = [math.cos(2 * math.pi * m / 12) for m in range(12)]


def heat_r2(s: dict) -> float:
    y = [s["month"][m] / s["mdays"][m] for m in range(12)]
    my, mx = statistics.fmean(y), statistics.fmean(COS)
    sxy = sum((a - mx) * (b - my) for a, b in zip(COS, y))
    sxx = sum((a - mx) ** 2 for a in COS)
    syy = sum((b - my) ** 2 for b in y)
    return 0.0 if syy == 0 else sxy * sxy / (sxx * syy)


def mean_kwh(s: dict) -> float:
    return s["kwh"] / s["days"]


def ev_shaped_share(s: dict, scale: float) -> float:
    return sum(e * scale >= EV_EXCESS_KWH for e in s["excess"]) / s["days"]


def cheapest_plan(s: dict, scale: float) -> str:
    e = mean_kwh(s) * scale
    p, n = s["peak"] / s["kwh"], s["night"] / s["kwh"]
    cost = {"Anytime": ANYTIME[0] + ANYTIME[1] * e,
            "Time of Use": TOU[0] + e * (TOU[1] * p + TOU[2] * (1 - p - n) + TOU[3] * n),
            "Free Weekends": FREE_WEEKENDS[0] + FREE_WEEKENDS[1] * e * (1 - s["wkfree"] / s["kwh"])}
    return min(cost, key=cost.get)


# --------------------------------------------------------------------------- designs

def current_rule(n: int, strata: dict, eligible_n: int) -> list[str]:
    """cohort.py today: quotas from the eligible pool, hash order within a group."""
    chosen = []
    for _, members in sorted(strata.items()):
        take = round(n * len(members) / eligible_n)
        chosen += sorted(members, key=lambda k: _h(k, co.SEED))[:take]
    return sorted(chosen, key=lambda k: _h(k, co.SEED))[:n]


def proposed_rule(n: int, households: list[dict], eligible: set[str]) -> list[str]:
    """cohort.select() itself, with the usage it sorts on: the cache's kWh per day read."""
    masks, _, kwh, _ = co.load_coverage()
    mean = {k: kwh.get(k, 0.0) / max(1, bin(masks.get(k, 0)).count("1")) for k in eligible}
    return [h["LCLid"] for h in co.select(households, eligible, mean, n)[0]]


def solar_by_rule(cohort: list[str], acorns: dict, fixed: bool) -> set[str]:
    """Today's hash share, or cohort.solar_households()'s fixed count."""
    if fixed:
        return co.solar_households([{"LCLid": k, "Acorn_grouped": acorns[k]} for k in cohort])
    return {k for k in cohort if _h(k, "solar") % 100 < (14 if acorns[k] == "Affluent" else 6)}


def describe(label: str, cohort: list[str], P: dict, acorns: dict, tariffs: dict,
             blocks: dict, current: set[str], fixed_solar: bool) -> dict:
    n = len(cohort)
    scale = NZ_TARGET / (sum(P[k]["kwh"] for k in cohort) / sum(P[k]["days"] for k in cohort))
    solar = solar_by_rule(cohort, acorns, fixed_solar)
    other = [k for k in cohort if k not in solar]
    nz_annual = {k: mean_kwh(P[k]) * scale * 365.25 for k in cohort}
    a = Counter(acorns[k] for k in cohort)
    pay = Counter(payment_profile(k) for k in cohort)
    adversity = [k for k in cohort if acorns[k] == "Adversity"]
    plans = Counter(cheapest_plan(P[k], scale) for k in other)
    day = sorted(P[k]["day"] / P[k]["kwh"] for k in other)
    arrays = Counter(co.pv_kwp(nz_annual[k]) for k in solar)
    return {
        "design": label, "n": n, "scale": scale,
        "affluent": a["Affluent"] / n, "adversity": a["Adversity"] / n,
        "comfortable": a["Comfortable"] / n, "acorn_u": a["ACORN-U"],
        "tou": sum(tariffs[k] == "ToU" for k in cohort),
        "heating": sum(heat_r2(P[k]) >= HEAT_PROXY_CUT for k in cohort),
        "solar": len(solar),
        "solar_strict": sum(ev_shaped_share(P[k], scale) <= planted.MAX_EV_SHAPED_SHARE for k in solar),
        "solar_loose": sum(ev_shaped_share(P[k], scale) <= planted.MAX_SOLAR_EV_SHAPED_SHARE for k in solar),
        "arrays": dict(sorted(arrays.items())),
        "clean": sum(ev_shaped_share(P[k], scale) <= planted.MAX_EV_SHAPED_SHARE for k in other),
        "heater_runs": sum(ev_shaped_share(P[k], scale) > 0.02 for k in cohort),
        "night_heavy": sum(P[k]["night"] / P[k]["kwh"] >= 0.40 for k in cohort),
        "day_p10": day[len(day) // 10], "day_p90": day[(9 * len(day)) // 10],
        "lfc_over": sum(nz_annual[k] >= 8000 for k in other),
        "max_kwh_day": max(mean_kwh(P[k]) for k in cohort),
        "anytime": plans["Anytime"], "free_weekends": plans["Free Weekends"],
        "late": pay["LATE_STREAK"], "dd": pay["DD_FAILURE"], "part": pay["PART_PAYMENT"],
        "stopped": pay["STOPPED"], "on_time": pay["PAY_ON_TIME"],
        "vulnerable": sum(_h(k, "v") % 100 < 18 for k in adversity),
        "medical": sum(_h(k, "m") % 100 < 7 for k in adversity),
        "blocks": len({blocks[k] for k in cohort}), "kept": len(set(cohort) & current),
        # Machine state, not a property of the design: init-sources unzips nothing
        # while raw/lcl/extracted exists, so these are the blocks to unzip by hand.
        "to_extract": len({blocks[k] for k in cohort}
                          - {f.stem for f in EXTRACTED.glob("block_*.csv")}),
    }


# --------------------------------------------------------------------------- serving

SERVING_SQL = Path(__file__).resolve().parent / "serving_now.sql"


def psql(container: str, sql: str) -> list[list[str]]:
    r = subprocess.run(["docker", "exec", "-i", container, "psql", "-U", "kea", "-d", "kea",
                        "-v", "ON_ERROR_STOP=1", "-A", "-F", "\t", "-P", "footer=off"],
                       input=f"BEGIN READ ONLY;\n{sql}\nROLLBACK;\n",
                       capture_output=True, text=True, check=True)
    return [line.split("\t") for line in r.stdout.splitlines()
            if line and line not in ("BEGIN", "ROLLBACK")]


def serving_report(container: str, P: dict, current: list[str]) -> str:
    """The published 50 as the features see them, and the heating proxy's calibration."""
    lines = ["# Serving today: the published 50-household cohort\n",
             f"Run {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC by `{Path(__file__).name} "
             f"--serving`, read-only, against `{container}`. Queries: `{SERVING_SQL.name}`. "
             "Generated accounts only (account_id >= 1,000,000).\n"]
    for block in SERVING_SQL.read_text().split("\n-- @")[1:]:
        title, sql = block.split("\n", 1)
        rows = psql(container, sql)
        lines.append(f"\n## {title.strip()}\n")
        if rows:
            lines.append("| " + " | ".join(rows[0]) + " |")
            lines.append("|" + "---|" * len(rows[0]))
            lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]

    # Calibration: the heating proxy against the accounts the real fit made forecastable.
    flags = dict(psql(container, """
        SELECT c.account_number, bool_or(f.is_forecastable)::text
          FROM kea.mart_account_context c JOIN kea.mart_forecast_input f USING (account_id)
         WHERE c.account_id >= 1000000 GROUP BY 1;""")[1:])
    pairs = [(heat_r2(P[k]), flags[account_number(k)] == "true")
             for k in current if account_number(k) in flags]
    lines.append("\n## Calibration: the heating proxy against `is_forecastable`\n")
    lines.append(f"{len(pairs)} published accounts matched; "
                 f"{sum(f for _, f in pairs)} were forecastable on at least one snapshot.\n")
    lines.append("| Proxy R² cut | Proxy positive | … forecastable | Forecastable but proxy negative |")
    lines.append("|---:|---:|---:|---:|")
    for cut in (0.3, 0.4, 0.5, 0.6, 0.7):
        pos = [f for r, f in pairs if r >= cut]
        lines.append(f"| {cut} | {len(pos)} | {sum(pos)} | "
                     f"{sum(f for r, f in pairs if r < cut)} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- report

def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="50,100,150,200,300")
    ap.add_argument("--serving", action="store_true", help="also query kea-db, read-only")
    ap.add_argument("--db-container", default="kea-db")
    ap.add_argument("--baseline", type=Path, default=co.OUT,
                    help="the cohort.json serving was published from, if cohort.json has moved on")
    args = ap.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]

    households = load_households()
    frac, blocks = load_coverage()
    acorns = {h["LCLid"]: acorn(h) for h in households}
    tariffs = {h["LCLid"]: h["stdorToU"] for h in households}
    eligible = {k for k in acorns if frac.get(k, 0) >= MIN_COVERAGE}
    baseline = json.loads(args.baseline.read_text())["households"]
    current = [h["lclid"] for h in baseline]
    print(f"profiling {len(eligible)} eligible households from {ZIP} ...", file=sys.stderr)
    P = profile(eligible, blocks)

    pop = Counter(group(h) for h in households)
    share = {k: v / len(households) for k, v in pop.items()}
    strata: dict = defaultdict(list)
    for h in households:
        if h["LCLid"] in P:
            strata[group(h)].append(h["LCLid"])

    out = ["# Cohort designs measured against the LCL pool\n",
           f"Run {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC by `{Path(__file__).name}`. "
           f"Window {tz.LCL_START} .. {tz.LCL_END}; eligible = at least {MIN_COVERAGE:.0%} of it. "
           "Proxies are defined in the script's docstring.\n"]

    out.append("## 1. Coverage threshold and Acorn mix\n")
    out.append("| Pool | Households | Affluent | Adversity | Comfortable | ToU |")
    out.append("|---|---:|---:|---:|---:|---:|")
    def mix(keys):
        a = Counter(acorns[k] for k in keys)
        n = len(keys)
        return (f"{n:,} | {pct(a['Affluent'] / n)} | {pct(a['Adversity'] / n)} | "
                f"{pct(a['Comfortable'] / n)} | {pct(sum(tariffs[k] == 'ToU' for k in keys) / n)}")
    out.append(f"| All LCL households | {mix(list(acorns))} |")
    for cut in (0.99, 0.97, 0.95, 0.90, 0.80):
        out.append(f"| ≥ {cut:.0%} coverage | {mix([k for k in acorns if frac.get(k, 0) >= cut])} |")

    out.append("\n## 2. Usage level and feature behaviour, eligible pool\n")
    scale_pool = NZ_TARGET / (sum(s["kwh"] for s in P.values()) / sum(s["days"] for s in P.values()))
    means = sorted(mean_kwh(s) for s in P.values())
    cuts = (means[len(means) // 3], means[2 * len(means) // 3])
    out.append(f"Tertile cuts {cuts[0]:.2f} and {cuts[1]:.2f} kWh/day (London, unscaled); "
               f"EV-shaped share at the pool's own scale ×{scale_pool:.2f}.\n")
    out.append(f"| Usage tertile | Households | Heating proxy ≥ {HEAT_PROXY_CUT} | EV-shaped on ≤ 0.5% of days |")
    out.append("|---|---:|---:|---:|")
    for name, lo, hi in (("Low", 0, cuts[0]), ("Mid", cuts[0], cuts[1]), ("High", cuts[1], 1e9)):
        g = [s for s in P.values() if lo <= mean_kwh(s) < hi]
        out.append(f"| {name} | {len(g)} | {pct(sum(heat_r2(s) >= HEAT_PROXY_CUT for s in g) / len(g))} | "
                   f"{pct(sum(ev_shaped_share(s, scale_pool) <= planted.MAX_EV_SHAPED_SHARE for s in g) / len(g))} |")
    plans = Counter(cheapest_plan(s, scale_pool) for s in P.values())
    out.append("\nCheapest plan across the pool: " +
               ", ".join(f"{k} {v} ({pct(v / len(P))})" for k, v in plans.most_common()) + ".\n")

    out.append("## 3. Designs\n")
    out.append("`current 50` is the published cohort (`--baseline`). `current rule` is cohort.py's allocation "
               "at that size; `proposed` is population quotas, a usage-systematic sample and a fixed "
               f"{SOLAR_SHARE:.0%} solar count. Payment profiles are 08_payments.sql's hash buckets "
               "before any minimum is applied.\n")
    rows = [describe("current 50", current, P, acorns, tariffs, blocks, set(current), False)]
    for n in sizes:
        rows.append(describe(f"current rule {n}", current_rule(n, strata, len(P)), P, acorns,
                             tariffs, blocks, set(current), False))
        rows.append(describe(f"proposed {n}", proposed_rule(n, households, eligible), P, acorns,
                             tariffs, blocks, set(current), True))
    cols = [("Design", "design", "{}"), ("N", "n", "{}"), ("Scale", "scale", "{:.2f}"),
            ("Affluent", "affluent", "{:.0%}"), ("Adversity", "adversity", "{:.0%}"),
            ("Comfortable", "comfortable", "{:.0%}"),
            ("ACORN-U", "acorn_u", "{}"), ("ToU", "tou", "{}"),
            ("Heating proxy", "heating", "{}"), ("Solar", "solar", "{}"),
            ("Solar clean ≤0.5%", "solar_strict", "{}"), ("Solar clean ≤1.5%", "solar_loose", "{}"),
            ("Non-solar clean", "clean", "{}"), ("Heater runs >2%", "heater_runs", "{}"),
            ("Night-heavy", "night_heavy", "{}"), ("Anytime cheapest", "anytime", "{}"),
            ("Free Weekends cheapest", "free_weekends", "{}"), ("LATE", "late", "{}"),
            ("DD", "dd", "{}"), ("PART", "part", "{}"), ("STOPPED", "stopped", "{}"),
            ("On time", "on_time", "{}"), ("Vulnerable", "vulnerable", "{}"),
            ("Medical", "medical", "{}"), ("Non-solar ≥8,000 kWh/yr", "lfc_over", "{}"),
            ("Blocks", "blocks", "{}"), ("Not yet unzipped", "to_extract", "{}"),
            ("Kept from current", "kept", "{}")]
    out.append("| " + " | ".join(c[0] for c in cols) + " |")
    out.append("|" + "---|" * len(cols))
    for r in rows:
        out.append("| " + " | ".join(f.format(r[k]) for _, k, f in cols) + " |")
    out.append("\nArray sizes (kWp: households) and the non-solar daytime (09:00–17:00) share, "
               "p10 to p90:\n")
    for r in rows:
        if r["design"].startswith(("current 50", "proposed")):
            out.append(f"- {r['design']}: {r['arrays']}; daytime {r['day_p10']:.2f}–{r['day_p90']:.2f}; "
                       f"largest household {r['max_kwh_day']:.1f} kWh/day (London)")

    # Groups that run out first under the population quotas.
    out.append("\n## 4. Where population quotas stop being exact\n")
    out.append("| Group | Population share | Eligible | Largest N it can fill |")
    out.append("|---|---:|---:|---:|")
    for k in sorted(strata, key=lambda k: len(strata[k]) / share[k]):
        out.append(f"| {k[0]} / {k[1]} | {pct(share[k])} | {len(strata[k])} | "
                   f"{int(len(strata[k]) / share[k]):,} |")

    # Planting: the real screen against this proxy, on today's candidates.
    cand = planted.candidates(baseline)
    scale_now = NZ_TARGET / (sum(P[k]["kwh"] for k in current) / sum(P[k]["days"] for k in current))
    proxy_clean = sum(ev_shaped_share(P[h["lclid"]], scale_now) <= planted.MAX_EV_SHAPED_SHARE for h in cand)
    out.append("\n## 5. The EV-shaped proxy against planted.screen()\n")
    out.append(f"Today's {len(cand)} planting candidates: the proxy calls {proxy_clean} clean for an EV. "
               "The screen itself (docs/sprints/sprint-4/data-plan.md, 17 Sep) passed 10 for an EV and "
               "7 for a hot-water element, so the screen keeps about "
               f"{10 / proxy_clean:.0%} and {7 / proxy_clean:.0%} of what the proxy calls clean.\n")

    OUT.mkdir(exist_ok=True)
    (OUT / "designs.md").write_text("\n".join(out) + "\n")
    print(f"wrote {OUT / 'designs.md'}", file=sys.stderr)
    if args.serving:
        (OUT / "serving_now.md").write_text(serving_report(args.db_container, P, current))
        print(f"wrote {OUT / 'serving_now.md'}", file=sys.stderr)


if __name__ == "__main__":
    main()
