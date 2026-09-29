# Botman backtest, Stage 3: extras on the month-end run

Export sealed **2026-09-17 23:34 UTC** (the newest `_SUCCESS` of `metering.icp`, `metering.meter_register`, `metering.meter_read_hh`, `billing.account`, `kea.mart_forecast_input`, `kea.mart_daily_usage`). Half-hourly reads from `KEA.KEA_GOLD.FCT_METER_READ` (last altered 2026-09-17 23:33 UTC), checked against the export where they overlap. Champion `hdd-linear-v2` refitted as at each date, imported. Run 2026-09-18 00:27 UTC in 3 min. Aggregates only.

**The export ships another champion.** Rebuilding v2's `is_forecastable` from the export gives 9,666 rows against the 8,716 the export publishes, differing on 1,632, and the exported heating fits differ from v2's rule by up to 32.8540 (0 present on one side only). That is Sprint 4: `hdd-linear-v3` fits net of detected loads, on the corrected completeness rule. The comparison here is against **v2 refitted as at each date**, computed in this script from the reads, and the look-ahead section against the shipped fit is left out.

**Not comparable with Stage 2's report,** which read the export sealed 2026-09-12 22:52 UTC. Every figure below was measured on today's export, the baseline row included, so the extras are compared only with each other and with that baseline.

## The baseline on this export

The paper's setting — k = 100, week normalisation, the whole cohort — on the export named above. Held-out cycles start 2026-02-22; every extra is judged on the same boundary.

| Criterion | Result | |
|---|---|---|
| P10–P90 coverage between 70% and 90% | 81.6% over 6,492 snapshots | pass |
| Median APE where v2 can't forecast ≤ v2's own + 5 points | 6.3% (2,408 snapshots) against 5.2% + 5 = 10.2% (4,217) | pass |
| Head-to-head mean CRPS ≤ 1.10 × v2's | 0.88 × over 4,084 snapshots | pass |

Gate: **PASS** on this export.

## Seasonal neighbours (`--variant seasonal`)

A neighbour's continuation must start within 30 days of the as-at day's time of year and at least 180 days earlier — over a 13-month span, the same season a year back. This is a different model, so the gate is applied again.

| Criterion | Result | |
|---|---|---|
| P10–P90 coverage between 70% and 90% | 88.4% over 1,515 snapshots | pass |
| Median APE where v2 can't forecast ≤ v2's own + 5 points | 8.7% (508 snapshots) against 5.2% + 5 = 10.2% (4,217) | pass |
| Head-to-head mean CRPS ≤ 1.10 × v2's | 1.84 × over 1,007 snapshots | **fail** |

Gate: **FAIL**.

Head to head on the 1,515 held-out snapshots both settings served:

| | Snapshots served | kNN CRPS | kNN MdAPE | kNN cover | Band width (kWh) | Too few weeks |
|---|---:|---:|---:|---:|---:|---:|
| Baseline (all weeks) | 27,882 | 36.0 | 4.4% | 89% | 146 | 81 |
| Seasonal | 16,000 | 78.2 | 6.7% | 88% | 191 | 11,963 |

Seasonal CRPS over the baseline's on those snapshots: 2.17 ×. The filter also costs coverage: a snapshot is served only where 100 weeks survive the ±30-day window, which is why the served count falls to 57% of the baseline's.

## Library size (`--pool-sizes`)

Each pool is the first N households of one seeded order, so the 10 are inside the 25. Only that pool's households bring weeks, and only their snapshots are scored; the last column is the whole cohort's library on exactly those snapshots, which is the like-for-like comparison.

| Households | Library weeks | Snapshots served | kNN CRPS | kNN MdAPE | kNN cover | Whole cohort, same snapshots | Ratio |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 | 7,147 | 5,756 | 40.8 | 4.7% | 78% | 34.6 | 1.18 |
| 25 | 17,860 | 13,303 | 42.2 | 5.7% | 78% | 39.3 | 1.07 |
| 49 | 34,635 | 27,882 | 44.5 | 5.5% | 82% | 44.5 | 1.00 |

The cohort has 49 households with any complete day, so a pool at or above that size is the whole cohort and its ratio is 1.00 by construction.

## k and normalisation (`--grid`), in-sample

Mean CRPS on the 6,492 held-out snapshots every setting served. In-sample: nothing is held back from this table, and it does not pick the headline, which stays at the paper's k = 100 with week normalisation.

| k | week | profile | none |
|---:|---:|---:|---:|
| 50 | 44.4 | 40.9 | 47.6 |
| 100 | 44.5 | 39.3 | 48.0 |
| 200 | 45.5 | 38.4 | 50.3 |

| k | week MdAPE / cover | profile MdAPE / cover | none MdAPE / cover |
|---:|---:|---:|---:|
| 50 | 5.5% / 78% | 4.2% / 60% | 6.2% / 65% |
| 100 | 5.5% / 82% | 4.0% / 62% | 6.1% / 70% |
| 200 | 5.8% / 83% | 4.0% / 63% | 5.8% / 73% |

Best to worst: 31% of the best mean CRPS. The paper's LCL grid (Table 3b) spans 4%, and k barely matters there; a much wider spread here would say the method is sensitive to a choice the paper treats as settled.

## Data and checks

| Run | Library weeks | Pool | Served | Too few weeks | Own history |
|---|---:|---:|---:|---:|---:|
| baseline (paper's setting) | 34,635 | 49 | 27,882 | 81 | 25.0% |
| seasonal | 34,635 | 49 | 16,000 | 11,963 | 15.8% |
| pool of 10 | 7,147 | 10 | 5,756 | 46 | 42.1% |
| pool of 25 | 17,860 | 25 | 13,303 | 53 | 34.5% |
| pool of 50 | 34,635 | 49 | 27,882 | 81 | 25.0% |
| k=50, week | 34,635 | 49 | 27,891 | 72 | 28.8% |
| k=50, profile | 34,635 | 49 | 27,891 | 72 | 21.2% |
| k=50, none | 35,103 | 49 | 27,891 | 72 | 29.3% |
| k=100, week | 34,635 | 49 | 27,882 | 81 | 25.0% |
| k=100, profile | 34,635 | 49 | 27,882 | 81 | 18.3% |
| k=100, none | 35,103 | 49 | 27,882 | 81 | 25.7% |
| k=200, week | 34,635 | 49 | 27,850 | 113 | 20.8% |
| k=200, profile | 34,635 | 49 | 27,850 | 113 | 15.3% |
| k=200, none | 35,103 | 49 | 27,850 | 113 | 21.6% |

Cohort forecast-input rows 31,794 across 45 accounts; 29,926 have the cycle's final kWh. Eligible with the as-at refit: 5,461 across 34 accounts. Last day with weather 2026-08-17.

## Settings

- **Seasonal:** `knn_quantile.seasonal_rows()`; the continuation's first day sits within ±30 days of the as-at day's day of the year and at least 180 days before it. The leakage rule still applies on top. A leap year shifts a neighbour by one day inside that window.
- **Normalisation:** `week` divides a window by its largest half-hour (the paper's), `profile` by its mean, so only the shape is compared, `none` compares raw kWh. The same divisor rescales the neighbour's continuation back.
- **Pools:** one seeded permutation (seed 20260918) of the households with any complete day, truncated to each size.
- **Held-out:** the baseline run's boundary, reused by every extra, so a setting that serves fewer snapshots is not judged on a different set of cycles.
- **Everything else** — the champion, the truth, CRPS, coverage, the bootstrap — is Stage 2's, unchanged.
