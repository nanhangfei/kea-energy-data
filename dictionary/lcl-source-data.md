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

## Plan assignment, and why ecoANYTIME is the majority

Ecotricity positions ecoANYTIME at homes **without** a smart meter, and every
household here comes from a smart-meter trial. That is read as marketing rather
than as an eligibility bar: a household with a smart meter can still prefer a
fixed rate, and many do.

LCL's `stdorToU` field records whether a household was on a standard or a
time-of-use UK tariff, so the mapping follows the source directly:

| Household | Plan | Count |
|---|---|---|
| has solar | ecoSOLAR | 53 |
| `stdorToU='ToU'` | ecoSAVER | 74 |
| `stdorToU='Std'` | ecoANYTIME | 328 |
| 10% of the above | ecoWHOLESALE | 45 |

That is the assignment `gen_billing.py` makes. The database shows 52 and 73 for
ecoSOLAR and ecoSAVER, not 53 and 74, because `07_billing_defects.sql` later
breaks two accounts' `PLAN_CODE` to `ECO_GONE_VECT_STA` — the orphan-reference
defect §2.1.1 needs in order to have anything to fire on.

A flat tariff also earns its place rather than merely being permitted. On a
time-of-use plan a bill can rise because consumption **moved into peak** without
total usage changing at all, so a rise has two causes tangled together. On
ecoANYTIME the rate is constant and fixed for three years, so a rise is volume
or weather and nothing else. That is the clean case the bill explainer should be
right about before it has to separate the ambiguous ones.

## ecoWHOLESALE cannot be modelled faithfully

Spot pricing changes every half hour. `rate_components` holds a scalar
`unit_rate` with a `valid_from` date, so a truthful spot plan would need one
component row per trading period — about 17,500 a year per plan. What is
modelled is a wide peak/shoulder/off-peak spread standing in for spot's average
shape.

This is a real gap in the source schema that the four published plans exposed.
No reconciliation bill is on a spot plan, so it does not block Stage 2, but a
spot bill **cannot** be reproduced from this catalogue.

## Only one plan in the catalogue is fully bill-sourced

Retail rates are quoted per delivery address, so most of the catalogue could
not be sourced:

| Provenance | Covers |
|---|---|
| `[BILL]` | Vector network + ecoSAVER energy + export + levy, STANDARD user, 2024 version |
| `[PUBLISHED]` | ecoSOLAR import 18c/17c and export 21c/16c |
| `[DERIVED]` | $0.7182/day of non-levy "Other" charges, back-solved from Bill A |
| `[ASSUMED]` | the other three networks, all LOW-user variants, the 2025 version, ecoANYTIME and ecoWHOLESALE energy |

Of 64 plans, exactly one — `ECO_SAVR_VECT_STA` version 1 — rests entirely on a
printed bill. At most 81 of the 500 households sit on VECT + STANDARD, and only
periods before 2025-04-01 use that version.

**Any claim that a generated dollar amount is realistic is confined to that
subset.** The rest are internally consistent and structurally correct, which is
what the pipeline is being tested on; they are not evidence about NZ prices.
