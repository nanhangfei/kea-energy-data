"""Score Sprint 4 against the answers the source generator planted.

    python3 evaluate_sprint4.py --out DIR      # writes DIR/report.md and DIR/metrics.csv

`make evaluate-sprint4` runs it, after the rebuild. The method and every
threshold are in analysis/sprint4_evaluation/README.md, written before the
first run; this file implements that README and nothing else.

READS ONLY, FROM THREE PLACES.
  * Serving, as kea_loader, in a READ ONLY transaction that is rolled back:
    the published marts, forecasts, estimates and bills.
  * The source database, as its owner, read-only: gen_audit's planted
    answers and defect manifest, and which reads were revised. Nothing the
    pipeline reads comes from there.
  * Two Snowflake silver tables, for the planted households only.
    INT_METERING__DETECTED_HALF_HOURS for the four hot-water ones: the
    night-timer measure needs each half-hour's class, and no served table
    keeps it. INT_METERING__IMPORT_HALF_HOURS for `is_masked` and
    `is_daylight_shoulder`: which half-hours the panels hid is what §1's
    masking split is measured against, and the marts publish only the
    COUNT of them per run and per day, never which.
Writes only --out.

AGGREGATES ONLY. The report names households by their planted role
("wallbox, evening, 1") and never prints an ICP, an account number or a
customer. Dates and kWh are not identifiers and do appear.

THE HARDSHIP REPLAY re-runs build_hardship_assessments' own functions at
weekly dates, with one change: what had been paid on a bill is summed from
the payments made by that date, not read from billing.bill.payments_applied,
which is the biller's figure when the data was extracted and would show a
past date payments that had not happened yet.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import functools
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE.parent / "gen_source"))

import planted  # noqa: E402

NZ = "Pacific/Auckland"
HALF_HOUR_S = 1800
BEFORE_JSON = _HERE.parents[2] / "analysis" / "sprint4_evaluation" / "before.json"

# ---------------------------------------------------------------- thresholds
# Fixed in the README before the first run. Changing one is a new evaluation.

SESSION_MIN_OVERLAP = 0.5          # of BOTH sessions' durations
BOUNDS_TOLERANCE_KWH = 0.01        # the marts round to 4 dp; the planted file too
NIGHT_MIN_CLASSED_SHARE = 0.5      # of a hot-water session's half-hours
FIT_MIN_DAYS_IN_MONTH = 20         # int_metering__heating_sensitivity
FIT_MIN_MONTHS = 6
FIT_MIN_R2 = 0.30                  # dbt var min_heating_fit_r2
REPLICATION_TOLERANCE = 0.0005     # published fits are rounded to 4 dp
REPLAY_STEP_DAYS = 7
REPLAY_FIRST_AFTER_DAYS = 91       # after the cohort's first bill: three paid bills
ALIGNMENT_MIN_SHARE = 0.95         # served session starts found as silver `ev` half-hours
# ⚠️ There is no ONE array size any more. cohort.py sizes each household's roof
# to its own load, so §9 must read the size off the cohort row; a constant here
# would model a 4 kWp roof against a 1.5 kWp household's readings and report the
# difference as the model's error.
COHORT_JSON = Path(__file__).resolve().parent / "gen_source" / "cohort.json"

TARGETS = {
    "ev_high_power_precision": 0.90,
    "ev_high_power_recall": 0.85,
    "daily_bounds_share": 0.90,
    "night_timer_share": 0.80,
    "bill_step_ratio": 0.70,          # for every wallbox household
    "hardship_median_lead_days": 21,
    "hardship_gradual_caught": 2,     # of the 3 SLIDING accounts
    # detect-v2's whole claim, and the one number here with a hard zero: a run
    # the panels hid cannot be told from a portable charger, so asserting
    # low_power about one is the false claim this release removes. The dbt
    # singular test assert_masked_runs_have_no_charger_class.sql enforces it
    # over the whole cohort; this scores it on the planted three.
    "low_power_claims_on_masked_runs": 0,
    "false_alarms_per_100_account_years": 5.0,
}


# ---------------------------------------------------------------- pure parts

def planted_slots(kind: str, kwh: float, half_hours: int) -> tuple[float, ...]:
    """A planted session's kWh per half-hour, rebuilt as planted.sessions() made it.

    gen_audit keeps a session's total and length, not its slots. The slots are
    full half-hours at the charger's power and then the remainder, so the two
    give them back. A session cut at the end of the span has only full slots,
    and its recorded total is what they drew. A remainder under 0.00005 kWh
    rounds away in the file's four decimals; that slot is given back as 0.
    """
    slots = planted._slots(kwh, planted.KW[kind])[:half_hours]
    if len(slots) == half_hours - 1:
        slots += (0.0,)
    if len(slots) != half_hours:
        raise ValueError(f"{kind} session of {kwh} kWh does not fill {half_hours} "
                         "half-hours: the planted file and planted.py disagree")
    return slots


def overlap_s(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def sessions_match(a_start: int, a_end: int, b_start: int, b_end: int,
                   min_share: float = SESSION_MIN_OVERLAP) -> bool:
    """Whether two sessions overlap by at least min_share of each one's length."""
    shared = overlap_s(a_start, a_end, b_start, b_end)
    return (shared >= min_share * (a_end - a_start)
            and shared >= min_share * (b_end - b_start))


def match_sessions(planted_spans: list[tuple[int, int]],
                   detected_spans: list[tuple[int, int]]) -> tuple[set[int], set[int]]:
    """(indexes of planted sessions matched, indexes of detected sessions matched).

    Both lists are one household's. Sessions within a list never overlap, so
    a sweep over both in start order finds every pair.
    """
    p_order = sorted(range(len(planted_spans)), key=lambda i: planted_spans[i])
    d_order = sorted(range(len(detected_spans)), key=lambda i: detected_spans[i])
    p_hit: set[int] = set()
    d_hit: set[int] = set()
    j0 = 0
    for i in p_order:
        ps, pe = planted_spans[i]
        while j0 < len(d_order) and detected_spans[d_order[j0]][1] <= ps:
            j0 += 1
        j = j0
        while j < len(d_order) and detected_spans[d_order[j]][0] < pe:
            ds, de = detected_spans[d_order[j]]
            if sessions_match(ps, pe, ds, de):
                p_hit.add(i)
                d_hit.add(d_order[j])
            j += 1
    return p_hit, d_hit


def in_bounds(value: float, low: float, high: float,
              tol: float = BOUNDS_TOLERANCE_KWH) -> bool:
    return low - tol <= value <= high + tol


def outside_by(value: float, low: float, high: float,
               tol: float = BOUNDS_TOLERANCE_KWH) -> float:
    """How far outside [low, high] the value falls, in kWh. 0.0 when inside.

    Inside/outside alone reads as a failure whether the band misses by a
    kilowatt-hour or by a hundredth of one, and these bands are often under
    1 kWh wide, so the distance is reported beside the share.
    """
    if in_bounds(value, low, high, tol):
        return 0.0
    return low - value if value < low else value - high


@dataclass(frozen=True)
class Fit:
    months: int
    slope: float | None
    intercept: float | None
    r2: float | None

    @property
    def usable(self) -> bool:
        return self.r2 is not None and self.r2 >= FIT_MIN_R2


def ols(xs: list[float], ys: list[float]) -> tuple[float | None, float | None, float | None]:
    """Snowflake's regr_slope, regr_intercept and regr_r2 over the same pairs.

    regr_r2 is NULL when x does not vary and 1 when only y does not.
    """
    n = len(xs)
    if n < 2:
        return None, None, None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    if sxx == 0:
        return None, None, None
    slope = sxy / sxx
    r2 = 1.0 if syy == 0 else sxy * sxy / (sxx * syy)
    return slope, my - slope * mx, r2


def monthly_fit(days: list[tuple[dt.date, float, float]]) -> Fit:
    """The heating fit's rule on (date, heating degree days, kWh) of complete days."""
    by_month: dict[dt.date, list[tuple[float, float]]] = defaultdict(list)
    for day, hdd, kwh in days:
        by_month[day.replace(day=1)].append((hdd, kwh))
    months = [(sum(h for h, _ in v) / len(v), sum(k for _, k in v) / len(v))
              for _, v in sorted(by_month.items()) if len(v) >= FIT_MIN_DAYS_IN_MONTH]
    if len(months) < FIT_MIN_MONTHS:
        return Fit(len(months), None, None, None)
    slope, intercept, r2 = ols([h for h, _ in months], [k for _, k in months])
    return Fit(len(months), slope, intercept, r2)


def episodes(flags: list[bool]) -> int:
    """How many times a weekly flag switches on: one alarm per run of weeks."""
    return sum(1 for i, f in enumerate(flags) if f and (i == 0 or not flags[i - 1]))


def median(values) -> float | None:
    values = [v for v in values if v is not None]
    return float(statistics.median(values)) if values else None


def share(hits: int, total: int) -> float | None:
    return hits / total if total else None


def fmt(value, digits: int = 1, pct: bool = False) -> str:
    if value is None:
        return "–"
    if pct:
        return f"{value * 100:.{digits}f}%"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return f"{value:,}"


def verdict(ok: bool | None) -> str:
    return {True: "PASS", False: "MISS", None: "NO DATA"}[ok]


# ---------------------------------------------------------------- reading

@functools.cache
def _zone():
    from zoneinfo import ZoneInfo
    return ZoneInfo(NZ)


def nz_date(ts: int) -> dt.date:
    return dt.datetime.fromtimestamp(ts, _zone()).date()


def _ts(value: dt.datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("a naive timestamp reached the evaluation")
    return int(value.timestamp())


@dataclass
class Household:
    icp_number: str
    kind: str
    pattern: str
    label: str
    icp_id: int | None = None
    account_id: int | None = None
    sessions: list[tuple[int, int, tuple[float, ...]]] = field(default_factory=list)

    @property
    def is_ev(self) -> bool:
        return self.kind.startswith("ev_")

    @property
    def first_day(self) -> dt.date:
        return nz_date(min(s for s, _, _ in self.sessions))

    def slot_kwh(self) -> dict[int, float]:
        out: dict[int, float] = {}
        for start, _, slots in self.sessions:
            for i, kwh in enumerate(slots):
                out[start + i * HALF_HOUR_S] = out.get(start + i * HALF_HOUR_S, 0.0) + kwh
        return out

    def daily_kwh(self) -> dict[dt.date, float]:
        out: dict[dt.date, float] = defaultdict(float)
        for ts, kwh in self.slot_kwh().items():
            out[nz_date(ts)] += kwh
        return dict(out)

    def daily_half_hours(self) -> dict[dt.date, int]:
        """Half-hours the planted load actually drew in, by date."""
        out: dict[dt.date, int] = defaultdict(int)
        for ts, kwh in self.slot_kwh().items():
            if kwh > 0:
                out[nz_date(ts)] += 1
        return dict(out)


def connect_source():
    import os

    import psycopg2
    conn = psycopg2.connect(
        host=os.environ.get("KEA_SOURCE_HOST", "kea-source-db"),
        port=int(os.environ.get("KEA_SOURCE_PORT", "5432")),
        dbname="kea", user="kea", password=os.environ["KEA_SOURCE_PASSWORD"])
    conn.set_session(readonly=True)
    return conn


def load_planted(src) -> tuple[list[Household], list[dict]]:
    cur = src.cursor()
    cur.execute("""
        select icp_identifier, load_kind, pattern, session_start, session_end,
               half_hours, kwh
        from gen_audit.planted_load
        order by icp_identifier, session_start""")
    by_icp: dict[str, Household] = {}
    for icp, kind, pattern, start, end, half_hours, kwh in cur.fetchall():
        h = by_icp.get(icp)
        if h is None:
            h = by_icp[icp] = Household(icp, kind, pattern, "")
        if (kind, pattern) != (h.kind, h.pattern):
            raise SystemExit("a planted household has two kinds of load: "
                             "the one-label rule is broken")
        slots = planted_slots(kind, float(kwh), int(half_hours))
        s, e = _ts(start), _ts(end)
        if e - s != half_hours * HALF_HOUR_S:
            raise SystemExit("a planted session's end is not start + its half-hours")
        h.sessions.append((s, e, slots))
    households = sorted(by_icp.values(), key=lambda h: (h.kind, h.pattern, h.first_day))
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for h in households:
        counts[(h.kind, h.pattern)] += 1
        name = {"ev_wallbox_7kw": "wallbox", "ev_portable_2kw": "portable",
                "ev_solar_wallbox_7kw": "solar wallbox",
                "hot_water_3kw": "hot water"}[h.kind]
        h.label = f"{name}, {h.pattern}, {counts[(h.kind, h.pattern)]}"
    expected = defaultdict(int)
    for place in planted.PLAN + planted.SOLAR_PLAN:
        expected[(place[0], place[1])] += 1
    if dict(counts) != dict(expected):
        raise SystemExit(f"gen_audit.planted_load has {dict(counts)}, "
                         f"the two plans say {dict(expected)}")

    cur.execute("""
        select acct_no, profile, trigger_issued_on, drift_from, arrears_onset_on
        from gen_audit.planted_hardship order by profile, arrears_onset_on""")
    sliders = [dict(zip(("acct_no", "profile", "trigger_on", "drift_from", "onset_on"), r))
               for r in cur.fetchall()]
    return households, sliders


def load_excluded_days(src, icps: list[str]) -> tuple[dict[str, set[dt.date]], dict]:
    """Days the generator altered after planting, per ICP, and why."""
    cur = src.cursor()
    cur.execute(f"""
        select icp_identifier, defect_kind,
               coalesce(nz_date, (interval_start at time zone '{NZ}')::date)
        from gen_audit.defects
        where defect_kind like 'src:%%' and icp_identifier = any(%s)""", (icps,))
    out: dict[str, set[dt.date]] = defaultdict(set)
    reasons: dict[str, int] = defaultdict(int)
    undated = 0
    for icp, kind, day in cur.fetchall():
        if day is None:
            undated += 1
            continue
        if day not in out[icp]:
            reasons[kind] += 1
        out[icp].add(day)
    cur.execute(f"""
        select distinct icp_identifier, (interval_start at time zone '{NZ}')::date
        from src_metering.interval_reads
        where version_no > 1 and icp_identifier = any(%s)""", (icps,))
    for icp, day in cur.fetchall():
        if day not in out[icp]:
            reasons["revised read"] += 1
        out[icp].add(day)
    return out, {"reasons": dict(reasons), "undated_defects_on_planted": undated}


def load_export_on_import_months(src) -> dict[str, set[str]]:
    cur = src.cursor()
    cur.execute("""
        select icp_identifier, detail->>'month' from gen_audit.defects
        where defect_kind = 'src:export_on_import'""")
    out: dict[str, set[str]] = defaultdict(set)
    for icp, month in cur.fetchall():
        out[icp].add(month)
    return out


# ---------------------------------------------------------------- measures

@dataclass
class Metric:
    section: str
    measure: str
    value: object
    target: object = ""
    passed: bool | None = None


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.metrics: list[Metric] = []

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)

    def metric(self, section: str, measure: str, value, target="", passed=None) -> None:
        self.metrics.append(Metric(section, measure, value, target, passed))

    def table(self, header: list[str], rows: list[list]) -> None:
        self.lines.append("| " + " | ".join(header) + " |")
        self.lines.append("|" + "---|" * len(header))
        for r in rows:
            self.lines.append("| " + " | ".join(str(c) for c in r) + " |")
        self.lines.append("")


def write_results(out: Path, rep: Report) -> None:
    """Write repository-clean report artifacts with one LF at each line end."""
    out.mkdir(parents=True, exist_ok=True)
    report = "\n".join(rep.lines).rstrip("\n") + "\n"
    (out / "report.md").write_text(report, encoding="utf-8")
    with open(out / "metrics.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["section", "measure", "value", "target", "passed"])
        for metric in rep.metrics:
            w.writerow([
                metric.section,
                metric.measure,
                round(metric.value, 6) if isinstance(metric.value, float) else metric.value,
                metric.target,
                "" if metric.passed is None else metric.passed,
            ])


def eligible_days(assessed: set[dt.date], excluded: set[dt.date],
                  start: int, end: int) -> bool:
    """A session counts only if every date it touches was assessed and unaltered."""
    days = {nz_date(t) for t in range(start, end, HALF_HOUR_S)}
    return days <= assessed and not days & excluded


def is_hidden(start: int, end: int, hidden: set[int]) -> bool:
    """Whether any half-hour of a session is one the panels hid.

    ANY, not all: one exporting half-hour inside a run is enough to make the
    run's shape unreadable, which is exactly what detect-v2 refuses to guess
    past. A session with none of them was metered in full.
    """
    return any(t in hidden for t in range(start, end, HALF_HOUR_S))


def measure_sessions(rep: Report, households: list[Household], detected: dict,
                     assessed: dict, excluded: dict, hidden: dict) -> None:
    rep.add("## 1. Charging sessions", "")
    rows = []
    tp = fp = 0
    found = total = 0
    portable_hit = portable_total = 0
    high_on_portable = 0
    left_out_planted = left_out_detected = 0
    for h in households:
        a, x = assessed.get(h.icp_id, set()), excluded.get(h.icp_number, set())
        ev_sessions = [(s, e) for s, e, _ in h.sessions] if h.is_ev else []
        kept_p = [p for p in ev_sessions if eligible_days(a, x, *p)]
        left_out_planted += len(ev_sessions) - len(kept_p)
        dets = detected.get(h.icp_id, [])
        # Indexed, not destructured: a detected row carries its masked half-hour
        # count too since detect-v2, and this filter only needs the span.
        kept_d = [d for d in dets if eligible_days(a, x, d[0], d[1])]
        left_out_detected += len(dets) - len(kept_d)
        high = [(d[0], d[1]) for d in kept_d if d[2] == "high_power"]
        low = [(d[0], d[1]) for d in kept_d if d[2] == "low_power"]
        # ⚠️ MATCHED FOR THE ROW, COUNTED ONLY FOR PLAN'S EIGHT. A solar host is
        # matched so its own row's "not matching" column means what it says, but
        # it never enters the headline precision and recall -- those stay on the
        # population Sprint 4 measured, so the two runs stay comparable. The
        # solar hosts are scored in their own split below.
        own = kept_p if h.kind in planted.WALLBOX_KINDS else []
        p_hit, d_hit = match_sessions(own, high)
        if h.kind == "ev_wallbox_7kw":
            tp += len(d_hit)
            fp += len(high) - len(d_hit)
            found += len(p_hit)
            total += len(own)
        if h.kind == "ev_portable_2kw":
            pp, _ = match_sessions(kept_p, low)
            portable_hit += len(pp)
            portable_total += len(kept_p)
            ph, _ = match_sessions(kept_p, high)
            high_on_portable += len(ph)
        rows.append([h.label, len(kept_p), len(p_hit) if own else "–",
                     len(high), len(high) - len(d_hit), len(low)])
    precision, recall = share(tp, tp + fp), share(found, total)
    rep.table(["Household", "Planted sessions counted", "Wallbox sessions found",
               "high_power detected", "…not matching a wallbox session", "low_power detected"],
              rows)
    rep.table(["Measure", "Result", "Target", ""], [
        ["high_power precision (planted households)", f"{tp} of {tp + fp} = {fmt(precision, pct=True)}",
         "≥ 90%", verdict(None if precision is None else precision >= TARGETS["ev_high_power_precision"])],
        ["high_power recall (wallbox sessions)", f"{found} of {total} = {fmt(recall, pct=True)}",
         "≥ 85%", verdict(None if recall is None else recall >= TARGETS["ev_high_power_recall"])],
        ["portable sessions found as low_power (no target)",
         f"{portable_hit} of {portable_total} = {fmt(share(portable_hit, portable_total), pct=True)}", "–", ""],
        ["portable sessions found as high_power (a class error)", high_on_portable, "–", ""],
    ])
    rep.add(f"Left out because a date they touch was not assessed or was altered after "
            f"planting: {left_out_planted} planted EV sessions, {left_out_detected} detected "
            "sessions.", "")
    rep.metric("sessions", "high_power_precision", precision, "0.90",
               None if precision is None else precision >= TARGETS["ev_high_power_precision"])
    rep.metric("sessions", "high_power_recall", recall, "0.85",
               None if recall is None else recall >= TARGETS["ev_high_power_recall"])
    rep.metric("sessions", "portable_low_power_recall", share(portable_hit, portable_total))
    rep.metric("sessions", "planted_sessions_left_out", left_out_planted)
    measure_masking_split(rep, households, detected, assessed, excluded, hidden)


def measure_masking_split(rep: Report, households: list[Household], detected: dict,
                          assessed: dict, excluded: dict, hidden: dict) -> None:
    """§1's split by masking: the three households that have both panels and a car."""
    solar = [h for h in households if h.kind == "ev_solar_wallbox_7kw"]
    if not solar:
        return
    rep.add("### 1a. Where the panels were in the way", "",
            "The three households with both an array and a car, scored apart from the eight "
            "above so that no Sprint 4 figure moves. A planted session is **hidden** when any "
            "of its half-hours was exporting, or sat between that day's first and last export "
            "(`is_masked` / `is_daylight_shoulder`, read from silver and measured off the "
            "export register -- no PV curve is modelled anywhere in this report's inputs). "
            "The rest are **clear**.", "",
            "⚠️ **Found means a published session overlaps it, whatever class it carries.** "
            "detect-v2 withholds `charger_class` on a hidden run rather than guessing it, and "
            "a withheld class is not a missing EV -- counting only `high_power` here would "
            "score the fix as a regression.", "")
    rows = []
    clear_hit = clear_total = hid_hit = hid_total = 0
    low_on_hidden = low_power_total = 0
    for h in solar:
        a, x = assessed.get(h.icp_id, set()), excluded.get(h.icp_number, set())
        hid = hidden.get(h.icp_number, set())
        kept_p = [(s, e) for s, e, _ in h.sessions if eligible_days(a, x, s, e)]
        kept_d = [d for d in detected.get(h.icp_id, []) if eligible_days(a, x, d[0], d[1])]
        spans = [(d[0], d[1]) for d in kept_d]
        clear = [p for p in kept_p if not is_hidden(*p, hid)]
        shaded = [p for p in kept_p if is_hidden(*p, hid)]
        c_hit, _ = match_sessions(clear, spans)
        s_hit, _ = match_sessions(shaded, spans)
        clear_hit += len(c_hit)
        clear_total += len(clear)
        hid_hit += len(s_hit)
        hid_total += len(shaded)
        lp = [d for d in kept_d if d[2] == "low_power"]
        low_power_total += len(lp)
        low_on_hidden += sum(1 for d in lp if (d[3] or 0) > 0)
        # The household's own load, as the pipeline sees it: sessions published
        # on days before the car arrived cannot be planted ones.
        background = [d for d in kept_d if nz_date(d[0]) < h.first_day]
        before_days = len({day for day in a if day < h.first_day} - x)
        rate = share(len(background) * 365.25, before_days) if before_days else None
        rows.append([h.label, f"{planted.SOLAR_ARRAY_KWP[h.pattern]:g} kWp",
                     len(clear), f"{len(c_hit)} = {fmt(share(len(c_hit), len(clear)), pct=True)}",
                     len(shaded), f"{len(s_hit)} = {fmt(share(len(s_hit), len(shaded)), pct=True)}",
                     sum(1 for d in kept_d if d[2] is None),
                     len(lp), f"{len(background)} in {before_days} d ({fmt(rate, 1)}/yr)"])
    rep.table(["Household", "Array", "Clear sessions", "…found", "Hidden sessions", "…found",
               "Runs published with no class", "low_power runs",
               "Before the car arrived"], rows)
    rep.table(["Measure", "Result", "Target", ""], [
        ["recall, sessions the meter saw whole (no target)",
         f"{clear_hit} of {clear_total} = {fmt(share(clear_hit, clear_total), pct=True)}",
         "–", ""],
        ["recall, sessions the panels hid (no target)",
         f"{hid_hit} of {hid_total} = {fmt(share(hid_hit, hid_total), pct=True)}", "–", ""],
        ["low_power claimed on a run the panels hid", low_on_hidden, "0",
         verdict(low_on_hidden == TARGETS["low_power_claims_on_masked_runs"])],
    ])
    rep.add("Read the two recalls as a pair, not as a pass and a fail. **The clear one is the "
            "claim**: a solar household's night is as readable as anyone else's, so it should "
            "sit near §1's headline recall, and if it does not then `is_daylight_shoulder` is "
            "reaching into the dark and damaging detection that worked. **The hidden one has "
            "no target and never will.** When the array covers the whole draw the import "
            "register reads 0 for the length of the charge: there is no excess above the "
            "day's floor, so there is no run to find and no class to withhold -- the session "
            "is missing from `mart_charging_session` outright. That is the meter's limit, not "
            "the ruleset's, and the honest report of it is a number with no line under it.", "",
            "The last column is each host's own-load background. SOLAR_PLAN's hosts are "
            f"screened at MAX_SOLAR_EV_SHAPED_SHARE = {planted.MAX_SOLAR_EV_SHAPED_SHARE:g} of "
            "assessed days rather than PLAN's "
            f"{planted.MAX_EV_SHAPED_SHARE:g} -- only eight households in the cohort have "
            "panels, and the strict bound leaves two of them for three places -- so these "
            "three carry a little more label noise than the eight above, and it is reported "
            "here rather than assumed away.", "")
    rep.metric("sessions", "solar_recall_clear", share(clear_hit, clear_total))
    rep.metric("sessions", "solar_recall_hidden", share(hid_hit, hid_total))
    rep.metric("sessions", "solar_low_power_runs", low_power_total)
    rep.metric("sessions", "low_power_claims_on_masked_runs", low_on_hidden, "0",
               low_on_hidden == TARGETS["low_power_claims_on_masked_runs"])


def measure_daily_bounds(rep: Report, households: list[Household], daily: dict,
                         excluded: dict) -> dict:
    rep.add("## 2. Daily bounds", "",
            "A planted day is a date with planted kWh on it, split by half-hour. It is inside "
            f"when low − {BOUNDS_TOLERANCE_KWH} ≤ planted ≤ high + {BOUNDS_TOLERANCE_KWH}, "
            "against the EV bounds for an EV and the timed-night bounds for hot water.", "")
    rows = []
    inside_all = counted_all = 0
    by_kind: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    example = None
    distances: list[tuple[float, float]] = []   # (kWh outside the band, planted kWh)
    spans: list[float] = []
    hh_diff: list[int] = []                     # published half-hours − planted ones
    for h in households:
        days = h.daily_kwh()
        planted_hh = h.daily_half_hours()
        rows_by_day = daily.get(h.icp_id, {})
        x = excluded.get(h.icp_number, set())
        inside = counted = not_assessed = altered = below = above = 0
        for day, kwh in sorted(days.items()):
            if day in x:
                altered += 1
                continue
            r = rows_by_day.get(day)
            if r is None:
                not_assessed += 1
                continue
            low, high = ((r["ev_low"], r["ev_high"]) if h.is_ev
                         else (r["tn_low"], r["tn_high"]))
            counted += 1
            distances.append((outside_by(kwh, low, high), kwh))
            spans.append(high - low)
            hh_diff.append(((r["ev_hh"] if h.is_ev else r["tn_hh"]) or 0)
                           - planted_hh.get(day, 0))
            if in_bounds(kwh, low, high):
                inside += 1
                if example is None and h.kind == "ev_wallbox_7kw" and r["ev_hh"]:
                    example = (h.label, day, kwh, low, high, r["ev_hh"])
            elif kwh < low:
                below += 1
            else:
                above += 1
        inside_all += inside
        counted_all += counted
        by_kind[h.kind][0] += inside
        by_kind[h.kind][1] += counted
        rows.append([h.label, len(days), altered, not_assessed, counted,
                     f"{inside} ({fmt(share(inside, counted), pct=True)})", below, above])
    rep.table(["Household", "Planted days", "Altered after planting", "Not assessed",
               "Counted", "Inside", "Below low", "Above high"], rows)
    overall = share(inside_all, counted_all)
    ok = None if overall is None else overall >= TARGETS["daily_bounds_share"]
    rep.table(["Measure", "Result", "Target", ""], [
        ["planted days inside [low, high]", f"{inside_all} of {counted_all} = {fmt(overall, pct=True)}",
         "≥ 90%", verdict(ok)],
        *[[f"…{kind}", f"{v[0]} of {v[1]} = {fmt(share(*v), pct=True)}", "–", ""]
          for kind, v in sorted(by_kind.items())],
    ])
    rep.metric("daily_bounds", "share_inside", overall, "0.90", ok)
    for kind, v in sorted(by_kind.items()):
        rep.metric("daily_bounds", f"share_inside_{kind}", share(*v))

    if distances:
        n = len(distances)
        rep.add("", "Inside or outside is a hard edge on a band that is often under a "
                "kilowatt-hour wide, so here is how far outside the days fall (reported, "
                "no target). The distance is 0 for a day inside the band.", "")
        rep.table(["Outside the band by at most", "Days", "Share"],
                  [[f"{tol:.2f} kWh",
                    sum(1 for d, _ in distances if d <= tol),
                    fmt(share(sum(1 for d, _ in distances if d <= tol), n), pct=True)]
                   for tol in (0.10, 0.25, 0.50, 1.00)])
        rep.table(["…or, against the day's own planted kWh, at most", "Days", "Share"],
                  [[f"{pct:.0%} of it",
                    sum(1 for d, k in distances if k and d / k <= pct),
                    fmt(share(sum(1 for d, k in distances if k and d / k <= pct), n), pct=True)]
                   for pct in (0.02, 0.05, 0.10, 0.20)])
        outside = [d / k for d, k in distances if d > 0 and k]
        rep.add(f"Median band width: {median(spans):.2f} kWh. For the days outside it, the "
                f"median miss is {median([d for d, _ in distances if d > 0]):.2f} kWh, "
                f"{fmt(median(outside), pct=True)} of the day's planted kWh. Published "
                f"half-hours minus planted half-hours: median {median(hh_diff):.1f}, mean "
                f"{sum(hh_diff) / len(hh_diff):.2f} — the model finds the right half-hours, "
                "and the low bound sits a little above the planted kWh in them.", "")
        rep.metric("daily_bounds", "share_within_0.50_kwh",
                   share(sum(1 for d, _ in distances if d <= 0.50), n))
        rep.metric("daily_bounds", "share_within_10pct_of_the_day",
                   share(sum(1 for d, k in distances if k and d / k <= 0.10), n))
        rep.metric("daily_bounds", "median_published_minus_planted_half_hours",
                   median(hh_diff))
    return {"example": example}


def measure_night_timer(rep: Report, households: list[Household], silver: dict,
                        assessed: dict, excluded: dict) -> None:
    rep.add("## 3. Night timer", "",
            f"A planted night is detected when at least {NIGHT_MIN_CLASSED_SHARE:.0%} of its "
            "half-hours are classed `timed_night` in silver.", "")
    rows = []
    hit_all = total_all = 0
    for h in households:
        if h.kind != "hot_water_3kw":
            continue
        classes = silver.get(h.icp_number, {})
        a, x = assessed.get(h.icp_id, set()), excluded.get(h.icp_number, set())
        hit = total = as_ev = 0
        for s, e, _ in h.sessions:
            if not eligible_days(a, x, s, e):
                continue
            total += 1
            slots = range(s, e, HALF_HOUR_S)
            timed = sum(1 for t in slots if classes.get(t) == "timed_night")
            if timed >= NIGHT_MIN_CLASSED_SHARE * len(slots):
                hit += 1
            elif sum(1 for t in slots if classes.get(t) == "ev") >= NIGHT_MIN_CLASSED_SHARE * len(slots):
                as_ev += 1
        hit_all += hit
        total_all += total
        rows.append([h.label, total, f"{hit} ({fmt(share(hit, total), pct=True)})", as_ev])
    rep.table(["Household", "Nights counted", "Detected", "Read as EV instead"], rows)
    overall = share(hit_all, total_all)
    ok = None if overall is None else overall >= TARGETS["night_timer_share"]
    rep.table(["Measure", "Result", "Target", ""], [
        ["planted nights detected", f"{hit_all} of {total_all} = {fmt(overall, pct=True)}",
         "≥ 80%", verdict(ok)]])
    rep.metric("night_timer", "share_detected", overall, "0.80", ok)


def measure_unlabelled(rep: Report, cohort_icps: dict, planted_ids: set[int], solar_ids: set[int],
                       detected: dict, daily: dict) -> None:
    rep.add("## 4. Households with nothing planted (unlabelled)", "",
            "LCL households have loads of their own, so what is found here is reported, "
            "not scored.", "")
    rows = []
    # ⚠️ BOTH GROUPS EXCLUDE planted_ids. Three of the generating households now
    # have a planted car, and leaving them in would put sessions with a known
    # answer into the group that exists to report what is found where there is
    # none -- inflating "solar" and making the two groups' rates incomparable.
    for group, ids in (("no solar", [i for i in cohort_icps if i not in planted_ids | solar_ids]),
                       ("solar", [i for i in cohort_icps
                                  if i in solar_ids and i not in planted_ids])):
        days = sum(len(daily.get(i, {})) for i in ids)
        hp = sum(1 for i in ids for d in detected.get(i, []) if d[2] == "high_power")
        lp = sum(1 for i in ids for d in detected.get(i, []) if d[2] == "low_power")
        with_ev = sum(1 for i in ids if detected.get(i))
        with_tn = sum(1 for i in ids if any(r["tn_hh"] for r in daily.get(i, {}).values()))
        years = days / 365.25
        rows.append([group, len(ids), fmt(years, 1), with_ev,
                     fmt(hp / years if years else None, 1), fmt(lp / years if years else None, 1),
                     with_tn])
        rep.metric("unlabelled", f"households_with_ev_sessions_{group.replace(' ', '_')}", with_ev)
        rep.metric("unlabelled", f"households_with_timed_night_{group.replace(' ', '_')}", with_tn)
    rep.table(["Group", "Households", "Assessed household-years", "With an EV session",
               "high_power per household-year", "low_power per household-year",
               "With timed-night days"], rows)


def measure_fits(rep: Report, households: list[Household], cohort_icps: dict,
                 usage: dict, daily: dict, published: dict, before: dict) -> None:
    rep.add("## 5. Heating fit", "",
            "Refitted here with the model's rule (months of ≥ 20 complete days, ≥ 6 months, "
            "OLS) three ways for each planted household: on import as metered (**gross**), "
            "net of the published low bounds (**net**, what dbt fits), and net of the exact "
            "planted kWh (**oracle**, the fit the household would have had with nothing "
            "planted). The net fit works if it sits near the oracle.", "")
    mismatches = 0
    compared = 0
    fits_after = []
    for icp_id, icp_number in cohort_icps.items():
        pub = published.get(icp_number)
        if pub is not None and pub["r2"] is not None:
            fits_after.append(pub["r2"])
        days = usage.get(icp_id, [])
        dl = daily.get(icp_id, {})
        net = monthly_fit([(d, hdd, kwh - (dl[d]["ev_low"] + dl[d]["tn_low"] if d in dl else 0.0))
                           for d, hdd, kwh in days])
        if pub is not None and pub["slope"] is not None and net.slope is not None:
            compared += 1
            if (abs(net.slope - pub["slope"]) > REPLICATION_TOLERANCE
                    or abs(net.r2 - pub["r2"]) > REPLICATION_TOLERANCE):
                mismatches += 1
    rows = []
    for h in households:
        days = usage.get(h.icp_id, [])
        dl = daily.get(h.icp_id, {})
        planted_by_day = h.daily_kwh()
        gross = monthly_fit(days)
        net = monthly_fit([(d, hdd, kwh - (dl[d]["ev_low"] + dl[d]["tn_low"] if d in dl else 0.0))
                           for d, hdd, kwh in days])
        oracle = monthly_fit([(d, hdd, kwh - planted_by_day.get(d, 0.0)) for d, hdd, kwh in days])

        def cell(f: Fit) -> str:
            if f.slope is None:
                return f"no fit ({f.months} months)"
            return f"{f.slope:.3f} / R² {f.r2:.2f}" + ("" if f.usable else " (under 0.30)")
        rows.append([h.label, cell(gross), cell(net), cell(oracle),
                     fmt(None if net.slope is None or oracle.slope is None
                         else net.slope - oracle.slope, 3)])
        if net.slope is not None and oracle.slope is not None:
            rep.metric("heating_fit", f"net_minus_oracle_slope[{h.label}]",
                       round(net.slope - oracle.slope, 4))
    rep.table(["Household", "Gross slope / R²", "Net (published rule)", "Oracle",
               "Net − oracle slope"], rows)
    usable_after = sum(1 for r in fits_after if r >= FIT_MIN_R2)
    rep.table(["Cohort", "Before (18 Sep, recorded)", "After"], [
        ["ICPs with a fit", before.get("warehouse_fits", "–"), len(fits_after)],
        ["…with R² ≥ 0.30", before.get("warehouse_fits_r2_ok", "–"), usable_after],
        ["median R²", before.get("warehouse_median_r2", "–"), fmt(median(fits_after), 4)],
    ])
    rep.add(f"Replication: the net refit here matches the published silver fit on "
            f"{compared - mismatches} of {compared} ICPs (tolerance {REPLICATION_TOLERANCE}).", "")
    rep.metric("heating_fit", "replication_mismatches", mismatches, "0", mismatches == 0)
    rep.metric("heating_fit", "icps_with_fit", len(fits_after))
    rep.metric("heating_fit", "icps_with_usable_fit", usable_after)
    rep.metric("heating_fit", "median_r2", median(fits_after))


def measure_bill_step(rep: Report, households: list[Household], explanations: dict,
                      daily: dict) -> dict:
    rep.add("## 6. Bill driver", "",
            "For each EV household, the explained bill whose period contains its first planted "
            "session, against its prior bill. Detected step = (this − prior) × period days, from "
            "the mart's own per-day means. Planted step = the same arithmetic on the planted kWh "
            "over the same assessed days.", "")
    rows = []
    ratios = []
    example = None
    for h in households:
        if not h.is_ev:
            continue
        first = h.first_day
        bill = next((b for b in explanations.get(h.account_id, [])
                     if b["period_start"] <= first <= b["period_end"]
                     and b["prior_period_start"] is not None), None)
        if bill is None:
            rows.append([h.label, "no bill pair", "–", "–", "–", ""])
            if h.kind == "ev_wallbox_7kw":
                ratios.append(None)
            continue
        dl = daily.get(h.icp_id, {})
        planted_by_day = h.daily_kwh()

        def planted_mean(lo: dt.date, hi: dt.date) -> float | None:
            days = [d for d in dl if lo <= d <= hi]
            return (sum(planted_by_day.get(d, 0.0) for d in days) / len(days)) if days else None
        p_this = planted_mean(bill["period_start"], bill["period_end"])
        p_prior = planted_mean(bill["prior_period_start"], bill["prior_period_end"])
        d_this, d_prior = bill["det_this"], bill["det_prior"]
        planted_step = (None if p_this is None or p_prior is None
                        else (p_this - p_prior) * bill["period_days"])
        detected_step = (None if d_this is None or d_prior is None
                         else (d_this - d_prior) * bill["period_days"])
        ratio = (None if planted_step in (None, 0) or detected_step is None
                 else detected_step / planted_step)
        if h.kind == "ev_wallbox_7kw":
            ratios.append(ratio)
        ok = None if ratio is None else ratio >= TARGETS["bill_step_ratio"]
        rows.append([h.label, f"{(first - bill['period_start']).days + 1} of {bill['period_days']}",
                     fmt(planted_step, 1), fmt(detected_step, 1), fmt(ratio, 0, pct=True),
                     verdict(ok) if h.kind == "ev_wallbox_7kw" else "(no target)"])
        rep.metric("bill_driver", f"step_ratio[{h.label}]", ratio,
                   "0.70" if h.kind == "ev_wallbox_7kw" else "", ok if h.kind == "ev_wallbox_7kw" else None)
        if (example is None and h.kind == "ev_wallbox_7kw" and detected_step is not None
                and bill["driver"] is not None and bill["prior_fixed"] is not None):
            example = dict(bill, label=h.label, planted_step=planted_step,
                           detected_step=detected_step)
    rep.table(["Household", "EV's first day in the period", "Planted step kWh",
               "Detected step kWh", "Detected ÷ planted", "≥ 70%"], rows)
    all_ok = bool(ratios) and all(r is not None and r >= TARGETS["bill_step_ratio"] for r in ratios)
    rep.metric("bill_driver", "all_wallboxes_at_least_70pct", all_ok, "true", all_ok)
    return {"example": example}


def measure_forecasts(rep: Report, households: list[Household], forecasts: list[tuple]) -> None:
    rep.add("## 7. Forecast `hdd-linear-v3` (reported, no target)", "",
            "Absolute percentage error of `projected_kwh` against the cycle's import, summed "
            "from daily usage where every day of the cycle is complete. For EV households, "
            "only cycles starting after the EV's first session.", "")
    ev_from = {h.account_id: h.first_day for h in households if h.is_ev}
    hot_water = {h.account_id for h in households if h.kind == "hot_water_3kw"}
    groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    models = defaultdict(int)
    for account_id, period_start, days_elapsed, projected, truth, model in forecasts:
        models[model] += 1
        if truth is None or truth <= 0:
            continue
        if account_id in ev_from:
            if period_start <= ev_from[account_id]:
                continue
            group = "EV households, after arrival"
        elif account_id in hot_water:
            group = "hot-water households"
        else:
            group = "nothing planted"
        ape = abs(projected - truth) / truth
        groups[group]["all"].append(ape)
        groups[group]["days 1–14" if days_elapsed < 15 else "day 15 on"].append(ape)
    rep.table(["Group", "Forecasts scored", "Median APE", "…days 1–14", "…day 15 on"],
              [[g, len(v["all"]), fmt(median(v["all"]), 1, pct=True),
                fmt(median(v["days 1–14"]), 1, pct=True), fmt(median(v["day 15 on"]), 1, pct=True)]
               for g, v in sorted(groups.items())])
    rep.add("Rows by model version: " + ", ".join(f"`{m}` {n:,}" for m, n in sorted(models.items(), key=str))
            + ".", "")
    for g, v in groups.items():
        rep.metric("forecast", f"median_ape[{g}]", median(v["all"]))
    only_v3 = set(models) == {"hdd-linear-v3"}
    rep.metric("forecast", "only_v3_rows", only_v3, "true", only_v3)


# ---------------------------------------------------------------- hardship

REPLAY_BILL_SQL = f"""
select b.account_id, b.bill_id, b.due_date, b.total_charges,
       coalesce(sum(p.amount) filter (where not p.is_failed and p.paid_at <
           ((%(as_of)s::date + 1)::timestamp at time zone '{NZ}')), 0),
       max((p.paid_at at time zone '{NZ}')::date)
           filter (where not p.is_failed
                   and p.paid_at < ((%(as_of)s::date + 1)::timestamp at time zone '{NZ}'))
from billing.bill b
left join billing.payment p on p.bill_id = b.bill_id
where b.due_date >= %(observed_from)s::date
  and b.due_date <= %(as_of)s::date
  and b.status <> 'cancelled'
  and b.total_charges > 0
group by b.account_id, b.bill_id, b.due_date, b.total_charges
order by b.account_id, b.due_date, b.bill_id
"""


def replay_bills(H, cur, *, observed_from: dt.date, as_of: dt.date) -> dict:
    """build_hardship_assessments._load_bills, with paid-by-date in place of
    payments_applied."""
    cur.execute(REPLAY_BILL_SQL, {"observed_from": observed_from, "as_of": as_of})
    out: dict[int, list] = {}
    for account_id, bill_id, due_on, amount, paid, latest in cur.fetchall():
        out.setdefault(int(account_id), []).append(H.BillEvidence(
            bill_id=int(bill_id), due_on=due_on, amount_due=Decimal(str(amount)),
            payments_applied=Decimal(str(paid)), latest_successful_payment_on=latest))
    return out


def is_early(codes: set[str], account_id: int, as_of: dt.date) -> bool:
    """The alert job's own early-outreach rule, on one assessment's codes."""
    import create_hardship_alerts as alerts
    return alerts.Candidate(0, account_id, as_of, frozenset(codes),
                            already_alerted=False, has_open_outreach=False).is_early


def replay_usage_to(latest_meter_on: dt.date | None, as_of: dt.date,
                    max_stale_days: int) -> dt.date | None:
    """build_hardship_assessments.main's usage window end, at a past date."""
    usage_to = latest_meter_on
    if usage_to is not None and usage_to > as_of:
        usage_to = as_of
    if usage_to is not None and (as_of - usage_to).days > max_stale_days:
        usage_to = None
    return usage_to


ON_TIME_SQL = f"""
with horizon as (
    select max(b.period_end) - 5 as cutoff
    from billing.bill b join billing.account a using (account_id)
    join metering.icp i on i.icp_id = a.icp_id
    where i.icp_number like '09%%'
), per_bill as (
    select b.account_id, b.total_charges, b.due_date,
           coalesce(sum(p.amount) filter (where not p.is_failed), 0) as paid,
           max((p.paid_at at time zone '{NZ}')::date) filter (where not p.is_failed) as last_paid
    from billing.bill b
    left join billing.payment p on p.bill_id = b.bill_id
    cross join horizon h
    where b.status <> 'cancelled' and b.total_charges > 0 and b.due_date <= h.cutoff
    group by b.account_id, b.bill_id, b.total_charges, b.due_date
)
select a.account_id
from billing.account a
join metering.icp i on i.icp_id = a.icp_id
where a.end_date is null and i.icp_number like '09%%'
  and exists (select 1 from per_bill pb where pb.account_id = a.account_id)
  and not exists (select 1 from billing.payment p
                  where p.account_id = a.account_id and p.is_failed)
  and not exists (select 1 from per_bill pb
                  where pb.account_id = a.account_id
                    and (pb.paid < pb.total_charges - 0.01
                         or pb.last_paid is null or pb.last_paid > pb.due_date))
"""


def measure_hardship(rep: Report, cur, sliders: list[dict]) -> None:
    import build_hardship_assessments as H

    rep.add("## 8. Hardship early warning (weekly replay)", "")
    cur.execute("""
        select count(*) filter (where abs(b.payments_applied - coalesce(p.paid, 0)) > 0.01),
               count(*)
        from billing.bill b
        left join (select bill_id, sum(amount) as paid from billing.payment
                   where not is_failed group by bill_id) p using (bill_id)
        where b.total_charges > 0 and b.status <> 'cancelled'""")
    differ, bills = cur.fetchone()
    latest_meter_on, latest_payment_on = H._load_anchor_dates(cur)
    end = max(d for d in (latest_meter_on, latest_payment_on) if d is not None)
    cur.execute("""select min(b.issue_date) from billing.bill b
                   join billing.account a using (account_id)
                   join metering.icp i on i.icp_id = a.icp_id
                   where i.icp_number like '09%%'""")
    start = cur.fetchone()[0] + dt.timedelta(days=REPLAY_FIRST_AFTER_DAYS)
    accounts = H._load_accounts(cur, None)
    by_number = {a.account_number: a.account_id for a in accounts}
    cur.execute(ON_TIME_SQL)
    slider_ids = {by_number.get(s["acct_no"]) for s in sliders}
    on_time = {r[0] for r in cur.fetchall()} - slider_ids

    weeks: list[dt.date] = []
    day = start
    while day <= end:
        weeks.append(day)
        day += dt.timedelta(days=REPLAY_STEP_DAYS)
    early: dict[int, list[bool]] = defaultdict(list)
    first_early: dict[int, dt.date] = {}
    first_v1: dict[int, dt.date] = {}
    first_outlook: dict[int, tuple[dt.date, dt.date]] = {}
    v1_on_time: set[int] = set()
    bad_outlooks = 0
    for as_of in weeks:
        observed_from = as_of - dt.timedelta(days=H.OBSERVATION_DAYS - 1)
        usage_to = replay_usage_to(latest_meter_on, as_of, H.MAX_METER_STALENESS_DAYS)
        usage = ({} if usage_to is None else H._load_daily_usage(
            cur, usage_from=usage_to - dt.timedelta(days=H.USAGE_DAYS - 1), usage_to=usage_to))
        assessments = H.build_assessments(
            accounts=accounts, as_of=as_of, observed_from=observed_from,
            bills_by_account=replay_bills(H, cur, observed_from=observed_from, as_of=as_of),
            failed_dd_by_account=H._load_failed_direct_debits(
                cur, observed_from=observed_from, as_of=as_of),
            history_by_account=H._load_payment_history(cur, as_of=as_of),
            daily_usage_by_account=usage, usage_to=usage_to,
            recent_bills_by_account=H._load_recent_bills(
                cur, history_from=as_of - dt.timedelta(days=H.EARLY_WARNING_HISTORY_DAYS),
                as_of=as_of),
            forecast_by_account=H._load_open_forecasts(cur, as_of=as_of))
        for a in assessments:
            aid = a.account.account_id
            codes = {s.code for s in a.signals}
            e = is_early(codes, aid, as_of)
            early[aid].append(e)
            if e:
                first_early.setdefault(aid, as_of)
            if H.has_v1_signal(a):
                first_v1.setdefault(aid, as_of)
                if aid in on_time:
                    v1_on_time.add(aid)
            if a.outlook is not None:
                if a.outlook.projected_arrears_on <= as_of:
                    bad_outlooks += 1
                first_outlook.setdefault(aid, (as_of, a.outlook.projected_arrears_on))

    rows = []
    leads = []
    caught = gradual = 0
    for n, s in enumerate(sliders, start=1):
        aid = by_number.get(s["acct_no"])
        onset = s["onset_on"]
        fe, fv = first_early.get(aid), first_v1.get(aid)
        early_before = fe is not None and onset is not None and fe < onset
        lead = (onset - fe).days if early_before else None
        v1_lead = (onset - fv).days if fv is not None and onset is not None and fv < onset else None
        ol = first_outlook.get(aid)
        if s["profile"] == "SLIDING":
            gradual += 1
            if early_before:
                caught += 1
                leads.append(lead)
        rows.append([f"{s['profile'].lower()} {n}", onset or "–",
                     fe or "never", fmt(lead) if lead is not None else "–",
                     fv or "never", fmt(v1_lead) if v1_lead is not None else "–",
                     f"{ol[0]} → {ol[1]}" if ol else "–"])
    rep.table(["Account", "Arrears onset (planted)", "First early outreach",
               "Days before onset", "First v1 signal", "v1 days before onset",
               "First outlook (made → projected onset)"], rows)
    med = median(leads)
    account_years = len(on_time) * len(weeks) * REPLAY_STEP_DAYS / 365.25
    alarms = sum(episodes(early[aid]) for aid in on_time)
    rate = alarms / account_years * 100 if account_years else None
    ok_lead = None if med is None else med >= TARGETS["hardship_median_lead_days"]
    ok_caught = caught >= TARGETS["hardship_gradual_caught"]
    ok_rate = None if rate is None else rate <= TARGETS["false_alarms_per_100_account_years"]
    rep.table(["Measure", "Result", "Target", ""], [
        ["median days of warning (gradual accounts caught)", fmt(med, 0), "≥ 21",
         verdict(ok_lead)],
        ["gradual accounts caught before onset", f"{caught} of {gradual}", "≥ 2 of 3",
         verdict(ok_caught)],
        ["early outreaches per 100 on-time account-years",
         f"{alarms} in {account_years:.1f} = {fmt(rate, 2)}", "≤ 5", verdict(ok_rate)],
    ])
    rep.add(f"Replay: {len(weeks)} weekly dates from {weeks[0]} to {weeks[-1]}, "
            f"{len(accounts)} open cohort accounts. On-time accounts ({len(on_time)}): not "
            "planted, no failed payment, and every bill due by the generator's payment horizon "
            "paid in full on or before its due date. An alarm is a run of consecutive weeks "
            "with the early combination; the job itself keeps one outreach open per account.",
            "",
            f"{len(v1_on_time)} on-time accounts ever had a v1 signal in the replay. "
            f"Outlooks projecting arrears on or before their own date: {bad_outlooks}. "
            f"`payments_applied` differs from the sum of successful payments on {differ} of "
            f"{bills} bills, which is why the replay sums payments by date.", "",
            "SLIDING_SUDDEN stops paying with no warning sign first, so a miss there is expected.",
            "")
    rep.metric("hardship", "median_lead_days", med, "21", ok_lead)
    rep.metric("hardship", "gradual_caught", caught, "2", ok_caught)
    rep.metric("hardship", "false_alarms_per_100_account_years", rate, "5", ok_rate)
    rep.metric("hardship", "outlooks_not_after_their_date", bad_outlooks, "0", bad_outlooks == 0)
    rep.metric("hardship", "on_time_accounts", len(on_time))
    rep.metric("hardship", "on_time_with_v1_signal", len(v1_on_time))
    rep.metric("hardship", "bills_where_payments_applied_differs", differ)


# ---------------------------------------------------------------- solar

def solar_arrays(households: list[Household] | None = None) -> dict[str, float]:
    """ICP number -> the household's array in kWp.

    cohort.json sizes a roof to the load the household had before any planted
    appliance. The three it also gave a car to sized theirs for the car, and
    planted.SOLAR_PLAN declares that size; reading the cohort alone would
    check a 8 kWp roof against 3 kWp of modelled generation and report the
    household as exporting three times what it should.
    """
    spec = json.loads(COHORT_JSON.read_text())
    arrays = {h["icp"]: h["pv_kwp"] for h in spec["households"] if h["has_solar"]}
    for h in households or ():
        if h.kind == "ev_solar_wallbox_7kw":
            arrays[h.icp_number] = planted.SOLAR_ARRAY_KWP[h.pattern]
    return arrays


def measure_solar(rep: Report, cur, src_months: dict[str, set[str]], cohort_icps: dict,
                  households: list[Household] | None = None) -> None:
    import build_solar_estimates as S
    import estimate_common as common
    import gen_metering as G

    arrays = solar_arrays(households)
    span = (f"{min(arrays.values()):g}–{max(arrays.values()):g} kWp" if arrays else "n/a")
    rep.add("## 9. Solar sanity check (no known answer)", "",
            "For the real solar households: the generator's own panels have a known output, so "
            "each half-hour's load is import − export + that output. Each household is checked "
            "against ITS OWN array rather than one size for all "
            f"({span}, sized to load by `cohort.py`, or to the car by "
            "`planted.SOLAR_PLAN` for the three households that have one), "
            "and the model's exported share of "
            "generation is set beside the metered one. The two use different sun shapes and "
            "yields, so they should be close, not equal.", "")
    zone = _zone()
    yields = S.load_yields()
    region_map = S.load_region_map()
    window = common.usage_window(cur)
    rows = []
    gaps = []
    n = 0
    for acct in common.load_accounts(cur):
        if not acct["has_export"] or acct["icp_id"] not in cohort_icps:
            continue
        n += 1
        icp_number = cohort_icps.get(acct["icp_id"], "")
        skip_months = src_months.get(icp_number, set())
        kwp = arrays.get(icp_number)
        region = S.weather_region(acct["climate_region"], region_map, yields)
        hhs = [h for h in common.load_half_hours(cur, acct["icp_id"], window)
               if h.loc.strftime("%Y-%m") not in skip_months]
        if region is None or not hhs or kwp is None:
            rows.append([f"solar {n}", "–", "no region, no reads or no array",
                         "–", "–", "–"])
            continue
        gen_total = exp_total = 0.0
        loads = []
        for h in hhs:
            local = h.at.astimezone(zone)
            g = G.pv_half_hour(local, G.pv_daily_kwh(local.date(), kwp))
            gen_total += g
            exp_total += h.export_kwh
            loads.append(common.HalfHour(h.at, h.loc, h.utc_offset_min,
                                         max(0.0, h.import_kwh - h.export_kwh + g), 0.0))
        metered = exp_total / gen_total if gen_total else None
        shares = S.solar_shares(loads, yields[region]["lat"], yields[region]["lon"])
        model = {}
        for bound in ("low", "high"):
            _, generated, used = S.simulate(loads, shares, yields[region][bound], kwp)
            model[bound] = (1 - used / generated) if generated else None
        gaps.append(None if metered is None or model["low"] is None
                    else (model["low"] + model["high"]) / 2 - metered)
        left_out = f" ({', '.join(sorted(skip_months))} left out)" if skip_months else ""
        rows.append([f"solar {n}", f"{kwp:g} kWp", f"{len(hhs):,}{left_out}",
                     fmt(metered, 1, pct=True),
                     f"{fmt(model['low'], 1, pct=True)} – {fmt(model['high'], 1, pct=True)}",
                     fmt(gaps[-1] * 100 if gaps[-1] is not None else None, 1)])
    rep.table(["Household", "Array", "Half-hours", "Metered export share",
               "Model export share (low – high yield)", "Model mid − metered (points)"], rows)
    rep.metric("solar", "median_gap_points", None if median(gaps) is None else median(gaps) * 100)


def measure_solar_soak(rep: Report, cur) -> None:
    """§9's second half: the recommendation a household with both actually wants.

    NO TARGET, and the reason is worth stating. There is ground truth for the
    kWh -- gen_audit knows what was planted -- but none at all for the money:
    what a household saves by charging into its own generation depends on its
    plan, its roof and its habits, and nothing in the generator decides any of
    those. A number with a line under it here would be a target invented to be
    met. So the rows are reported, including the losing ones.
    """
    rep.add("### 9a. Charging into your own sun (`solar_soak`)", "",
            "`build_ev_scenarios.py` offers this pattern only to an account whose basis "
            "window actually exported, and prices it with the same `charge_half_hour()` as "
            "every other pattern -- which already charges from export before import. Each "
            "account is shown on ITS CURRENT PLAN (`plan_id = current_plan_id`), because "
            "that is the comparison a customer is being offered.", "")
    cur.execute("""
        select e.scenario, e.charging_pattern, e.account_id,
               e.annual_ev_kwh, e.saving_vs_baseline_nzd
        from kea.ev_scenario e
        join billing.account a on a.account_id = e.account_id
        join metering.icp i on i.icp_id = a.icp_id and i.icp_number like '09%%'
        where e.computed_at = (select max(computed_at) from kea.ev_scenario)
          and e.plan_id = e.current_plan_id""")
    by: dict[tuple[str, int], dict[str, tuple[float, float]]] = defaultdict(dict)
    for scenario, pattern, account_id, kwh, saving in cur.fetchall():
        by[(scenario, int(account_id))][pattern] = (float(kwh), float(saving))
    rows = []
    for scenario in ("add_ev", "shift_detected"):
        accounts = sorted(a for (sc, a) in by if sc == scenario and "solar_soak" in by[(sc, a)])
        soaks = [by[(scenario, a)]["solar_soak"] for a in accounts]
        nights = [by[(scenario, a)].get("night") for a in accounts]
        beats = sum(1 for soak, night in zip(soaks, nights)
                    if night is not None and soak[1] > night[1])
        losses = sum(1 for soak in soaks if soak[1] < 0)
        rows.append([scenario, len(accounts),
                     fmt(median([k for k, _ in soaks]), 0),
                     fmt(median([v for _, v in soaks]), 2),
                     fmt(median([n[1] for n in nights if n is not None]), 2),
                     f"{beats} of {len(accounts)}", losses])
        rep.metric("solar_soak", f"accounts_offered_{scenario}", len(accounts))
        rep.metric("solar_soak", f"median_saving_nzd_{scenario}", median([v for _, v in soaks]))
        rep.metric("solar_soak", f"beats_night_{scenario}", beats)
        rep.metric("solar_soak", f"negative_savings_{scenario}", losses)
    rep.table(["Scenario", "Accounts offered it", "Median annual EV kWh",
               "Median solar_soak saving", "Median night saving", "…where solar_soak wins",
               "…where it loses money"], rows)
    rep.add("⚠️ **A losing row is published, not suppressed.** On Time of Use the import a "
            "roof does not cover sits in the most expensive window of the day, and on a plan "
            "that buys export back above its own night rate, moving a charge off the night "
            "rate and onto the roof can cost more than it saves. When that is the answer, "
            "saying so is the point: the last column is a count of honest negatives, not of "
            "failures. `shift_detected` moves a detected session's kWh and drops none of "
            "them, so its kWh column matches the `as_detected` baseline by construction; "
            "`add_ev` charges the same annual kWh as `evening` does.", "")


def solar_hand_check(cur) -> list[str]:
    cur.execute("""
        select system_kw, install_cost_nzd_low, install_cost_nzd_high,
               annual_saving_nzd_low, annual_saving_nzd_high,
               payback_years_low, payback_years_high, assumptions_version
        from kea.solar_estimate
        where computed_at = (select max(computed_at) from kea.solar_estimate)
          and annual_saving_nzd_low > 0
        order by account_id, system_kw limit 1""")
    r = cur.fetchone()
    if r is None:
        return ["No solar estimate with a positive saving to work through."]
    kw, cl, ch, sl, sh, pl, ph, ver = r
    best, worst = round(cl / sh, 1), round(ch / sl, 1)
    return [
        f"One {kw} kW estimate (`{ver}`): install ${cl:,} – ${ch:,}, saving "
        f"${sl:,} – ${sh:,} a year.",
        f"Best case ${cl:,} ÷ ${sh:,} = {best} years; worst case ${ch:,} ÷ ${sl:,} = "
        f"{worst} years. Published: {pl} – {ph}. "
        + ("Match." if (best, worst) == (pl, ph) else "**Mismatch.**"),
    ]


# ---------------------------------------------------------------- main

def load_serving(cur, households: list[Household]) -> dict:
    cur.execute("select icp_id, icp_number from metering.icp where icp_number like '09%%'")
    cohort_icps = {int(i): n for i, n in cur.fetchall()}
    by_number = {n: i for i, n in cohort_icps.items()}
    cur.execute("""select distinct icp_id from metering.meter_register
                   where direction = 'export'""")
    solar_ids = {int(r[0]) for r in cur.fetchall()} & set(cohort_icps)
    cur.execute("""select a.icp_id, a.account_id from billing.account a
                   where a.end_date is null""")
    account_of = {int(i): int(a) for i, a in cur.fetchall()}
    for h in households:
        h.icp_id = by_number.get(h.icp_number)
        if h.icp_id is None:
            raise SystemExit(f"planted household '{h.label}' is not in serving")
        h.account_id = account_of.get(h.icp_id)
        # ⚠️ BOTH DIRECTIONS. planted.py fills PLAN from households without
        # panels and SOLAR_PLAN from households with them, so a host on the
        # wrong side of this line means the two pools have crossed and nothing
        # below is comparable with Sprint 4's figures.
        generates = h.icp_id in solar_ids
        if generates != (h.kind == "ev_solar_wallbox_7kw"):
            raise SystemExit(f"planted household '{h.label}' "
                             + ("has an export register"
                                if generates else "has no export register"))

    ids = list(cohort_icps)
    cur.execute("""
        select icp_id, usage_date, ev_kwh_low, ev_kwh_high, ev_half_hours,
               timed_night_kwh_low, timed_night_kwh_high, timed_night_half_hours,
               masked_intervals
        from kea.mart_daily_detected_load where icp_id = any(%s)""", (ids,))
    daily: dict[int, dict] = defaultdict(dict)
    # masked_intervals is NULL on a row built before detect-v2 and 0 on a day
    # the meter saw whole; the two are kept apart rather than coalesced, so a
    # stale publication shows up as a blank rather than as "nothing was hidden".
    for icp_id, day, el, eh, ehh, tl, th, thh, mi in cur.fetchall():
        daily[int(icp_id)][day] = {"ev_low": float(el), "ev_high": float(eh), "ev_hh": ehh,
                                   "tn_low": float(tl), "tn_high": float(th), "tn_hh": thh,
                                   "masked": None if mi is None else int(mi)}
    cur.execute("""
        select icp_id, session_start, session_end, charger_class, masked_half_hours
        from kea.mart_charging_session where icp_id = any(%s)""", (ids,))
    # (start, end, charger_class, masked_half_hours). ⚠️ charger_class is NULL
    # on a run the panels hid -- withheld, not absent: the row is still an EV
    # session, which is why the masking measures below count rows, not classes.
    detected: dict[int, list] = defaultdict(list)
    for icp_id, s, e, cls, mhh in cur.fetchall():
        detected[int(icp_id)].append((_ts(s), _ts(e), cls,
                                      None if mhh is None else int(mhh)))
    cur.execute("""
        select icp_id, usage_date, heating_degree_days, import_kwh
        from kea.mart_daily_usage
        where icp_id = any(%s) and heating_degree_days is not null
          and interval_count = expected_intervals
        order by icp_id, usage_date""", (ids,))
    usage: dict[int, list] = defaultdict(list)
    for icp_id, day, hdd, kwh in cur.fetchall():
        usage[int(icp_id)].append((day, float(hdd), float(kwh)))
    # The mart's bill ids are the billing tables' (both are the invoice id).
    # The prior bill's fixed charge is its per-day lines, as the model takes it.
    cur.execute("""
        select e.account_id, e.period_start, e.period_end, e.period_days,
               p.period_start, p.period_end,
               e.detected_load_kwh_day_this, e.detected_load_kwh_day_prior,
               e.delta_from_detected_loads, e.amount_delta, e.delta_unexplained,
               e.prior_total_amount, e.prior_total_kwh, e.prior_period_days,
               (select sum(l.amount) from billing.bill_line l
                where l.bill_id = e.prior_bill_id and l.unit = 'per_day')
        from kea.mart_bill_explanation e
        left join billing.bill p on p.bill_id = e.prior_bill_id
        where e.account_id = any(%s)
        order by e.account_id, e.period_start""",
                ([h.account_id for h in households if h.account_id is not None],))
    explanations: dict[int, list] = defaultdict(list)
    for (aid, ps, pe, days, qs, qe, dt_this, dt_prior, driver, delta, unexpl,
         q_amount, q_kwh, q_days, q_fixed) in cur.fetchall():
        explanations[int(aid)].append({
            "period_start": ps, "period_end": pe, "period_days": int(days),
            "prior_period_start": qs, "prior_period_end": qe,
            "det_this": None if dt_this is None else float(dt_this),
            "det_prior": None if dt_prior is None else float(dt_prior),
            "driver": driver, "amount_delta": delta, "unexplained": unexpl,
            "prior_amount": q_amount, "prior_kwh": q_kwh, "prior_days": q_days,
            "prior_fixed": q_fixed})
    # Truth: the cycle's import from daily usage, only where every day of the
    # cycle has a complete row -- the backtest's rule.
    cur.execute("""
        with cycles as (
            select distinct f.account_id, a.icp_id, f.period_start, f.period_end
            from kea.bill_forecast f
            join billing.account a on a.account_id = f.account_id
            join metering.icp i on i.icp_id = a.icp_id and i.icp_number like '09%%'
        ), truth as (
            select c.account_id, c.period_start, sum(u.import_kwh) as kwh
            from cycles c
            join kea.mart_daily_usage u
              on u.icp_id = c.icp_id
             and u.usage_date between c.period_start and c.period_end
             and u.interval_count = u.expected_intervals
            group by c.account_id, c.period_start, c.period_end
            having count(*) = c.period_end - c.period_start + 1
        )
        select f.account_id, f.period_start, f.days_elapsed, f.projected_kwh,
               t.kwh, f.model_version
        from kea.bill_forecast f
        join cycles c using (account_id, period_start, period_end)
        left join truth t using (account_id, period_start)""")
    forecasts = [(int(a), ps, de, float(p), None if t is None else float(t), m)
                 for a, ps, de, p, t, m in cur.fetchall()]
    return dict(cohort_icps=cohort_icps, solar_ids=solar_ids, daily=daily, detected=detected,
                usage=usage, explanations=explanations, forecasts=forecasts)


def load_silver(households: list[Household],
                sessions_by_icp: dict) -> tuple[dict, dict, dict]:
    """(half-hour classes, half-hours the panels hid, published fits) by ICP number.

    Timestamps come back as epoch seconds computed in Snowflake. Before they
    are trusted, every served EV session start must be a classed `ev`
    half-hour in silver: if the two disagree about time, nothing here is
    comparable.

    The hidden set is `is_masked or is_daylight_shoulder` -- exporting, so the
    import register carries nothing about load, or between the day's first and
    last export, so it understates load by an unknown amount. Both are
    MEASURED off the export register (design §15.2); neither is a modelled PV
    curve, and a household without panels contributes an empty set.
    """
    from export_warehouse_to_lake import _connect
    conn = _connect()
    try:
        cur = conn.cursor()
        numbers = [h.icp_number for h in households]
        marks = ", ".join(["%s"] * len(numbers))
        cur.execute(f"""
            select i.icp_number, date_part(epoch_second, h.interval_start), h.detected_class
            from KEA_SILVER.INT_METERING__DETECTED_HALF_HOURS h
            join KEA_GOLD.DIM_ICP i on i.icp_id = h.icp_id
            where i.icp_number in ({marks})""", numbers)
        classes: dict[str, dict[int, str]] = defaultdict(dict)
        for number, epoch, cls in cur.fetchall():
            classes[number][int(epoch)] = cls
        starts = [classes[h.icp_number].get(s) == "ev"
                  for h in households for s, *_ in sessions_by_icp.get(h.icp_id, [])]
        if not starts or sum(starts) < ALIGNMENT_MIN_SHARE * len(starts):
            raise SystemExit(f"{sum(starts)} of {len(starts)} served EV sessions start on a "
                             "silver `ev` half-hour: the two disagree about time")
        cur.execute(f"""
            select i.icp_number, date_part(epoch_second, h.interval_start)
            from KEA_SILVER.INT_METERING__IMPORT_HALF_HOURS h
            join KEA_GOLD.DIM_ICP i on i.icp_id = h.icp_id
            where i.icp_number in ({marks})
              and (h.is_masked or h.is_daylight_shoulder)""", numbers)
        hidden: dict[str, set[int]] = defaultdict(set)
        for number, epoch in cur.fetchall():
            hidden[number].add(int(epoch))
        cur.execute("""
            select i.icp_number, h.heating_sensitivity_kwh_per_hdd, h.fit_r2
            from KEA_SILVER.INT_METERING__HEATING_SENSITIVITY h
            join KEA_GOLD.DIM_ICP i on i.icp_id = h.icp_id
            where i.icp_number like '09%'""")
        fits = {n: {"slope": None if s is None else float(s), "r2": None if r is None else float(r)}
                for n, s, r in cur.fetchall()}
        return classes, hidden, fits
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--skip-hardship", action="store_true",
                    help="leave out the weekly replay (the slow part)")
    args = ap.parse_args(argv)

    from load_export_to_serving import _connect as connect_serving
    before = json.loads(BEFORE_JSON.read_text()) if BEFORE_JSON.exists() else {}

    src = connect_source()
    try:
        households, sliders = load_planted(src)
        excluded, excluded_info = load_excluded_days(src, [h.icp_number for h in households])
        solar_months = load_export_on_import_months(src)
    finally:
        src.rollback()
        src.close()
    if excluded_info["undated_defects_on_planted"]:
        raise SystemExit("a source defect with no date touches a planted household: "
                         "99_assert.sql should have stopped that")

    conn = connect_serving()
    conn.set_session(readonly=True)
    try:
        cur = conn.cursor()
        s = load_serving(cur, households)
        assessed = {i: set(days) for i, days in s["daily"].items()}
        silver, hidden, fits = load_silver(households, s["detected"])

        rep = Report()
        head = ["# Sprint 4 evaluation against the planted answers", "",
                f"Run {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC. Method and thresholds: "
                "`analysis/sprint4_evaluation/README.md`. Planted data proves the pipeline "
                "finds what the generator planted. It does not prove real households behave "
                "that way.", "",
                f"Planted households: {len(households)}. Days left out because the generator "
                f"altered them after planting: {sum(len(v) for v in excluded.values())} "
                f"({', '.join(f'{k} {v}' for k, v in sorted(excluded_info['reasons'].items())) or 'none'}).",
                ""]
        measure_sessions(rep, households, s["detected"], assessed, excluded, hidden)
        bounds = measure_daily_bounds(rep, households, s["daily"], excluded)
        measure_night_timer(rep, households, silver, assessed, excluded)
        measure_unlabelled(rep, s["cohort_icps"], {h.icp_id for h in households},
                           s["solar_ids"], s["detected"], s["daily"])
        measure_fits(rep, households, s["cohort_icps"], s["usage"], s["daily"], fits, before)
        bill = measure_bill_step(rep, households, s["explanations"], s["daily"])
        measure_forecasts(rep, households, s["forecasts"])
        if args.skip_hardship:
            rep.add("## 8. Hardship early warning", "", "Skipped (--skip-hardship).", "")
        else:
            measure_hardship(rep, cur, sliders)
        measure_solar(rep, cur, solar_months, s["cohort_icps"], households)
        measure_solar_soak(rep, cur)

        rep.add("## 10. Hand checks", "")
        ex = bounds["example"]
        if ex:
            rep.add(f"* **A planted wallbox day** ({ex[0]}, {ex[1]}): {ex[2]:.2f} kWh planted "
                    f"in the half-hours of that date; the mart publishes {ex[5]} EV half-hours "
                    f"and [{ex[3]:.2f}, {ex[4]:.2f}] kWh. Inside.")
        b = bill["example"]
        if b:
            rate = (float(b["prior_amount"]) - float(b["prior_fixed"])) / float(b["prior_kwh"])
            rep.add(f"* **A bill pair spanning the EV's arrival** ({b['label']}): "
                    f"({b['det_this']:.4f} − {b['det_prior']:.4f}) kWh/day × {b['period_days']} days "
                    f"= {b['detected_step']:.1f} kWh detected, against {b['planted_step']:.1f} kWh "
                    "planted.",
                    f"  The prior bill's variable rate = (${b['prior_amount']} − "
                    f"${b['prior_fixed']} fixed) ÷ {float(b['prior_kwh']):,.1f} kWh = "
                    f"${rate:.4f}/kWh, so the driver ≈ {b['detected_step']:.1f} × {rate:.4f} = "
                    f"${b['detected_step'] * rate:.2f}. Published "
                    f"`delta_from_detected_loads`: ${b['driver']} (the mart multiplies the "
                    f"unrounded means; the served ones have 4 decimals, so a few cents apart "
                    f"is expected), of an amount "
                    f"change of ${b['amount_delta']}; ${b['unexplained']} is left unexplained.")
        rep.add(*("* **A solar payback:** " + line if i == 0 else "  " + line
                  for i, line in enumerate(solar_hand_check(cur))))
        rep.add("")

        summary = ["## Summary", "",
                   "| Section | Measure | Value | Target | |", "|---|---|---|---|---|"]
        for m in rep.metrics:
            if m.passed is None:
                continue
            value = fmt(m.value, 3) if isinstance(m.value, float) else m.value
            summary.append(f"| {m.section} | {m.measure} | {value} | {m.target} | "
                           f"{verdict(m.passed)} |")
        rep.lines = head + summary + [""] + rep.lines
    finally:
        conn.rollback()
        conn.close()

    write_results(args.out, rep)
    misses = [m for m in rep.metrics if m.passed is False]
    print(f"evaluation: {len(rep.metrics)} figures, {len(misses)} missed a target; "
          f"report in {args.out / 'report.md'}")
    for m in misses:
        print(f"  MISS {m.section}.{m.measure} = {m.value} (target {m.target})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
