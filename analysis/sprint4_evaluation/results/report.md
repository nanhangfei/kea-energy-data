# Sprint 4 evaluation against the planted answers

Run 2026-09-28 23:42 UTC. Method and thresholds: `analysis/sprint4_evaluation/README.md`. Planted data proves the pipeline finds what the generator planted. It does not prove real households behave that way.

Planted households: 11. Days left out because the generator altered them after planting: 887 (revised read 176, src:duplicate 236, src:estimated_day 328, src:final 1, src:gap 62, src:spike 75, src:stuck_meter 9).

## Summary

| Section | Measure | Value | Target | |
|---|---|---|---|---|
| sessions | high_power_precision | 1.000 | 0.90 | PASS |
| sessions | high_power_recall | 1.000 | 0.85 | PASS |
| sessions | low_power_claims_on_masked_runs | 0 | 0 | PASS |
| daily_bounds | share_inside | 0.616 | 0.90 | MISS |
| night_timer | share_detected | 1.000 | 0.80 | PASS |
| heating_fit | replication_mismatches | 0 | 0 | PASS |
| bill_driver | step_ratio[wallbox, evening, 1] | 1.098 | 0.70 | PASS |
| bill_driver | step_ratio[wallbox, evening, 2] | 1.104 | 0.70 | PASS |
| bill_driver | step_ratio[wallbox, timer, 1] | 1.000 | 0.70 | PASS |
| bill_driver | all_wallboxes_at_least_70pct | True | true | PASS |
| forecast | only_v3_rows | True | true | PASS |
| hardship | median_lead_days | 18.000 | 21 | MISS |
| hardship | gradual_caught | 3 | 2 | PASS |
| hardship | false_alarms_per_100_account_years | 0.000 | 5 | PASS |
| hardship | outlooks_not_after_their_date | 0 | 0 | PASS |

## 1. Charging sessions

| Household | Planted sessions counted | Wallbox sessions found | high_power detected | …not matching a wallbox session | low_power detected |
|---|---|---|---|---|---|
| portable, evening, 1 | 87 | – | 0 | 0 | 87 |
| solar wallbox, evening, 1 | 115 | 115 | 115 | 0 | 1 |
| solar wallbox, midday, 1 | 134 | 34 | 34 | 0 | 2 |
| solar wallbox, timer, 1 | 64 | 64 | 64 | 0 | 0 |
| wallbox, evening, 1 | 136 | 136 | 136 | 0 | 2 |
| wallbox, evening, 2 | 118 | 118 | 118 | 0 | 3 |
| wallbox, timer, 1 | 95 | 95 | 95 | 0 | 1 |
| hot water, timer, 1 | 0 | – | 0 | 0 | 1 |
| hot water, timer, 2 | 0 | – | 0 | 0 | 0 |
| hot water, timer, 3 | 0 | – | 0 | 0 | 0 |
| hot water, timer, 4 | 0 | – | 0 | 0 | 0 |

| Measure | Result | Target |  |
|---|---|---|---|
| high_power precision (planted households) | 349 of 349 = 100.0% | ≥ 90% | PASS |
| high_power recall (wallbox sessions) | 349 of 349 = 100.0% | ≥ 85% | PASS |
| portable sessions found as low_power (no target) | 87 of 87 = 100.0% | – |  |
| portable sessions found as high_power (a class error) | 0 | – |  |

Left out because a date they touch was not assessed or was altered after planting: 114 planted EV sessions, 94 detected sessions.

### 1a. Where the panels were in the way

The three households with both an array and a car, scored apart from the eight above so that no Sprint 4 figure moves. A planted session is **hidden** when any of its half-hours was exporting, or sat between that day's first and last export (`is_masked` / `is_daylight_shoulder`, read from silver and measured off the export register -- no PV curve is modelled anywhere in this report's inputs). The rest are **clear**.

⚠️ **Found means a published session overlaps it, whatever class it carries.** detect-v2 withholds `charger_class` on a hidden run rather than guessing it, and a withheld class is not a missing EV -- counting only `high_power` here would score the fix as a regression.

| Household | Array | Clear sessions | …found | Hidden sessions | …found | Runs published with no class | low_power runs | Before the car arrived |
|---|---|---|---|---|---|---|---|---|
| solar wallbox, evening, 1 | 5 kWp | 115 | 115 = 100.0% | 0 | 0 = – | 0 | 1 | 1 in 405 d (0.9/yr) |
| solar wallbox, midday, 1 | 8 kWp | 6 | 6 = 100.0% | 128 | 56 = 43.8% | 28 | 2 | 2 in 355 d (2.1/yr) |
| solar wallbox, timer, 1 | 3 kWp | 64 | 64 = 100.0% | 0 | 0 = – | 0 | 0 | 0 in 455 d (0.0/yr) |

| Measure | Result | Target |  |
|---|---|---|---|
| recall, sessions the meter saw whole (no target) | 185 of 185 = 100.0% | – |  |
| recall, sessions the panels hid (no target) | 56 of 128 = 43.8% | – |  |
| low_power claimed on a run the panels hid | 0 | 0 | PASS |

Read the two recalls as a pair, not as a pass and a fail. **The clear one is the claim**: a solar household's night is as readable as anyone else's, so it should sit near §1's headline recall, and if it does not then `is_daylight_shoulder` is reaching into the dark and damaging detection that worked. **The hidden one has no target and never will.** When the array covers the whole draw the import register reads 0 for the length of the charge: there is no excess above the day's floor, so there is no run to find and no class to withhold -- the session is missing from `mart_charging_session` outright. That is the meter's limit, not the ruleset's, and the honest report of it is a number with no line under it.

The last column is each host's own-load background. SOLAR_PLAN's hosts are screened at MAX_SOLAR_EV_SHAPED_SHARE = 0.015 of assessed days rather than PLAN's 0.005 -- only eight households in the cohort have panels, and the strict bound leaves two of them for three places -- so these three carry a little more label noise than the eight above, and it is reported here rather than assumed away.

## 2. Daily bounds

A planted day is a date with planted kWh on it, split by half-hour. It is inside when low − 0.01 ≤ planted ≤ high + 0.01, against the EV bounds for an EV and the timed-night bounds for hot water.

| Household | Planted days | Altered after planting | Not assessed | Counted | Inside | Below low | Above high |
|---|---|---|---|---|---|---|---|
| portable, evening, 1 | 123 | 13 | 1 | 109 | 48 (44.0%) | 59 | 2 |
| solar wallbox, evening, 1 | 133 | 10 | 1 | 122 | 40 (32.8%) | 65 | 17 |
| solar wallbox, midday, 1 | 152 | 16 | 2 | 134 | 0 (0.0%) | 0 | 134 |
| solar wallbox, timer, 1 | 151 | 23 | 0 | 128 | 78 (60.9%) | 42 | 8 |
| wallbox, evening, 1 | 153 | 12 | 1 | 140 | 47 (33.6%) | 93 | 0 |
| wallbox, evening, 2 | 135 | 14 | 0 | 121 | 27 (22.3%) | 94 | 0 |
| wallbox, timer, 1 | 182 | 16 | 2 | 164 | 162 (98.8%) | 2 | 0 |
| hot water, timer, 1 | 757 | 83 | 2 | 672 | 641 (95.4%) | 16 | 15 |
| hot water, timer, 2 | 757 | 68 | 5 | 684 | 606 (88.6%) | 60 | 18 |
| hot water, timer, 3 | 757 | 80 | 7 | 670 | 340 (50.7%) | 325 | 5 |
| hot water, timer, 4 | 757 | 84 | 3 | 670 | 236 (35.2%) | 428 | 6 |

| Measure | Result | Target |  |
|---|---|---|---|
| planted days inside [low, high] | 2225 of 3614 = 61.6% | ≥ 90% | MISS |
| …ev_portable_2kw | 48 of 109 = 44.0% | – |  |
| …ev_solar_wallbox_7kw | 118 of 384 = 30.7% | – |  |
| …ev_wallbox_7kw | 236 of 425 = 55.5% | – |  |
| …hot_water_3kw | 1823 of 2696 = 67.6% | – |  |


Inside or outside is a hard edge on a band that is often under a kilowatt-hour wide, so here is how far outside the days fall (reported, no target). The distance is 0 for a day inside the band.

| Outside the band by at most | Days | Share |
|---|---|---|
| 0.10 kWh | 2660 | 73.6% |
| 0.25 kWh | 3048 | 84.3% |
| 0.50 kWh | 3220 | 89.1% |
| 1.00 kWh | 3318 | 91.8% |

| …or, against the day's own planted kWh, at most | Days | Share |
|---|---|---|
| 2% of it | 2789 | 77.2% |
| 5% of it | 3239 | 89.6% |
| 10% of it | 3373 | 93.3% |
| 20% of it | 3448 | 95.4% |

Median band width: 0.47 kWh. For the days outside it, the median miss is 0.17 kWh, 2.6% of the day's planted kWh. Published half-hours minus planted half-hours: median 0.0, mean -0.15 — the model finds the right half-hours, and the low bound sits a little above the planted kWh in them.

## 3. Night timer

A planted night is detected when at least 50% of its half-hours are classed `timed_night` in silver.

| Household | Nights counted | Detected | Read as EV instead |
|---|---|---|---|
| hot water, timer, 1 | 601 | 600 (99.8%) | 0 |
| hot water, timer, 2 | 623 | 623 (100.0%) | 0 |
| hot water, timer, 3 | 592 | 592 (100.0%) | 0 |
| hot water, timer, 4 | 595 | 595 (100.0%) | 0 |

| Measure | Result | Target |  |
|---|---|---|---|
| planted nights detected | 2410 of 2411 = 100.0% | ≥ 80% | PASS |

## 4. Households with nothing planted (unlabelled)

LCL households have loads of their own, so what is found here is reported, not scored.

| Group | Households | Assessed household-years | With an EV session | high_power per household-year | low_power per household-year | With timed-night days |
|---|---|---|---|---|---|---|
| no solar | 34 | 67.2 | 31 | 49.1 | 19.9 | 1 |
| solar | 5 | 10.2 | 4 | 26.8 | 15.9 | 0 |

## 5. Heating fit

Refitted here with the model's rule (months of ≥ 20 complete days, ≥ 6 months, OLS) three ways for each planted household: on import as metered (**gross**), net of the published low bounds (**net**, what dbt fits), and net of the exact planted kWh (**oracle**, the fit the household would have had with nothing planted). The net fit works if it sits near the oracle.

| Household | Gross slope / R² | Net (published rule) | Oracle | Net − oracle slope |
|---|---|---|---|---|
| portable, evening, 1 | 0.210 / R² 0.53 | 0.210 / R² 0.53 | 0.210 / R² 0.53 | 0.000 |
| solar wallbox, evening, 1 | 1.107 / R² 0.66 | 1.098 / R² 0.67 | 1.107 / R² 0.66 | -0.009 |
| solar wallbox, midday, 1 | 1.316 / R² 0.74 | 1.332 / R² 0.74 | 1.316 / R² 0.74 | 0.015 |
| solar wallbox, timer, 1 | 0.624 / R² 0.69 | 0.624 / R² 0.69 | 0.624 / R² 0.69 | 0.000 |
| wallbox, evening, 1 | 0.428 / R² 0.20 (under 0.30) | 0.427 / R² 0.20 (under 0.30) | 0.428 / R² 0.20 (under 0.30) | -0.001 |
| wallbox, evening, 2 | 0.105 / R² 0.06 (under 0.30) | 0.089 / R² 0.04 (under 0.30) | 0.105 / R² 0.06 (under 0.30) | -0.016 |
| wallbox, timer, 1 | 0.049 / R² 0.41 | 0.013 / R² 0.41 | 0.049 / R² 0.41 | -0.036 |
| hot water, timer, 1 | 1.356 / R² 0.54 | 0.629 / R² 0.27 (under 0.30) | 0.492 / R² 0.22 (under 0.30) | 0.136 |
| hot water, timer, 2 | 0.917 / R² 0.79 | 0.080 / R² 0.04 (under 0.30) | 0.054 / R² 0.02 (under 0.30) | 0.026 |
| hot water, timer, 3 | 2.065 / R² 0.65 | 1.190 / R² 0.50 | 1.200 / R² 0.51 | -0.010 |
| hot water, timer, 4 | 1.085 / R² 0.73 | 0.217 / R² 0.21 (under 0.30) | 0.223 / R² 0.21 (under 0.30) | -0.006 |

| Cohort | Before (18 Sep, recorded) | After |
|---|---|---|
| ICPs with a fit | 41 | 49 |
| …with R² ≥ 0.30 | 27 | 32 |
| median R² | 0.4834 | 0.4988 |

Replication: the net refit here matches the published silver fit on 49 of 49 ICPs (tolerance 0.0005).

## 6. Bill driver

For each EV household, the explained bill whose period contains its first planted session, against its prior bill. Detected step = (this − prior) × period days, from the mart's own per-day means. Planted step = the same arithmetic on the planted kWh over the same assessed days.

| Household | EV's first day in the period | Planted step kWh | Detected step kWh | Detected ÷ planted | ≥ 70% |
|---|---|---|---|---|---|
| portable, evening, 1 | 6 of 31 | 152.2 | 155.2 | 102% | (no target) |
| solar wallbox, evening, 1 | 24 of 31 | 58.4 | 63.2 | 108% | (no target) |
| solar wallbox, midday, 1 | 9 of 30 | 199.0 | 40.3 | 20% | (no target) |
| solar wallbox, timer, 1 | 2 of 31 | 281.8 | 279.6 | 99% | (no target) |
| wallbox, evening, 1 | 27 of 31 | 81.4 | 89.4 | 110% | PASS |
| wallbox, evening, 2 | 2 of 30 | 226.1 | 249.6 | 110% | PASS |
| wallbox, timer, 1 | 2 of 31 | 286.7 | 286.7 | 100% | PASS |

## 7. Forecast `hdd-linear-v3` (reported, no target)

Absolute percentage error of `projected_kwh` against the cycle's import, summed from daily usage where every day of the cycle is complete. For EV households, only cycles starting after the EV's first session.

| Group | Forecasts scored | Median APE | …days 1–14 | …day 15 on |
|---|---|---|---|---|
| hot-water households | 180 | 5.4% | 6.7% | 5.1% |
| nothing planted | 5831 | 4.8% | 7.4% | 3.0% |

Rows by model version: `hdd-linear-v3` 9,354.

## 8. Hardship early warning (weekly replay)

| Account | Arrears onset (planted) | First early outreach | Days before onset | First v1 signal | v1 days before onset | First outlook (made → projected onset) |
|---|---|---|---|---|---|---|
| sliding 1 | 2026-06-22 | 2026-05-02 | 51 | 2026-07-04 | – | 2026-05-02 → 2026-06-22 |
| sliding 2 | 2026-07-29 | 2026-07-11 | 18 | 2026-08-08 | – | 2026-07-11 → 2026-07-29 |
| sliding 3 | 2026-08-04 | 2026-07-18 | 17 | 2026-08-15 | – | 2026-07-18 → 2026-08-04 |
| sliding_sudden 4 | 2027-06-18 | never | – | 2027-06-19 | – | – |

| Measure | Result | Target |  |
|---|---|---|---|
| median days of warning (gradual accounts caught) | 18 | ≥ 21 | MISS |
| gradual accounts caught before onset | 3 of 3 | ≥ 2 of 3 | PASS |
| early outreaches per 100 on-time account-years | 0 in 55.3 = 0.00 | ≤ 5 | PASS |

Replay: 93 weekly dates from 2025-11-22 to 2027-08-28, 45 open cohort accounts. On-time accounts (31): not planted, no failed payment, and every bill due by the generator's payment horizon paid in full on or before its due date. An alarm is a run of consecutive weeks with the early combination; the job itself keeps one outreach open per account.

14 on-time accounts ever had a v1 signal in the replay. Outlooks projecting arrears on or before their own date: 0. `payments_applied` differs from the sum of successful payments on 0 of 1126 bills, which is why the replay sums payments by date.

SLIDING_SUDDEN stops paying with no warning sign first, so a miss there is expected.

## 9. Solar sanity check (no known answer)

For the real solar households: the generator's own panels have a known output, so each half-hour's load is import − export + that output. Each household is checked against ITS OWN array rather than one size for all (1.5–8 kWp, sized to load by `cohort.py`, or to the car by `planted.SOLAR_PLAN` for the three households that have one), and the model's exported share of generation is set beside the metered one. The two use different sun shapes and yields, so they should be close, not equal.

| Household | Array | Half-hours | Metered export share | Model export share (low – high yield) | Model mid − metered (points) |
|---|---|---|---|---|---|
| solar 1 | 8 kWp | 17,426 | 61.8% | 57.2% – 63.5% | -1.5 |
| solar 2 | 4 kWp | 17,256 | 48.1% | 40.9% – 50.8% | -2.2 |
| solar 3 | 8 kWp | 17,435 | 48.0% | 40.4% – 50.3% | -2.6 |
| solar 4 | 3 kWp | 17,423 | 48.8% | 43.4% – 50.5% | -1.8 |
| solar 5 | 3 kWp | 17,439 | 76.9% | 73.0% – 78.4% | -1.3 |
| solar 6 | 1.5 kWp | 17,386 (2025-11 left out) | 39.1% | 32.1% – 41.6% | -2.3 |
| solar 7 | 3 kWp | 17,225 (2025-11 left out) | 45.8% | 38.8% – 48.2% | -2.3 |
| solar 8 | 5 kWp | 17,471 | 59.7% | 52.2% – 62.4% | -2.4 |

### 9a. Charging into your own sun (`solar_soak`)

`build_ev_scenarios.py` offers this pattern only to an account whose basis window actually exported, and prices it with the same `charge_half_hour()` as every other pattern -- which already charges from export before import. Each account is shown on ITS CURRENT PLAN (`plan_id = current_plan_id`), because that is the comparison a customer is being offered.

| Scenario | Accounts offered it | Median annual EV kWh | Median solar_soak saving | Median night saving | …where solar_soak wins | …where it loses money |
|---|---|---|---|---|---|---|
| add_ev | 8 | 2,084 | 277.52 | -2.60 | 5 of 8 | 0 |
| shift_detected | 7 | 992 | 58.85 | 0.00 | 5 of 7 | 0 |

⚠️ **A losing row is published, not suppressed.** On Time of Use the import a roof does not cover sits in the most expensive window of the day, and on a plan that buys export back above its own night rate, moving a charge off the night rate and onto the roof can cost more than it saves. When that is the answer, saying so is the point: the last column is a count of honest negatives, not of failures. `shift_detected` moves a detected session's kWh and drops none of them, so its kWh column matches the `as_detected` baseline by construction; `add_ev` charges the same annual kWh as `evening` does.

## 10. Hand checks

* **A planted wallbox day** (wallbox, evening, 1, 2026-09-20): 20.05 kWh planted in the half-hours of that date; the mart publishes 6 EV half-hours and [19.52, 21.85] kWh. Inside.
* **A bill pair spanning the EV's arrival** (wallbox, evening, 1): (2.8842 − 0.0000) kWh/day × 31 days = 89.4 kWh detected, against 81.4 kWh planted.
  The prior bill's variable rate = ($146.91 − $39.09 fixed) ÷ 381.5 kWh = $0.2826/kWh, so the driver ≈ 89.4 × 0.2826 = $25.27. Published `delta_from_detected_loads`: $25.27 (the mart multiplies the unrounded means; the served ones have 4 decimals, so a few cents apart is expected), of an amount change of $25.57; $-5.49 is left unexplained.
* **A solar payback:** One 3.0 kW estimate (`sprint4-assumptions-v1`): install $6,000.00 – $9,000.00, saving $409.57 – $519.99 a year.
  Best case $6,000.00 ÷ $519.99 = 11.5 years; worst case $9,000.00 ÷ $409.57 = 22.0 years. Published: 11.5 – 22.0. Match.
