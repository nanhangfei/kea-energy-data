# Sprint 4 evaluation: detected loads, the net heating fit, the bill driver and early warning

This measures whether the Sprint 4 pipeline finds what the source generator planted:
- 8 EV households and 8 hot-water timers, none of them with panels (`gen_source/planted.py`) — 4 and 4
  until the cohort grew to 200 (`design/kea-cohort-selection.md`);
- 3 households with **both an array and a car** (`planted.SOLAR_PLAN`), added 20 Sep 2026 with
  `detect-v2`. They are measured in their own split, §1a, and never enter the planted sixteen's precision
  and recall — so every Sprint 4 figure above stays a comparison with Sprint 4's;
- 8 sliding payers, 6 gradual and 2 sudden (`08_payments.sql`); 3 and 1 before the cohort grew.

It runs after `make rebuild-all`. The method and every threshold below were written down
**before the first run**; changing one makes it a new evaluation. The one threshold added since
is §1a's hard zero, written down here before detect-v2 was measured.

**What it proves.** Planted data proves the pipeline finds what the generator planted. It doesn't
prove real households behave that way: that needs real EV and arrears history.

```bash
cd data
make evaluate-sprint4            # unit tests in the image, then the evaluation
make rebuild-all EVALUATE=1     # the same, as the last step of a full demo rebuild
```

- **Code:** `pipeline/scripts/ops/evaluate_sprint4.py`. Tests: `pipeline/tests/test_evaluate_sprint4.py`.
- **Output:** `results/report.md` and `results/metrics.csv`, one row per figure, with its target and
  pass or miss.
- **Before figures:** `before.json`, recorded from serving and the warehouse on 18 Sep 2026, before
  the rebuild.

## What it reads and writes

| Source | How | What |
|---|---|---|
| Serving (kea-db) | `KEA_PUBLISH_DSN`, a READ ONLY transaction that is rolled back | the published marts, forecasts, estimates, bills and payments |
| The source database (kea-source-db) | the owner role, read-only session | `gen_audit.planted_load`, `gen_audit.planted_hardship`, `gen_audit.defects`, and which reads `06_revisions.sql` revised |
| Snowflake silver | the pipeline's Snowflake role, three SELECTs | `INT_METERING__DETECTED_HALF_HOURS` for the planted households; `INT_METERING__IMPORT_HALF_HOURS` for `is_masked` / `is_daylight_shoulder`; `INT_METERING__HEATING_SENSITIVITY` for the cohort |

- **Writes:** only `results/`.
- **Why the source database:** the planted answers live there and never reach serving. Nothing the
  pipeline reads comes from it.
- **Why silver:** the night-timer measure needs each half-hour's class, and no served table keeps
  it. Nor does any served table keep **which** half-hours the panels hid — the marts publish the
  count per run and per day, never the instants — and §1a is measured against those instants. The
  session starts served in `kea.mart_charging_session` must be `ev` half-hours in silver
  (at least 95% of them), or the run stops: the two would disagree about time.

**Privacy.**
- The report names households by their planted role, for example "wallbox, evening, 1", and
  sliding payers as "sliding 1".
- It never prints an ICP, an account number or a customer.
- Dates and kWh aren't identifiers and do appear.

## Days left out

A planted session or day is left out of sections 1–3 when the source generator altered one of its
dates **after** planting:
- any `src:%` row in `gen_audit.defects` for that ICP and NZ date (the row's `nz_date`, or the NZ
  date of its `interval_start`);
- any read that `06_revisions.sql` revised (`version_no > 1`), because a revision rescales the
  whole day.

A defect row with no date (the §7 month, §8, §9, the orphans) must not touch a planted household.
`99_assert.sql` checks that, and the run stops if one does.

A session counts only if **every** NZ date it touches was assessed (it has a row in
`kea.mart_daily_detected_load`) and unaltered. The report gives the number left out.

## The measures

**1. Charging sessions.**
- **The match:** a detected session matches a planted one when they overlap by at least 50% of
  **both** durations.
- **Precision:** of all `high_power` sessions detected in the 8 planted households, the share that
  match a wallbox session. A `high_power` session on a hot-water or portable household is a false
  positive.
- **Recall:** the share of the 3 wallbox households' planted sessions matched by a `high_power`
  session.
- **The portable charger:** its sessions found as `low_power`, and any found as `high_power` (a
  class error), are reported with no target.

**1a. Where the panels were in the way.** The 3 households with both an array and a car, scored
apart from the 8 above so that no Sprint 4 figure moves.
- **Hidden:** a planted session with any half-hour that was exporting, or that sat between that
  day's first and last export (`is_masked` / `is_daylight_shoulder`, measured off the export
  register in silver — no PV curve is modelled). The rest are **clear**.
- **Found:** a published session overlaps it, **whatever class it carries**. `detect-v2` withholds
  `charger_class` on a hidden run rather than guessing it, and a withheld class is not a missing
  EV; scoring only `high_power` here would read the refusal as a regression.
- **Clear recall:** reported with no target, and read against §1's headline recall. A solar
  household's night is as readable as anyone else's, so a gap between the two means
  `is_daylight_shoulder` is reaching into the dark.
- **Hidden recall:** reported with **no target, permanently**. When the array covers the whole
  draw the import register reads 0 for the length of the charge, so there is no excess above the
  day's floor, no run to find and no class to withhold — the session is simply absent. That is the
  meter's limit, not the ruleset's.
- **`low_power` on a hidden run: target 0.** A wallbox shaved by PV and a portable charger are the
  same signal, so this is the one false claim detect-v2 exists to remove. The dbt singular test
  `assert_masked_runs_have_no_charger_class.sql` enforces it over the whole cohort; this scores it
  on the planted three.
- **Own-load background:** each host's published sessions on assessed days **before its car
  arrived**, per household-year. `SOLAR_PLAN`'s hosts are screened at
  `MAX_SOLAR_EV_SHAPED_SHARE` (1.5% of assessed days) rather than `PLAN`'s 0.5% — only 8 cohort
  households have panels, and the strict bound leaves 2 of them for 3 places — so they carry a
  little more label noise than the 8, and it is reported rather than assumed away.

**2. Daily bounds.** For each planted date, the planted kWh on that date, split by half-hour, is
compared with the day's bounds:
- the EV bounds for an EV household;
- the timed-night bounds for a hot-water household.

It is inside when `low − 0.01 ≤ planted ≤ high + 0.01`. The marts round to 4 decimals, and so does
the planted file.

Inside/outside is a hard edge on a band that is often under 1 kWh wide, so the report also gives,
with no target, how far outside the days that miss fall — in kWh and against the day's own planted
kWh — beside the median band width and the published half-hour count minus the planted one. A band
that is narrow and a little high reads the same as one that missed the load entirely, and these
three figures tell them apart.

**3. Night timer.** A planted hot-water night is detected when at least half its half-hours are
classed `timed_night` in silver. Nights read as `ev` instead are counted.

**4. Households with nothing planted.** LCL households have loads of their own, so their detections
are **reported, not scored**. Solar and non-solar households are shown separately, and **both
groups exclude every planted household** — including the 3 with panels, which would otherwise put
sessions with a known answer into the group that exists to report what is found where there is
none.

**5. Heating fit.** For each planted household, the model's rule is refitted three ways:
- **Gross:** on metered import.
- **Net:** less the published low bounds, which is what dbt fits.
- **Oracle:** less the exact planted kWh, the fit the household would have had with nothing
  planted.

The net fit works if it sits near the oracle.
- **The rule:** months with at least 20 complete days, at least 6 months, OLS; a fit is usable at
  R² ≥ 0.30.
- **Replication check:** the net refit must match the published silver fit for every cohort ICP,
  within 0.0005.
- **Before and after:** ICPs with a fit, those with R² ≥ 0.30, and the median R².

**6. Bill driver.** For each EV household, take the explained bill whose period contains its first
planted session, and its prior bill.
- **Detected step:** `(detected_load_kwh_day_this − _prior) × period_days`.
- **Planted step:** the same arithmetic on the planted kWh, over the same assessed days.

**7. Forecast `hdd-linear-v3`.** Reported, with no target.
- **Error:** the absolute percentage error of `projected_kwh` against the cycle's import. The
  cycle's import is summed from `kea.mart_daily_usage` where every day of the cycle is complete.
- **Groups:**
  - EV households, cycles starting after the EV's first session;
  - hot-water households;
  - everyone else.
- **Buckets:** days 1–14 and day 15 on.
- **Model check:** `kea.bill_forecast` must hold only v3 rows.

**8. Hardship early warning: a weekly replay.**
- **Dates:** every 7 days, from 91 days after the cohort's first bill (three bills paid) to the
  latest meter or payment date.
- **What runs:** at each date, the job's own `build_assessments()` with the job's own loaders.
  Nothing is inserted.
- **One change:** what had been paid on a bill is summed from the successful payments made by that
  date. `billing.bill.payments_applied` is the biller's figure at extraction, and it would show
  payments at a past date that hadn't happened yet. The report counts the bills where the two
  differ.
- **An early outreach:** an assessment with the alert job's early combination
  (`create_hardship_alerts.Candidate.is_early`): `bill_above_usual_payment` with `payment_drift`
  or `first_shortfall`, and no v1 code.
- **Lead:** the days from the first early outreach to the planted arrears onset. The first v1
  signal and the first outlook are shown beside it.
- **False alarms:** measured on on-time accounts. Those are open cohort accounts, not planted, with
  no failed payment, and with every bill due by the generator's payment horizon (the last period
  end − 5 days) paid in full on or before its due date. An alarm is a run of consecutive weeks with
  the early combination, and the rate is per 100 account-years of replay.
- **Outlook check:** an outlook that projects arrears on or before its own date is a fault
  (target 0).
- **`SLIDING_SUDDEN`:** it stops paying with no warning sign, so a miss there is expected and
  reported.

**9. Solar check.** There is no known answer, so this is a sanity check with no target.
- **The load:** the generator's panels have a known output, so a solar household's load is
  `import − export + output` for each half-hour.
- **Each household's own array.** Since 20 Sep 2026 there is no single size: `cohort.py` sizes each
  roof to that household's own annual load, and `planted.SOLAR_PLAN` sizes the 3 solar+EV hosts to
  the car. The size is read from `cohort.json` per household — checking a 1.5 kWp household against
  4 kWp of modelled generation would report the difference as the model's error.
- **The comparison:** the model runs on that load at that household's size, and its exported share
  of generation is shown beside the metered one.
- **Months left out:** the month that `05_defects.sql` §7 folds into import is left out for the
  households it touches. The month is read from the defect's `detail`.

**9a. `solar_soak`.** The charging pattern that puts a session into the half-hours the meter
actually exported. Reported per scenario on each account's **current** plan, with **no target**:
there is ground truth for the kWh — the generator planted it — but none at all for the money, and
a figure with a line under it here would be a target invented to be met.
- **Reported:** accounts offered it, median annual EV kWh, median `solar_soak` saving beside the
  median `night` saving, how many accounts it wins on, and how many it **loses** on.
- **A losing row is published, not suppressed.** On a plan that buys export back above its own
  night rate, moving a charge onto the roof can cost more than it saves, and saying so is the
  point.

**10. Hand checks.**
- One wallbox day inside its bounds.
- One bill pair worked through `delta_from_detected_loads`, at the prior bill's variable rate:
  `(prior amount − prior per-day charges) ÷ prior kWh`.
- One solar payback worked from its published inputs.

## Thresholds

| Feature | Measure | Target |
|---|---|---|
| EV `high_power` sessions | precision; recall | ≥ 90%; ≥ 85% |
| Daily bounds | planted days inside [low, high] | ≥ 90% |
| Night timer | planted nights detected | ≥ 80% |
| Bill driver | detected step ÷ planted step, for each of the 3 wallbox households | ≥ 70% |
| Hardship | median days of warning, among the 3 gradual accounts caught | ≥ 21 |
| Hardship | gradual accounts caught before onset | ≥ 2/3 of them (2 of 3; 4 of 6 at 200 households) |
| False alarms | early outreaches per 100 on-time account-years | ≤ 5 |
| §1a masking | `low_power` claimed on a run the panels hid | 0 |
| Checks | net fit replicates silver; only v3 forecasts; no outlook on or before its date | exact |

**What a miss means.**
- **A detection miss:** the tables still publish, and the report says which figure missed.
- **A hardship miss:** the early-outreach rule goes back to v1.
  - **Decided 18 Sep 2026, after the run: the rule stays at v2.** The median lead missed
    by 3 days (18 against 21) while all 3 gradual accounts were caught — 51, 18 and 17
    days before onset, each ahead of its own first v1 signal — with 0 false alarms in
    55.3 on-time account-years. The threshold is not retired: three accounts cannot
    establish a median, and the outreach stays staff-only until real arrears history can
    score it. See `docs/decision-log.md`.
