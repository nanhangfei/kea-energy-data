"""Backtest a kNN-quantile forecast (Botman et al. 2025) against the bill
forecast that ships. Offline and read-only: it writes reports and nothing else.

    python3 backtest_bill_forecast.py reproduce [--k 100] [--out DIR]
    python3 backtest_bill_forecast.py monthend  [--k 100] [--out DIR]
        [--variant seasonal] [--pool-sizes 10,25,50] [--grid]
        [--shipped-fit skip]

STAGE 1, `reproduce`. The paper's day-ahead experiment on our cohort: kNN
(k=100, week normalisation, Euclidean distance) against the paper's two
baselines, historical sampling and probabilistic persistence, scored by CRPS on
every complete household-day in the later half of the span. The question is
whether this implementation behaves like the paper's, so the report compares
% gains, not absolute CRPS.

STAGE 2, `monthend`. The rest of each billing cycle, from every snapshot in
kea.mart_forecast_input: kNN's whole-continuation ensemble against
build_bill_forecasts.project_forecast() (imported, not copied), replayed with
the heating fit refitted as at each date and, labelled look-ahead, as shipped.
It first rebuilds is_forecastable and stops if that differs from the export.
The gate is applied to held-out cycles. analysis/forecast_backtest/README.md
has all three stages and the gate; knn_quantile.py has the model.

STAGE 3, the dials above. Any of them turns the same month-end run into the
extras report: --variant seasonal keeps only neighbours from the same time of
year a year back and is judged against the gate again, --pool-sizes repeats
the run with smaller libraries, and --grid sweeps k and the normalisation
in-sample. Each is scored beside a baseline at the paper's setting on the
same export, because an export rewritten since Stage 2 makes that report's
figures incomparable. --shipped-fit skip is for such an export: Sprint 4
ships hdd-linear-v3, whose is_forecastable and heating fit are not v2's, so
the look-ahead comparison is dropped and only the as-at refit, computed here
from the reads, stands as the champion.

WHAT IT READS. Sealed Parquet under export/ in the lake, through
load_export_to_serving._read_export(), and never kea-db. Only the generated
cohort (icp_number LIKE '09%'), because the nine fixtures are hand-written
scenarios, not households. Only import registers, the rule mart_daily_usage
uses. Stage 2 also runs one read-only SELECT on KEA_GOLD.FCT_METER_READ: the
export keeps 13 months of half-hourly reads, and the weather v2 needs ends
before that window starts. The warehouse reads must equal the export's where
the two overlap, or the run stops.

WHAT IT WRITES. <out>/stage1_reproduce.md, <out>/stage2_monthend.md,
<out>/metrics.csv and, for Stage 3, <out>/stage3_extras.md and
<out>/stage3_metrics.csv: aggregates only, no ICP numbers, no per-household
rows and no neighbour traces.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
# The scripts were grouped by layer on 29-30 Sep 2026; the champion and the
# export reader moved with them, and this harness lost them until 2 Oct.
for _folder in ("decisions", "serving", "warehouse"):
    sys.path.insert(0, str(Path(__file__).resolve().parent / _folder))

import build_bill_forecasts as champion  # noqa: E402
import knn_quantile as kq  # noqa: E402
from kea_pipeline import lake, serving  # noqa: E402
from load_export_to_serving import _read_export  # noqa: E402

COHORT_PREFIX = "09"
NS_PER_MINUTE = 60_000_000_000
READ_TABLES = ("metering.icp", "metering.meter_register",
               "metering.meter_read_hh")
WAREHOUSE_READS = "KEA.KEA_GOLD.FCT_METER_READ"
MODELS = ("knn", "hs", "persistence")
LABELS = {"knn": "kNN, k={k}, week normalisation",
          "hs": "Historical sampling",
          "persistence": "Probabilistic persistence"}
# Table 1b, Low Carbon London, 363 households.
PAPER_CRPS = {"knn": 0.091197, "hs": 0.098104, "persistence": 0.125323}
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 125168          # the paper's article number


# ---------------------------------------------------------------------------
# Reading the export

@dataclass(frozen=True)
class Cohort:
    """Household x NZ day x 48 matrices, plus facts for the report.

    Rows are households in icp_id order; the report never names one.
    """
    cal: kq.Calendar
    values: object          # (households, days, 48) kWh
    complete: object        # (households, days)
    has_export: object      # (households,) the household has an export register
    facts: dict
    icps: object = None     # (households,) icp_id of each row, never reported
    read_all: object = None  # (households, days) every import register read
                             # every half-hour, nothing interpolated


def _columns(name: str, columns: list[str]):
    import pyarrow as pa

    return pa.concat_tables(
        [part.select(columns) for part in _read_export(serving.get(name))])


def sealed_at(name: str) -> dt.datetime | None:
    """When the export of `name` was sealed: its _SUCCESS object's mtime."""
    bucket = os.environ.get("KEA_LAKE_BUCKET", lake.BUCKET)
    info = lake.s3_filesystem().get_file_info(
        f"{bucket}/{lake.export_prefix(name)}/_SUCCESS")
    if info.mtime is None:
        return None
    mtime = info.mtime
    if mtime.tzinfo is None:
        mtime = mtime.replace(tzinfo=dt.timezone.utc)
    return mtime.astimezone(dt.timezone.utc)


@dataclass(frozen=True)
class Reads:
    """Import reads, sorted by icp_id. minute is UTC minutes since the epoch."""
    icp: object
    register: object
    minute: object
    kwh: object
    rows: int
    null_kwh: int


def _cohort_registers():
    """(cohort icp_ids, import register ids, import registers per ICP,
    ICPs with an export register), from the export."""
    import numpy as np

    icp = _columns("metering.icp", ["icp_id", "icp_number"])
    numbers = icp["icp_number"].to_pylist()
    ids = icp["icp_id"].to_numpy(zero_copy_only=False)
    cohort = np.array(sorted(
        int(i) for i, n in zip(ids, numbers)
        if n is not None and n.startswith(COHORT_PREFIX)), dtype=np.int64)

    reg = _columns("metering.meter_register",
                   ["register_id", "icp_id", "direction"])
    reg_icp = reg["icp_id"].to_numpy(zero_copy_only=False)
    reg_id = reg["register_id"].to_numpy(zero_copy_only=False)
    direction = np.array(reg["direction"].to_pylist(), dtype=object)
    in_cohort = np.isin(reg_icp, cohort)
    importing = in_cohort & (direction == "import")
    registers = {int(i): int(n) for i, n in
                 zip(*np.unique(reg_icp[importing], return_counts=True))}
    exporting = set(int(i) for i in reg_icp[in_cohort
                                            & (direction == "export")])
    return cohort, reg_id[importing].astype(np.int64), registers, exporting


def _sorted_reads(icps, regs, minutes, kwh, rows, null_kwh) -> Reads:
    import numpy as np

    icp_of = np.concatenate(icps).astype(np.int64)
    register_of = np.concatenate(regs).astype(np.int64)
    minute = np.concatenate(minutes).astype(np.int64)
    kwh = np.concatenate(kwh).astype(float)
    order = np.lexsort((minute, register_of, icp_of))
    return Reads(icp_of[order], register_of[order], minute[order], kwh[order],
                 rows, null_kwh)


def export_reads(import_ids) -> Reads:
    import numpy as np
    import pyarrow as pa

    icps, regs, minutes, kwh = [], [], [], []
    rows = null_kwh = 0
    for part in _read_export(serving.get("metering.meter_read_hh")):
        keep = np.isin(part["register_id"].to_numpy(zero_copy_only=False),
                       import_ids)
        part = part.filter(pa.array(keep)).select(
            ["icp_id", "register_id", "interval_start", "kwh"])
        rows += part.num_rows
        valid = part["kwh"].is_valid().to_numpy(zero_copy_only=False)
        null_kwh += int((~valid).sum())
        part = part.filter(pa.array(valid))
        ns = part["interval_start"].cast(pa.int64()).to_numpy()
        icps.append(part["icp_id"].to_numpy(zero_copy_only=False))
        regs.append(part["register_id"].to_numpy(zero_copy_only=False))
        minutes.append(ns // NS_PER_MINUTE)
        kwh.append(part["kwh"].to_numpy(zero_copy_only=False))
    return _sorted_reads(icps, regs, minutes, kwh, rows, null_kwh)


def warehouse_reads(import_ids) -> tuple[Reads, dict]:
    """Every cohort import read in the warehouse fact, read-only.

    The export keeps only the last 13 months of half-hourly reads, and the
    champion can only be replayed where there is weather, which ends before
    that window starts (analysis/forecast_backtest/README.md). One SELECT;
    nothing is written.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    from export_warehouse_to_lake import _connect

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "select last_altered from KEA.INFORMATION_SCHEMA.TABLES "
            "where table_schema = 'KEA_GOLD' "
            "and table_name = 'FCT_METER_READ'")
        (altered,) = cur.fetchone()
        cur.execute(
            "select icp_id, register_id, "
            "date_part(epoch_second, interval_start) as epoch_second, kwh "
            f"from {WAREHOUSE_READS} "
            f"where icp_number like '{COHORT_PREFIX}%'")
        icps, regs, minutes, kwh = [], [], [], []
        rows = null_kwh = 0
        for batch in cur.fetch_arrow_batches():
            if isinstance(batch, pa.RecordBatch):
                batch = pa.Table.from_batches([batch])
            batch = batch.rename_columns(
                [c.lower() for c in batch.column_names])
            reg = pc.cast(batch["register_id"], pa.int64())
            batch = batch.filter(pc.is_in(
                reg, value_set=pa.array(import_ids, pa.int64())))
            rows += batch.num_rows
            valid = batch["kwh"].is_valid()
            null_kwh += batch.num_rows - int(pc.sum(valid).as_py() or 0)
            batch = batch.filter(valid)
            icps.append(pc.cast(batch["icp_id"], pa.int64()).to_numpy())
            regs.append(pc.cast(batch["register_id"], pa.int64()).to_numpy())
            minutes.append(
                pc.cast(batch["epoch_second"], pa.int64()).to_numpy() // 60)
            kwh.append(pc.cast(batch["kwh"], pa.float64()).to_numpy())
    finally:
        conn.close()
    if altered is not None and altered.tzinfo is not None:
        altered = altered.astimezone(dt.timezone.utc)
    return (_sorted_reads(icps, regs, minutes, kwh, rows, null_kwh),
            {"last_altered": altered})


def overlap_check(warehouse: Reads, export: Reads) -> dict:
    """The warehouse reads from the export's first half-hour on must be the
    export's reads exactly: same register-half-hours, same kWh."""
    import numpy as np

    first = int(export.minute.min())
    tail = warehouse.minute >= first
    same_keys = (int(tail.sum()) == len(export.minute)
                 and np.array_equal(warehouse.register[tail], export.register)
                 and np.array_equal(warehouse.minute[tail], export.minute))
    diff = (float(np.abs(warehouse.kwh[tail] - export.kwh).max())
            if same_keys else float("nan"))
    return {"rows": int(tail.sum()), "export_rows": len(export.minute),
            "kwh": float(warehouse.kwh[tail].sum()),
            "export_kwh": float(export.kwh.sum()),
            "identical": same_keys and diff < 1e-9,
            "first_minute": first}


def load_cohort(source: str = "export") -> Cohort:
    """The cohort's daily matrices, from the export's reads (Stage 1) or the
    warehouse's, checked against the export where they overlap (Stage 2)."""
    import numpy as np
    from zoneinfo import ZoneInfo

    cohort, import_ids, registers, exporting = _cohort_registers()
    extra = {}
    if source == "warehouse":
        reads, extra = warehouse_reads(import_ids)
        extra["overlap"] = overlap_check(reads, export_reads(import_ids))
        if not extra["overlap"]["identical"]:
            raise SystemExit(
                "the warehouse reads differ from the sealed export where they "
                "overlap; re-export or investigate before scoring: "
                f"{extra['overlap']}")
    else:
        reads = export_reads(import_ids)
    icp_of, register_of, minute, kwh = (reads.icp, reads.register,
                                        reads.minute, reads.kwh)

    read = set(int(i) for i in np.unique(icp_of))
    households = np.array([i for i in cohort
                           if int(i) in registers and int(i) in read],
                          dtype=np.int64)

    zone = ZoneInfo(kq.NZ)
    first = dt.datetime.fromtimestamp(int(minute.min()) * 60, zone).date()
    last = dt.datetime.fromtimestamp(int(minute.max()) * 60, zone).date()
    cal = kq.calendar(first, (last - first).days + 1)
    per_day = cal.expected.sum(axis=1)

    lo = np.searchsorted(icp_of, households, side="left")
    hi = np.searchsorted(icp_of, households, side="right")
    values = np.zeros((len(households), cal.n_days, kq.SLOTS))
    complete = np.zeros((len(households), cal.n_days), dtype=bool)
    read_all = np.zeros((len(households), cal.n_days), dtype=bool)
    for h, (i, a, b) in enumerate(zip(households, lo, hi)):
        values[h], complete[h] = kq.day_matrix(
            cal, minute[a:b], kwh[a:b], registers=registers[int(i)])
        # The corrected mart rule: distinct import half-hours read, against
        # the day's expected count on every register. No interpolation.
        pairs = np.unique(np.stack([register_of[a:b], minute[a:b]]), axis=1)
        on_grid = (((pairs[1] - cal.bounds[0]) % 30 == 0)
                   & (pairs[1] >= cal.bounds[0])
                   & (pairs[1] < cal.bounds[-1]))
        day = np.searchsorted(cal.bounds, pairs[1][on_grid], side="right") - 1
        counted = np.bincount(day, minlength=cal.n_days)
        read_all[h] = counted == per_day * registers[int(i)]

    pairs = (register_of << 32) | (minute - minute.min())
    facts = {
        "source": source,
        "cohort_icps": len(cohort),
        "with_import_register": sum(1 for i in cohort if int(i) in registers),
        "households": len(households),
        "with_export_register": sum(1 for i in households
                                    if int(i) in exporting),
        "multi_register": sum(1 for i in households
                              if registers[int(i)] > 1),
        "import_reads": reads.rows,
        "null_kwh": reads.null_kwh,
        "repeated_reads": int(len(pairs) - len(np.unique(pairs))),
        "off_half_hour": int(((minute - cal.bounds[0]) % 30 != 0).sum()),
        "first_day": first,
        "last_day": last,
        "dst_short_days": int((per_day < kq.SLOTS).sum()),
        "dst_long_days": int((per_day > kq.SLOTS).sum()),
        "seals": {name: sealed_at(name) for name in READ_TABLES},
        **extra,
    }
    return Cohort(cal, values, complete,
                  np.array([int(i) in exporting for i in households]), facts,
                  icps=households, read_all=read_all)


# ---------------------------------------------------------------------------
# Stage 1: day-ahead

def score_day_ahead(cohort: Cohort, k: int):
    """Every model on the same household-days -> (scores, facts).

    scores[model][form] is a (households, days) array, NaN where a
    household-day was not scored; form is crps, qcrps or cover.
    """
    import numpy as np

    values, complete = cohort.values, cohort.complete
    n_h, n_d = complete.shape
    library = kq.window_library(values, complete)

    active = np.flatnonzero(complete.any(axis=0))
    first, last = int(active[0]), int(active[-1])
    split = max(first + (last - first + 1) // 2, kq.WINDOW_DAYS + 1)

    scores = {m: {f: np.full((n_h, n_d), np.nan)
                  for f in ("crps", "qcrps", "cover")} for m in MODELS}
    eligible = no_baseline = own = 0

    def keep(model, h, d, ensemble, observed):
        scores[model]["crps"][h, d] = kq.crps_ensemble(
            ensemble, observed).mean(axis=-1)
        scores[model]["qcrps"][h, d] = kq.quantile_crps(
            ensemble, observed).mean(axis=-1)
        scores[model]["cover"][h, d] = kq.in_band(
            ensemble, observed).mean(axis=-1)

    for d in range(split, last + 1):
        week = complete[:, d - kq.WINDOW_DAYS:d].all(axis=1)
        targets = np.flatnonzero(complete[:, d] & week)
        if not targets.size:
            continue
        eligible += targets.size
        queries = values[targets, d - kq.WINDOW_DAYS:d].reshape(
            targets.size, -1)
        # knn_forecast(), with the rows kept for the own-history share
        normalised, scale = kq.normalise(queries)
        rows = kq.neighbours(library, normalised, d, k)
        own += int((library.household[rows] == targets[:, None]).sum())
        ensemble = library.continuations[rows] * scale[:, None, None]
        # members on the last axis: (targets, 48, k)
        keep("knn", targets, d, ensemble.transpose(0, 2, 1),
             values[targets, d])

        for h in targets:
            sampled = kq.historical_sampling(values, complete, h, d)
            persisted = kq.persistence(values, complete, h, d)
            if not (len(sampled) and len(persisted)):
                no_baseline += 1
                continue
            keep("hs", h, d, sampled.T, values[h, d])
            keep("persistence", h, d, persisted.T, values[h, d])

    # Only household-days every model scored.
    scored = np.ones((n_h, n_d), dtype=bool)
    for m in MODELS:
        scored &= ~np.isnan(scores[m]["crps"])
    for m in MODELS:
        for f in scores[m]:
            scores[m][f][~scored] = np.nan

    observed = values.mean(axis=2)
    facts = {
        "library_windows": len(library.last_day),
        "split_day": cohort.cal.date(split),
        "last_scored": cohort.cal.date(last),
        "eligible": eligible,
        "no_baseline": no_baseline,
        "scored": int(scored.sum()),
        "scored_households": int(scored.any(axis=1).sum()),
        "own_share": own / (eligible * k) if eligible else float("nan"),
        "mean_kwh": float(observed[scored].mean()),
        "complete_share": float(complete[:, first:last + 1].mean()),
        "complete_days": int(complete.sum()),
    }
    return scores, scored, facts


def gain(base: float, model: float) -> float:
    """% by which `model` lowers `base`'s CRPS; the paper's comparison."""
    return 100.0 * (1.0 - model / base)


def bootstrap_gain(knn_by_h, base_by_h):
    """95% interval of the pooled gain, resampling households.

    Every model is scored on the same household-days, so the counts cancel
    and per-household sums are enough.
    """
    import numpy as np

    rng = np.random.default_rng(BOOTSTRAP_SEED)
    draws = rng.integers(0, len(knn_by_h),
                         size=(BOOTSTRAP_DRAWS, len(knn_by_h)))
    gains = 100.0 * (1.0 - knn_by_h[draws].sum(axis=1)
                     / base_by_h[draws].sum(axis=1))
    low, high = np.percentile(gains, [2.5, 97.5])
    return float(low), float(high)


def summarise(cohort: Cohort, scores, scored, k: int):
    import numpy as np

    by_h = {m: np.where(scored, scores[m]["crps"], 0.0).sum(axis=1)
            for m in MODELS}
    counted = scored.sum(axis=1)
    present = counted > 0

    def pooled(mask, form="crps"):
        cells = scored & mask
        return {m: float(np.nanmean(scores[m][form][cells])) for m in MODELS}

    everyone = np.ones_like(scored)
    summary = {
        "crps": pooled(everyone),
        "qcrps": pooled(everyone, "qcrps"),
        "cover": pooled(everyone, "cover"),
        "ci": {b: bootstrap_gain(by_h["knn"][present], by_h[b][present])
               for b in ("hs", "persistence")},
        "wins": {b: float(((by_h["knn"] < by_h[b])[present]).mean())
                 for b in ("hs", "persistence")},
        "k": k,
    }

    groups = []
    for label, rows in (("No export register", ~cohort.has_export),
                        ("Export register (synthetic solar)",
                         cohort.has_export)):
        mask = np.zeros_like(scored)
        mask[rows] = True
        if not (scored & mask).any():
            continue
        groups.append((label, int((present & rows).sum()),
                       int((scored & mask).sum()), pooled(mask)))
    summary["groups"] = groups

    months = []
    dates = [cohort.cal.date(d) for d in range(cohort.cal.n_days)]
    keys = sorted({(t.year, t.month) for t in dates})
    for year, month in keys:
        cols = np.array([(t.year, t.month) == (year, month) for t in dates])
        mask = np.zeros_like(scored)
        mask[:, cols] = True
        if not (scored & mask).any():
            continue
        months.append((f"{year}-{month:02d}", int((scored & mask).sum()),
                       pooled(mask), pooled(mask, "cover")["knn"]))
    summary["months"] = months
    return summary


def verdict(summary) -> tuple[str, str]:
    """The plan's review point, applied to the pooled gains and their
    household-bootstrap intervals."""
    crps, ci, wins = summary["crps"], summary["ci"], summary["wins"]
    vs_p = gain(crps["persistence"], crps["knn"])
    vs_hs = gain(crps["hs"], crps["knn"])
    pool = ("A small pool is the paper's own stated failure mode "
            "(section 5), and Stage 3's pool-size curve measures it.")
    if vs_p <= 0 or ci["persistence"][0] <= 0:
        return ("Not clearly ahead of persistence",
                "Per the plan, the implementation is suspect: debug it "
                "before anything else. Stage 2 does not start.")
    if vs_hs <= 0:
        return ("Beats persistence, loses to historical sampling",
                "Per the plan, run the pool-size curve before Stage 2. "
                + pool)
    if ci["hs"][0] <= 0:
        return ("Clearly beats persistence; level with historical sampling",
                f"kNN's {_pct(vs_hs)} over historical sampling has an "
                f"interval that spans zero, and kNN wins on only "
                f"{wins['hs']:.0%} of households. The plan's review point "
                "has no rule for a tie: clearing persistence permits "
                "Stage 2, and falling short of the paper's "
                f"{_pct(gain(PAPER_CRPS['hs'], PAPER_CRPS['knn']))} over "
                "historical sampling points at the pool size. " + pool
                + " Which runs first is your call.")
    return ("Beats both baselines",
            "Per the plan, Stage 2 (month-end against hdd-linear-v2) can "
            "start once you say go.")


# ---------------------------------------------------------------------------
# The report

def _fmt_time(t: dt.datetime | None) -> str:
    return t.strftime("%Y-%m-%d %H:%M UTC") if t else "unknown"


def _pct(x: float) -> str:
    return f"{x:+.1f}%"


def write_stage1(out: Path, cohort: Cohort, facts, summary,
                 runtime: float) -> Path:
    c, s = cohort.facts, summary
    crps, qcrps, cover = s["crps"], s["qcrps"], s["cover"]
    seals = [t for t in c["seals"].values() if t]
    seal = _fmt_time(max(seals)) if seals else "unknown"
    headline, action = verdict(s)
    k = s["k"]

    lines = [
        "# Botman backtest, Stage 1: reproduce the paper, day-ahead",
        "",
        f"Export sealed **{seal}** (the newest `_SUCCESS` of "
        f"{', '.join(f'`{t}`' for t in READ_TABLES)}; "
        + ", ".join(f"`{n}` {_fmt_time(t)}" for n, t in c["seals"].items())
        + f"). Run {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC "
        f"in {runtime:.0f} s. Aggregates only.",
        "",
        f"## Verdict: {headline}",
        "",
        action,
        "",
        "## Scores",
        "",
        "CRPS is kWh per half-hour, averaged over every scored half-hour. "
        "All three models are scored on the same "
        f"{facts['scored']:,} household-days.",
        "",
        "| Model | CRPS (ensemble) | CRPS (paper's quantile form) "
        "| P10–P90 coverage | Paper's CRPS (LCL) |",
        "|---|---:|---:|---:|---:|",
    ]
    for m in MODELS:
        lines.append(
            f"| {LABELS[m].format(k=k)} | {crps[m]:.5f} | {qcrps[m]:.5f} "
            f"| {cover[m]:.1%} | {PAPER_CRPS[m]:.4f} |")
    lines += [
        "",
        "The absolute CRPS is not comparable with the paper's: the "
        "households, dates and scale differ. Compare the gains.",
        "",
        "| kNN gain over | Here (ensemble) | 95% CI, households resampled "
        "| Here (quantile form) | Paper | Households where kNN wins |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for b in ("hs", "persistence"):
        low, high = s["ci"][b]
        lines.append(
            f"| {LABELS[b]} | {_pct(gain(crps[b], crps['knn']))} "
            f"| {_pct(low)} to {_pct(high)} "
            f"| {_pct(gain(qcrps[b], qcrps['knn']))} "
            f"| {_pct(gain(PAPER_CRPS[b], PAPER_CRPS['knn']))} "
            f"| {s['wins'][b]:.0%} |")
    lines += [
        "",
        f"Mean import on the scored days is {facts['mean_kwh']:.3f} kWh per "
        f"half-hour, so kNN's CRPS is {crps['knn'] / facts['mean_kwh']:.0%} "
        "of it. A P10–P90 band should cover 80%; "
        + "; ".join(f"{LABELS[m].split(',')[0]}'s covers {cover[m]:.1%} "
                    f"({'too narrow' if cover[m] < 0.8 else 'too wide'})"
                    for m in MODELS)
        + ". The quantiles are numpy's linear ones, as the paper's "
        "`nanquantile()`.",
        "",
        "### By household group",
        "",
        "| Group | Households | Household-days | kNN CRPS "
        "| Gain over HS | Gain over persistence |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, n_h, n_cells, g in s["groups"]:
        lines.append(
            f"| {label} | {n_h} | {n_cells:,} | {g['knn']:.5f} "
            f"| {_pct(gain(g['hs'], g['knn']))} "
            f"| {_pct(gain(g['persistence'], g['knn']))} |")
    lines += [
        "",
        "An export register means the Stage 2 generator netted synthetic "
        "solar out of that household's import, so its shape is no longer "
        "plain LCL.",
        "",
        "### By month",
        "",
        "| Month | Household-days | kNN CRPS | Gain over HS "
        "| Gain over persistence | kNN coverage |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for month, n_cells, g, cov in s["months"]:
        lines.append(
            f"| {month} | {n_cells:,} | {g['knn']:.5f} "
            f"| {_pct(gain(g['hs'], g['knn']))} "
            f"| {_pct(gain(g['persistence'], g['knn']))} | {cov:.0%} |")
    lines += [
        "",
        "## Data",
        "",
        "| | |",
        "|---|---:|",
        f"| Cohort ICPs in the export | {c['cohort_icps']} |",
        f"| … with an import register | {c['with_import_register']} |",
        f"| … with import reads (households) | {c['households']} |",
        f"| … of which with an export register | "
        f"{c['with_export_register']} |",
        f"| … of which with more than one import register | "
        f"{c['multi_register']} |",
        f"| Import reads | {c['import_reads']:,} |",
        f"| … with no kWh (treated as unread) | {c['null_kwh']:,} |",
        f"| … repeating a register and half-hour (day incomplete) | "
        f"{c['repeated_reads']:,} |",
        f"| … not on a half-hour (ignored) | {c['off_half_hour']:,} |",
        f"| NZ days in the calendar | {c['first_day']} to {c['last_day']} |",
        f"| DST days (46 / 50 half-hours) | {c['dst_short_days']} / "
        f"{c['dst_long_days']} |",
        f"| Complete household-days | {facts['complete_days']:,} "
        f"({facts['complete_share']:.1%} of the active span) |",
        f"| Library windows (7 days + next day, all complete) | "
        f"{facts['library_windows']:,} |",
        f"| Scored days | {facts['split_day']} to {facts['last_scored']} |",
        f"| Household-days with a complete day and week before | "
        f"{facts['eligible']:,} |",
        f"| … dropped, no earlier baseline member | "
        f"{facts['no_baseline']:,} |",
        f"| Household-days scored | {facts['scored']:,} across "
        f"{facts['scored_households']} households |",
        f"| Neighbours from the target's own history | "
        f"{facts['own_share']:.1%} |",
        "",
        "## Settings",
        "",
        f"- k = {k}; queries and windows are 7 days × 48 half-hours, each "
        "divided by its own maximum; Euclidean distance; the forecast is the "
        "k neighbours' next days × the query week's maximum.",
        "- **Leakage rule:** a window is usable for day d only when the "
        "window *and* its next day end before d, for every household. This "
        "is stricter than the paper's wording (section 3.3); "
        "`test_knn_quantile.py` pins it.",
        "- **Scored:** every household-day in the later half of the active "
        "span whose day and 7 days before are complete; each is forecast "
        "with only the history before it.",
        "- **Historical sampling:** every earlier complete day on the same "
        "weekday. **Persistence:** yesterday plus each earlier day-on-day "
        "change, slot by slot, clipped at zero.",
        "- **CRPS:** the exact ensemble form `E|X−y| − ½E|X−X′|` is the "
        "headline. The paper's form, the pinball loss at the 100 levels "
        "0.01–1.00 (eqs. 5–6), is averaged rather than summed, which leaves "
        "every ratio unchanged.",
        f"- **Interval:** {BOOTSTRAP_DRAWS} bootstrap draws of households, "
        f"seed {BOOTSTRAP_SEED}.",
        "",
        "## How this differs from the paper",
        "",
        "| | Paper (LCL, Table 1b) | Here |",
        "|---|---|---|",
        "| Households | 363 | "
        f"{c['households']}, so a smaller library; the paper warns a small "
        "pool hurts kNN (section 5) |",
        "| Span and split | LCL subset; last 20% as test | "
        f"{c['first_day']:%b %Y} to {c['last_day']:%b %Y}; the later half "
        "re-forecast day by day |",
        "| Library | windows up to and excluding day d | window and next day "
        "end before d |",
        "| Data | LCL as recorded, UK clock | LCL households rescaled and "
        "moved onto NZ dates by the source generator; "
        f"{c['with_export_register']} carry synthetic solar netted from "
        "import |",
        "| Clock changes | UK | NZ: a 46-half-hour day fills the skipped hour "
        "by interpolation, a 50-half-hour day sums the repeated hour |",
        "| Gaps | isolated one-step gaps interpolated | the same; any other "
        "gap makes the day incomplete, never used |",
        "| CRPS | quantile form, summed | ensemble form (headline) and the "
        "quantile form, averaged |",
        "| Persistence | error quantiles from a training set | every earlier "
        "day pair; members clipped at zero, which can only help it |",
        "",
        "`billing.account` was not needed at this stage: day-ahead scoring "
        "works on ICPs alone.",
        "",
    ]
    out.mkdir(parents=True, exist_ok=True)
    path = out / "stage1_reproduce.md"
    path.write_text("\n".join(lines))
    return path


def reproduce(k: int, out: Path) -> int:
    started = time.monotonic()
    cohort = load_cohort()
    scores, scored, facts = score_day_ahead(cohort, k)
    if not facts["scored"]:
        raise SystemExit("no household-day could be scored")
    summary = summarise(cohort, scores, scored, k)
    path = write_stage1(out, cohort, facts, summary,
                        time.monotonic() - started)

    crps = summary["crps"]
    headline, _ = verdict(summary)
    print(f"scored {facts['scored']:,} household-days across "
          f"{facts['scored_households']} households")
    for b in ("hs", "persistence"):
        low, high = summary["ci"][b]
        print(f"  kNN gain over {b}: {_pct(gain(crps[b], crps['knn']))} "
              f"(95% CI {_pct(low)} to {_pct(high)}; paper "
              f"{_pct(gain(PAPER_CRPS[b], PAPER_CRPS['knn']))})")
    print("  P10-P90 coverage: " + ", ".join(
        f"{m} {summary['cover'][m]:.1%}" for m in MODELS))
    print(f"verdict: {headline}")
    print(f"wrote {path}")
    return 0


# ---------------------------------------------------------------------------
# Stage 2: month-end against hdd-linear-v2

FORECAST_COLUMNS = ["account_id", "period_start", "as_at_date",
                    "days_elapsed", "days_total", "kwh_to_date",
                    "cost_to_date", "baseline_daily_kwh",
                    "same_period_last_year_amount", "hdd_expected_remaining",
                    "heating_sensitivity_kwh_per_hdd", "fixed_charges_period",
                    "is_forecastable"]
USAGE_COLUMNS = ["icp_id", "usage_date", "import_kwh", "heating_degree_days",
                 "interval_count", "expected_intervals"]
MONTHEND_TABLES = ("billing.account", "kea.mart_forecast_input",
                   "kea.mart_daily_usage")
DBT_PROJECT = (Path(__file__).resolve().parents[1] / "dbt_project"
               / "dbt_project.yml")
# int_metering__heating_sensitivity.sql
MIN_DAYS_IN_MONTH = 20
MIN_MONTHS = 6
# docs/sprints/sprint-3/data-plan.md: cohort snapshots v2 could forecast.
PUBLISHED_FORECASTABLE = 7468
BUCKETS = (("1–6", 1, 6), ("7–9", 7, 9), ("10–14", 10, 14),
           ("15–19", 15, 19), ("20+", 20, 10_000))
# The gate, written down before the first run (README, "The gate").
GATE_COVERAGE = (0.70, 0.90)
GATE_APE_POINTS = 5.0
GATE_CRPS_RATIO = 1.10
FITS = {"asat": "refitted as at each date (headline)",
        "shipped": "as shipped (look-ahead)"}


def min_heating_fit_r2() -> float:
    """The fit floor is_forecastable uses, read from dbt_project.yml."""
    match = re.search(r"^\s*min_heating_fit_r2:\s*([0-9.]+)\s*$",
                      DBT_PROJECT.read_text(), re.M)
    if not match:
        raise SystemExit(f"min_heating_fit_r2 not found in {DBT_PROJECT}")
    return float(match.group(1))


def _round4(x: float) -> float:
    """Snowflake's round(x, 4): half away from zero."""
    return float(Decimal(repr(float(x))).quantize(Decimal("0.0001"),
                                                  ROUND_HALF_UP))


def ols(x, y):
    """Snowflake's regr_slope, regr_intercept and regr_r2 of y on x."""
    import numpy as np

    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    vx = ((x - x.mean()) ** 2).mean()
    if vx == 0:
        return None, None, None
    vy = ((y - y.mean()) ** 2).mean()
    cov = ((x - x.mean()) * (y - y.mean())).mean()
    slope = cov / vx
    r2 = 1.0 if vy == 0 else cov * cov / (vx * vy)
    return slope, y.mean() - slope * x.mean(), r2


def heating_fits(month_of, kwh, hdd, usable, n_months: int):
    """One ICP's monthly heating fit on the months before each cut.

    Returns (n_months + 1, 3) slope, baseline, r2, NaN where there is no fit;
    row c uses only months with index < c, so row n_months is the fit on
    every month, as int_metering__heating_sensitivity makes it.
    """
    import numpy as np

    ok = usable & np.isfinite(kwh) & np.isfinite(hdd)
    days = np.bincount(month_of[ok], minlength=n_months)
    kwh_sum = np.bincount(month_of[ok], weights=kwh[ok], minlength=n_months)
    hdd_sum = np.bincount(month_of[ok], weights=hdd[ok], minlength=n_months)
    good = days >= MIN_DAYS_IN_MONTH
    fits = np.full((n_months + 1, 3), np.nan)
    for cut in range(n_months + 1):
        months = np.flatnonzero(good[:cut])
        if len(months) < MIN_MONTHS:
            continue
        fit = ols(hdd_sum[months] / days[months],
                  kwh_sum[months] / days[months])
        if fit[0] is not None:
            fits[cut] = [_round4(v) for v in fit]
    return fits


@dataclass
class Snapshots:
    """Every cohort row of kea.mart_forecast_input, as arrays, plus what
    Stage 2 derives for it. Never written out row by row."""
    n: int
    col: dict
    dec: dict
    facts: dict


def _month_index(dates, first: dt.date):
    import numpy as np

    return np.array([(t.year - first.year) * 12 + t.month - first.month
                     for t in dates], dtype=np.int64)


def load_snapshots(cohort: Cohort, threshold: float) -> Snapshots:
    """The forecast inputs, the daily mart, both heating fits and both
    eligibility flags, checked against what the export says."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.compute as pc

    cal, n_days = cohort.cal, cohort.cal.n_days
    cohort_icps, _, _, exporting = _cohort_registers()
    first = pa.scalar(cal.first_day, pa.date32())

    # --- the daily mart, one row per cohort ICP -------------------------
    usage = _columns("kea.mart_daily_usage", USAGE_COLUMNS)
    usage = usage.filter(pc.is_in(usage["icp_id"],
                                  value_set=pa.array(cohort_icps)))
    u_icp = pc.cast(usage["icp_id"], pa.int64()).to_numpy()
    u_day = pc.days_between(first, usage["usage_date"]).to_numpy(
        zero_copy_only=False).astype(np.int64)
    icps = np.unique(u_icp)
    row_of = {int(i): r for r, i in enumerate(icps)}
    inside = (u_day >= 0) & (u_day < n_days)
    ui = np.searchsorted(icps, u_icp[inside])
    ud = u_day[inside]

    def grid(column, dtype=float, fill=np.nan):
        out = np.full((len(icps), n_days), fill, dtype=dtype)
        values = pc.cast(usage[column], pa.float64()).to_numpy(
            zero_copy_only=False)[inside]
        out[ui, ud] = values
        return out

    kwh = grid("import_kwh")
    hdd = grid("heating_degree_days")
    present = np.zeros((len(icps), n_days), dtype=bool)
    present[ui, ud] = True
    old_complete = present & (grid("interval_count")
                              == grid("expected_intervals"))
    household = {int(i): h for h, i in enumerate(cohort.icps)}
    read_all = np.zeros_like(present)
    for i, r in row_of.items():
        if i in household:
            read_all[r] = cohort.read_all[household[i]]
    new_complete = present & read_all
    # One import register and no export: the corrected rule and the mart's
    # must agree. With an export register the mart's rule counts both.
    plain = np.array([int(i) not in exporting for i in icps])
    rule_check = {
        "plain_differs": int((old_complete[plain]
                              != new_complete[plain]).sum()),
        "solar_old": int(old_complete[~plain].sum()),
        "solar_new": int(new_complete[~plain].sum()),
    }
    weather_days = present & np.isfinite(hdd)
    hdd_days = np.flatnonzero(weather_days.any(axis=0))
    last_weather = int(hdd_days[-1])

    def cumulative(a):
        return np.concatenate([np.zeros((a.shape[0], 1)),
                               np.cumsum(a, axis=1)], axis=1)

    cs_weather = cumulative(weather_days)
    cs_present = cumulative(present)
    cs_kwh = cumulative(np.where(present, np.nan_to_num(kwh), 0.0))

    # --- both heating fits, per ICP ---------------------------------------
    dates = [cal.date(d) for d in range(n_days + 1)]
    month_of = _month_index(dates, cal.first_day)
    n_months = int(month_of[n_days - 1]) + 1
    shipped_fit = np.full((len(icps), 3), np.nan)
    asat_fits = np.full((len(icps), n_months + 1, 3), np.nan)
    for r in range(len(icps)):
        shipped_fit[r] = heating_fits(month_of[:n_days], kwh[r], hdd[r],
                                      old_complete[r], n_months)[n_months]
        asat_fits[r] = heating_fits(month_of[:n_days], kwh[r], hdd[r],
                                    new_complete[r], n_months)

    # --- the forecast inputs ---------------------------------------------
    accounts = _columns("billing.account", ["account_id", "icp_id"])
    icp_of_account = dict(zip(
        pc.cast(accounts["account_id"], pa.int64()).to_pylist(),
        pc.cast(accounts["icp_id"], pa.int64()).to_pylist()))
    table = _columns("kea.mart_forecast_input", FORECAST_COLUMNS)
    account = pc.cast(table["account_id"], pa.int64()).to_numpy()
    icp = np.array([icp_of_account.get(int(a), -1) for a in account],
                   dtype=np.int64)
    keep = np.isin(icp, cohort_icps)
    table = table.filter(pa.array(keep))
    account, icp = account[keep], icp[keep]
    n = table.num_rows

    col = {"account": account, "icp": icp}
    for name in ("days_elapsed", "days_total"):
        col[name] = pc.cast(table[name], pa.int64()).to_numpy()
    for name in ("kwh_to_date", "cost_to_date", "baseline_daily_kwh",
                 "hdd_expected_remaining", "heating_sensitivity_kwh_per_hdd",
                 "fixed_charges_period"):
        col[name] = pc.cast(table[name], pa.float64()).to_numpy(
            zero_copy_only=False)
    col["flag"] = table["is_forecastable"].to_numpy(
        zero_copy_only=False).astype(bool)
    col["start"] = pc.days_between(first, table["period_start"]).to_numpy(
        zero_copy_only=False).astype(np.int64)
    col["day"] = pc.days_between(first, table["as_at_date"]).to_numpy(
        zero_copy_only=False).astype(np.int64)
    dec = {name: [_decimal(v) for v in table[name].to_pylist()] for name in (
        "kwh_to_date", "cost_to_date", "baseline_daily_kwh",
        "same_period_last_year_amount", "hdd_expected_remaining",
        "heating_sensitivity_kwh_per_hdd", "fixed_charges_period")}

    e, total = col["days_elapsed"], col["days_total"]
    start, day = col["start"], col["day"]
    u = np.array([row_of.get(int(i), -1) for i in icp], dtype=np.int64)
    has_usage = u >= 0
    uu = np.where(has_usage, u, 0)

    def span(cs, lo, hi):
        """Sum of cs's daily values over [lo, hi), clipped to the calendar."""
        lo = np.clip(lo, 0, n_days)
        hi = np.clip(hi, 0, n_days)
        return np.where(has_usage, cs[uu, hi] - cs[uu, lo], 0.0)

    all_weather = span(cs_weather, start, day + 1) == e
    end = start + total          # exclusive
    truth_ok = (has_usage & (start >= 0) & (end <= n_days)
                & (span(cs_present, start, end) == total))
    truth = np.where(truth_ok, span(cs_kwh, start, end), np.nan)
    in_cal = has_usage & (start >= 0) & (day < n_days)
    to_date = span(cs_kwh, start, day + 1)
    to_date_diff = float(np.abs(to_date - col["kwh_to_date"])[in_cal].max())

    cost, fixed = dec["cost_to_date"], dec["fixed_charges_period"]
    variable = np.array([
        float(c - _money(f * Decimal(int(a)) / Decimal(int(t))))
        if c is not None and f is not None else np.nan
        for c, f, a, t in zip(cost, fixed, e, total)])
    rate = np.where(col["kwh_to_date"] > 0,
                    variable / np.where(col["kwh_to_date"] > 0,
                                        col["kwh_to_date"], 1.0), np.nan)

    def eligible(slope, base, r2):
        with np.errstate(invalid="ignore"):
            return ((slope >= 0) & (base >= 0) & (r2 >= threshold)
                    & (col["kwh_to_date"] > 0) & (variable >= 0)
                    & (col["fixed_charges_period"] >= 0) & all_weather
                    & np.isfinite(col["hdd_expected_remaining"]))

    shipped_r2 = np.where(has_usage, shipped_fit[uu, 2], np.nan)
    col["shipped"] = eligible(col["heating_sensitivity_kwh_per_hdd"],
                              col["baseline_daily_kwh"], shipped_r2)

    nxt = np.clip(day + 1, 0, n_days)
    cut = np.where((day >= 0) & (day < n_days), month_of[nxt], -1)
    fit = np.where(((cut >= 0) & has_usage)[:, None],
                   asat_fits[uu, np.clip(cut, 0, n_months)], np.nan)
    col["asat_slope"], col["asat_base"], col["asat_r2"] = fit.T
    col["asat"] = eligible(*fit.T)

    # The shipped fit, replicated, against the exported slope and baseline.
    exported = {}
    for i, s, b in zip(icp, col["heating_sensitivity_kwh_per_hdd"],
                       col["baseline_daily_kwh"]):
        exported.setdefault(int(i), (s, b))
    fit_diff, fit_presence = 0.0, 0
    for i, (s, b) in exported.items():
        mine = shipped_fit[row_of[i]] if i in row_of else [np.nan] * 3
        if np.isfinite(s) != np.isfinite(mine[0]):
            fit_presence += 1
        elif np.isfinite(s):
            fit_diff = max(fit_diff, abs(s - mine[0]), abs(b - mine[1]))

    household_of = np.array([household.get(int(i), -1) for i in icp],
                            dtype=np.int64)
    solar = np.array([int(i) in exporting for i in icp])
    col.update(truth=truth, rate=rate, household=household_of, solar=solar,
               remaining=total - e, in_window=day <= last_weather)
    facts = {
        "rows": n,
        "accounts": len(np.unique(account)),
        "exported_forecastable": int(col["flag"].sum()),
        "flag_mismatch": int((col["flag"] != col["shipped"]).sum()),
        "shipped": int(col["shipped"].sum()),
        "shipped_accounts": len(np.unique(account[col["shipped"]])),
        "asat": int(col["asat"].sum()),
        "asat_accounts": len(np.unique(account[col["asat"]])),
        "asat_solar_accounts": len(np.unique(
            account[col["asat"] & solar])),
        "shipped_solar_accounts": len(np.unique(
            account[col["shipped"] & solar])),
        "fits_exported": sum(1 for s, _ in exported.values()
                             if np.isfinite(s)),
        "fit_diff": fit_diff,
        "fit_presence": fit_presence,
        "to_date_diff": to_date_diff,
        "with_truth": int(truth_ok.sum()),
        "cycles_with_truth": len({(a, s) for a, s, ok in
                                  zip(account, start, truth_ok) if ok}),
        "last_weather": cal.date(last_weather),
        "usage_outside_calendar": int((~inside).sum()),
        "solar_accounts": len(np.unique(account[solar])),
        "threshold": threshold,
        **rule_check,
    }
    return Snapshots(n, col, dec, facts)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _decimal(value) -> Decimal | None:
    """Serving hands project_forecast() Decimals; so does the replay."""
    if value is None or isinstance(value, Decimal):
        return value
    return Decimal(repr(float(value)))


def replay_champion(snaps: Snapshots, fit: str):
    """build_bill_forecasts.project_forecast() on every eligible snapshot.

    fit 'shipped' passes the exported slope and baseline; 'asat' the refit.
    Returns (projected kWh, band half-width in kWh, band width in NZD), NaN
    where the champion made no forecast.
    """
    import numpy as np

    c, d = snaps.col, snaps.dec
    projected = np.full(snaps.n, np.nan)
    half_kwh = np.full(snaps.n, np.nan)
    width_nzd = np.full(snaps.n, np.nan)
    for j in np.flatnonzero(c[fit]):
        if fit == "shipped":
            slope = d["heating_sensitivity_kwh_per_hdd"][j]
            base = d["baseline_daily_kwh"][j]
        else:
            slope = Decimal(f"{c['asat_slope'][j]:.4f}")
            base = Decimal(f"{c['asat_base'][j]:.4f}")
        p = champion.project_forecast(
            days_elapsed=int(c["days_elapsed"][j]),
            days_total=int(c["days_total"][j]),
            kwh_to_date=d["kwh_to_date"][j],
            cost_to_date=d["cost_to_date"][j],
            baseline_daily_kwh=base,
            # The champion here is v2: v3's remaining-days term is baseline
            # PLUS detected load, so v2 is v3 with none. project_forecast()
            # made the argument required on 23 Sep (#203) so no caller falls
            # back silently; this caller means it, and the as-at fit above is
            # gross, which is what v2 fitted.
            detected_load_kwh_day=Decimal(0),
            hdd_expected_remaining=d["hdd_expected_remaining"][j],
            heating_sensitivity_kwh_per_hdd=slope,
            fixed_charges_period=d["fixed_charges_period"][j],
            baseline_amount=d["same_period_last_year_amount"][j])
        projected[j] = float(p.projected_kwh)
        # The same ±15% of what is still to come, in kWh rather than NZD.
        half_kwh[j] = float(champion.UNCERTAINTY_PCT
                            * (p.projected_kwh - d["kwh_to_date"][j]))
        width_nzd[j] = float(p.projected_high - p.projected_low)
    return projected, half_kwh, width_nzd


def score_monthend(cohort: Cohort, snaps: Snapshots, k: int,
                   norm: str = "week", variant: str = "all", pool=None):
    """kNN's remaining-kWh ensemble for every snapshot it can serve.

    A snapshot is served when its household's 7 days up to the as-at date
    are complete, the cycle still has days to run, the cycle's final kWh is
    known, and at least k weeks of history qualify for that horizon.

    `norm`, `variant` and `pool` are Stage 3's dials, and their defaults are
    the paper's method on the whole cohort. `variant` 'seasonal' keeps only
    neighbours from the same time of year a year back (kq.seasonal_rows()).
    `pool` is a boolean mask over the cohort's households: only those bring
    weeks to the library, and only their snapshots are scored, so a smaller
    pool is measured on its own households and not on borrowed ones.
    """
    import numpy as np

    c = snaps.col
    n_days = cohort.cal.n_days
    h, day, remaining = c["household"], c["day"], c["remaining"]
    in_library = cohort.complete
    if pool is not None:
        pool = np.asarray(pool, dtype=bool)
        in_library = in_library & pool[:, None]
    library = kq.trajectory_library(cohort.values, in_library,
                                    max_horizon=int(remaining.max()),
                                    norm=norm)
    run = np.concatenate([np.zeros((len(cohort.icps), 1), dtype=np.int64),
                          np.cumsum(cohort.complete, axis=1)], axis=1)
    base = (h >= 0) & (day >= kq.WINDOW_DAYS - 1) & (day < n_days)
    hh = np.where(base, h, 0)
    dd = np.where(base, day, kq.WINDOW_DAYS - 1)
    week = base & (run[hh, dd + 1] - run[hh, dd + 1 - kq.WINDOW_DAYS]
                   == kq.WINDOW_DAYS)
    wanted = week & (remaining >= 1) & np.isfinite(c["truth"])
    if pool is not None:
        wanted &= base & pool[hh]

    out = {name: np.full(snaps.n, np.nan) for name in
           ("crps", "p10", "p50", "p90", "cover")}
    too_few = own = 0
    for as_at in np.unique(day[wanted]):
        rows = np.flatnonzero(wanted & (day == as_at))
        allowed = (kq.seasonal_rows(library, int(as_at))
                   if variant == "seasonal" else None)
        counts = {int(r): int(kq.trajectory_candidates(
            library, int(as_at), int(r), allowed).sum())
            for r in np.unique(remaining[rows])}
        enough = np.array([counts[int(r)] >= k for r in remaining[rows]])
        too_few += int((~enough).sum())
        rows = rows[enough]
        if not rows.size:
            continue
        start = int(as_at) - kq.WINDOW_DAYS + 1
        queries = cohort.values[h[rows], start:int(as_at) + 1].reshape(
            rows.size, -1)
        normalised, scale = kq.normalise(queries, norm)
        nearest = kq.trajectory_neighbours(library, normalised, int(as_at),
                                           remaining[rows], k, allowed)
        own += int((library.household[nearest] == h[rows][:, None]).sum())
        members = (library.totals[nearest, remaining[rows][:, None]]
                   * scale[:, None])
        projected = c["kwh_to_date"][rows][:, None] + members
        truth = c["truth"][rows]
        out["crps"][rows] = kq.crps_ensemble(projected, truth)
        out["p10"][rows], out["p50"][rows], out["p90"][rows] = np.quantile(
            projected, [0.1, 0.5, 0.9], axis=1)
        out["cover"][rows] = kq.in_band(projected, truth)
    served = np.isfinite(out["crps"])
    facts = {
        "k": k,
        "norm": norm,
        "variant": variant,
        "pool_households": int(len(np.unique(library.household))),
        "library_weeks": len(library.start),
        "no_household": int((h < 0).sum()),
        "no_week": int(((h >= 0) & ~week).sum()),
        "final_day": int((week & (remaining < 1)).sum()),
        "no_truth": int((week & (remaining >= 1)
                         & ~np.isfinite(c["truth"])).sum()),
        "too_few": too_few,
        "served": int(served.sum()),
        "own_share": own / (served.sum() * k) if served.any() else np.nan,
    }
    return out, facts


def _ape(forecast, truth):
    import numpy as np

    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(truth > 0, np.abs(forecast - truth) / truth * 100.0,
                        np.nan)


def monthend_frame(snaps: Snapshots, knn, champions, mid: int | None = None):
    """One row per snapshot with every score; aggregated, never written.

    `mid` fixes where held-out cycles begin. Stage 3 passes the headline
    run's, so a variant that serves fewer snapshots is judged on the same
    cycles and not on a window of its own.
    """
    import numpy as np
    import pandas as pd

    c = snaps.col
    truth = c["truth"]
    frame = pd.DataFrame({
        "account": c["account"], "start": c["start"], "day": c["day"],
        "elapsed": c["days_elapsed"], "remaining": c["remaining"],
        "solar": c["solar"], "in_window": c["in_window"], "truth": truth,
        "knn": np.isfinite(knn["crps"]),
        "knn_crps": knn["crps"],
        "knn_ape": _ape(knn["p50"], truth),
        "knn_cover": knn["cover"],
        "knn_width": np.where(c["rate"] >= 0,
                              (knn["p90"] - knn["p10"]) * c["rate"], np.nan),
        "knn_width_kwh": knn["p90"] - knn["p10"],
    })
    for fit, (projected, half, width) in champions.items():
        frame[fit] = (np.isfinite(projected) & np.isfinite(truth)
                      & (c["remaining"] >= 1))
        frame[f"{fit}_crps"] = np.abs(projected - truth)
        frame[f"{fit}_ape"] = _ape(projected, truth)
        frame[f"{fit}_cover"] = np.where(
            np.isfinite(projected),
            (truth >= projected - half) & (truth <= projected + half), np.nan)
        frame[f"{fit}_width"] = width
        frame[f"{fit}_width_kwh"] = 2 * half
    frame["bucket"] = ""
    for label, lo, hi in BUCKETS:
        frame.loc[frame.elapsed.between(lo, hi), "bucket"] = label

    served = frame[frame.knn & frame.in_window]
    first, last = int(served.day.min()), int(served.day.max())
    if mid is None:
        mid = first + (last - first) // 2
    frame["held_out"] = frame.start >= mid
    return frame, {"first": first, "last": last, "mid": mid}


def group_metrics(frame, fit: str | None):
    """Aggregates for one population; fit names the champion beside kNN."""
    out = {"n": len(frame), "accounts": frame.account.nunique(),
           "knn_crps": frame.knn_crps.mean(),
           "knn_mdape": frame.knn_ape.median(),
           "knn_cover": frame.knn_cover.mean(),
           "knn_width": frame.knn_width.median(),
           "knn_width_kwh": frame.knn_width_kwh.median(),
           "truth": frame.truth.mean()}
    if fit:
        out.update({
            "champ_crps": frame[f"{fit}_crps"].mean(),
            "champ_mdape": frame[f"{fit}_ape"].median(),
            "champ_cover": frame[f"{fit}_cover"].astype(float).mean(),
            "champ_width": frame[f"{fit}_width"].median(),
            "champ_width_kwh": frame[f"{fit}_width_kwh"].median(),
        })
        out["ratio"] = out["knn_crps"] / out["champ_crps"]
    return out


def populations(frame, fit: str, held_out: bool):
    base = frame[frame.in_window & (frame.held_out if held_out else True)]
    return {
        "h2h": base[base.knn & base[fit]],
        "fallback": base[base.knn & ~base[fit]],
        "champion": base[base[fit]],
        "knn": base[base.knn],
    }


def bootstrap_ratio(h2h, fit: str):
    """95% interval of kNN's pooled CRPS over the champion's, resampling
    accounts."""
    import numpy as np

    sums = h2h.groupby("account")[["knn_crps", f"{fit}_crps"]].sum()
    knn, champ = sums.knn_crps.to_numpy(), sums[f"{fit}_crps"].to_numpy()
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    draws = rng.integers(0, len(knn), size=(BOOTSTRAP_DRAWS, len(knn)))
    ratios = knn[draws].sum(axis=1) / champ[draws].sum(axis=1)
    low, high = np.percentile(ratios, [2.5, 97.5])
    return float(low), float(high)


def win_rate(h2h, fit: str) -> float:
    means = h2h.groupby("account")[["knn_crps", f"{fit}_crps"]].mean()
    return float((means.knn_crps < means[f"{fit}_crps"]).mean())


def gate(frame, fit: str = "asat"):
    """The three criteria on held-out cycles -> list of (name, value, pass)."""
    import math

    pops = populations(frame, fit, held_out=True)
    cover = pops["knn"].knn_cover.mean()
    fallback = pops["fallback"].knn_ape.median()
    champ = pops["champion"][f"{fit}_ape"].median()
    h2h = pops["h2h"]
    ratio = (h2h.knn_crps.mean() / h2h[f"{fit}_crps"].mean()
             if len(h2h) else math.nan)

    def ok(x, test):
        return bool(not math.isnan(x) and test(x))

    lo, hi = GATE_COVERAGE
    return [
        ("P10–P90 coverage between "
         f"{lo:.0%} and {hi:.0%}",
         f"{cover:.1%} over {len(pops['knn']):,} snapshots",
         ok(cover, lambda x: lo <= x <= hi)),
        (f"Median APE where v2 can't forecast ≤ v2's own + "
         f"{GATE_APE_POINTS:.0f} points",
         f"{fallback:.1f}% ({len(pops['fallback']):,} snapshots) against "
         f"{champ:.1f}% + {GATE_APE_POINTS:.0f} = "
         f"{champ + GATE_APE_POINTS:.1f}% "
         f"({len(pops['champion']):,})",
         ok(fallback - champ, lambda x: x <= GATE_APE_POINTS)),
        (f"Head-to-head mean CRPS ≤ {GATE_CRPS_RATIO:.2f} × v2's",
         f"{ratio:.2f} × over {len(h2h):,} snapshots",
         ok(ratio, lambda x: x <= GATE_CRPS_RATIO)),
    ]


def metrics_rows(frame):
    """The flat table behind metrics.csv."""
    rows = []
    for fit in FITS:
        if fit not in frame.columns:
            continue
        for held_out in (True, False):
            pops = populations(frame, fit, held_out)
            for pop in ("h2h", "fallback"):
                data = pops[pop]
                for label, lo, hi in (("all", 0, 10_000),) + BUCKETS:
                    part = data[data.elapsed.between(lo, hi)]
                    if not len(part):
                        continue
                    m = group_metrics(part, fit if pop == "h2h" else None)
                    rows.append({"champion_fit": fit,
                                 "cycles": "held_out" if held_out else "all",
                                 "population": pop, "days_elapsed": label,
                                 **m})
    beyond = frame[frame.knn & ~frame.in_window]
    if len(beyond):
        rows.append({"champion_fit": "none", "cycles": "all",
                     "population": "beyond_weather", "days_elapsed": "all",
                     **group_metrics(beyond, None)})
    return rows


def write_metrics(out: Path, rows) -> Path:
    import csv

    keys = ["champion_fit", "cycles", "population", "days_elapsed", "n",
            "accounts", "truth", "knn_crps", "champ_crps", "ratio",
            "knn_mdape", "champ_mdape", "knn_cover", "champ_cover",
            "knn_width", "champ_width", "knn_width_kwh", "champ_width_kwh"]
    out.mkdir(parents=True, exist_ok=True)
    path = out / "metrics.csv"
    with path.open("w", newline="") as handle:
        # lineterminator is not the default: csv writes \r\n per RFC 4180, so a
        # committed result file arrives as CRLF and `git diff --check` reports
        # every line as trailing whitespace. Same fix as load_weather.py.
        writer = csv.DictWriter(handle, fieldnames=keys,
                                extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: (f"{row[key]:.6g}"
                                   if isinstance(row.get(key), float)
                                   else row.get(key, ""))
                             for key in keys})
    return path


def export_note(snaps: Snapshots) -> str | None:
    """One line when the export no longer ships the champion we replicate.

    Sprint 4 moved the published forecast to `hdd-linear-v3`, whose heating
    fit is net of detected loads. Its `is_forecastable` is then not v2's, and
    its slope and baseline are not v2's either, so the look-ahead comparison
    against the shipped fit has nothing to compare and is left out. The
    headline is unaffected: it refits v2 as at each date from the reads.
    """
    s = snaps.facts
    if not (s["flag_mismatch"] or s["shipped"] != s["exported_forecastable"]):
        return None
    return (f"**The export ships another champion.** Rebuilding v2's "
            f"`is_forecastable` from the export gives {s['shipped']:,} "
            f"rows against the {s['exported_forecastable']:,} the export "
            f"publishes, differing on {s['flag_mismatch']:,}, and the "
            f"exported heating fits differ from v2's rule by up to "
            f"{s['fit_diff']:.4f} ({s['fit_presence']} present on one side "
            f"only). That is Sprint 4: `hdd-linear-v3` fits net of detected "
            f"loads, on the corrected completeness rule. The comparison "
            f"here is against **v2 refitted as at each date**, computed in "
            f"this script from the reads, and the look-ahead section "
            f"against the shipped fit is left out.")


def _f(x, spec: str, suffix: str = "") -> str:
    import math

    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    return f"{x:{spec}}{suffix}"


def write_stage2(out: Path, cohort: Cohort, snaps: Snapshots, knn_facts,
                 frame, span, k: int, runtime: float) -> Path:
    c, s, kf = cohort.facts, snaps.facts, knn_facts
    cal = cohort.cal
    seals = {**c["seals"], **{n: sealed_at(n) for n in MONTHEND_TABLES}}
    newest = max(t for t in seals.values() if t)
    altered = c.get("last_altered")
    results = gate(frame)
    passed = all(ok for _, _, ok in results)
    has_shipped = "shipped" in frame.columns
    note = export_note(snaps)
    held = populations(frame, "asat", held_out=True)
    h2h = held["h2h"]
    ci = bootstrap_ratio(h2h, "asat") if len(h2h) else (float("nan"),) * 2

    lines = [
        "# Botman backtest, Stage 2: month-end against `hdd-linear-v2`",
        "",
        f"Export sealed **{_fmt_time(newest)}** (the newest `_SUCCESS` of "
        + ", ".join(f"`{n}`" for n in seals) + "). Half-hourly reads from "
        f"`{WAREHOUSE_READS}` (last altered {_fmt_time(altered)}), checked "
        "against the export where they overlap. Champion "
        f"`{champion.MODEL_VERSION}`, imported. Run "
        f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC in "
        f"{runtime:.0f} s. Aggregates only.",
        *(["", note] if note else []),
        "",
        f"## Gate: {'PASS' if passed else 'FAIL'}",
        "",
        "Held-out cycles are those starting on or after "
        f"{cal.date(span['mid'])}, the middle of the served span "
        f"({cal.date(span['first'])} to {cal.date(span['last'])}). The "
        f"champion is v2 refitted as at each date. k = {k}.",
        "",
        "| Criterion | Result | |",
        "|---|---|---|",
    ]
    for name, value, ok in results:
        lines.append(f"| {name} | {value} | {'pass' if ok else '**fail**'} |")
    lines += [
        "",
        ("All three hold, so the plan recommends integrating kNN as a "
         "fallback. That is a separate decision: it needs a migration, a "
         "readiness flag and a wider Soda check on our side, and a reader "
         "in the backend."
         if passed else
         "Per the plan, fallback integration is not recommended. The "
         "report stands as a documented result."),
        "",
        "## Head to head, held-out cycles",
        "",
        "Snapshots both models forecast. CRPS and widths are per snapshot; "
        "v2's CRPS is its absolute error. APE is kNN's P50 or v2's point "
        "against the cycle's final kWh. v2's band is its ±15% of the kWh "
        "still to come; widths in NZD use the snapshot's own rate. "
        f"Mean final kWh: {_f(group_metrics(h2h, 'asat')['truth'], '.0f')}."
        if len(h2h) else "No snapshot was forecast by both models.",
        "",
        "| Days elapsed | Snapshots | kNN CRPS | v2 CRPS | Ratio "
        "| kNN MdAPE | v2 MdAPE | kNN cover | v2 cover "
        "| kNN width | v2 width |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    def h2h_row(label, part, fit):
        m = group_metrics(part, fit)
        return (f"| {label} | {m['n']:,} | {_f(m['knn_crps'], '.1f')} "
                f"| {_f(m['champ_crps'], '.1f')} | {_f(m['ratio'], '.2f')} "
                f"| {_f(m['knn_mdape'], '.1f', '%')} "
                f"| {_f(m['champ_mdape'], '.1f', '%')} "
                f"| {_f(m['knn_cover'], '.0%')} "
                f"| {_f(m['champ_cover'], '.0%')} "
                f"| ${_f(m['knn_width'], '.2f')} "
                f"| ${_f(m['champ_width'], '.2f')} |")

    for label, lo, hi in BUCKETS:
        part = h2h[h2h.elapsed.between(lo, hi)]
        if len(part):
            lines.append(h2h_row(label, part, "asat"))
    if len(h2h):
        lines.append(h2h_row("**All**", h2h, "asat"))
        lines += [
            "",
            f"- kNN's CRPS over v2's, accounts resampled: 95% interval "
            f"{ci[0]:.2f} to {ci[1]:.2f}.",
            f"- kNN has the lower mean CRPS on {win_rate(h2h, 'asat'):.0%} "
            f"of the {h2h.account.nunique()} accounts.",
            f"- Median band width in kWh: kNN "
            f"{_f(h2h.knn_width_kwh.median(), '.0f')}, v2 "
            f"{_f(h2h.asat_width_kwh.median(), '.0f')}.",
        ]

    fb = held["fallback"]
    lines += [
        "",
        "## Where v2 can't forecast, held-out cycles",
        "",
        "| Days elapsed | Snapshots | Accounts | kNN CRPS | kNN MdAPE "
        "| kNN cover | kNN width |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]

    def knn_row(label, part):
        m = group_metrics(part, None)
        return (f"| {label} | {m['n']:,} | {m['accounts']} "
                f"| {_f(m['knn_crps'], '.1f')} "
                f"| {_f(m['knn_mdape'], '.1f', '%')} "
                f"| {_f(m['knn_cover'], '.0%')} "
                f"| ${_f(m['knn_width'], '.2f')} |")

    for label, lo, hi in BUCKETS:
        part = fb[fb.elapsed.between(lo, hi)]
        if len(part):
            lines.append(knn_row(label, part))
    if len(fb):
        lines.append(knn_row("**All**", fb))
    lines += [
        "",
        "The NZD width is missing where the snapshot's implied rate is "
        "negative (export credit on the prior bill).",
        "",
        "## Coverage gain",
        "",
        "Snapshots up to the last day with weather "
        f"({s['last_weather']}), all cycles.",
        "",
        "| | Snapshots | Accounts |",
        "|---|---:|---:|",
    ]
    window = frame[frame.in_window & (frame.remaining >= 1)
                   & frame.truth.notna()]
    for label, part in (
            ("With a final kWh and days still to run", window),
            *((("v2 as shipped forecasts", window[window.shipped]),)
              if has_shipped else ()),
            ("v2 refitted as at each date forecasts", window[window.asat]),
            ("kNN forecasts", window[window.knn]),
            ("kNN forecasts, v2 (as at) doesn't",
             window[window.knn & ~window.asat]),
            ("Either forecasts", window[window.knn | window.asat])):
        lines.append(f"| {label} | {len(part):,} "
                     f"| {part.account.nunique()} |")

    lines += [
        "",
        "## By household group, held-out cycles",
        "",
        "| Group | Head to head | Ratio | kNN cover | Fallback "
        "| kNN MdAPE (fallback) | kNN cover (fallback) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, solar in (("No export register", False),
                         ("Export register (synthetic solar)", True)):
        a = h2h[h2h.solar == solar]
        b = fb[fb.solar == solar]
        ma = group_metrics(a, "asat") if len(a) else {}
        mb = group_metrics(b, None) if len(b) else {}
        lines.append(
            f"| {label} | {len(a):,} | {_f(ma.get('ratio'), '.2f')} "
            f"| {_f(ma.get('knn_cover'), '.0%')} | {len(b):,} "
            f"| {_f(mb.get('knn_mdape'), '.1f', '%')} "
            f"| {_f(mb.get('knn_cover'), '.0%')} |")

    if has_shipped:
        lines += [
            "",
            "## Look-ahead: v2 as shipped",
            "",
            "The shipped heating fit uses every month, including months "
            "after the snapshot, so these rows flatter v2. Held-out cycles.",
            "",
            "| Days elapsed | Snapshots | kNN CRPS | v2 CRPS | Ratio "
            "| kNN MdAPE | v2 MdAPE | kNN cover | v2 cover "
            "| kNN width | v2 width |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        ship = populations(frame, "shipped", held_out=True)["h2h"]
        for label, lo, hi in BUCKETS:
            part = ship[ship.elapsed.between(lo, hi)]
            if len(part):
                lines.append(h2h_row(label, part, "shipped"))
        if len(ship):
            lines.append(h2h_row("**All**", ship, "shipped"))
        ship_gate = gate(frame, "shipped")
        lines += [
            "",
            "The gate against the shipped fit, for reference: "
            + "; ".join(f"({n}) {value}, {'pass' if ok else 'fail'}"
                        for n, (_, value, ok) in enumerate(ship_gate, 1))
            + ".",
        ]
    else:
        lines += [
            "",
            "## Look-ahead: v2 as shipped",
            "",
            "Left out: the export's published fit is not v2's, so there is "
            "no shipped v2 to compare against. See the note above.",
        ]
    lines += [
        "",
        "## All cycles, not only held-out",
        "",
        "| Champion fit | Head to head | Ratio | kNN cover | v2 cover "
        "| Fallback | kNN MdAPE (fallback) | kNN cover (fallback) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for fit, label in FITS.items():
        if fit not in frame.columns:
            continue
        pops = populations(frame, fit, held_out=False)
        a, b = pops["h2h"], pops["fallback"]
        ma = group_metrics(a, fit) if len(a) else {}
        mb = group_metrics(b, None) if len(b) else {}
        lines.append(
            f"| {label} | {len(a):,} | {_f(ma.get('ratio'), '.2f')} "
            f"| {_f(ma.get('knn_cover'), '.0%')} "
            f"| {_f(ma.get('champ_cover'), '.0%')} | {len(b):,} "
            f"| {_f(mb.get('knn_mdape'), '.1f', '%')} "
            f"| {_f(mb.get('knn_cover'), '.0%')} |")
    beyond = frame[frame.knn & ~frame.in_window]
    if len(beyond):
        m = group_metrics(beyond, None)
        lines += [
            "",
            f"After the weather extract ends, kNN still serves "
            f"{m['n']:,} snapshots ({m['accounts']} accounts): MdAPE "
            f"{m['knn_mdape']:.1f}%, P10–P90 coverage {m['knn_cover']:.0%}. "
            "v2 can't run there, so they are outside the gate.",
        ]

    ov = c["overlap"]
    lines += [
        "",
        "## Data and checks",
        "",
        "| | |",
        "|---|---:|",
        f"| Cohort ICPs / households with import reads | "
        f"{c['cohort_icps']} / {c['households']} |",
        f"| Warehouse import reads | {c['import_reads']:,} "
        f"({c['first_day']} to {c['last_day']}) |",
        f"| … the export's 13 months, compared read by read | "
        f"{ov['rows']:,} rows, {'identical' if ov['identical'] else 'DIFFERENT'} "
        f"to the export ({ov['export_rows']:,} rows) |",
        f"| … with no kWh / repeated / off the half-hour | "
        f"{c['null_kwh']:,} / {c['repeated_reads']:,} / "
        f"{c['off_half_hour']:,} |",
        f"| Complete household-days (kNN) | "
        f"{int(cohort.complete.sum()):,} |",
        f"| Fully read household-days (refit) | "
        f"{int(cohort.read_all.sum()):,} |",
        f"| … households without an export register, days where that "
        f"differs from the mart's rule | {s['plain_differs']:,} |",
        f"| … households with one: complete days by the mart's rule / "
        f"fully read | {s['solar_old']:,} / {s['solar_new']:,} |",
        f"| Library weeks (7 complete days) | {kf['library_weeks']:,} |",
        f"| Cohort forecast-input rows / accounts | {s['rows']:,} / "
        f"{s['accounts']} |",
        f"| … with the cycle's final kWh | {s['with_truth']:,} "
        f"({s['cycles_with_truth']:,} cycles) |",
        f"| `kwh_to_date` against the daily mart, largest difference | "
        f"{s['to_date_diff']:.4f} kWh |",
        f"| Exported heating fits replicated by v2's rule (ICPs) | "
        f"{s['fits_exported']} (largest difference {s['fit_diff']:.4f}; "
        f"{s['fit_presence']} present on one side only) |",
        f"| `is_forecastable` in the export | "
        f"{s['exported_forecastable']:,} (Sprint 3 published "
        f"{PUBLISHED_FORECASTABLE:,}) |",
        f"| … rebuilt here | {s['shipped']:,} across "
        f"{s['shipped_accounts']} accounts, {s['flag_mismatch']} rows "
        "differing |",
        f"| Eligible with the as-at refit | {s['asat']:,} across "
        f"{s['asat_accounts']} accounts |",
        f"| Accounts with an export register: eligible as shipped / as at | "
        f"{s['shipped_solar_accounts']} / {s['asat_solar_accounts']} of "
        f"{s['solar_accounts']} |",
        f"| Last day with weather | {s['last_weather']} |",
        f"| Snapshots kNN served | {kf['served']:,} |",
        f"| … not served: no household matrix / week incomplete | "
        f"{kf['no_household']:,} / {kf['no_week']:,} |",
        f"| … not served: final day / no final kWh / fewer than k weeks | "
        f"{kf['final_day']:,} / {kf['no_truth']:,} / {kf['too_few']:,} |",
        f"| Neighbours from the household's own history | "
        f"{kf['own_share']:.1%} |",
        f"| `min_heating_fit_r2` (dbt_project.yml) | {s['threshold']:.2f} |",
        "",
        "## Settings",
        "",
        "- **kNN:** the household's 7 days up to the as-at date, divided by "
        f"their maximum; the k = {k} nearest weeks of any household; each "
        "brings its whole continuation for the days left in the cycle, "
        "summed and rescaled. Projected kWh is `kwh_to_date` plus each "
        "total; P10, P50 and P90 are taken over those totals.",
        "- **Leakage:** a week and its continuation end on or before the "
        "as-at date. `test_knn_quantile.py` pins it.",
        "- **v2 refitted as at each date:** the monthly heating fit on months "
        "that ended by the as-at date, ≥ 20 fully read days each, ≥ 6 "
        "months, OLS, rounded as dbt rounds; eligibility then follows "
        "`mart_forecast_input` with that fit. Fully read means every import "
        "register read every half-hour, Sprint 4's corrected rule, so "
        "households with an export register can qualify here and not as "
        "shipped.",
        "- **v2 as shipped:** the exported slope and baseline, fitted on "
        "every month.",
        "- **Truth:** the cycle's import kWh from `kea.mart_daily_usage`, "
        "only where every day of the cycle has a row. Snapshots on a "
        "cycle's last day have nothing left to forecast and are skipped.",
        "- **CRPS:** the ensemble form; for v2's single value it is the "
        "absolute error. Coverage is the share of final kWh inside "
        "P10–P90, or inside v2's ±15% of the kWh still to come.",
        f"- **Interval:** {BOOTSTRAP_DRAWS} bootstrap draws of accounts, "
        f"seed {BOOTSTRAP_SEED}.",
        "- **Why the warehouse:** the export keeps 13 months of half-hourly "
        "reads, and the weather v2 needs ends before that window starts. "
        "The reads come from one read-only SELECT; everything else is the "
        "sealed export.",
        "",
    ]
    out.mkdir(parents=True, exist_ok=True)
    path = out / "stage2_monthend.md"
    path.write_text("\n".join(lines))
    return path


# ---------------------------------------------------------------------------
# Stage 3: extras on the month-end run

POOL_SEED = 20260918        # the household order a smaller pool draws from
GRID_K = (50, 100, 200)
STAGE2_REPORT = "stage2_monthend.md"


@dataclass(frozen=True)
class Run:
    """One scored setting: what it was, what it served, what it scored."""
    label: str
    facts: dict
    frame: object


def pool_masks(cohort: Cohort, sizes) -> dict:
    """size -> boolean mask over the cohort's households, nested by size.

    One seeded order, truncated: the 10 are the first 10 of the 25, so the
    curve is one pool growing and not three unrelated draws.
    """
    import numpy as np

    usable = np.flatnonzero(cohort.complete.any(axis=1))
    order = np.random.default_rng(POOL_SEED).permutation(usable)
    out = {}
    for size in sizes:
        mask = np.zeros(len(cohort.icps), dtype=bool)
        mask[order[:size]] = True
        out[size] = mask
    return out


def common_rows(frames):
    """Held-out, in-window snapshots every one of `frames` served."""
    import numpy as np

    first = frames[0]
    mask = (first.in_window & first.held_out).to_numpy()
    for frame in frames:
        mask &= frame.knn.to_numpy()
    return np.flatnonzero(mask)


def _run_metrics(run: Run, rows, fit: str | None = "asat") -> dict:
    part = run.frame.iloc[rows]
    if fit and not part[fit].any():
        fit = None
    return group_metrics(part, fit)


def write_stage3(out: Path, cohort: Cohort, snaps: Snapshots, k: int, span,
                 base: Run, seasonal: Run | None, pools, grid_runs,
                 runtime: float) -> Path:
    """`stage3_extras.md`: the baseline, then each extra beside it."""
    import numpy as np

    c, s = cohort.facts, snaps.facts
    cal = cohort.cal
    seals = {**c["seals"], **{n: sealed_at(n) for n in MONTHEND_TABLES}}
    newest = max(t for t in seals.values() if t)
    note = export_note(snaps)
    stage2 = out / STAGE2_REPORT
    earlier = None
    if stage2.exists():
        match = re.search(r"Export sealed \*\*(.+?)\*\*", stage2.read_text())
        earlier = match.group(1) if match else None

    def gate_table(frame):
        rows = ["| Criterion | Result | |", "|---|---|---|"]
        results = gate(frame)
        for name, value, ok in results:
            rows.append(f"| {name} | {value} | "
                        f"{'pass' if ok else '**fail**'} |")
        return rows, all(ok for _, _, ok in results)

    base_rows, base_passed = gate_table(base.frame)
    lines = [
        "# Botman backtest, Stage 3: extras on the month-end run",
        "",
        f"Export sealed **{_fmt_time(newest)}** (the newest `_SUCCESS` of "
        + ", ".join(f"`{n}`" for n in seals) + "). Half-hourly reads from "
        f"`{WAREHOUSE_READS}` (last altered {_fmt_time(c.get('last_altered'))})"
        ", checked against the export where they overlap. Champion "
        f"`{champion.MODEL_VERSION}` refitted as at each date, imported. Run "
        f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC in "
        f"{runtime / 60:.0f} min. Aggregates only.",
        *(["", note] if note else []),
    ]
    if earlier and earlier != _fmt_time(newest):
        lines += [
            "",
            f"**Not comparable with Stage 2's report,** which read the "
            f"export sealed {earlier}. Every figure below was measured on "
            f"today's export, the baseline row included, so the extras are "
            f"compared only with each other and with that baseline.",
        ]
    lines += [
        "",
        "## The baseline on this export",
        "",
        f"The paper's setting — k = {k}, week normalisation, the whole "
        f"cohort — on the export named above. Held-out cycles start "
        f"{cal.date(span['mid'])}; every extra is judged on the same "
        f"boundary.",
        "",
        *base_rows,
        "",
        f"Gate: **{'PASS' if base_passed else 'FAIL'}** on this export.",
    ]

    if seasonal is not None:
        rows, passed = gate_table(seasonal.frame)
        both = common_rows([base.frame, seasonal.frame])
        a, b = _run_metrics(base, both), _run_metrics(seasonal, both)
        lines += [
            "",
            "## Seasonal neighbours (`--variant seasonal`)",
            "",
            f"A neighbour's continuation must start within "
            f"{kq.SEASONAL_DAYS} days of the as-at day's time of year and "
            f"at least {kq.SEASONAL_MIN_LAG} days earlier — over a 13-month "
            "span, the same season a year back. This is a different model, "
            "so the gate is applied again.",
            "",
            *rows,
            "",
            f"Gate: **{'PASS' if passed else 'FAIL'}**.",
            "",
            "Head to head on the "
            f"{len(both):,} held-out snapshots both settings served:",
            "",
            "| | Snapshots served | kNN CRPS | kNN MdAPE | kNN cover "
            "| Band width (kWh) | Too few weeks |",
            "|---|---:|---:|---:|---:|---:|---:|",
            f"| Baseline (all weeks) | {base.facts['served']:,} "
            f"| {_f(a['knn_crps'], '.1f')} "
            f"| {_f(a['knn_mdape'], '.1f', '%')} "
            f"| {_f(a['knn_cover'], '.0%')} "
            f"| {_f(a['knn_width_kwh'], '.0f')} "
            f"| {base.facts['too_few']:,} |",
            f"| Seasonal | {seasonal.facts['served']:,} "
            f"| {_f(b['knn_crps'], '.1f')} "
            f"| {_f(b['knn_mdape'], '.1f', '%')} "
            f"| {_f(b['knn_cover'], '.0%')} "
            f"| {_f(b['knn_width_kwh'], '.0f')} "
            f"| {seasonal.facts['too_few']:,} |",
            "",
            f"Seasonal CRPS over the baseline's on those snapshots: "
            f"{b['knn_crps'] / a['knn_crps']:.2f} ×. The filter also costs "
            f"coverage: a snapshot is served only where "
            f"{k} weeks survive the ±{kq.SEASONAL_DAYS}-day window, which "
            f"is why the served count falls to "
            f"{seasonal.facts['served'] / base.facts['served']:.0%} of the "
            "baseline's.",
        ]

    if pools:
        lines += [
            "",
            "## Library size (`--pool-sizes`)",
            "",
            "Each pool is the first N households of one seeded order, so "
            "the 10 are inside the 25. Only that pool's households bring "
            "weeks, and only their snapshots are scored; the last column is "
            "the whole cohort's library on exactly those snapshots, which "
            "is the like-for-like comparison.",
            "",
            "| Households | Library weeks | Snapshots served | kNN CRPS "
            "| kNN MdAPE | kNN cover | Whole cohort, same snapshots "
            "| Ratio |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for run in pools:
            rows = common_rows([base.frame, run.frame])
            a, b = _run_metrics(base, rows), _run_metrics(run, rows)
            ratio = b["knn_crps"] / a["knn_crps"] if a["knn_crps"] else np.nan
            lines.append(
                f"| {run.facts['pool_households']} "
                f"| {run.facts['library_weeks']:,} "
                f"| {run.facts['served']:,} | {_f(b['knn_crps'], '.1f')} "
                f"| {_f(b['knn_mdape'], '.1f', '%')} "
                f"| {_f(b['knn_cover'], '.0%')} "
                f"| {_f(a['knn_crps'], '.1f')} | {_f(ratio, '.2f')} |")
        lines += [
            "",
            f"The cohort has {base.facts['pool_households']} households with "
            "any complete day, so a pool at or above that size is the whole "
            "cohort and its ratio is 1.00 by construction.",
        ]

    if grid_runs:
        rows = common_rows([base.frame] + [r.frame for r in grid_runs])
        cells = {(r.facts["k"], r.facts["norm"]): _run_metrics(r, rows)
                 for r in grid_runs}
        crps = {key: m["knn_crps"] for key, m in cells.items()}
        best, worst = min(crps.values()), max(crps.values())
        lines += [
            "",
            "## k and normalisation (`--grid`), in-sample",
            "",
            f"Mean CRPS on the {len(rows):,} held-out snapshots every "
            "setting served. In-sample: nothing is held back from this "
            "table, and it does not pick the headline, which stays at the "
            f"paper's k = {kq.PAPER_K} with week normalisation.",
            "",
            "| k | " + " | ".join(kq.NORMS) + " |",
            "|---:|" + "---:|" * len(kq.NORMS),
        ]
        for kk in GRID_K:
            lines.append(f"| {kk} | " + " | ".join(
                _f(crps.get((kk, nm)), '.1f') for nm in kq.NORMS) + " |")
        lines += [
            "",
            "| k | " + " | ".join(f"{nm} MdAPE / cover"
                                  for nm in kq.NORMS) + " |",
            "|---:|" + "---:|" * len(kq.NORMS),
        ]
        for kk in GRID_K:
            cell = []
            for nm in kq.NORMS:
                m = cells.get((kk, nm), {})
                cell.append(f"{_f(m.get('knn_mdape'), '.1f', '%')} / "
                            f"{_f(m.get('knn_cover'), '.0%')}")
            lines.append(f"| {kk} | " + " | ".join(cell) + " |")
        lines += [
            "",
            f"Best to worst: {(worst - best) / best:.0%} of the best mean "
            f"CRPS. The paper's LCL grid (Table 3b) spans 4%, and k barely "
            "matters there; a much wider spread here would say the method "
            "is sensitive to a choice the paper treats as settled.",
        ]

    lines += [
        "",
        "## Data and checks",
        "",
        "| Run | Library weeks | Pool | Served | Too few weeks "
        "| Own history |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for run in [base] + ([seasonal] if seasonal else []) + list(pools) \
            + list(grid_runs):
        f = run.facts
        lines.append(
            f"| {run.label} | {f['library_weeks']:,} "
            f"| {f['pool_households']} | {f['served']:,} "
            f"| {f['too_few']:,} | {_f(f['own_share'], '.1%')} |")
    lines += [
        "",
        f"Cohort forecast-input rows {s['rows']:,} across {s['accounts']} "
        f"accounts; {s['with_truth']:,} have the cycle's final kWh. "
        f"Eligible with the as-at refit: {s['asat']:,} across "
        f"{s['asat_accounts']} accounts. Last day with weather "
        f"{s['last_weather']}.",
        "",
        "## Settings",
        "",
        "- **Seasonal:** `knn_quantile.seasonal_rows()`; the continuation's "
        f"first day sits within ±{kq.SEASONAL_DAYS} days of the as-at day's "
        f"day of the year and at least {kq.SEASONAL_MIN_LAG} days before "
        "it. The leakage rule still applies on top. A leap year shifts a "
        "neighbour by one day inside that window.",
        "- **Normalisation:** `week` divides a window by its largest "
        "half-hour (the paper's), `profile` by its mean, so only the shape "
        "is compared, `none` compares raw kWh. The same divisor rescales "
        "the neighbour's continuation back.",
        f"- **Pools:** one seeded permutation (seed {POOL_SEED}) of the "
        "households with any complete day, truncated to each size.",
        "- **Held-out:** the baseline run's boundary, reused by every "
        "extra, so a setting that serves fewer snapshots is not judged on "
        "a different set of cycles.",
        "- **Everything else** — the champion, the truth, CRPS, coverage, "
        "the bootstrap — is Stage 2's, unchanged.",
        "",
    ]
    out.mkdir(parents=True, exist_ok=True)
    path = out / "stage3_extras.md"
    path.write_text("\n".join(lines))
    return path


def write_stage3_metrics(out: Path, runs) -> Path:
    """One row per run and population, beside the reports."""
    import csv

    keys = ["run", "k", "norm", "variant", "pool_households", "cycles",
            "population", "days_elapsed", "n", "accounts", "knn_crps",
            "knn_mdape", "knn_cover", "knn_width_kwh", "champ_crps", "ratio"]
    rows = []
    for run in runs:
        f = run.facts
        for pop in ("knn", "h2h", "fallback"):
            part = populations(run.frame, "asat", held_out=True)[pop]
            if not len(part):
                continue
            rows.append({"run": run.label, "k": f["k"], "norm": f["norm"],
                         "variant": f["variant"],
                         "pool_households": f["pool_households"],
                         "cycles": "held_out", "population": pop,
                         "days_elapsed": "all",
                         **group_metrics(part, "asat" if pop == "h2h"
                                         else None)})
    out.mkdir(parents=True, exist_ok=True)
    path = out / "stage3_metrics.csv"
    with path.open("w", newline="") as handle:
        # lineterminator is not the default: csv writes \r\n per RFC 4180, so a
        # committed result file arrives as CRLF and `git diff --check` reports
        # every line as trailing whitespace. Same fix as load_weather.py.
        writer = csv.DictWriter(handle, fieldnames=keys,
                                extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: (f"{row[key]:.6g}"
                                   if isinstance(row.get(key), float)
                                   else row.get(key, ""))
                             for key in keys})
    return path


def monthend(k: int, out: Path, variant: str = "all", pool_sizes=(),
             grid: bool = False, shipped_fit: str = "check") -> int:
    started = time.monotonic()
    cohort = load_cohort("warehouse")
    snaps = load_snapshots(cohort, min_heating_fit_r2())
    s = snaps.facts
    if shipped_fit == "check" and (s["flag_mismatch"]
                                   or s["shipped"] != s["exported_forecastable"]):
        raise SystemExit(
            f"the rebuilt is_forecastable differs from the export on "
            f"{s['flag_mismatch']} rows ({s['shipped']} against "
            f"{s['exported_forecastable']}); the replay is wrong, stopping. "
            "If the export ships a champion this script does not replicate "
            "(Sprint 4's hdd-linear-v3), --shipped-fit skip compares "
            "against the as-at refit alone and says so in the report")
    fits = list(FITS) if shipped_fit == "check" else ["asat"]
    champions = {fit: replay_champion(snaps, fit) for fit in fits}
    knn, knn_facts = score_monthend(cohort, snaps, k)
    frame, span = monthend_frame(snaps, knn, champions)
    print(f"is_forecastable rebuilt: {s['shipped']:,} "
          f"(export {s['exported_forecastable']:,}); as-at refit "
          f"{s['asat']:,}")
    print(f"kNN served {knn_facts['served']:,} snapshots")
    for name, value, ok in gate(frame):
        print(f"  {'pass' if ok else 'FAIL'}  {name}: {value}")

    if variant == "all" and not pool_sizes and not grid:
        path = write_stage2(out, cohort, snaps, knn_facts, frame, span, k,
                            time.monotonic() - started)
        csv_path = write_metrics(out, metrics_rows(frame))
        print(f"wrote {path}")
        print(f"wrote {csv_path}")
        return 0

    # Stage 3: the same snapshots, scored again with one dial moved. The
    # baseline above is what each extra is compared with, because Stage 2's
    # report may have read another export.
    base = Run("baseline (paper's setting)", knn_facts, frame)

    def scored(label, **dials):
        scores, facts = score_monthend(cohort, snaps, dials.pop("k", k),
                                       **dials)
        other, _ = monthend_frame(snaps, scores, champions, mid=span["mid"])
        print(f"  {label}: served {facts['served']:,}")
        return Run(label, facts, other)

    seasonal = None
    if variant == "seasonal":
        seasonal = scored("seasonal", variant="seasonal")
        for name, value, ok in gate(seasonal.frame):
            print(f"    {'pass' if ok else 'FAIL'}  {name}: {value}")
    pools = [scored(f"pool of {size}", pool=mask) for size, mask
             in pool_masks(cohort, pool_sizes).items()]
    grid_runs = []
    if grid:
        grid_runs = [scored(f"k={kk}, {nm}", k=kk, norm=nm)
                     for kk in GRID_K for nm in kq.NORMS]

    runs = [base] + ([seasonal] if seasonal else []) + pools + grid_runs
    path = write_stage3(out, cohort, snaps, k, span, base, seasonal, pools,
                        grid_runs, time.monotonic() - started)
    csv_path = write_stage3_metrics(out, runs)
    print(f"wrote {path}")
    print(f"wrote {csv_path}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="stage", required=True)
    stage1 = sub.add_parser(
        "reproduce", help="Stage 1: day-ahead, against the paper's baselines")
    stage1.add_argument("--k", type=int, default=kq.PAPER_K)
    stage1.add_argument("--out", type=Path, default=Path("/out/results"))
    stage2 = sub.add_parser(
        "monthend", help="Stage 2: month-end, against hdd-linear-v2; "
                         "Stage 3 with any of the dials below")
    stage2.add_argument("--k", type=int, default=kq.PAPER_K)
    stage2.add_argument("--out", type=Path, default=Path("/out/results"))
    stage2.add_argument(
        "--variant", choices=("all", "seasonal"), default="all",
        help="Stage 3: 'seasonal' keeps only neighbours from the same time "
             "of year a year back")
    stage2.add_argument(
        "--pool-sizes", default="", metavar="10,25,50",
        help="Stage 3: repeat the run with libraries of this many "
             "households")
    stage2.add_argument(
        "--grid", action="store_true",
        help=f"Stage 3: k in {GRID_K} x normalisation in {kq.NORMS}, "
             "in-sample, never used to pick the headline")
    stage2.add_argument(
        "--shipped-fit", choices=("check", "skip"), default="check",
        help="'check' stops when the export's is_forecastable is not v2's; "
             "'skip' compares against the as-at refit alone, for an export "
             "that ships another champion")
    args = parser.parse_args(argv)
    if args.stage == "reproduce":
        return reproduce(args.k, args.out)
    if args.stage == "monthend":
        sizes = tuple(int(n) for n in args.pool_sizes.split(",")
                      if n.strip())
        return monthend(args.k, args.out, args.variant, sizes, args.grid,
                        args.shipped_fit)
    return 2


if __name__ == "__main__":
    sys.exit(main())
