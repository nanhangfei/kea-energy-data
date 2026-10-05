# Serving today: the published 50-household cohort

Run 2026-10-01 23:32 UTC by `measure_cohort_designs.py --serving`, read-only, against `kea-db`. Queries: `serving_now.sql`. Generated accounts only (account_id >= 1,000,000).


## Accounts on supply, by plan and panels

| plan_code | has_solar | accounts | vulnerable | medical |
|---|---|---|---|---|
| KEA_ANY_VECT_STA | f | 16 | 1 | 0 |
| KEA_ANY_VECT_STA | t | 2 | 0 | 0 |
| KEA_TOU_VECT_STA | f | 11 | 2 | 1 |
| KEA_TOU_VECT_STA | t | 3 | 0 | 0 |
| KEA_WKND_VECT_STA | f | 10 | 0 | 0 |
| KEA_WKND_VECT_STA | t | 3 | 0 | 0 |

## Feature 1: bills, and the ones the explanation may present

| bills | accounts | explainable | explainable_rises_10pct | accounts_with_one |
|---|---|---|---|---|
| 1151 | 47 | 211 | 60 | 27 |

## Feature 1: the largest driver of each explainable rise of 10% or more

| driver | bills |
|---|---|
| unexplained | 30 |
| weather | 14 |
| detected loads | 11 |
| period length | 3 |
| base load | 2 |

## Feature 2: the latest recommendation generation

| accounts | told_to_switch | switch_to |
|---|---|---|
| 44 | 26 | KEA_TOU_VECT_STA |

## Feature 3: forecast coverage

| snapshots | forecastable | accounts | accounts_ever_forecastable |
|---|---|---|---|
| 35345 | 9670 | 50 | 29 |

## Feature 3: forecasts published, and the ones flagged unusually high

| model_version | forecasts | accounts | anomalous | accounts_anomalous |
|---|---|---|---|---|
| hdd-linear-v3 | 9354 | 28 | 246 | 19 |

## Feature 4 and S3: signals on each account's latest assessment

| signal_code | accounts |
|---|---|
| bill_above_usual_payment | 16 |
| late_payment_streak | 6 |
| part_payment_arrears | 6 |
| direct_debit_failed | 3 |
| usage_collapse | 3 |
| payments_stopped | 2 |

## S3: accounts with a bill that could start a slide (>= 130% of the median of the six before, three bills after)

| accounts_with_trigger | accounts_with_seven_bills |
|---|---|
| 36 | 45 |

## S1: generated ICPs with a detected load, by class and confidence

| class | confidence | icps |
|---|---|---|
| ev | low | 34 |
| ev | medium | 31 |
| ev | high | 22 |
| timed_night | high | 4 |

## S2: solar estimates, by system and battery

| system_kw | battery_kwh | accounts | payback_low | payback_high |
|---|---|---|---|---|
| 3.0 | 0.0 | 36 | 4.6 | 36.0 |
| 3.0 | 13.5 | 36 | 10.8 | 52.7 |
| 5.0 | 0.0 | 36 | 5.7 | 36.0 |
| 5.0 | 13.5 | 36 | 9.4 | 45.7 |
| 7.0 | 0.0 | 36 | 6.7 | 36.0 |
| 7.0 | 13.5 | 36 | 8.6 | 43.2 |

## S2: EV what-ifs

| scenario | accounts | saving_min | saving_median | saving_max |
|---|---|---|---|---|
| add_ev | 44 | -44 | 0 | 439 |
| shift_detected | 28 | -209 | 0 | 616 |

## Calibration: the heating proxy against `is_forecastable`

45 published accounts matched; 28 were forecastable on at least one snapshot.

| Proxy R² cut | Proxy positive | … forecastable | Forecastable but proxy negative |
|---:|---:|---:|---:|
| 0.3 | 37 | 28 | 0 |
| 0.4 | 35 | 28 | 0 |
| 0.5 | 35 | 28 | 0 |
| 0.6 | 27 | 22 | 6 |
| 0.7 | 20 | 17 | 11 |
