# Low Carbon London — the source behind `src_metering`

Every reading in the generated cohort is real metering from **Low Carbon London (LCL)**: about 5,566
London households, half-hourly, 23 Nov 2011 to 28 Feb 2014. This document records what the data is,
the three conversions that turn it into New Zealand data, and the decisions and surprises that cannot
be read off the files.

**Source of truth for every pinned value:** `uk_to_nz.py`, `cohort.json` and `scale.json` in
[`pipeline/scripts/gen_source/`](../pipeline/scripts/gen_source/README.md). If a figure here disagrees
with them, this file is stale. Figures are for the current 200-household cohort unless marked.

## 1. Why real metering and not a simulation

Almost every feature is a claim about load **structure**: intraday shape, weekday versus weekend,
variation between households, the right-hand tail. A simulated cohort contains only the structure its
author put in, so each feature would recover the parameter its input was generated from, and the result
would read as validation.

The nine synthetic fixtures show the cost. They use one hourly shape for every day, so their
weekday/weekend difference is at most 0.0001 kWh, and their heating sensitivity is a typed-in constant
(0.93 kWh/HDD). LCL has real day-of-week variation, an inherited right-skewed tail, and ~1.3% of
household-days already imperfect before any defect is injected.

**The trade, stated plainly:** real London load means the *daily* weather response belongs to London,
not Auckland (§8). That is repairable by better analysis; an assumed load shape is not.

**Rule:** real data for shape; synthetic only for what LCL cannot contain (solar, EVs, payment
difficulty, integrity defects), always recorded in `gen_audit` and never silently blended in.

## 2. The three conversions

| | Conversion | Where | Section |
|---|---|---|---|
| 1 | **when in the year**: UK winter must land in NZ winter | `uk_to_nz.py` | §8 |
| 2 | **which instant**: NZ daylight saving differs, so a local day is 46, 48 or 50 half-hours | `uk_to_nz.py` | §9, §10 |
| 3 | **how much**: NZ households use roughly twice what London ones did | `gen_metering.py` | §14 |

One thing must **not** change: the wall clock. A reading at 18:30 London time becomes 18:30 Auckland
time, because dinner is at dinner time in both hemispheres. That forces the shift to be a whole number
of **days**; shifting by hours would smear the evening peak.

## 3. The whole path

[`figures/lcl-transform-path.html`](figures/lcl-transform-path.html) (open in a browser) shows the
stage chain, from the checksummed Kaggle archive through `cohort.json` and `scale.json` to the four
schemas, and follows one reading through every step.

The order of the last steps is deliberate:
1. **Scale before planting.** Planted appliances are specified in NZ kilowatts, so they join a load
   already at NZ level.
2. **Plant before the solar split.** An appliance joins the household's load, so a solar household's
   EV charges under its own panels and only the uncovered part reaches the import register.
3. **Clamp inside the split.** Import and export are both `max(0, …)`: solar is a second register,
   never a negative number on the first.

Three things never happen: a missing reading is never interpolated, the clock is never moved by hours,
and a household's own load never decides membership (apart from the planting screen, §15, which can
only exclude).

## 4. Provenance

| | |
|---|---|
| Source | https://www.kaggle.com/datasets/jeanmidev/smart-meters-in-london |
| Retrieved | 2026-09-20 |
| Archive | `smart-meters-in-london.zip`, 1.2 GB |
| SHA-256 | `8839527a354c28013a025508ffad4ad26822f2aa255447dff1a3a4b3575f56f5` |

The archive lives in `raw/lcl/` (gitignored) and is fetched by `make init-sources`
([`fetch_lcl.sh`](../pipeline/scripts/gen_source/fetch_lcl.sh)), which writes a local manifest.
This table is the tracked record of what was fetched. Raw data is never edited by hand; fixes are
scripts.

## 5. `tstp` is UTC, not London local time

Settled from the data (`probe_tstp.py`): every UK daylight-saving day holds 48 readings per household,
not 46 or 50, so `tstp` is an absolute instant. The transform therefore converts UTC → `Europe/London`
first to recover the wall clock, and the UK's short and long days appear at that point.

## 6. LCL is clean, but not perfectly clean

In LCL's `block_0` (25,574 household-days), 98.68% of days are complete; the rest have 47, 49, 46, 23 or
a single reading. The synthetic defects in `05_defects.sql` sit on top of this small amount of real mess,
and cohort selection filters on coverage because of it (§13).

## 7. The Acorn mapping is an assumption

`Acorn_grouped` is a UK geodemographic classifier with no NZ equivalent. It varies customer attributes
(vulnerability, payment method, solar likelihood) so the population is not uniform, and it is half of
the cohort's stratification key (§13). It is **not** evidence about NZ households.

## 8. Seasons are flipped on purpose

Readings are shifted a whole number of days so UK winter lands in NZ winter. The shift is **derived and
asserted**, not hard-coded: `derive_shift_days()` takes it from the span ends and refuses any value
that fails one of three properties.

| Property | Why | Value |
|---|---|---|
| a whole number of **weeks** | weekday/weekend shape and day-of-week tariff windows survive | 4,928 days = 704 weeks |
| about **half a year** past a whole year | the season flip itself | fraction 0.4924 (must be 0.48–0.52) |
| lands on the pinned end date | the span is a decision, not a side effect | 2027-08-27 |

```
LCL_START  2012-02-03 (Fri)  + 4,928 days  ->  2025-08-01 (Fri)
LCL_END    2014-02-28 (Fri)  + 4,928 days  ->  2027-08-27 (Fri)
```

**`NZ_SPAN_END` is pinned, never `date.today()`.** A moving anchor would shift every timestamp daily
and nothing would reproduce. The season-flip assertion makes valid end dates sparse; the next is
**2028-08-25** (shift 5,292). Moving it is a full rebuild (stack doc §7).

### The seasons align; the days do not

Cohort consumption rises from 19.6 kWh/day in April to 23.9 in May and stays high through winter, so
the seasonal shift works. But a **daily** regression of usage on Auckland heating degree days is noise
(median R² 0.07, slopes −3.98 to 8.44): each household's day-to-day variation responds to *London's*
weather that day. `gen_metering.py` takes no temperature input at all.

**The fix was the estimator, not the data.** Bills spend the heating slope per billing period, so it is
fitted monthly:

| Fit | Households | Median slope | Median R² |
|---|---:|---:|---:|
| daily | 499 | 0.45 | 0.069 |
| **monthly** | 446 | **0.74** | **0.467** |

(Measured on the archived 500-household run; the rule, not the coverage numbers, carries over.) Fit and
spend the slope at monthly grain; a daily fit on this cohort always measures London against Auckland.

This does **not** explain the forecast band's poor calibration (design §15.12): that is band
construction, not weather.

## 9. Daylight-saving days are not energy-normalised

An Auckland day is 46, 48 or 50 half-hours. On a 50-half-hour day one UK reading fills two NZ slots,
so the day's total is ~4% above its neighbours; on a 46 day it is ~4% below. **This is correct**: a
25-hour day uses more electricity, and rescaling would break `interval_count = expected_intervals`.

The NZ grid is iterated in **absolute UTC time** from local midnight, so a day's length falls out of
the timezone rather than being assumed. Two silent traps avoided:
- Aware-datetime arithmetic in one timezone is wall-clock arithmetic, so consecutive local midnights
  always differ by exactly one day, giving 48 half-hours every day.
- Localising 48 wall clocks independently gives a hole and a collision, not a clean 46 and 50.

Genuine source gaps stay gaps: the generator skips the row.

## 10. A UK daylight-saving day collides with an NZ one

Because the shift is whole days, a UK spring-forward date can land on an NZ date that still asks for
01:00 and 01:30, wall clocks London did not have:

| UK date | NZ date | Effect |
|---|---|---|
| 2012-03-25 (spring forward) | 2025-09-21 | two slots estimated |
| 2013-03-31 (spring forward) | 2026-09-27 | two slots estimated |
| 2012-10-28 (fall back) | 2026-04-26 | two readings discarded |
| 2013-10-27 (fall back) | 2027-04-25 | two readings discarded |

Missing slots are filled from the previous half-hour and marked `read_status = 'E'`, the only estimated
reads not injected on purpose. Left alone, every household would be two readings short on the same two
days, which looks like ordinary missing data one household at a time. On fall-back days the **first**
of two readings is kept; writing into a dict would silently keep the last.

## 11. UK public holidays are not carried across

UK Christmas 2012 lands on an ordinary NZ Tuesday in June, and its load anomaly ends up in
`delta_unexplained`. Small, known, not corrected.

## 12. The usable window starts in February 2012

LCL recruited in waves, so the full 829-day span is not a usable panel: only 77 households cover 99%
of it. The start is pinned at **2012-02-03**, where 609 households hold ≥ 99% of the remaining 757
days (`cohort.py --report-windows` reproduces the trade-off).

The start was chosen **from the NZ side**: 2012-02-03 lands on **2025-08-01**, a month boundary. Bill
periods are monthly, and the year-on-year comparison sums a year-ago window with no guard for partial
coverage. A half-covered window comes back as **half**, which reads as a doubling: a fabricated bill
shock on every household at once. The window also contains all four NZ daylight-saving transitions
(two 46-hour and two 50-hour days), so both paths are tested twice; across the span that is exactly
757 × 48 = 36,336 slots per household.

## 13. How the cohort is chosen

**200 households**, chosen by the rule in [`design/kea-cohort-selection.md`](../design/kea-cohort-selection.md):
≥ 99% coverage, population quotas on tariff × Acorn, a systematic sample across usage within each
group, and exactly 10% with solar. `cohort.py` makes the pick and `cohort.json` records it.

- **Deterministic, not random.** Ordering uses a seeded hash (`SEED = "kea-stage2-v1"`), so the pick
  reproduces without storing a list and changes only when the eligible pool changes.
- **Exact size, asserted.** The cohort is exactly `--size`, or the run fails.
- **Order is load-bearing.** A household's position sets the last two digits of its generated ICP, so
  reordering renames households even when membership is identical.
- **Current mix:** Affluent 79, Adversity 65, Comfortable 53, unclassified 3; all on the Vector network
  in the Auckland climate region.

## 14. Consumption is scaled by a single global factor

`gen_metering.py --stats` measures the cohort's own London mean and writes `scale.json`; generation
refuses to run without it.

```
lcl_mean_kwh_day   10.2831    over 150,797 household-days, 200 households
nz_target_kwh_day  20.0       7,300 kWh/yr, close to the NZ residential average
scale              1.944941   applied as raw × scale
```

**One factor, not one per household.** Per-household scaling would normalise away the variation
between households, the main thing real data supplies.

**Why the level matters.** A bill is affine in kWh (`fixed + kWh × rate`), not proportional to it. On
Anytime ($1.45/day, 32.5c/kWh) over 30 days:

| | kWh/month | Bill | Fixed share |
|---|---:|---:|---:|
| unscaled London level (~10.3 kWh/day) | 309 | $143.90 | **30%** |
| scaled to 20 kWh/day | 600 | $238.50 | **18%** |

Unscaled, the bill explainer would overweight the standing charge, solar export shares would be
inflated (arrays are sized independently), every dollar threshold would be miscalibrated, and plan
ranking would tilt toward the lowest daily charge.

**Known tension:** every household is priced as a Standard User, but 7,300 kWh/yr is below the
8,000 kWh/yr low-user threshold most NZ networks use. Low-user pricing is not modelled, so this is a
scope decision today and becomes a defect the moment it is.

**The tail is inherited** (archived 500-household run): median 14.6 kWh/day against a mean of 20; 95th
percentile 52.8; 99th 94.2. UK winters are colder than NZ ones, so the seasonal swing (+61% winter over
summer) is larger than NZ-realistic and fitted heating slopes run high.

## 15. Planted loads LCL never had

LCL (2011–14) has no EV charging, no generation and no household sliding into arrears. The generator
adds known answers on top of the scaled load, in two pools that never overlap:

| Pool | Households | What is added |
|---|---:|---|
| `PLAN` (no panels) | 16 | 6 × 7.2 kW wallboxes and 2 × 2.3 kW portable chargers arriving mid-span; 8 × 3 kW water-heater elements on a 23:00 timer |
| `SOLAR_PLAN` (panels) | 3 | a 7.2 kW wallbox each, on declared 8 / 5 / 3 kWp roofs, charging at midday, evening and 23:00 |

Separately, 8 billing accounts get a deteriorating payment history (6 gradual, 2 sudden). Every planted
session and account is recorded in `gen_audit` and never reaches serving.

**Hosts are screened, not sampled.** Scaled ×1.94, a UK electric heater draws like a wallbox, so many
households already show EV-shaped runs in their own load. `planted.screen()` replays the detector's own
thresholds (read from `dbt_project.yml`, with a test that fails if they drift) against each
candidate's untouched load:

| Place | Requirement on the host's own load |
|---|---|
| EV, no panels | EV-shaped runs on ≤ 0.5% of assessed days |
| Water heater | the same, with the element added, **and** read as a timer on ≥ 98% of nights |
| EV with panels | loosened to ≤ 1.5%, because too few solar households pass the strict bound |

The loosening is a stated trade: the evaluation reports each solar host's own-load background beside
its recall. Solar hosts are screened on **net import** (what the detector sees), at the largest declared
array. Water-heater places are filled first, because their screen is strictly stronger. If any place
runs out of clean candidates, the generator **fails** rather than planting fewer. Households already
broken by injected source defects are excluded, and `99_assert.sql` checks it.

The three solar hosts are the **highest-load** solar households, and their roof size is declared for
the car rather than taken from `cohort.json`: a household that buys an EV and charges from its own roof
sizes the roof for the car.

## 16. Solar arrays are added; LCL has none

Exactly **10%** of the cohort (20 households) has panels, chosen by a seeded hash tilted toward Affluent
households, never by load. Arrays are sized from each household's **NZ-scaled** annual load, bucketed
to real product sizes (1.5–8 kWp; 1.5–6 kWp in the current cohort), at 1,300 kWh/kWp with a seasonal
amplitude of 0.45 peaking near the summer solstice.

Solar is subtracted from load, and the remainder splits across two registers
(`gen_metering.py`):

```python
load = raw * scale + planted_load
imp, exp = max(0.0, load - pv), max(0.0, pv - load)   # UN and EX registers
```

| | Synthetic part | Sign | Registers |
|---|---|---|---|
| EV / water heater | a session schedule | **+** load | `UN` |
| Solar | a generation curve | **−** load | `UN` and `EX` |

Consequences: a solar household's import is no longer its consumption; solar households carry twice
the rows; and export is modelled by direction, never by sign (design §8.2.3).

**Coupling to know about:** solar membership is drawn from the whole cohort and planting from whoever
is left, so changing the solar rule or seed re-deals every planted load.

One injected defect breaks the split on purpose: `05_defects.sql` adds two households' export onto
their import register for November 2025. It passes every constraint; only a distribution check notices
midday import **rising** on sunny days.

## 17. Plan assignment is deliberately not the best plan

Each generated account is assigned one of the three plans (Anytime Rate, Time of Use, Free Weekends) by
a stable hash of household identity, never by which plan is cheapest for its load. A cohort placed
optimally at generation would give the recommender no wrong-plan customers to find. Plans and their
provenance: [`ecotricity-tariff-source.md`](ecotricity-tariff-source.md).

## 18. What the catalogue rates rest on

See [`ecotricity-tariff-source.md`](ecotricity-tariff-source.md) §1–§2: the three plans' prices are
design inputs (`[DESIGN]`), the line-loss factor comes from a real bill (`[BILL]`), stored ex-GST
rates are back-solved (`[DERIVED]`), and the export rate is assumed (`[ASSUMED]`). Dollar figures are
illustrative.

## 19. The levy line on Bill B cannot be reproduced exactly

The printed bill's levy rate (0.0013/kWh) and amount ($0.22) cannot both hold for 180.43 kWh: the
amount needs a rate in `[0.0011916, 0.0012470)`, the rate in `[0.00125, 0.00135)`. The catalogue
stores the printed **rate**; the back-solved 0.0012193 is recorded here and deliberately not used,
because tuning an input until the output matches would prove nothing. Every other line reproduces
exactly (design §10.3).
