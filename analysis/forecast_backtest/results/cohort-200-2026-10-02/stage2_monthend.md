# Botman backtest, Stage 2: month-end against `hdd-linear-v2`

Export sealed **2026-10-02 01:49 UTC** (the newest `_SUCCESS` of `metering.icp`, `metering.meter_register`, `metering.meter_read_hh`, `billing.account`, `kea.mart_forecast_input`, `kea.mart_daily_usage`). Half-hourly reads from `KEA.KEA_GOLD.FCT_METER_READ` (last altered 2026-10-02 01:44 UTC), checked against the export where they overlap. Champion `hdd-linear-v3`, imported. Run 2026-10-02 02:29 UTC in 195 s. Aggregates only.

**The export ships another champion.** Rebuilding v2's `is_forecastable` from the export gives 41,330 rows against the 37,647 the export publishes, differing on 5,743, and the exported heating fits differ from v2's rule by up to 27.3632 (0 present on one side only). That is Sprint 4: `hdd-linear-v3` fits net of detected loads, on the corrected completeness rule. The comparison here is against **v2 refitted as at each date**, computed in this script from the reads, and the look-ahead section against the shipped fit is left out.

## Gate: PASS

Held-out cycles are those starting on or after 2026-02-21, the middle of the served span (2025-08-29 to 2026-08-17). The champion is v2 refitted as at each date. k = 100.

| Criterion | Result | |
|---|---|---|
| P10–P90 coverage between 70% and 90% | 81.1% over 29,302 snapshots | pass |
| Median APE where v2 can't forecast ≤ v2's own + 5 points | 5.6% (10,863 snapshots) against 5.9% + 5 = 10.9% (18,996) | pass |
| Head-to-head mean CRPS ≤ 1.10 × v2's | 0.85 × over 18,439 snapshots | pass |

All three hold, so the plan recommends integrating kNN as a fallback. That is a separate decision: it needs a migration, a readiness flag and a wider Soda check on our side, and a reader in the backend.

## Head to head, held-out cycles

Snapshots both models forecast. CRPS and widths are per snapshot; v2's CRPS is its absolute error. APE is kNN's P50 or v2's point against the cycle's final kWh. v2's band is its ±15% of the kWh still to come; widths in NZD use the snapshot's own rate. Mean final kWh: 741.

| Days elapsed | Snapshots | kNN CRPS | v2 CRPS | Ratio | kNN MdAPE | v2 MdAPE | kNN cover | v2 cover | kNN width | v2 width |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1–6 | 3,956 | 117.6 | 110.8 | 1.06 | 12.8% | 10.2% | 78% | 60% | $76.08 | $40.35 |
| 7–9 | 1,952 | 90.1 | 97.6 | 0.92 | 10.4% | 8.7% | 78% | 58% | $61.49 | $33.62 |
| 10–14 | 3,179 | 73.6 | 86.1 | 0.86 | 8.8% | 7.7% | 79% | 55% | $50.24 | $27.26 |
| 15–19 | 3,092 | 52.3 | 69.8 | 0.75 | 6.0% | 5.9% | 80% | 53% | $35.89 | $19.74 |
| 20+ | 6,260 | 22.6 | 42.3 | 0.53 | 2.2% | 3.1% | 80% | 41% | $14.98 | $8.20 |
| **All** | 18,439 | 63.9 | 75.0 | 0.85 | 6.0% | 5.9% | 79% | 51% | $38.92 | $21.54 |

- kNN's CRPS over v2's, accounts resampled: 95% interval 0.78 to 0.93.
- kNN has the lower mean CRPS on 66% of the 146 accounts.
- Median band width in kWh: kNN 140, v2 77.

## Where v2 can't forecast, held-out cycles

| Days elapsed | Snapshots | Accounts | kNN CRPS | kNN MdAPE | kNN cover | kNN width |
|---|---:|---:|---:|---:|---:|---:|
| 1–6 | 2,353 | 90 | 82.1 | 12.5% | 83% | $81.66 |
| 7–9 | 1,164 | 90 | 73.7 | 10.5% | 84% | $70.46 |
| 10–14 | 1,881 | 91 | 55.2 | 8.0% | 86% | $56.26 |
| 15–19 | 1,814 | 90 | 33.2 | 5.4% | 86% | $38.57 |
| 20+ | 3,651 | 91 | 15.3 | 2.1% | 84% | $15.78 |
| **All** | 10,863 | 92 | 45.9 | 5.6% | 84% | $43.24 |

The NZD width is missing where the snapshot's implied rate is negative (export credit on the prior bill).

## Coverage gain

Snapshots up to the last day with weather (2026-08-17), all cycles.

| | Snapshots | Accounts |
|---|---:|---:|
| With a final kWh and days still to run | 63,060 | 194 |
| v2 refitted as at each date forecasts | 23,152 | 148 |
| kNN forecasts | 60,522 | 194 |
| kNN forecasts, v2 (as at) doesn't | 38,082 | 194 |
| Either forecasts | 61,234 | 194 |

## By household group, held-out cycles

| Group | Head to head | Ratio | kNN cover | Fallback | kNN MdAPE (fallback) | kNN cover (fallback) |
|---|---:|---:|---:|---:|---:|---:|
| No export register | 16,152 | 0.86 | 80% | 10,176 | 5.5% | 85% |
| Export register (synthetic solar) | 2,287 | 0.77 | 76% | 687 | 8.3% | 71% |

## Look-ahead: v2 as shipped

Left out: the export's published fit is not v2's, so there is no shipped v2 to compare against. See the note above.

## All cycles, not only held-out

| Champion fit | Head to head | Ratio | kNN cover | v2 cover | Fallback | kNN MdAPE (fallback) | kNN cover (fallback) |
|---|---:|---:|---:|---:|---:|---:|---:|
| refitted as at each date (headline) | 22,440 | 0.84 | 80% | 53% | 38,082 | 5.7% | 85% |

After the weather extract ends, kNN still serves 64,604 snapshots (194 accounts): MdAPE 5.1%, P10–P90 coverage 84%. v2 can't run there, so they are outside the gate.

## Data and checks

| | |
|---|---:|
| Cohort ICPs / households with import reads | 200 / 199 |
| Warehouse import reads | 7,180,694 (2025-08-01 to 2027-08-27) |
| … the export's 13 months, compared read by read | 3,764,668 rows, identical to the export (3,764,668 rows) |
| … with no kWh / repeated / off the half-hour | 0 / 0 / 0 |
| Complete household-days (kNN) | 148,814 |
| Fully read household-days (refit) | 148,162 |
| … households without an export register, days where that differs from the mart's rule | 0 |
| … households with one: complete days by the mart's rule / fully read | 14,890 / 14,890 |
| Library weeks (7 complete days) | 142,365 |
| Cohort forecast-input rows / accounts | 139,032 / 195 |
| … with the cycle's final kWh | 134,139 (4,416 cycles) |
| `kwh_to_date` against the daily mart, largest difference | 0.0000 kWh |
| Exported heating fits replicated by v2's rule (ICPs) | 194 (largest difference 27.3632; 0 present on one side only) |
| `is_forecastable` in the export | 37,647 (Sprint 3 published 7,468) |
| … rebuilt here | 41,330 across 122 accounts, 5743 rows differing |
| Eligible with the as-at refit | 24,287 across 148 accounts |
| Accounts with an export register: eligible as shipped / as at | 15 / 17 of 19 |
| Last day with weather | 2026-08-17 |
| Snapshots kNN served | 125,126 |
| … not served: no household matrix / week incomplete | 730 / 5,645 |
| … not served: final day / no final kWh / fewer than k weeks | 4,303 / 2,825 / 403 |
| Neighbours from the household's own history | 11.8% |
| `min_heating_fit_r2` (dbt_project.yml) | 0.30 |

## Settings

- **kNN:** the household's 7 days up to the as-at date, divided by their maximum; the k = 100 nearest weeks of any household; each brings its whole continuation for the days left in the cycle, summed and rescaled. Projected kWh is `kwh_to_date` plus each total; P10, P50 and P90 are taken over those totals.
- **Leakage:** a week and its continuation end on or before the as-at date. `test_knn_quantile.py` pins it.
- **v2 refitted as at each date:** the monthly heating fit on months that ended by the as-at date, ≥ 20 fully read days each, ≥ 6 months, OLS, rounded as dbt rounds; eligibility then follows `mart_forecast_input` with that fit. Fully read means every import register read every half-hour, Sprint 4's corrected rule, so households with an export register can qualify here and not as shipped.
- **v2 as shipped:** the exported slope and baseline, fitted on every month.
- **Truth:** the cycle's import kWh from `kea.mart_daily_usage`, only where every day of the cycle has a row. Snapshots on a cycle's last day have nothing left to forecast and are skipped.
- **CRPS:** the ensemble form; for v2's single value it is the absolute error. Coverage is the share of final kWh inside P10–P90, or inside v2's ±15% of the kWh still to come.
- **Interval:** 2000 bootstrap draws of accounts, seed 125168.
- **Why the warehouse:** the export keeps 13 months of half-hourly reads, and the weather v2 needs ends before that window starts. The reads come from one read-only SELECT; everything else is the sealed export.
