# Cohort selection: how many households, and how they are chosen

**Decision (2 Oct 2026):** the generated cohort grows from **50 to 200 households**, chosen by a
stratified rule instead of a hash. Implemented in `pipeline/scripts/gen_source/cohort.py`, rebuilt end
to end the same day (release `demo-20261002T134314`, about 20 minutes).

**The rule, in one list:**
1. Keep eligibility at **≥ 99% coverage** of the 757-day window.
2. Allocate places across **tariff × Acorn by the whole LCL population**, not by the eligible pool.
3. Within each group, **sample systematically across usage**.
4. Fix **solar at 10%** of the cohort.
5. Guarantee **at least three accounts per payment-trouble profile**.
6. **Double the planted test answers.**

Every figure here is printed by
[`analysis/cohort_selection/measure_cohort_designs.py`](../analysis/cohort_selection/README.md)
(§10).

---

## 1. What the cohort is for

The nine hand-built fixtures are regression cases. The generated cohort is the population every
feature is demonstrated on and measured against:

| Feature | Reads |
|---|---|
| Explain a high bill | `kea.mart_bill_explanation` |
| Compare plans; recommend a switch only when it saves | `kea.plan_recommendation` |
| Warn mid-cycle when a bill is heading high | `kea.bill_forecast` |
| Recognise payment difficulty and escalate | `kea.hardship_assessment`, `kea.hardship_signal` |
| Detected loads (appliance-level breakdown) | `kea.mart_daily_detected_load`, planted answers |
| Solar payback and EV charging what-ifs | `kea.solar_estimate`, `kea.ev_scenario` |
| Hardship early warning | `kea.hardship_outlook`, planted answers |
| kNN forecast backtest | offline (design §15.12) |

**Constraints:** a full rebuild ideally under 30 minutes, an 8 GB Docker VM, and a Snowflake account
that was available until 3 Oct 2026.

## 2. What 50 households gave

Measured on the published 50 (`results/serving_now.md`). The size had been chosen for rebuild time
alone, and drawn in proportion to a skewed eligible pool.

| Feature | Measured at 50 | Limitation |
|---|---|---|
| Bill explanation | 60 explainable rises ≥ 10%; period length led 3, base load 2 | too few examples per driver |
| Plans | every switch recommendation went to Time of Use | the other plans are cheapest for only 2–3% of households |
| Forecast | 29 of 50 accounts ever forecastable | the heating-fit gate |
| Hardship | 1 medically dependent account; one profile hashed to nobody | profiles depend on hash luck |
| Detected loads | 7 clean hosts for 4 hot-water places | too few clean planting hosts |
| Solar + EV | 2 clean solar hosts for 3 places | the screen had to be loosened |
| Early warning | ~72 on-time account-years | cannot measure a false-alarm rate under 5 per 100 |
| kNN | pool of 49 | the paper used 363 |

## 3. Eligibility stays at ≥ 99% coverage

Long-history features need near-complete households: year-on-year comparison, the monthly heating
fit, kNN's complete weeks, six bills of payment history, and detected-load rules that count warm-month
nights. Only **609 of 5,566** LCL households pass, and they are skewed, because the households with two
unbroken years are disproportionately affluent:

| Pool | Households | Affluent | Adversity | Comfortable | ToU |
|---|---:|---:|---:|---:|---:|
| All LCL households | 5,566 | 39.4% | 32.6% | 27.1% | 20.2% |
| ≥ 99% coverage | 609 | 50.4% | 23.6% | 25.0% | 15.8% |
| ≥ 90% | 1,484 | 47.6% | 28.9% | 22.8% | 18.6% |
| ≥ 80% | 3,919 | 41.3% | 32.9% | 25.1% | 20.2% |

The mix only approaches the population at 80%, and a fifth of days missing would break every feature
above. So **the threshold stays and the skew is corrected in allocation** (§4).

### The window, in NZ dates

The cohort uses 757 days of the London trial, **2012-02-03 → 2014-02-28**, shifted by **4,928 days**
onto **2025-08-01 → 2027-08-27**. The shift is a whole number of weeks (weekday shapes survive), about
half a year past a whole number of years (London winter lands in NZ winter), and ends on the pinned
`NZ_SPAN_END`. Clock times are kept as wall-clock times, with NZ daylight saving regenerated. Seasons
align; daily weather does not. Details: [`lcl-source-data.md`](../dictionary/lcl-source-data.md) §8 and
§12.

## 4. The rule

1. **Eligibility:** ≥ 99% coverage, unchanged.
2. **Population quotas on tariff × Acorn.** Each group gets places in proportion to its share of all
   5,566 households, at least one place if it has an eligible household, largest remainder for an exact
   total, capped at its eligible count. The size is asserted exactly. Acorn matters because the
   generator uses it: vulnerability and medical-dependence flags, and non-direct-debit payment, are
   drawn for Adversity households, so under-sampling Adversity under-samples hardship. Quotas stay
   exact up to **317** households (the ToU/Adversity group runs out).
3. **Systematic sample across usage within each group.** Members are sorted by mean daily kWh and
   taken at even steps from a seeded start. Usage moves feature behaviour more than Acorn does:

   | Usage tertile | Heating proxy ≥ 0.5 | Clean enough to plant on |
   |---|---:|---:|
   | Low | 55.7% | 84.2% |
   | Mid | 61.1% | 45.3% |
   | High | 78.3% | 7.4% |

   Heavy users give bill shock and heating sensitivity; light users give clean planting hosts.
4. **Solar fixed at 10%.** Previously the count was whatever the hash produced (12–16%).
5. **At least three accounts per payment-trouble profile** (late streak, direct-debit failure, part
   payment, stopped payments), topped up from their hash shares.
6. **Double the planted answers:** 16 planted-load households (8 EV, 8 water heater) and 8 hardship
   accounts (6 sliding, 2 sudden). Recall over 8 households moves in steps of 12.5%, not 25%.
   Thresholds are unchanged and written as rates.

**Unchanged:** the seed, the final hash sort, one network (Vector, Auckland), one customer category.

## 5. What each feature needs, and what 200 gives

Simulated over the eligible pool (`results/designs.md` §3).

| Feature | Needs | 50 | 200 |
|---|---|---|---|
| Bill explanation | households whose usage tracks heating | 39 | **137** |
| Plans | ≥ 3 households where each non-default plan is cheapest | 3 and 2 | **4 and 6** |
| Forecast | forecastable households, plus a population for the fallback | 29 | **~110**, ~90 for the fallback |
| Hardship | ≥ 3 per profile, ≥ 3 medically dependent | 0–5 per profile; 1 medical | **≥ 3 each by rule; 6 medical** |
| Detected loads | clean hosts for 16 planted loads | 10 EV, 7 water heater | **49 EV, 28 water heater** |
| Solar + EV | ≥ 3 clean solar hosts | 2 | **3** (at the loosened bound) |
| Early warning | on-time account-years for a ≤ 5 per 100 target | ~72 | **~330** |
| kNN | a large, varied pool | 49 | **200** |

With ~72 on-time account-years, one false alarm already puts the 95% upper bound at 6.6 per 100; with
~330, up to nine false alarms still bound it below 5.

## 6. How many

| N | Adversity | Heating proxy | Clean hosts | Medical | On time | Est. rebuild |
|---:|---:|---:|---:|---:|---:|---:|
| 50 | 22% | 39 | 25 | 1 | 39 | 9 min (measured) |
| 100 | 31% | 70 | 36 | 3 | 89 | 14–19 min |
| 150 | 32% | 101 | 63 | 5 | 133 | 19–28 min |
| **200** | **32%** | **137** | **80** | **6** | **169** | **23–37 min** |
| 300 | 32% | 190 | 122 | 9 | 259 | 33–56 min |

- **150** is the largest size whose worst-case estimate stays under 30 minutes, but it leaves plans and
  payment profiles thin.
- **200** is the first size where every feature in §5 has at least three examples and the false-alarm
  target becomes measurable.
- **300** adds little any feature needs, for about 50% more run time.

**Measured at 200:**

| Step | Time |
|---|---|
| Stack | 7 s |
| Sources | 9m27s |
| Lake | 2m22s |
| Warehouse (RAW, dbt, Soda) | 3m43s |
| Serving (export, publish, gates) | 2m50s |
| Dashboards | 1m47s |
| **Total, net of retries** | **~20 min** |

Monitoring services were stopped during the warehouse and serving steps; with everything running, the
8 GB Docker VM killed the RAW loader. Scaling up also surfaced three checks that had only held at 50
(a solar base-load test, the Soda volume band, and bill reproduction), all fixed. The temp tables in
`05_defects.sql` now need `ANALYZE`, without which one `DELETE` did not finish in 14 minutes.

The backtest is not part of the rebuild; its run time grows roughly with N².

## 7. What it costs

- **Every generated figure changes.** Only 17 of the previous 50 households remain, and the scale factor
  moves from 1.72 to ~1.95. Old figures remain in git history.
- **The demo household is re-pinned** (`DEMO_ICP` 1027 → 1086), because surrogate ids are regenerated.
- **More heater-shaped false detections.** At ×1.95 a 3 kW UK heater becomes 5.9 kW, and 82 of 200
  households show EV-shaped runs on more than 2% of days. That is realistic, and it is what EV precision
  is honestly measured against.
- **Disk:** the 200 households live in 69 of 112 LCL blocks (~2.8 GB unzipped).

## 8. What this does not solve

- **Cold start.** Every account starts on the span's first day, so the paper's seven-day cold-start
  property cannot be tested. That needs late-joining accounts, a generator change rather than a
  selection rule.
- **The proxies are proxies.** The heating proxy is validated against the real fit; the EV-shape proxy
  is only detect-v1's high-power rule; the cheapest-plan proxy ignores solar, term credits and losses.
- **Acorn is a UK classifier**, used to keep the mix defensible, not as evidence about NZ households.

## 9. Implementation

| Step | Status |
|---|---|
| `cohort.py`: quotas, systematic sampling, fixed solar share, exact size, tests | done |
| Payment-profile minimums, doubled planted answers, evaluation thresholds as rates | done |
| Dry run: every generator, source load and `99_assert.sql` on a throwaway Postgres; `make ci` | done |
| Full rebuild (`demo-20261002T134314`) and Sprint 4 evaluation | done |
| Backtest stages 1–3 at 200 households | done ([results](../analysis/forecast_backtest/results/cohort-200-2026-10-02/)) |
| Re-pin the demo household and the data explorer default | done |
| Archive the 200-household warehouse | not recorded; the latest recorded archive is 30 Sep 2026 |

## 10. Reproducing the figures

```bash
python3 analysis/cohort_selection/measure_cohort_designs.py             # results/designs.md, ~70 s
python3 analysis/cohort_selection/measure_cohort_designs.py --serving   # + results/serving_now.md, needs kea-db
```

It reads the LCL archive, `cohort.py`'s coverage cache and `cohort.json` (plus `kea-db` read-only with
`--serving`), and writes only `analysis/cohort_selection/results/`. The planting-screen counts (49, 28
and 3) are `gen_metering.py`'s own output.
