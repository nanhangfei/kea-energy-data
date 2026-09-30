# Forecast backtest: kNN quantiles (Botman et al. 2025) against `hdd-linear-v2`

An offline question, answered before anything is published. Could a kNN-quantile forecast serve as a
**fallback** for the bill forecast where `hdd-linear-v2` gives none, and is its band better than ±15%?

- **Paper:** L. Botman et al., *A global probabilistic approach for short-term forecasting of
  individual households electricity consumption*, Applied Energy 382 (2025) 125168.
- **Reads:** the sealed lake export (`export/…` in MinIO, checked by its `_SUCCESS` seal), and never
  kea-db.
  - **Stage 1** reads nothing else and spends no Snowflake credits.
  - **Stage 2** also runs one read-only `SELECT` on `KEA_GOLD.FCT_METER_READ`, for the cohort's
    import reads. The export keeps only the last 13 months of half-hourly reads, and the weather
    `hdd-linear-v2` needs ends before that window starts, so the export alone leaves no snapshot
    both models can forecast.
  - **The check:** where the two overlap, the warehouse reads must equal the export's read for read,
    or the run stops. The report gives the table's last-altered time beside the export's seal.
- **Writes:** only `results/` here. Nothing goes to serving: there is no schema change, and
  `build_bill_forecasts.py` is unchanged.
- **Privacy:** results are aggregates only. They contain no ICP numbers, no per-household rows and
  no neighbour traces.
- **On a dashboard:** `make dashboards ONLY=backtest` builds *Forecast backtest — kNN quantiles against
  hdd-linear-v2* from the files in `results/`: the gate, Stage 2 by days elapsed, Stage 1 against the
  paper, and Stage 3's settings. It reads no table, so after a re-run, commit the results and rebuild
  the page. A report whose table has moved or changed shape stops that build rather than drawing
  something else (`analysis/metabase/build_backtest_dashboard.py`).
- **Code:**
  - `pipeline/scripts/knn_quantile.py`: the model and its scores, as pure numpy;
  - `pipeline/scripts/backtest_bill_forecast.py`: reads the export, runs each stage and writes
    the reports;
  - `pipeline/tests/test_knn_quantile.py`: the tests. They skip on the stdlib-only host and run
    in the `dataeng/dbt` image before each stage.

## The method

1. **Query.** Take a household's last 7 complete days, 7 × 48 half-hours. Divide them by that
   week's largest half-hour ("week normalisation").
2. **Library.** Every 7-day window of every cohort household (1-day step), each divided by its own
   maximum, together with the day or days that followed it.
3. **Neighbours.** The k = 100 windows nearest the query by Euclidean distance.
4. **Forecast.** The neighbours' continuations × the query week's maximum. That gives k equally
   weighted values per half-hour, an empirical distribution.

**Leakage rule.** A window can be used for a forecast made before day d only when the window
*and* its continuation end before d, for every household. This is stricter than the paper's
wording (§3.3), and it is enforced in code and tested.

**Daily matrix.**
- Days are NZ local days mapped to 48 slots:
  - a 46-half-hour DST day fills the skipped hour by interpolation;
  - a 50-half-hour day sums the repeated hour.
- An isolated missing half-hour is interpolated, as in the paper. Any other gap makes the day
  incomplete, and an incomplete day is never a query, a window or a continuation.

**Scores.**
- **CRPS:** the exact ensemble form `E|X−y| − ½E|X−X′|`. For a single value it is the absolute
  error, so a point forecast is scored on the same scale.
- **The paper's quantile form:** the pinball loss at the levels 0.01–1.00, reported alongside.
- **Band coverage:** the share of actual values that fall inside P10–P90.

## Three stages, each started only on a go

| Stage | Question | Command | Report |
|---|---|---|---|
| 1. Reproduce | Does our kNN behave like the paper's, day-ahead? | `make backtest-reproduce` | `results/stage1_reproduce.md` |
| 2. Month-end against the champion | Is it a usable fallback for the bill? | `make backtest-forecast` | `results/stage2_monthend.md`, `results/metrics.csv` |
| 3. Extras (optional) | Would a seasonal library, a bigger pool or other settings change the answer? | `make backtest-forecast ARGS="--variant seasonal --pool-sizes 10,25,50 --grid"` | `results/stage3_extras.md`, `results/stage3_metrics.csv` |

Pass options through `ARGS`, for example `make backtest-reproduce ARGS="--k 50"`. The headline
always uses the paper's LCL setting: k = 100 with week normalisation.

### Stage 1: reproduce

- **What it compares:** kNN against the paper's two baselines, on the same household-days:
  - **historical sampling:** every earlier day on the same weekday;
  - **probabilistic persistence:** yesterday plus every earlier day-on-day change, clipped at 0.
- **Scored:** every complete household-day in the later half of the span whose previous 7 days
  are complete.
- **Compare gains, not absolute CRPS.** The paper's LCL gains are 7.0% over historical sampling
  and 27.2% over persistence (Table 1b, 363 households). With 50 households, expect less.
- **Review point:**
  - **kNN clearly beats persistence** (the household-bootstrap interval stays above zero): Stage 2
    may start.
  - **It loses to persistence:** the implementation is wrong. Debug it before anything else.
  - **It beats persistence but loses to historical sampling:** run Stage 3's pool-size curve
    first. A small pool is the paper's own stated failure mode (§5).

### Stage 2: month-end against `hdd-linear-v2`, with the gate

**How kNN forecasts the rest of a cycle.** Each neighbour carries its whole R-day continuation,
where R is the number of days left in the billing cycle. Per-day quantiles aren't summed, because
the sum of P90s is not the P90 of the sum.
- Projected kWh = `kwh_to_date` + each neighbour's continuation total × the query week's maximum.
- A window and its R-day continuation must end on or before the as-at date.

**The champion.** `build_bill_forecasts.project_forecast()`, imported rather than copied, is
replayed on the same `is_forecastable` snapshots.
- **Headline:** its heating fit is recomputed as at each date. Fitted as shipped, it has seen
  the future.
- **Also reported:** the as-shipped replay, labelled as look-ahead.

**The replay is checked first.** `is_forecastable` is rebuilt from the exported inputs and the
replicated heating fit. If it differs from the exported flag on any row, the run stops.

**Truth.** The cycle's import kWh from `kea.mart_daily_usage`, only where every day of the cycle has
a row. That equals `kwh_to_date` on a complete final snapshot.

**Held-out cycles** start on or after the middle of the span kNN served, counting only as-at dates
that have weather.

**Scoring,** in kWh and bucketed by days elapsed:
- CRPS;
- the median absolute percentage error;
- P10–P90 coverage beside the ±15% band's, and the median width of each band;
- the extra coverage kNN gives where the champion has no forecast;
- the per-account win rate.

**The gate.** Written down before the first run. Fallback integration is recommended only if
**all three** hold on held-out cycles:
1. kNN's P10–P90 coverage is between 70% and 90%.
2. Where the champion can't forecast, kNN's median APE is no worse than the champion's own median
   APE where it can, plus 5 points.
3. Head to head, kNN's mean CRPS is at most 10% worse than the champion's.

### Stage 3: extras (optional)

These are flags on Stage 2's run. Any of them turns the run into the extras report, and each dial
is scored **beside a baseline at the paper's setting on the same export**, so nothing depends on
an earlier report having read the same data.

- `--variant seasonal`: a neighbour's continuation must start within **±30 days** of the as-at
  day's day of the year and at least **180 days** before it. Over a 13-month span that is the same
  season a year back, and it is this backtest's stand-in for weather, which the paper does not use.
  The leakage rule still applies on top. A seasonal library is much smaller, so a snapshot is
  served only where k weeks survive the window; the report gives how many it lost.
- `--pool-sizes 10,25,50`: the run repeated with a library of that many households. The pools are
  nested — one seeded order, truncated — so the 10 sit inside the 25. Only the pool's households
  bring weeks, and only their snapshots are scored; the report puts the whole cohort's library on
  exactly those snapshots beside each pool, which is the like-for-like comparison.
- `--grid`: k ∈ {50, 100, 200} × normalisation ∈ {week, profile, none}, scored on the snapshots
  every setting served. `week` divides a window by its largest half-hour (the paper's), `profile`
  by its mean, so only the shape is compared, and `none` compares raw kWh. This is in-sample only
  and is never used to pick the headline.

Only the seasonal variant is judged against the gate again; the other two are diagnostics.
Held-out cycles keep the baseline run's boundary, so a setting that serves fewer snapshots is not
quietly judged on a different set of cycles.

**`--shipped-fit skip`.** Stage 2 stops when its rebuild of `is_forecastable` differs from the
export's, because that means the replay is wrong. After Sprint 4 it differs for a reason that is
not a bug: the export publishes `hdd-linear-v3`, fitted net of detected loads on the corrected
completeness rule, so neither its flag nor its slope is v2's. `--shipped-fit skip` accepts that,
drops the look-ahead comparison against the shipped fit — there is no shipped v2 to compare
against — and says so at the top of the report. The headline is unaffected: its champion is v2
refitted as at each date, which the script computes from the reads.

## Which export a report used

Each report names the export by its `_SUCCESS` seal time. `make publish-serving` rewrites the
export (its first step), and so does the Sprint 4 rebuild, which adds planted EV and hot-water loads and moves the
champion to `hdd-linear-v3`. Both wait for a go. A report produced after either change names a
different export and must say which champion it compared against.

That happened between Stage 2 and Stage 3, so `results/` holds three month-end reports:

| Report | Export sealed | Champion |
|---|---|---|
| `stage1_reproduce.md` | 2026-09-12 | none: the paper's baselines |
| `stage2_monthend.md` | 2026-09-12 | v2 refitted as at each date, and v2 as shipped |
| `stage2_monthend_sprint4_export.md` | 2026-09-17 | v2 refitted as at each date (`--shipped-fit skip`) |
| `stage3_extras.md` | 2026-09-17 | v2 refitted as at each date, with its own baseline |

The two exports hold different data — the Sprint 4 rebuild planted EV and hot-water loads in the
cohort's metering — so figures may be compared **within** a report, never across the seal.
