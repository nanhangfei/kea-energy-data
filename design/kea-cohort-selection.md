# Cohort selection: how many households, and how they are chosen

**Decided 2 Oct 2026. Code changed and dry-run the same day, not yet rebuilt:** `cohort.json` now
holds the 200, and the source load passes its gate on a throwaway database (§9). Serving still
publishes the 50 described in [`dictionary/lcl-source-data.md` §13](../dictionary/lcl-source-data.md).
The rebuild can't run after the Snowflake trial converts on 3 Oct 2026 (§7).

**The decision.** The generated cohort grows from **50 to 200 households**, chosen by a new rule:
1. **Keep eligibility at ≥ 99% coverage** of the 757-day window.
2. **Allocate places across tariff × Acorn by the whole LCL population**, not by the eligible pool,
   with at least one place per group.
3. **Within each group, sample systematically across usage.**
4. **Fix the solar count at 10%** of the cohort.
5. **Guarantee at least three accounts per payment-trouble profile.**
6. **Double the planted answers.**

The full rebuild is estimated at **23–37 minutes**, against the 9m17s measured at 50 households.
30 minutes is the preference, not a hard limit; the user accepted more if the features need it.

Every figure here is printed by
[`analysis/cohort_selection/measure_cohort_designs.py`](../analysis/cohort_selection/README.md):
- `results/designs.md` covers the pool and the designs;
- `results/serving_now.md` covers what the published 50 do today.

§10 says how to re-run it.

---

## 1. What the cohort is for

The nine hand-built fixtures keep the Sprint 2 regression cases. The generated cohort is everything
else: it is the only population most features are demonstrated on and measured against. It has to
exercise:

| | Feature (the brief) | What it reads |
|---|---|---|
| 1 | Explain a high bill from the customer's own half-hours | `kea.mart_bill_explanation` |
| 2 | Compare plans; recommend a switch only when it saves | `kea.plan_recommendation` |
| 3 | Warn mid-cycle when a bill is heading unusually high | `kea.bill_forecast` (`hdd-linear-v3`) |
| 4 | Recognise payment difficulty; escalate hardship and disconnection risk to a human | `kea.hardship_assessment`, `kea.hardship_signal` |
| 5 | Everyday account questions: usage, next-bill estimate, adding an EV or solar | the marts above |
| S1 | Appliance-level breakdown (detected loads) | `kea.mart_daily_detected_load`, planted answers in `gen_audit` |
| S2 | Solar payback and EV off-peak charging | `kea.solar_estimate`, `kea.ev_scenario` |
| S3 | Hardship early warning, weeks ahead | `kea.hardship_outlook`, planted answers in `gen_audit` |
| — | The Botman et al. (2025) kNN-quantile forecast | the backtest, design §15.12 (offline; not yet served) |

**The constraints:**
- **Time.** A full `make rebuild-all` is ideally 30 minutes or less, and was 9m17s at 50
  households (29 Sep 2026).
- **Snowflake.** The warehouse exists until 3 Oct 2026.
- **Disk.** 10 GB was free when this was written.
- **Docker memory.** 7.7 GB, and Metabase has already been OOM-killed once at 50.

## 2. What 50 households give the features today

Measured on the published cohort (`results/serving_now.md`). 45 of the 50 accounts are still on supply.

| Feature | Measured | What limits it |
|---|---|---|
| 1 | 211 of 1,151 bills explainable. 60 explainable rises of ≥10% across 27 accounts; their largest driver is unexplained 30, weather 14, detected loads 11, **period length 3, base load 2** | `is_explainable` needs a heating fit with R² ≥ 0.30; two drivers have too few examples to show |
| 2 | 26 of 44 accounts told to switch — **every one to Time of Use** | Anytime and Free Weekends are each the cheapest plan for only ~2–3% of households (§5) |
| 3 | 29 of 50 accounts ever forecastable; 246 unusually high forecasts on 19 accounts | the same heating-fit gate |
| 4 | Signals on the latest assessments: bill above usual payment 16, late streak 6, part-payment 6, DD failed 3, usage collapse 3, payments stopped 2. **1 medically dependent account** | profiles are 2–6% hash shares; STOPPED hashed to nobody and was forced to one |
| S1 | 22 generated ICPs have *high-confidence* EV-class detections, against 7 planted EVs; timed-night detections on 4 generated ICPs, as many as there are planted elements | planting needs a clean own load: **7 candidates for 4 hot-water places** |
| S2 | Solar estimates for 36 accounts, paybacks 4.6–52.7 years; EV what-ifs for 44 | only 8 households have panels; the strict screen left **2 of them for 3 solar+EV places**, so the bound was loosened (`planted.py`, `MAX_SOLAR_EV_SHAPED_SHARE`) |
| S3 | 36 of 45 accounts have a bill that could start a slide | not the candidates: the false-alarm target has about 70 on-time account-years under it (§5) |
| kNN | pool of 49; Stage 1 ties historical sampling (+0.9%) where the paper gains +7.0% | the pool-size curve runs out at 49 (§5) |

**Why 50.** It was set for rebuild time alone, in early September: nothing about it was derived from
these needs.

**Why it skews.** It is drawn in proportion to the *eligible pool*, which is skewed (§3): affluent
52%, adversity 22%, ToU 16%.

## 3. Eligibility stays at ≥ 99% coverage

A household is eligible only if it has readings on at least 99% of the 757 days. Every long-history
feature depends on that:
- year-on-year comparison and `same_period_last_year_kwh`;
- the monthly heating fit;
- kNN's complete weeks (an incomplete day is never a query, a window or a continuation);
- six bills of payment history before a slide can start;
- detected-load rules that count warm-month nights.

**What it costs:** 609 of 5,566 households pass, and those 609 are skewed. LCL recruited in waves,
and the households with two unbroken years are disproportionately affluent.

**Loosening the threshold doesn't fix it:**

| Pool | Households | Affluent | Adversity | Comfortable | ToU |
|---|---:|---:|---:|---:|---:|
| All LCL households | 5,566 | 39.4% | 32.6% | 27.1% | 20.2% |
| ≥ 99% coverage | 609 | 50.4% | 23.6% | 25.0% | 15.8% |
| ≥ 97% | 710 | 51.8% | 24.1% | 23.1% | 15.8% |
| ≥ 95% | 828 | 53.0% | 24.2% | 22.0% | 15.7% |
| ≥ 90% | 1,484 | 47.6% | 28.9% | 22.8% | 18.6% |
| ≥ 80% | 3,919 | 41.3% | 32.9% | 25.1% | 20.2% |

The mix only nears the population at 80%, and a fifth of the days missing would break every feature
listed above. So the threshold stays, and the skew is corrected where the places are allocated.

### The window, in NZ dates

The trial published readings from November 2011 to February 2014, but the cohort uses less of it,
and in NZ's calendar. Both choices are pinned in `pipeline/scripts/gen_source/uk_to_nz.py`.

**Only part of the London span is used: 2012-02-03 → 2014-02-28,** 757 days. LCL recruited in
waves, so the early months hold few households. The window starts where enough of them have two
unbroken years ([`lcl-source-data.md`](../dictionary/lcl-source-data.md) §12).

**Every date is shifted by the same 4,928 days,** and the shift has to have three properties
(§8 there):

| Property | Why |
|---|---|
| a whole number of weeks (704) | A Monday stays a Monday, so weekday and weekend shapes survive. Free Weekends and the Time of Use windows depend on them |
| about half a year past a whole number of years (13.49 years) | London's winter lands in NZ's winter, so heating load falls in the right season |
| it ends on `NZ_SPAN_END`, 2027-08-27 | The demo's pinned "today". Never `date.today()`, or every re-run would drift |

```
UK  2012-02-03 (Fri)  + 4,928 days  ->  NZ  2025-08-01 (Fri)
UK  2014-02-28 (Fri)  + 4,928 days  ->  NZ  2027-08-27 (Fri)
```

**What does not move:** the clock time. `tstp` is UTC, so it is converted to a London wall clock
first. That wall clock is kept and becomes an Auckland instant, with NZ daylight saving
regenerated, so a 07:00 kettle stays at 07:00 (§5 there). The seasons align but the days don't:
a London cold snap is not an Auckland one (§8 there).

**What a query of the source sees:** the 200 households read from 2025-08-01 to 2027-08-27
(8.11 M reads). A query over all of `src_metering.interval_reads` reaches back to 2023-06-01,
because the nine hand-built fixtures sit there (June 2023 – August 2024, 44,044 reads). They
are Sprint 2's regression households, not LCL data, and they never take a place in the cohort.

## 4. The rule

**1. Eligibility:** ≥ 99% of the window, unchanged (§3).

**2. Population quotas on tariff × Acorn.**
- **How:** each `(stdorToU, Acorn_grouped)` group gets places in proportion to its share of **all
  5,566** households. Every group with an eligible household gets at least one place. Largest
  remainder makes the total exact, and a group is capped at its eligible count.
- **Size check:** `len(cohort) == --size` is asserted, replacing today's silent undershoot. Today's
  rule asked for 150 returns 149.
- **Why Acorn:** it is the only socio-economic label LCL has, and the generator spends it.
  - `gen_billing.py` draws vulnerable (18%) and medically dependent (7%) flags only for Adversity
    households, and gives them a mix of payment methods instead of direct debit.
  - Under-sampling Adversity under-samples features 4 and S3.
- **Effect at 200:** affluent 40%, adversity 32%, comfortable 26%, ToU 41 households, and ACORN-U
  present (3), which keeps the NULL-Acorn path tested.
- **Where it stops being exact:** ToU/Adversity has 17 eligible households for a 5.4% share, so it
  can fill a cohort of at most 317. Above that the skew returns.

**3. A systematic sample across usage within each group.**
- **How:** members are sorted by mean daily kWh and taken at even steps from a hash-seeded start.
  This replaces "the first `take` by hash".
- **Why:** usage level moves feature behaviour more than Acorn does. Across the eligible pool:

  | Usage tertile | Heating proxy ≥ 0.5 (→ forecast, explanation) | Own load clean enough to plant on, at the pool's scale ×1.89 (→ S1, S2) |
  |---|---:|---:|
  | Low (< 6.64 kWh/day, London) | 55.7% | 84.2% |
  | Mid | 61.1% | 45.3% |
  | High (> 11.60) | 78.3% | 7.4% |

- **What it guarantees:** every group spans light and heavy households.
  - Heavy users give bill shock, heating sensitivity and heater-shaped loads.
  - Light users give clean planting hosts and households near the Low Fixed Charge line.
  - The hash alone would leave all of that to chance.

**4. Solar fixed at 10% of N.**
- **How:** households are ranked by the existing `has_solar` hash, with its Affluent tilt (14 against
  6), and the first `round(0.10 × N)` get panels.
- **Why:** today the count is whatever the hash gives. Under today's rule it was 16% of the cohort at
  50 and 12% at 200 in the designs measured. `SOLAR_PLAN` needs three clean, higher-load solar hosts,
  and at 50 the strict screen found 2.
- **Measured at 200 (2 Oct):** the screen found exactly **3** of the 17 candidate solar hosts clean at
  the loosened 1.5% bound. They are light households (2.0, 2.0 and 1.5 kWp arrays before the car
  re-roofs them). So the loosened bound stays, and the hosts are lighter than an EV owner is likely to
  be. More solar households wouldn't fix that cheaply: own-load cleanliness and high load rarely go
  together (§4, step 3's table).

**5. At least three accounts per payment-trouble profile** (`08_payments.sql`).
- **How:** each of LATE_STREAK, DD_FAILURE, PART_PAYMENT and STOPPED keeps its hash share but is
  topped up to three. This extends the existing ensure-steps, which top up to one.
- **Why:** at 200 the hash alone gives STOPPED 3 and PART_PAYMENT 6, but at 150 it gives PART 3 and
  DD 2. A rule should not depend on that luck.

**6. Double the planted answers.**
- **What changes:** `planted.PLAN` goes from 8 to 16 households (8 EV, 8 hot-water). The planted
  hardship accounts go from SLIDING 3 and SUDDEN 1 to 6 and 2. `SOLAR_PLAN` stays at 3, one per
  pattern.
- **Why:**
  - Every Sprint 4 figure is re-measured after a re-pick anyway.
  - Recall over 4 EV households moves in steps of 25%; over 8, in steps of 12.5%.
  - "≥ 2 of 3 gradual accounts caught" is a demonstration, not a rate.
- **Thresholds:** they stay the same, written as rates ("≥ 2/3 of the gradual accounts").

**Unchanged:** the 99% filter, `SEED`, the final global hash sort (it sets each ICP's last two
digits), one network (Vector, Auckland), `user_category` STANDARD, and the coverage window.

## 5. What each feature needs, and what 200 gives

Simulated over the eligible pool (`results/designs.md` §3).

| Feature | Needs from the cohort | 50 today | 200, proposed rule |
|---|---|---|---|
| 1. Explain a bill | households whose usage tracks heating (the fit gates `is_explainable`); enough rises per driver | 39 heating-proxy households | **137** — about 3.5× the explainable rises, so the two-example drivers get ~7 each |
| 2. Plans | at least ~3 households for whom Anytime, and for whom Free Weekends, is cheapest — each is cheapest for only 2.0% and 2.8% of the pool | 3 and 2 | **4 and 6** |
| 3. Mid-cycle warning | forecastable households, and a population the champion refuses (kNN's fallback) | 29 forecastable | **~110** forecastable (heating proxy × 0.8), **~90** for the fallback |
| 4. Hardship | ≥ 3 per payment profile, ≥ 3 medically dependent | LATE 3, DD 3, PART 5, STOPPED 0; medical 1 | ≥ 3 each by rule (hash alone: 10, 8, 6, 7); vulnerable **15**, medical **6** |
| 5. Everyday questions | a spread of usage | largest 36.1 kWh/day (London) | largest 41.6; **62** non-solar households at or above the 8,000 kWh/yr Low Fixed Charge line |
| S1 Breakdown | clean hosts for 16 planted loads; natural night loads; a realistic false-positive population | proxy-clean 25 (screen: 10 EV, 7 hot water); night-heavy 4; heater-shaped runs 12 | **measured by the screen: 49 clean for an EV, 28 for an element**, for 8 + 8 places; night-heavy **12**; heater-shaped **82** |
| S2 Solar & EV | ≥ 3 clean solar hosts; the range of array sizes; a range of daytime use | 8 solar; 2 strict-clean, so the bound was loosened to 1.5%; arrays 1.5, 3, 4, 8 kWp | **20** solar; **measured: 3 clean at 1.5%**, exactly enough, so the bound stays loosened (§4, step 4); arrays 1.5–6 kWp, six of the seven sizes; daytime share 0.26–0.43 |
| S3 Early warning | trigger bills (plentiful: 36 of 45 today); on-time account-years for the false-alarm target (≤ 5 per 100) | 39 on time → about 72 account-years after the planted 4 | **169** on time → about **330** after the planted 8 |
| kNN (Botman) | a big enough pool; a spread of load shapes | pool 49 | pool **200** |

**S3, in numbers.** About 72 on-time account-years cannot tell a false-alarm rate under 5 per 100
from one over it unless there are none at all; the 95% upper bound after one false alarm is already
6.6. About 330 can, with up to 9 (upper bound 15.7, so 4.7 per 100).

**kNN, in numbers.** The paper used 363 LCL households
(`pipeline/scripts/backtest_bill_forecast.py`, Table 1b). Ours had 49, and the pool-size curve (Stage
3, `analysis/forecast_backtest/results/stage3_extras.md`) was still falling where it stopped: 10
households cost 1.18×, 25 cost 1.07×, 49 is 1.00 by construction. 200 extends it to 100 and 200, so
"is our gap to the paper the pool?" gets an answer.

## 6. How many

The proposed rule at each size (`results/designs.md` §3):

| N | Adversity | Heating proxy | Solar (strict-clean) | Proxy-clean hosts | Anytime / Free Weekends cheapest | Medical | On time | LCL blocks | Est. rebuild |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 50 (today) | 22% | 39 | 8 (5) | 25 | 3 / 2 | 1 | 39 | 33 | 9m17s measured |
| 100 | 31% | 70 | 10 (10) | 36 | 1 / 4 | 3 | 89 | 60 | 14–19 min |
| 150 | 32% | 101 | 15 (9) | 63 | 1 / 3 | 5 | 133 | 70 | 19–28 min |
| **200** | **32%** | **137** | **20 (8)** | **80** | **4 / 6** | **6** | **169** | **69** | **23–37 min** |
| 300 | 32% | 190 | 30 (15) | 122 | 9 / 5 | 9 | 259 | 86 | 33–56 min |

**Why 200:**
- **150 is the smallest size whose estimate stays under 30 minutes in the worst case,** but it
  leaves feature 2 thin (one Anytime-cheapest household) and the payment profiles down to two or
  three by hash.
- **200 is the first size where every row of §5 has at least three examples**, and the false-alarm
  target becomes measurable.
- **300 adds little a feature needs.** It sits just below the 317 at which quotas stop being exact,
  and adds about half again to the run time.

**The time is an estimate, and the document says so.**
- **The range:** no step of `rebuild-all` has ever been timed on its own, so the fixed share of the
  9m17s (stack start, ~330 dbt nodes on Snowflake, the dashboards) is unknown. The low end assumes
  half of it is fixed; the high end assumes every minute grows with the number of households.
- **Measured so far (2 Oct, the Sources step at 200):** `gen_metering.py` took 3.5 min. The source
  load and its gate took 6m40s on a throwaway Postgres.
  - **The fix it needed:** `05_defects.sql` now ANALYZEs its temp tables. Without that, the gap
    `DELETE` built its hash on the 8 M-row reads table and had not finished after 14 minutes.
  - **What that means:** the Sources step is about 10 minutes, as long as the whole rebuild at 50.
  - **The full rebuild, measured 2 Oct 2026 (release `demo-20261002T134314`):**

    | Step | Time |
    |---|---|
    | Stack | 7 s |
    | Sources | 9m27s |
    | Lake | 2m22s |
    | Warehouse: RAW + dbt + Soda | 3m43s |
    | Serving: export, publish, gates | 2m50s |
    | Dashboards | 1m47s |
    | **Total, net of retries** | **about 20 min** |

  - **Conditions:** Airflow, Spark history, Grafana, Prometheus and Metabase were stopped through the
    warehouse and serving steps. With everything running, the 8.3 GB Docker VM killed the RAW
    loader (exit 137).
  - **What 200 surfaced:** three checks that had only held at 50 failed and were fixed —
    - the solar base-load dbt test, now checking the computation, not the London data;
    - the Soda volume band, now per household present;
    - bill reproduction, now on the latest *settled* bill.

    Two pinned households were re-chosen the same day, and all 8 dashboards verify:
    - `DEMO_ICP` 1027 → 1086, the SOLAR_PLAN evening host;
    - the data explorer's default → the unplanted solar household that exports most.

**Not in the rebuild: the backtest.** Its run time grows with roughly N², because every query
searches every household's weeks. At 49 households Stage 1 ran in 85 s, Stage 2 in 25 s and Stage 3
in 3 min. At 200 that is about 24, 7 and 50 minutes.

## 7. What it costs

- **Every generated figure changes.** Only 17 of today's 50 households remain. The scale factor moves
  from 1.72 to about 1.95, because the cohort's London mean falls. Every ICP, bill, Sprint 4
  evaluation figure, backtest result and brief-2 figure is replaced; the old ones stay in git and in
  the 30 Sep warehouse archive.
- **One household is pinned by hand.** `DEMO_ICP = 1027`
  (`analysis/metabase/build_sprint4_dashboard.py`) is the household the dashboard and the
  customer-facing site both show (decision log, 24 Sep 2026). Surrogate ids are regenerated, so it
  must be re-chosen and the site's owner told.
- **More heater-shaped false detections.** A scale of 1.95 turns a 3 kW UK heater into 5.9 kW, and 82
  of the 200 show EV-shaped runs on more than 2% of their days. That is realistic for heat-pump-heavy
  NZ, and it is what EV precision is honestly measured against. But the high-confidence EV count on
  unplanted households will rise, and design §15.10's figures will read worse.
- **Disk.** The 200 live in 69 of the 112 LCL blocks (~70 MB each). `init-sources` unzips nothing
  while `raw/lcl/extracted/` exists, so the 40 missing ones were unzipped by hand on 2 Oct, about
  2.8 GB.
- **One attempt.**
  - **What stops after 3 Oct:** the Snowflake trial converts, and `build-warehouse`,
    `publish-serving`, `rebuild-all`, `evaluate-sprint4` and backtest Stage 2 stop working.
  - **What survives:** the 30 Sep warehouse archive (`data/raw/snowflake-archive/2026-09-30/`).
  - **What must happen first:** the new cohort's own archive has to be taken before the trial ends.

## 8. What this does not solve

- **Cold start, the paper's headline property.** Botman's method forecasts from seven days of
  history, and nothing in this cohort is seven days old: every account starts on the span's first
  day, so a "new customer" exists only at the very start.
  - **The follow-up:** late-joining accounts, drawn from LCL households recruited later in the
    window (eligible on their own shorter span).
  - **What it needs:** a generator change, with per-account start dates in `gen_billing.py` and
    `gen_metering.py`. It is not a selection rule.
- **Concept drift is covered already.** The planted EVs arrive mid-span, and doubling `PLAN` doubles
  them.
- **The proxies are proxies.**
  - **Heating:** the heating proxy is checked against the real fit (`results/serving_now.md`: at R²
    ≥ 0.5 it marks 35 households, all 28 forecastable ones among them).
  - **EV-shaped:** this proxy is only detect-v1's high-power rule. The screen keeps about 42% (EV) and
    29% (element) of what it calls clean (`results/designs.md` §5), and §5's host counts are scaled
    by those.
  - **Plans:** the cheapest-plan proxy ignores solar, term credits and line losses.
- **Acorn is a UK classifier.** It is used to keep the mix defensible, not as evidence about NZ
  households (as `gen_billing.py` already says).

## 9. Implementation, in order

Each step needs its own go, and everything from step 4 needs Snowflake (until 3 Oct 2026).

1. ✅ **`cohort.py`:** steps 2–4 of §4 (`allocate()`, `select()`, `solar_households()`), the exact
   size, and `tests/test_cohort_selection.py`. `--size` and `COHORT_SIZE` default to 200.
2. ✅ **`08_payments.sql`, `99_assert.sql`, `planted.py`, `evaluate_sprint4.py`:**
   - minimum three per profile;
   - SLIDING 6 and SUDDEN 2;
   - `PLAN` doubled;
   - the solar bound left at 1.5%, because the strict one would leave too few hosts (measured, §4);
   - their tests, and the Sprint 4 evaluation's wording of its thresholds as rates.
3. ✅ **Dry run:**
   - every generator ran, and every `PLAN` and `SOLAR_PLAN` place filled;
   - the source load and `99_assert.sql` passed on a throwaway Postgres 16;
   - `make ci ONLY="lint test"` passed.
4. ✅ **Unzip:** the 40 blocks `cohort.json` names that weren't on disk.
5. ✅ **Rebuild:** release `demo-20261002T134314`, about 20 minutes net (§6). Evaluation not yet re-run.
6. **Backtest Stages 1–3**, with `--pool-sizes 10,25,50,100,200`.
7. **`archive_warehouse.py`** into a new dated folder.
8. ✅ **Re-pin:** `DEMO_ICP` = 1086, and the data explorer's `DEFAULT_ICP` re-picked. The backend's demo
   logins use the nine fixtures, which didn't move. The site owner should still confirm that nothing
   on the site names the old household.

## 10. Reproducing the figures

```bash
cd data
python3 analysis/cohort_selection/measure_cohort_designs.py             # results/designs.md, ~70 s
python3 analysis/cohort_selection/measure_cohort_designs.py --serving   # + results/serving_now.md, needs kea-db
```

- **What it reads:** the LCL zip, cohort.py's coverage cache and `cohort.json`, plus `kea-db`
  read-only with `--serving`.
- **What it writes:** only `analysis/cohort_selection/results/`.
- **Figures from elsewhere:** the planting screen's counts (49, 28 and 3) are `gen_metering.py`'s
  own output on 2 Oct. "~110 forecastable" is the heating proxy × 0.8, its measured precision.
