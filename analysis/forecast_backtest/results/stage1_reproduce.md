# Botman backtest, Stage 1: reproduce the paper, day-ahead

Export sealed **2026-09-12 22:52 UTC** (the newest `_SUCCESS` of `metering.icp`, `metering.meter_register`, `metering.meter_read_hh`; `metering.icp` 2026-09-12 22:51 UTC, `metering.meter_register` 2026-09-12 22:51 UTC, `metering.meter_read_hh` 2026-09-12 22:52 UTC). Run 2026-09-17 10:23 UTC in 85 s. Aggregates only.

## Verdict: Clearly beats persistence; level with historical sampling

kNN's +0.9% over historical sampling has an interval that spans zero, and kNN wins on only 35% of households. The plan's review point has no rule for a tie: clearing persistence permits Stage 2, and falling short of the paper's +7.0% over historical sampling points at the pool size. A small pool is the paper's own stated failure mode (section 5), and Stage 3's pool-size curve measures it. Which runs first is your call.

## Scores

CRPS is kWh per half-hour, averaged over every scored half-hour. All three models are scored on the same 9,235 household-days.

| Model | CRPS (ensemble) | CRPS (paper's quantile form) | P10–P90 coverage | Paper's CRPS (LCL) |
|---|---:|---:|---:|---:|
| kNN, k=100, week normalisation | 0.15192 | 0.07610 | 73.6% | 0.0912 |
| Historical sampling | 0.15336 | 0.07679 | 76.3% | 0.0981 |
| Probabilistic persistence | 0.18966 | 0.09488 | 77.8% | 0.1253 |

The absolute CRPS is not comparable with the paper's: the households, dates and scale differ. Compare the gains.

| kNN gain over | Here (ensemble) | 95% CI, households resampled | Here (quantile form) | Paper | Households where kNN wins |
|---|---:|---:|---:|---:|---:|
| Historical sampling | +0.9% | -4.7% to +6.6% | +0.9% | +7.0% | 35% |
| Probabilistic persistence | +19.9% | +18.3% to +21.6% | +19.8% | +27.2% | 100% |

Mean import on the scored days is 0.418 kWh per half-hour, so kNN's CRPS is 36% of it. A P10–P90 band should cover 80%; kNN's covers 73.6% (too narrow); Historical sampling's covers 76.3% (too narrow); Probabilistic persistence's covers 77.8% (too narrow). The quantiles are numpy's linear ones, as the paper's `nanquantile()`.

### By household group

| Group | Households | Household-days | kNN CRPS | Gain over HS | Gain over persistence |
|---|---:|---:|---:|---:|---:|
| No export register | 41 | 7,687 | 0.15868 | +0.9% | +19.8% |
| Export register (synthetic solar) | 8 | 1,548 | 0.11838 | +1.4% | +20.6% |

An export register means the Stage 2 generator netted synthetic solar out of that household's import, so its shape is no longer plain LCL.

### By month

| Month | Household-days | kNN CRPS | Gain over HS | Gain over persistence | kNN coverage |
|---|---:|---:|---:|---:|---:|
| 2027-02 | 858 | 0.10305 | +8.1% | +22.0% | 78% |
| 2027-03 | 1,459 | 0.11832 | -4.6% | +20.6% | 77% |
| 2027-04 | 1,396 | 0.15265 | -7.2% | +21.1% | 75% |
| 2027-05 | 1,445 | 0.17207 | -2.1% | +18.3% | 72% |
| 2027-06 | 1,424 | 0.17170 | +6.0% | +19.0% | 71% |
| 2027-07 | 1,468 | 0.17493 | +1.4% | +19.1% | 71% |
| 2027-08 | 1,185 | 0.15098 | +6.8% | +21.2% | 73% |

## Data

| | |
|---|---:|
| Cohort ICPs in the export | 50 |
| … with an import register | 49 |
| … with import reads (households) | 49 |
| … of which with an export register | 8 |
| … of which with more than one import register | 0 |
| Import reads | 926,178 |
| … with no kWh (treated as unread) | 0 |
| … repeating a register and half-hour (day incomplete) | 0 |
| … not on a half-hour (ignored) | 0 |
| NZ days in the calendar | 2026-07-27 to 2027-08-27 |
| DST days (46 / 50 half-hours) | 1 / 1 |
| Complete household-days | 19,187 (98.9% of the active span) |
| Library windows (7 days + next day, all complete) | 17,809 |
| Scored days | 2027-02-10 to 2027-08-26 |
| Household-days with a complete day and week before | 9,235 |
| … dropped, no earlier baseline member | 0 |
| Household-days scored | 9,235 across 49 households |
| Neighbours from the target's own history | 26.6% |

## Settings

- k = 100; queries and windows are 7 days × 48 half-hours, each divided by its own maximum; Euclidean distance; the forecast is the k neighbours' next days × the query week's maximum.
- **Leakage rule:** a window is usable for day d only when the window *and* its next day end before d, for every household. This is stricter than the paper's wording (section 3.3); `test_knn_quantile.py` pins it.
- **Scored:** every household-day in the later half of the active span whose day and 7 days before are complete; each is forecast with only the history before it.
- **Historical sampling:** every earlier complete day on the same weekday. **Persistence:** yesterday plus each earlier day-on-day change, slot by slot, clipped at zero.
- **CRPS:** the exact ensemble form `E|X−y| − ½E|X−X′|` is the headline. The paper's form, the pinball loss at the 100 levels 0.01–1.00 (eqs. 5–6), is averaged rather than summed, which leaves every ratio unchanged.
- **Interval:** 2000 bootstrap draws of households, seed 125168.

## How this differs from the paper

| | Paper (LCL, Table 1b) | Here |
|---|---|---|
| Households | 363 | 49, so a smaller library; the paper warns a small pool hurts kNN (section 5) |
| Span and split | LCL subset; last 20% as test | Jul 2026 to Aug 2027; the later half re-forecast day by day |
| Library | windows up to and excluding day d | window and next day end before d |
| Data | LCL as recorded, UK clock | LCL households rescaled and moved onto NZ dates by the source generator; 8 carry synthetic solar netted from import |
| Clock changes | UK | NZ: a 46-half-hour day fills the skipped hour by interpolation, a 50-half-hour day sums the repeated hour |
| Gaps | isolated one-step gaps interpolated | the same; any other gap makes the day incomplete, never used |
| CRPS | quantile form, summed | ensemble form (headline) and the quantile form, averaged |
| Persistence | error quantiles from a training set | every earlier day pair; members clipped at zero, which can only help it |

`billing.account` was not needed at this stage: day-ahead scoring works on ICPs alone.
