# Low Carbon London — what the source data actually is

Notes on the dataset behind `src_metering`, recorded because several of them
are decisions or surprises rather than things you can read off the file.

Source: `raw/lcl/` — see `PROVENANCE.md` there for the archive and checksum.

## `tstp` is UTC, not London local

Sources describe it both ways, so it was settled from the data by
`pipeline/scripts/gen_source/probe_tstp.py`:

| Raw date | Readings per household-day |
|---|---|
| 2012-03-25 (UK spring forward) | 48 |
| 2012-10-28 (UK fall back) | 48 |
| 2013-03-31 (UK spring forward) | 48 |
| 2013-10-27 (UK fall back) | 48 |

If the timestamps carried UK daylight saving, those days would hold 46 and 50.
They hold 48, so `tstp` is an absolute instant.

**Consequence for the transform.** `uk_to_nz.py` needs a London *wall clock* to
preserve, so the loader converts UTC → `Europe/London` first. The UK 46/50-hour
days appear at that point, not in the raw file, and still need handling: the
short day is missing two wall clocks, and the long day repeats two.

## LCL is clean, but not perfectly clean

The design doc calls it "a curated academic release — already cleaned...
very few organic defects". Broadly right, and worth a number. In `block_0`
(50 households, 25,574 household-days):

| Readings in the day | Days | Share |
|---|---|---|
| 48 (complete) | 25,236 | 98.68% |
| 47 | 144 | 0.56% |
| 1 | 78 | 0.31% |
| 49 | 50 | 0.20% |
| 46 | 10 | 0.04% |
| 23 | 7 | 0.03% |

So ~1.3% of household-days are already imperfect, and a few are *long* (49),
which a purely UTC series should not produce. The synthetic defect profile in
`05_defects.sql` therefore sits on top of a small amount of real mess rather
than on a spotless base. Cohort selection filters on coverage for this reason.

## Seasons are flipped on purpose

UK data, NZ product. Readings are shifted a whole number of days so that UK
winter lands in NZ winter — otherwise the bill explainer's weather driver reads
backwards and a cold snap appears in January. The shift is derived and asserted
in `uk_to_nz.py`; for the current span it is 4564 days.

Local clock time is preserved: an 18:30 London reading becomes an 18:30
Auckland reading. Evening peaks stay in the evening, which is the reason for
using real data at all.

### ⚠️ The seasons align. The DAYS do not — and that limits the weather driver

Measured 3 Sep 2026, once 120 days of metering were in the warehouse. The
seasonal shift works exactly as designed: cohort consumption rises from
**19.61 kWh/day in April to 23.85 in May** and stays there through winter. UK
winter really is landing in NZ winter.

But a **daily** regression of household kWh on NZ heating degree days is noise:
median R² **0.01** across 499 ICPs, slopes running **−3.98 to 8.44** where
design §8.4 expects ~0.9 for a typical NZ home. A negative slope asserts a house
uses more heating when it is warmer. Restricting to complete days only (53,723
of 61,300) barely moves it.

**The reason is structural, and no amount of data fixes it.** The shift aligns
seasons, not weather. A household's day-to-day variation is a response to
*London's* weather on that day; the HDD it is being regressed against is
*Auckland's*. The seasonal envelope matches; the daily wiggle inside it cannot.

### And the fix was the estimator, not the data

§8.5 spends the slope across **billing periods**, not days — so fitting it daily
measured precisely the component that is noise here. Refitted at monthly grain
once 12 months of metering were loaded, on the same data and the same
households:

| | ICPs | median slope | median R² |
|---|---:|---:|---:|
| daily | 499 | 0.45 | **0.069** |
| **monthly** | 446 | **0.74** | **0.467** |

`0.74` against design §8.4's ~0.9 for a typical NZ home on electric heating, and
**290 of 446 households clear the 0.30 fit floor**. `mart_bill_explanation` now
explains **2,426 of 11,528 bills**, with weather the dominant driver at **68.9%
of all movement**.

**So the cohort does not need regenerating**, and the sharper version of this
section's warning is: the shift aligns the seasonal component and only the
seasonal component. Fit — and spend — the slope at monthly grain. A daily
regression on this cohort is measuring London weather against Auckland weather
and will always return noise.

The fixtures' `0.93 kWh/HDD` remains a hand-set constant at
`generate_scenarios.py:51` applied to the nine, never a fitted property of the
500 — the 0.74 above is fitted, and the two agreeing to within 20% is a
coincidence worth not leaning on.

## Daylight-saving days are not energy-normalised

An Auckland local day is 46, 48 or 50 half-hours. On the 50-half-hour day one
UK reading fills two NZ slots, so the day's total sits about 4% above its
neighbours; on the 46 day it sits about 4% below.

**This is deliberate and must not be flagged as an anomaly.** A household
living through a 25-hour day genuinely uses more electricity. Rescaling to
conserve the daily total would be the wrong correction, and it would break the
`interval_count = expected_intervals` check.

## UK public holidays are not carried across

UK Christmas 2012 maps to an ordinary NZ Tuesday in June. The load anomaly
travels with the reading and lands in `delta_unexplained`. Known artefact,
small, not corrected. NZ holidays would have to be generated fresh.

## The Acorn mapping is an assumption

`Acorn_grouped` is a UK geodemographic classifier with no NZ equivalent. It is
used to vary customer attributes across the cohort — vulnerability flags,
payment method, low-user plan take-up, solar likelihood — so the population is
not uniform. It is **not** evidence about New Zealand households, and nothing
downstream should treat it as such. Flagged in the spirit of design doc §2.2.

## The usable window starts in March 2012, not November 2011

LCL recruited in waves, so the published span is not a usable panel. Coverage
of the full 829 days:

| Days held | Households |
|---|---|
| ≥ 828 | 21 |
| ≥ 800 | 317 |
| ≥ 700 | 1,120 |
| ≥ 600 | 4,176 |

Sampling at random would have filled the cohort with short histories and left
the year-on-year comparisons nothing to compare. The window therefore starts
**2012-03-21**, where 1,083 households hold ≥ 99% of the 710 remaining days.

Moving the start does **not** change the shift: it is derived from the window's
*end*, which has not moved. What it changes is how many NZ daylight-saving
transitions the span contains. 2012-03-21 keeps all four (two 46-hour days and
two 50-hour days) and two full NZ winters. One month later would have bought
1,633 households but dropped a 46-hour day, leaving the short-day path with
half its test coverage.

Reproduce the table with `cohort.py --report-windows`.

## Consumption is scaled by a single global factor

The cohort's measured LCL mean is **10.3009 kWh/day** over 353,754
household-days. NZ households use roughly twice that, so every reading is
multiplied by

    k = 20.0 / 10.3009 = 1.941577

Measured, not chosen: `gen_metering.py --stats` writes it to `scale.json`, and
the generator refuses to run without it. The figure shifted very slightly once
the UK daylight-saving fill and the first-wins rule below were added, because
both change which readings are counted.

**One global multiplier, not one per household.** Scaling per household would
normalise away the between-household variation, which is the main thing a real
dataset supplies and the whole reason for not generating the load synthetically.

The tail is inherited, not manufactured. Measured on the loaded cohort:

| | kWh/day |
|---|---|
| median | 14.6 |
| 95th percentile | 52.8 |
| 99th percentile | 94.2 |

Mean 20 against a median of 14.6 — right-skewed, as real consumption is. Exactly
one household of 500 averages over 100 kWh/day (140.0); that is a single large
all-electric LCL property, not an artefact. The largest single *day* in the data
is 934.8 kWh, and that one **is** an artefact: an injected spike from
`05_defects.sql`.

Consequence to state plainly: UK winters are colder than NZ ones, so the
seasonal amplitude here is larger than NZ-realistic — the loaded cohort shows
NZ winter at 24.05 kWh/day against summer at 14.93, a +61% swing. Heating
sensitivities fitted from this data will exceed the fixtures' 0.93 kWh/HDD.
Derive sensitivity *from* the shifted data; do not carry the fixture value over.

## Eight households carry loads LCL never had (Sprint 4)

LCL (2011–14) has no EV charging and no household that slides into arrears.
One regeneration therefore **adds** synthetic loads on top of the scaled LCL
load of eight households without solar: three 7.2 kW wallboxes and one 2.3 kW
portable charger arriving mid-span, and four 3 kW hot-water elements on a
23:00 timer every night. The scale factor above is measured before anything
is added. Their import is LCL × k **plus** the planted load, so those eight
households' daily totals and winter/summer swing are not pure LCL any more;
`gen_audit.planted_load` says exactly what was added and when. The eight are
not a random sample: scaled × k, a UK heater draws like a charger, so only
households whose own load the load detector would almost never read as an EV
(or, for the elements, would not confuse with the timer) are eligible. Three
billing accounts are also given a sliding payment history
(`gen_audit.planted_hardship`).
Details: `pipeline/scripts/gen_source/README.md`, "Planted answers".

## A UK daylight-saving day collides with an NZ one

The shift is a whole number of days, so a UK spring-forward date maps onto an
NZ date whose half-hour grid still asks for 01:00 and 01:30 — wall clocks
London does not have that day.

| UK date | NZ date |
|---|---|
| 2012-03-25 | 2024-09-22 |
| 2013-03-31 | 2025-09-28 |

Left alone this makes **every household in the cohort two readings short on the
same two days**, which looks exactly like ordinary missing data when viewed one
household at a time. Those slots are filled from the preceding half-hour and
marked `read_status='E'`. They are genuinely estimated, and they are the only
source of estimated reads that is not injected.

The UK *fall-back* day is the mirror image: two readings share one wall clock.
The **first** is kept. Keeping the last — the default behaviour of writing into
a dict — silently substitutes 02:00 GMT's value for 02:00 BST's.

## The EA levy line on Bill B cannot be reproduced exactly

The printed bill states both a rate (0.0013/kWh) and an amount ($0.22) on
180.43 kWh. These are mutually exclusive:

| To reproduce | Rate must lie in |
|---|---|
| the amount, $0.22 | `[0.0011916, 0.0012470)` |
| the rate, 0.0013 | `[0.0012500, 0.0013500)` |

The catalogue stores **0.0013, as printed**. The back-solved **0.0012193** is
recorded here as evidence and deliberately not used: storing it would make the
bill reconcile to the cent while proving only that an input had been tuned
until the output matched.

Every other line of Bill B reproduces exactly. See design §10.3.

## Plan assignment, and why it is deliberately not the best plan

**Rewritten 2026-09-08.** Sprint 3 now uses three Kea demo plans from the design
screenshots: Anytime Rate, Time of Use and Free Weekends. The compact cohort is
Vector / Standard User only. See `docs/sprints/sprint-3/plans-and-terms.md`.

The deterministic 50-household source generation currently assigns 20 Anytime,
17 Time of Use and 13 Free Weekends accounts. The assignment comes from a stable
hash of household identity, not from the load shape that makes a plan cheapest.
That is deliberate: a cohort placed optimally at generation time would give the
recommendation feature no wrong-plan examples to find.

Planted integrity defects later remove or invalidate some source accounts, so
the serving distribution is 18 Anytime, 14 Time of Use and 13 Free Weekends.
The recommender separately excludes the quarantined account and prices 44
eligible households.

All three plans share the same assumed 8c/kWh buy-back because the screenshots
do not specify an export rate. Solar therefore remains supported without an
invented export difference influencing the plan ranking.

## ecoWHOLESALE cannot be modelled faithfully — kept after its retirement

The plan is gone; the finding is not, and it will apply to any spot product that
replaces it.


Spot pricing changes every half hour. `rate_components` holds a scalar
`unit_rate` with a `valid_from` date, so a truthful spot plan would need one
component row per trading period — about 17,500 a year per plan. What is
modelled is a wide peak/shoulder/off-peak spread standing in for spot's average
shape.

This is a real gap in the source schema that the four published plans exposed.
No reconciliation bill is on a spot plan, so it does not block Stage 2, but a
spot bill **cannot** be reproduced from this catalogue.

## What the current catalogue rates rest on

Rewritten 2026-09-08. The two Sprint 3 screenshots are the source of truth for
the three customer-facing import rates, windows, daily charges, term credits
and early-exit caps. They are design inputs, not a claim about an existing
retailer's historical tariff.

| Provenance | Covers |
|---|---|
| `[DESIGN]` | Three advertised GST-inclusive import tariffs and daily charges; Flexible/6/12-month terms |
| `[BILL]` | The 5.11% import line-loss factor used by the existing billing model |
| `[DERIVED]` | Ex-GST catalogue rates adjusted so GST and line loss recompose to the screenshot prices |
| `[ASSUMED]` | Flat 8c/kWh export buy-back on all three plans; identical prices across the two abutting catalogue versions |

The generated dollar examples are valid demonstrations of the Kea design and
of relative plan fit on real LCL load shapes. They are not represented as
current offers from Contact Energy or Ecotricity.
