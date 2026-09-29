# Botman backtest, Stage 2: month-end against `hdd-linear-v2`

Export sealed **2026-09-12 22:52 UTC** (the newest `_SUCCESS` of `metering.icp`, `metering.meter_register`, `metering.meter_read_hh`, `billing.account`, `kea.mart_forecast_input`, `kea.mart_daily_usage`). Half-hourly reads from `KEA.KEA_GOLD.FCT_METER_READ` (last altered 2026-09-12 22:51 UTC), checked against the export where they overlap. Champion `hdd-linear-v2`, imported. Run 2026-09-17 13:41 UTC in 22 s. Aggregates only.

## Gate: PASS

Held-out cycles are those starting on or after 2026-02-22, the middle of the served span (2025-08-30 to 2026-08-17). The champion is v2 refitted as at each date. k = 100.

| Criterion | Result | |
|---|---|---|
| P10–P90 coverage between 70% and 90% | 82.8% over 6,492 snapshots | pass |
| Median APE where v2 can't forecast ≤ v2's own + 5 points | 5.8% (2,563 snapshots) against 5.2% + 5 = 10.2% (4,062) | pass |
| Head-to-head mean CRPS ≤ 1.10 × v2's | 0.85 × over 3,929 snapshots | pass |

All three hold, so the plan recommends integrating kNN as a fallback. That is a separate decision: it needs a migration, a readiness flag and a wider Soda check on our side, and a reader in the backend.

## Head to head, held-out cycles

Snapshots both models forecast. CRPS and widths are per snapshot; v2's CRPS is its absolute error. APE is kNN's P50 or v2's point against the cycle's final kWh. v2's band is its ±15% of the kWh still to come; widths in NZD use the snapshot's own rate. Mean final kWh: 670.

| Days elapsed | Snapshots | kNN CRPS | v2 CRPS | Ratio | kNN MdAPE | v2 MdAPE | kNN cover | v2 cover | kNN width | v2 width |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1–6 | 851 | 87.1 | 87.0 | 1.00 | 10.7% | 9.8% | 82% | 57% | $75.51 | $42.34 |
| 7–9 | 415 | 74.0 | 75.7 | 0.98 | 8.5% | 8.8% | 83% | 57% | $62.64 | $35.24 |
| 10–14 | 682 | 53.5 | 63.9 | 0.84 | 7.1% | 7.5% | 87% | 56% | $51.62 | $28.89 |
| 15–19 | 660 | 36.8 | 51.3 | 0.72 | 5.2% | 5.0% | 86% | 57% | $35.98 | $20.92 |
| 20+ | 1,321 | 15.0 | 26.1 | 0.57 | 1.9% | 2.6% | 82% | 43% | $13.34 | $7.78 |
| **All** | 3,929 | 47.2 | 55.3 | 0.85 | 5.0% | 5.1% | 83% | 52% | $38.59 | $21.34 |

- kNN's CRPS over v2's, accounts resampled: 95% interval 0.69 to 1.04.
- kNN has the lower mean CRPS on 55% of the 33 accounts.
- Median band width in kWh: kNN 138, v2 76.

## Where v2 can't forecast, held-out cycles

| Days elapsed | Snapshots | Accounts | kNN CRPS | kNN MdAPE | kNN cover | kNN width |
|---|---:|---:|---:|---:|---:|---:|
| 1–6 | 558 | 23 | 55.0 | 12.8% | 84% | $71.18 |
| 7–9 | 270 | 23 | 48.6 | 10.4% | 78% | $62.43 |
| 10–14 | 442 | 23 | 49.2 | 10.5% | 74% | $50.90 |
| 15–19 | 423 | 22 | 34.7 | 6.0% | 80% | $36.28 |
| 20+ | 870 | 23 | 11.8 | 1.9% | 86% | $13.31 |
| **All** | 2,563 | 23 | 35.3 | 5.8% | 82% | $37.27 |

The NZD width is missing where the snapshot's implied rate is negative (export credit on the prior bill).

## Coverage gain

Snapshots up to the last day with weather (2026-08-17), all cycles.

| | Snapshots | Accounts |
|---|---:|---:|
| With a final kWh and days still to run | 13,960 | 44 |
| v2 as shipped forecasts | 7,121 | 22 |
| v2 refitted as at each date forecasts | 4,969 | 33 |
| kNN forecasts | 13,397 | 44 |
| kNN forecasts, v2 (as at) doesn't | 8,593 | 44 |
| Either forecasts | 13,562 | 44 |

## By household group, held-out cycles

| Group | Head to head | Ratio | kNN cover | Fallback | kNN MdAPE (fallback) | kNN cover (fallback) |
|---|---:|---:|---:|---:|---:|---:|
| No export register | 3,061 | 0.88 | 84% | 2,259 | 5.6% | 81% |
| Export register (synthetic solar) | 868 | 0.78 | 81% | 304 | 7.1% | 86% |

## Look-ahead: v2 as shipped

The shipped heating fit uses every month, including months after the snapshot, so these rows flatter v2. Held-out cycles.

| Days elapsed | Snapshots | kNN CRPS | v2 CRPS | Ratio | kNN MdAPE | v2 MdAPE | kNN cover | v2 cover | kNN width | v2 width |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1–6 | 705 | 84.7 | 59.0 | 1.43 | 10.1% | 5.7% | 83% | 76% | $78.51 | $43.18 |
| 7–9 | 346 | 67.1 | 53.2 | 1.26 | 7.6% | 5.7% | 86% | 74% | $65.70 | $35.84 |
| 10–14 | 572 | 51.9 | 44.0 | 1.18 | 6.2% | 4.3% | 89% | 74% | $54.10 | $29.06 |
| 15–19 | 553 | 35.2 | 37.0 | 0.95 | 4.9% | 3.5% | 88% | 72% | $37.34 | $21.20 |
| 20+ | 1,083 | 13.5 | 20.8 | 0.65 | 1.8% | 2.1% | 83% | 55% | $14.11 | $8.42 |
| **All** | 3,259 | 45.0 | 39.3 | 1.14 | 4.6% | 3.5% | 85% | 68% | $40.28 | $22.68 |

The gate against the shipped fit, for reference: (1) 82.8% over 6,492 snapshots, pass; (2) 6.4% (3,233 snapshots) against 3.6% + 5 = 8.6% (3,371), pass; (3) 1.14 × over 3,259 snapshots, fail.

## All cycles, not only held-out

| Champion fit | Head to head | Ratio | kNN cover | v2 cover | Fallback | kNN MdAPE (fallback) | kNN cover (fallback) |
|---|---:|---:|---:|---:|---:|---:|---:|
| refitted as at each date (headline) | 4,804 | 0.85 | 84% | 54% | 8,593 | 5.9% | 83% |
| as shipped (look-ahead) | 6,798 | 0.96 | 86% | 65% | 6,599 | 6.9% | 81% |

After the weather extract ends, kNN still serves 14,349 snapshots (44 accounts): MdAPE 4.3%, P10–P90 coverage 85%. v2 can't run there, so they are outside the gate.

## Data and checks

| | |
|---|---:|
| Cohort ICPs / households with import reads | 50 / 49 |
| Warehouse import reads | 1,767,603 (2025-08-01 to 2027-08-27) |
| … the export's 13 months, compared read by read | 926,178 rows, identical to the export (926,178 rows) |
| … with no kWh / repeated / off the half-hour | 0 / 0 / 0 |
| Complete household-days (kNN) | 36,612 |
| Fully read household-days (refit) | 36,476 |
| … households without an export register, days where that differs from the mart's rule | 0 |
| … households with one: complete days by the mart's rule / fully read | 1 / 5,954 |
| Library weeks (7 complete days) | 34,435 |
| Cohort forecast-input rows / accounts | 31,653 / 45 |
| … with the cycle's final kWh | 29,785 (979 cycles) |
| `kwh_to_date` against the daily mart, largest difference | 0.0000 kWh |
| Shipped heating fits replicated (ICPs) | 36 (largest difference 0.0000; 0 present on one side only) |
| `is_forecastable` in the export | 7,468 (Sprint 3 published 7,468) |
| … rebuilt here | 7,468 across 22 accounts, 0 rows differing |
| Eligible with the as-at refit | 5,262 across 33 accounts |
| Accounts with an export register: eligible as shipped / as at | 0 / 6 of 8 |
| Last day with weather | 2026-08-17 |
| Snapshots kNN served | 27,746 |
| … not served: no household matrix / week incomplete | 730 / 1,391 |
| … not served: final day / no final kWh / fewer than k weeks | 965 / 740 / 81 |
| Neighbours from the household's own history | 23.0% |
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
