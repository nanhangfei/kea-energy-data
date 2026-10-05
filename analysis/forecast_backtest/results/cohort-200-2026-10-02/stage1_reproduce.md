# Botman backtest, Stage 1: reproduce the paper, day-ahead

Export sealed **2026-10-02 01:45 UTC** (the newest `_SUCCESS` of `metering.icp`, `metering.meter_register`, `metering.meter_read_hh`; `metering.icp` 2026-10-02 01:45 UTC, `metering.meter_register` 2026-10-02 01:45 UTC, `metering.meter_read_hh` 2026-10-02 01:45 UTC). Run 2026-10-02 02:24 UTC in 384 s. Aggregates only.

## Verdict: Clearly beats persistence; level with historical sampling

kNN's +1.2% over historical sampling has an interval that spans zero, and kNN wins on only 32% of households. The plan's review point has no rule for a tie: clearing persistence permits Stage 2, and falling short of the paper's +7.0% over historical sampling points at the pool size. A small pool is the paper's own stated failure mode (section 5), and Stage 3's pool-size curve measures it. Which runs first is your call.

## Scores

CRPS is kWh per half-hour, averaged over every scored half-hour. All three models are scored on the same 37,663 household-days.

| Model | CRPS (ensemble) | CRPS (paper's quantile form) | P10–P90 coverage | Paper's CRPS (LCL) |
|---|---:|---:|---:|---:|
| kNN, k=100, week normalisation | 0.17432 | 0.08728 | 72.9% | 0.0912 |
| Historical sampling | 0.17649 | 0.08833 | 75.6% | 0.0981 |
| Probabilistic persistence | 0.21505 | 0.10758 | 77.7% | 0.1253 |

The absolute CRPS is not comparable with the paper's: the households, dates and scale differ. Compare the gains.

| kNN gain over | Here (ensemble) | 95% CI, households resampled | Here (quantile form) | Paper | Households where kNN wins |
|---|---:|---:|---:|---:|---:|
| Historical sampling | +1.2% | -1.4% to +4.2% | +1.2% | +7.0% | 32% |
| Probabilistic persistence | +18.9% | +17.1% to +20.5% | +18.9% | +27.2% | 98% |

Mean import on the scored days is 0.441 kWh per half-hour, so kNN's CRPS is 40% of it. A P10–P90 band should cover 80%; kNN's covers 72.9% (too narrow); Historical sampling's covers 75.6% (too narrow); Probabilistic persistence's covers 77.7% (too narrow). The quantiles are numpy's linear ones, as the paper's `nanquantile()`.

### By household group

| Group | Households | Household-days | kNN CRPS | Gain over HS | Gain over persistence |
|---|---:|---:|---:|---:|---:|
| No export register | 179 | 33,882 | 0.17523 | +1.6% | +19.1% |
| Export register (synthetic solar) | 20 | 3,781 | 0.16624 | -2.0% | +17.4% |

An export register means the Stage 2 generator netted synthetic solar out of that household's import, so its shape is no longer plain LCL.

### By month

| Month | Household-days | kNN CRPS | Gain over HS | Gain over persistence | kNN coverage |
|---|---:|---:|---:|---:|---:|
| 2027-02 | 3,576 | 0.11598 | +13.9% | +22.3% | 76% |
| 2027-03 | 5,922 | 0.13896 | +0.0% | +20.8% | 75% |
| 2027-04 | 5,721 | 0.16577 | -6.6% | +19.2% | 74% |
| 2027-05 | 5,904 | 0.19890 | -1.0% | +18.0% | 71% |
| 2027-06 | 5,719 | 0.20338 | +5.0% | +17.9% | 70% |
| 2027-07 | 5,885 | 0.19538 | +1.0% | +17.6% | 72% |
| 2027-08 | 4,936 | 0.18079 | +1.5% | +19.5% | 73% |

## Data

| | |
|---|---:|
| Cohort ICPs in the export | 200 |
| … with an import register | 199 |
| … with import reads (households) | 199 |
| … of which with an export register | 20 |
| … of which with more than one import register | 0 |
| Import reads | 3,764,668 |
| … with no kWh (treated as unread) | 0 |
| … repeating a register and half-hour (day incomplete) | 0 |
| … not on a half-hour (ignored) | 0 |
| NZ days in the calendar | 2026-07-27 to 2027-08-27 |
| DST days (46 / 50 half-hours) | 1 / 1 |
| Complete household-days | 78,045 (99.0% of the active span) |
| Library windows (7 days + next day, all complete) | 73,698 |
| Scored days | 2027-02-10 to 2027-08-26 |
| Household-days with a complete day and week before | 37,663 |
| … dropped, no earlier baseline member | 0 |
| Household-days scored | 37,663 across 199 households |
| Neighbours from the target's own history | 14.0% |

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
| Households | 363 | 199, so a smaller library; the paper warns a small pool hurts kNN (section 5) |
| Span and split | LCL subset; last 20% as test | Jul 2026 to Aug 2027; the later half re-forecast day by day |
| Library | windows up to and excluding day d | window and next day end before d |
| Data | LCL as recorded, UK clock | LCL households rescaled and moved onto NZ dates by the source generator; 20 carry synthetic solar netted from import |
| Clock changes | UK | NZ: a 46-half-hour day fills the skipped hour by interpolation, a 50-half-hour day sums the repeated hour |
| Gaps | isolated one-step gaps interpolated | the same; any other gap makes the day incomplete, never used |
| CRPS | quantile form, summed | ensemble form (headline) and the quantile form, averaged |
| Persistence | error quantiles from a training set | every earlier day pair; members clipped at zero, which can only help it |

`billing.account` was not needed at this stage: day-ahead scoring works on ICPs alone.
