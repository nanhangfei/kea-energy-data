# Botman backtest, Stage 2: month-end against `hdd-linear-v2`

Export sealed **2026-09-17 23:34 UTC** (the newest `_SUCCESS` of `metering.icp`, `metering.meter_register`, `metering.meter_read_hh`, `billing.account`, `kea.mart_forecast_input`, `kea.mart_daily_usage`). Half-hourly reads from `KEA.KEA_GOLD.FCT_METER_READ` (last altered 2026-09-17 23:33 UTC), checked against the export where they overlap. Champion `hdd-linear-v2`, imported. Run 2026-09-18 00:20 UTC in 25 s. Aggregates only.

**The export ships another champion.** Rebuilding v2's `is_forecastable` from the export gives 9,666 rows against the 8,716 the export publishes, differing on 1,632, and the exported heating fits differ from v2's rule by up to 32.8540 (0 present on one side only). That is Sprint 4: `hdd-linear-v3` fits net of detected loads, on the corrected completeness rule. The comparison here is against **v2 refitted as at each date**, computed in this script from the reads, and the look-ahead section against the shipped fit is left out.

## Gate: PASS

Held-out cycles are those starting on or after 2026-02-22, the middle of the served span (2025-08-30 to 2026-08-17). The champion is v2 refitted as at each date. k = 100.

| Criterion | Result | |
|---|---|---|
| P10–P90 coverage between 70% and 90% | 81.6% over 6,492 snapshots | pass |
| Median APE where v2 can't forecast ≤ v2's own + 5 points | 6.3% (2,408 snapshots) against 5.2% + 5 = 10.2% (4,217) | pass |
| Head-to-head mean CRPS ≤ 1.10 × v2's | 0.88 × over 4,084 snapshots | pass |

All three hold, so the plan recommends integrating kNN as a fallback. That is a separate decision: it needs a migration, a readiness flag and a wider Soda check on our side, and a reader in the backend.

## Head to head, held-out cycles

Snapshots both models forecast. CRPS and widths are per snapshot; v2's CRPS is its absolute error. APE is kNN's P50 or v2's point against the cycle's final kWh. v2's band is its ±15% of the kWh still to come; widths in NZD use the snapshot's own rate. Mean final kWh: 686.

| Days elapsed | Snapshots | kNN CRPS | v2 CRPS | Ratio | kNN MdAPE | v2 MdAPE | kNN cover | v2 cover | kNN width | v2 width |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1–6 | 887 | 89.5 | 86.8 | 1.03 | 11.6% | 9.8% | 80% | 56% | $74.01 | $43.80 |
| 7–9 | 431 | 77.9 | 75.2 | 1.04 | 8.8% | 8.9% | 80% | 57% | $65.50 | $36.26 |
| 10–14 | 707 | 55.9 | 63.6 | 0.88 | 7.2% | 7.6% | 86% | 55% | $52.91 | $29.74 |
| 15–19 | 685 | 37.4 | 51.4 | 0.73 | 5.0% | 5.2% | 85% | 56% | $37.41 | $21.94 |
| 20+ | 1,374 | 15.1 | 26.6 | 0.57 | 2.1% | 2.7% | 80% | 42% | $13.74 | $8.28 |
| **All** | 4,084 | 48.7 | 55.4 | 0.88 | 5.1% | 5.1% | 82% | 51% | $39.47 | $22.47 |

- kNN's CRPS over v2's, accounts resampled: 95% interval 0.72 to 1.06.
- kNN has the lower mean CRPS on 56% of the 34 accounts.
- Median band width in kWh: kNN 142, v2 79.

## Where v2 can't forecast, held-out cycles

| Days elapsed | Snapshots | Accounts | kNN CRPS | kNN MdAPE | kNN cover | kNN width |
|---|---:|---:|---:|---:|---:|---:|
| 1–6 | 522 | 22 | 58.3 | 13.6% | 84% | $73.73 |
| 7–9 | 254 | 22 | 51.2 | 10.9% | 77% | $64.74 |
| 10–14 | 417 | 22 | 52.2 | 11.5% | 73% | $51.89 |
| 15–19 | 398 | 21 | 36.8 | 6.4% | 80% | $37.04 |
| 20+ | 817 | 22 | 12.4 | 2.0% | 86% | $14.17 |
| **All** | 2,408 | 22 | 37.4 | 6.3% | 81% | $38.46 |

The NZD width is missing where the snapshot's implied rate is negative (export credit on the prior bill).

## Coverage gain

Snapshots up to the last day with weather (2026-08-17), all cycles.

| | Snapshots | Accounts |
|---|---:|---:|
| With a final kWh and days still to run | 13,960 | 44 |
| v2 refitted as at each date forecasts | 5,161 | 34 |
| kNN forecasts | 13,397 | 44 |
| kNN forecasts, v2 (as at) doesn't | 8,401 | 44 |
| Either forecasts | 13,562 | 44 |

## By household group, held-out cycles

| Group | Head to head | Ratio | kNN cover | Fallback | kNN MdAPE (fallback) | kNN cover (fallback) |
|---|---:|---:|---:|---:|---:|---:|
| No export register | 3,216 | 0.92 | 82% | 2,104 | 6.2% | 80% |
| Export register (synthetic solar) | 868 | 0.78 | 80% | 304 | 7.1% | 88% |

## Look-ahead: v2 as shipped

Left out: the export's published fit is not v2's, so there is no shipped v2 to compare against. See the note above.

## All cycles, not only held-out

| Champion fit | Head to head | Ratio | kNN cover | v2 cover | Fallback | kNN MdAPE (fallback) | kNN cover (fallback) |
|---|---:|---:|---:|---:|---:|---:|---:|
| refitted as at each date (headline) | 4,996 | 0.88 | 82% | 54% | 8,401 | 6.5% | 81% |

After the weather extract ends, kNN still serves 14,485 snapshots (44 accounts): MdAPE 4.5%, P10–P90 coverage 84%. v2 can't run there, so they are outside the gate.

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
| … households with one: complete days by the mart's rule / fully read | 5,954 / 5,954 |
| Library weeks (7 complete days) | 34,635 |
| Cohort forecast-input rows / accounts | 31,794 / 45 |
| … with the cycle's final kWh | 29,926 (984 cycles) |
| `kwh_to_date` against the daily mart, largest difference | 0.0000 kWh |
| Exported heating fits replicated by v2's rule (ICPs) | 44 (largest difference 32.8540; 0 present on one side only) |
| `is_forecastable` in the export | 8,716 (Sprint 3 published 7,468) |
| … rebuilt here | 9,666 across 29 accounts, 1632 rows differing |
| Eligible with the as-at refit | 5,461 across 34 accounts |
| Accounts with an export register: eligible as shipped / as at | 6 / 6 of 8 |
| Last day with weather | 2026-08-17 |
| Snapshots kNN served | 27,882 |
| … not served: no household matrix / week incomplete | 730 / 1,392 |
| … not served: final day / no final kWh / fewer than k weeks | 969 / 740 / 81 |
| Neighbours from the household's own history | 25.0% |
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
