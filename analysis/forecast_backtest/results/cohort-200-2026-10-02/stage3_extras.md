# Botman backtest, Stage 3: extras on the month-end run

Export sealed **2026-10-02 01:49 UTC** (the newest `_SUCCESS` of `metering.icp`, `metering.meter_register`, `metering.meter_read_hh`, `billing.account`, `kea.mart_forecast_input`, `kea.mart_daily_usage`). Half-hourly reads from `KEA.KEA_GOLD.FCT_METER_READ` (last altered 2026-10-02 01:44 UTC), checked against the export where they overlap. Champion `hdd-linear-v3` refitted as at each date, imported. Run 2026-10-02 03:01 UTC in 32 min. Aggregates only.

**The export ships another champion.** Rebuilding v2's `is_forecastable` from the export gives 41,330 rows against the 37,647 the export publishes, differing on 5,743, and the exported heating fits differ from v2's rule by up to 27.3632 (0 present on one side only). That is Sprint 4: `hdd-linear-v3` fits net of detected loads, on the corrected completeness rule. The comparison here is against **v2 refitted as at each date**, computed in this script from the reads, and the look-ahead section against the shipped fit is left out.

## The baseline on this export

The paper's setting — k = 100, week normalisation, the whole cohort — on the export named above. Held-out cycles start 2026-02-21; every extra is judged on the same boundary.

| Criterion | Result | |
|---|---|---|
| P10–P90 coverage between 70% and 90% | 81.1% over 29,302 snapshots | pass |
| Median APE where v2 can't forecast ≤ v2's own + 5 points | 5.6% (10,863 snapshots) against 5.9% + 5 = 10.9% (18,996) | pass |
| Head-to-head mean CRPS ≤ 1.10 × v2's | 0.85 × over 18,439 snapshots | pass |

Gate: **PASS** on this export.

## Seasonal neighbours (`--variant seasonal`)

A neighbour's continuation must start within 30 days of the as-at day's time of year and at least 180 days earlier — over a 13-month span, the same season a year back. This is a different model, so the gate is applied again.

| Criterion | Result | |
|---|---|---|
| P10–P90 coverage between 70% and 90% | 80.0% over 6,962 snapshots | pass |
| Median APE where v2 can't forecast ≤ v2's own + 5 points | 6.2% (2,439 snapshots) against 5.9% + 5 = 10.9% (18,996) | pass |
| Head-to-head mean CRPS ≤ 1.10 × v2's | 1.58 × over 4,523 snapshots | **fail** |

Gate: **FAIL**.

Head to head on the 6,962 held-out snapshots both settings served:

| | Snapshots served | kNN CRPS | kNN MdAPE | kNN cover | Band width (kWh) | Too few weeks |
|---|---:|---:|---:|---:|---:|---:|
| Baseline (all weeks) | 125,126 | 60.1 | 4.6% | 85% | 153 | 403 |
| Seasonal | 71,566 | 117.6 | 6.9% | 80% | 171 | 53,963 |

Seasonal CRPS over the baseline's on those snapshots: 1.96 ×. The filter also costs coverage: a snapshot is served only where 100 weeks survive the ±30-day window, which is why the served count falls to 57% of the baseline's.

## Library size (`--pool-sizes`)

Each pool is the first N households of one seeded order, so the 10 are inside the 25. Only that pool's households bring weeks, and only their snapshots are scored; the last column is the whole cohort's library on exactly those snapshots, which is the like-for-like comparison.

| Households | Library weeks | Snapshots served | kNN CRPS | kNN MdAPE | kNN cover | Whole cohort, same snapshots | Ratio |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 | 7,044 | 6,230 | 72.2 | 7.2% | 70% | 61.2 | 1.18 |
| 25 | 17,953 | 16,265 | 63.0 | 6.8% | 71% | 60.4 | 1.04 |
| 50 | 35,917 | 31,787 | 67.2 | 6.5% | 76% | 69.3 | 0.97 |
| 100 | 71,332 | 63,919 | 54.3 | 6.0% | 78% | 55.4 | 0.98 |
| 199 | 142,365 | 125,126 | 57.2 | 5.8% | 81% | 57.2 | 1.00 |

The cohort has 199 households with any complete day, so a pool at or above that size is the whole cohort and its ratio is 1.00 by construction.

## k and normalisation (`--grid`), in-sample

Mean CRPS on the 29,302 held-out snapshots every setting served. In-sample: nothing is held back from this table, and it does not pick the headline, which stays at the paper's k = 100 with week normalisation.

| k | week | profile | none |
|---:|---:|---:|---:|
| 50 | 60.2 | 57.5 | 60.8 |
| 100 | 57.2 | 55.9 | 61.0 |
| 200 | 55.8 | 56.0 | 62.7 |

| k | week MdAPE / cover | profile MdAPE / cover | none MdAPE / cover |
|---:|---:|---:|---:|
| 50 | 5.9% / 78% | 4.7% / 65% | 6.4% / 69% |
| 100 | 5.8% / 81% | 4.6% / 67% | 6.3% / 73% |
| 200 | 5.9% / 82% | 4.6% / 69% | 6.4% / 75% |

Best to worst: 12% of the best mean CRPS. The paper's LCL grid (Table 3b) spans 4%, and k barely matters there; a much wider spread here would say the method is sensitive to a choice the paper treats as settled.

## Data and checks

| Run | Library weeks | Pool | Served | Too few weeks | Own history |
|---|---:|---:|---:|---:|---:|
| baseline (paper's setting) | 142,365 | 199 | 125,126 | 403 | 11.8% |
| seasonal | 142,365 | 199 | 71,566 | 53,963 | 6.9% |
| pool of 10 | 7,044 | 10 | 6,230 | 55 | 45.6% |
| pool of 25 | 17,953 | 25 | 16,265 | 79 | 30.5% |
| pool of 50 | 35,917 | 50 | 31,787 | 103 | 22.7% |
| pool of 100 | 71,332 | 100 | 63,919 | 237 | 16.9% |
| pool of 200 | 142,365 | 199 | 125,126 | 403 | 11.8% |
| k=50, week | 142,365 | 199 | 125,126 | 403 | 14.2% |
| k=50, profile | 142,365 | 199 | 125,126 | 403 | 10.3% |
| k=50, none | 143,138 | 199 | 125,126 | 403 | 15.3% |
| k=100, week | 142,365 | 199 | 125,126 | 403 | 11.8% |
| k=100, profile | 142,365 | 199 | 125,126 | 403 | 8.4% |
| k=100, none | 143,138 | 199 | 125,126 | 403 | 12.7% |
| k=200, week | 142,365 | 199 | 125,081 | 448 | 9.4% |
| k=200, profile | 142,365 | 199 | 125,081 | 448 | 6.5% |
| k=200, none | 143,138 | 199 | 125,081 | 448 | 10.1% |

Cohort forecast-input rows 139,032 across 195 accounts; 134,139 have the cycle's final kWh. Eligible with the as-at refit: 24,287 across 148 accounts. Last day with weather 2026-08-17.

## Settings

- **Seasonal:** `knn_quantile.seasonal_rows()`; the continuation's first day sits within ±30 days of the as-at day's day of the year and at least 180 days before it. The leakage rule still applies on top. A leap year shifts a neighbour by one day inside that window.
- **Normalisation:** `week` divides a window by its largest half-hour (the paper's), `profile` by its mean, so only the shape is compared, `none` compares raw kWh. The same divisor rescales the neighbour's continuation back.
- **Pools:** one seeded permutation (seed 20260918) of the households with any complete day, truncated to each size.
- **Held-out:** the baseline run's boundary, reused by every extra, so a setting that serves fewer snapshots is not judged on a different set of cycles.
- **Everything else** — the champion, the truth, CRPS, coverage, the bootstrap — is Stage 2's, unchanged.
