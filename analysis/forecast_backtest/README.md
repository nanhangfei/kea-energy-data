# Forecast backtest: kNN quantiles (Botman et al., 2025) against `hdd-linear-v2`

**Question:** can a kNN-quantile forecast serve as a **fallback** where the production bill forecast
gives none, and is its uncertainty band better calibrated than the production ±15%?

**Answer (200-household cohort, export of 2 Oct 2026):** yes on both counts. The gate passes, and the
production band turns out to contain the final usage only about half the time. Nothing is wired into
serving; whether to adopt the fallback is a product decision (design §12, §15.12).

## Results

| Gate clause (fixed before the first run) | Threshold | Result | |
|---|---|---|---|
| kNN's P10–P90 coverage | 70–90% | **81.1%** over 29,302 snapshots | pass |
| Accuracy where the champion declines | median APE ≤ champion's own + 5 points (10.9%) | **5.6%** over 10,863 snapshots | pass |
| Head to head | mean CRPS ≤ 1.10× champion's | **0.85×** over 18,439 snapshots (95% CI 0.78–0.93) | pass |

**Head to head, by days elapsed in the billing cycle** (snapshots both models forecast):

| Days elapsed | kNN CRPS | Champion CRPS | kNN coverage | ±15% band coverage | kNN width | ±15% width |
|---|---:|---:|---:|---:|---:|---:|
| 1–6 | 117.6 | 110.8 | 78% | 60% | $76.08 | $40.35 |
| 7–14 | 79.0–90.1 | 86.1–97.6 | 78–79% | 55–58% | $50–61 | $27–34 |
| 15–19 | 52.3 | 69.8 | 80% | 53% | $35.89 | $19.74 |
| 20+ | 22.6 | 42.3 | 80% | **41%** | $14.98 | $8.20 |
| **All** | **63.9** | **75.0** | **79%** | **51%** | **$38.92** | **$21.54** |

**What it means:**
- **The ±15% band is not a calibrated interval.** It is narrow because ±15% was chosen, and it contains
  the outcome least often late in the cycle, when it should be easiest.
- **kNN buys calibration with width**: median band 140 kWh against 77.
- **As a fallback it extends coverage**: snapshots with a forecast rise from **23,152 to 61,234** (148 →
  194 accounts).
- kNN has the lower CRPS on **66% of 146 accounts**.

**Supporting stages:**
- **Stage 1 (reproduce the paper):** kNN beats probabilistic persistence by **+18.9%** (paper: +27.2%) and
  is level with historical sampling (+1.2%, CI −1.4% to +4.2%; paper: +7.0%).
- **Stage 3 (extras):** a seasonal-neighbour library is **rejected**: CRPS 1.96× the plain kNN's, with
  only 57% of snapshots served. Pool size stops mattering beyond ~25 households (10 households: 1.18×;
  25: 1.04×; 100: 0.98×).

Full reports: [`results/cohort-200-2026-10-02/`](results/cohort-200-2026-10-02/).

## Method

1. **Query:** a household's last 7 complete days (7 × 48 half-hours), divided by that week's peak
   half-hour.
2. **Library:** every 7-day window of every cohort household (1-day step), each normalised by its own
   peak, with the days that followed it.
3. **Neighbours:** the k = 100 nearest windows by Euclidean distance.
4. **Forecast:** the neighbours' continuations × the query week's peak: an empirical distribution per
   half-hour.

**Month-end forecasts** sum each neighbour's whole remaining-cycle continuation. Per-day quantiles are
never summed, because the sum of P90s is not the P90 of the sum.

**Leakage rule:** a window may be used for a forecast made before day *d* only if the window **and its
continuation** end before *d*, for every household. This is stricter than the paper, enforced in code,
and tested.

**Daily matrix:** NZ local days mapped to 48 slots; a 46-half-hour DST day interpolates the skipped
hour, a 50-half-hour day sums the repeated one. An isolated missing half-hour is interpolated; any
other gap makes the day unusable as a query, window or continuation.

**Scores:** CRPS in its exact ensemble form (`E|X−y| − ½E|X−X′|`; for a point forecast, the absolute
error), the paper's pinball-loss form alongside, and P10–P90 coverage.

**The champion** is `build_bill_forecasts.project_forecast()`, imported rather than copied, with its
heating fit **refitted as at each date** so it never sees the future. Truth is the cycle's metered import
from `kea.mart_daily_usage`, only where every day of the cycle is present. Held-out cycles start on or
after the middle of the served span.

## Running it

| Stage | Question | Command | Report |
|---|---|---|---|
| 1 | Does our kNN behave like the paper's, day-ahead? | `make backtest-reproduce` | `stage1_reproduce.md` |
| 2 | Is it a usable fallback for the month-end bill? | `make backtest-forecast` | `stage2_monthend.md`, `metrics.csv` |
| 3 | Do a seasonal library, pool size or other settings change the answer? | `make backtest-forecast ARGS="--variant seasonal --pool-sizes 10,25,50,100,200 --grid"` | `stage3_extras.md`, `stage3_metrics.csv` |

Each target first runs the 43 tests in `pipeline/tests/test_knn_quantile.py` inside the `dataeng/dbt`
image and refuses to score if they fail. CI runs them the same way (`make ci ONLY=backtest`) and fails
on any skip.

**Options:**
- `--shipped-fit skip`: the export now ships `hdd-linear-v3`, so the as-shipped comparison against v2 is
  dropped; the headline (v2 refitted as at each date) is unaffected.
- Only the seasonal variant is judged against the gate again; pool sizes and the k × normalisation grid
  are diagnostics and never pick the headline.

**Data access:**
- Reads the sealed lake export (checked by its `_SUCCESS` seal) and never the serving database.
- Stage 2 also runs one read-only `SELECT` on `KEA_GOLD.FCT_METER_READ`, because the export keeps only 13
  months of half-hourly reads. Where the two overlap they must match read for read, or the run stops.
- Writes only to `results/`. Results are aggregates: no ICP numbers, per-household rows or neighbour
  traces.

**Code:** `pipeline/scripts/knn_quantile.py` (the model, pure numpy, no I/O),
`pipeline/scripts/backtest_bill_forecast.py` (the harness), and
`analysis/metabase/build_backtest_dashboard.py` (`make dashboards ONLY=backtest`, built from the
committed results).

## Which export a report used

Each report names the export it scored by its seal time, and figures may be compared only **within**
one export.

| Report | Export sealed | Cohort |
|---|---|---|
| `results/cohort-200-2026-10-02/` (stages 1–3) | 2026-10-02 | 200 households, current |
| `results/stage1_reproduce.md`, `stage2_monthend.md` | 2026-09-12 | 50 households |
| `results/stage2_monthend_sprint4_export.md`, `stage3_extras.md` | 2026-09-17 | 50 households, after the detected-load rebuild |
