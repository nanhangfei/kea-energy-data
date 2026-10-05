# Low Carbon London — what the source data actually is

Notes on the dataset behind `src_metering`, recorded because several of them
are decisions or surprises rather than things you can read off the file.

Every reading in the generated cohort is real metering from **Low Carbon London**:
about 5,567 London households, half-hourly, **23 Nov 2011 to 28 Feb 2014**. Real
data is the point — load shape is what the bill explainer and the plan
recommender both reason about, and a simulation gives you back the shape you
already assumed.

⚠️ **Figures here are the current pin, and the pin moves.** The span, the shift
and the scale factor all changed with the move to a 2027 end date and again when
the demo cohort dropped from 500 households to 50. `uk_to_nz.py`, `cohort.json`
and `scale.json` are the source of truth; a figure in this file that disagrees
with them is stale, not a second opinion. Rebuilding after a move is the stack
doc's §7.

## 1. Why real metering and not a simulation

The intro states it in one line; this is the evidence, because the decision is worth
being able to defend. A generated load curve can only contain the structure whoever
wrote it put in — and almost every feature in this product is a claim about structure.

**The control group is in this repo.** The nine sample fixtures *are* synthetic, and
they show exactly what a simulated cohort costs:

- `generate_scenarios.py:45` shapes every half-hour with a single `CASE` on the hour —
  no day-of-week term, no per-household term. So `mart_hourly_profile.is_weekday`
  carries no signal in the fixtures: the largest weekday/weekend difference in any hour
  across all nine is **0.0001 kWh**, two curves drawn on top of each other, while design
  §8.7 argues weekday and weekend are different curves and the mart doubles its row count
  to hold that. Carried as a known fixture defect in stack doc §6.5's "what is not built"
  table.
- The heating sensitivity is a hand-set constant, `0.93 kWh/HDD` at
  `generate_scenarios.py:51`. Fitting it on the fixtures returns what was typed in.

LCL has neither problem. Its day-of-week variation is real, its distribution tail is
inherited rather than chosen — mean 20 against a median of 14.6 — and ~1.3% of its
household-days are already imperfect before `05_defects.sql` injects anything.

**What a simulation would have had to assume**, and where each assumption would then be
"discovered" by a feature built to find it:

| Would have to be assumed | The feature that reads it back |
|---|---|
| intraday shape, per household | the hourly-profile panel and the load detector |
| weekday versus weekend | `mart_hourly_profile.is_weekday`, TOU day-of-week windows |
| variance *between* households | the plan recommender, which ranks each household separately |
| the right-hand tail | bill-shock detection and the hardship signal |

Assume them and the demo becomes circular: each feature recovers the parameter its own
input was generated from, and the result reads as validation. The same trap is why the
scale factor is one global multiplier and not one per household — see "Consumption is
scaled by a single global factor".

**The cost, stated plainly.** Real UK data carries a defect a simulation would not have:
the daily weather response belongs to *London*, so a daily HDD regression on this cohort
is noise (median R² **0.01**). A simulated cohort would have had a correct HDD
relationship by construction — because it would have been built with one. That is the
trade actually taken: **a genuine load shape with a broken daily weather link, over a
plausible weather link with a load shape that was assumed.** Only the first is repairable
by better analysis, and it was — at monthly grain. See "The seasons align. The DAYS do
not" above.

**Where synthesis was unavoidable, it is marked rather than blended in.** LCL has no
generation, no EV charging and no household sliding into arrears, so solar arrays, eight
planted load profiles, four sliding payment accounts and the integrity defects are all
added — each recorded in `gen_audit`, which never reaches serving. The decision-log entry
of 2026-09-17 puts the limit on what that proves: planted data shows the pipeline finds
what was planted, not that real households behave that way. The rule the whole cohort
follows: **real data for shape, synthetic only for what LCL cannot contain, and never
silently.**

## 2. What it has to become, and the three conversions that get it there

Kea is a New Zealand retailer, and the serving schema derives every date through
`Pacific/Auckland`. So the data has to read as New Zealand data — right
calendar, right seasons, right daylight saving — while remaining the same
households' real behaviour.

Two of the three conversions are about time, and `uk_to_nz.py`'s docstring is
scoped to those; the third is about size and lives in `gen_metering.py`, which
is why it is easy to miss when reading the transform alone.

| | Conversion | Where | Section |
|---|---|---|---|
| 1 | **when in the year** a reading sits — UK winter must land in NZ winter | `uk_to_nz.py` | §8 |
| 2 | **which instant** a local wall clock means — NZ daylight saving moves on different dates, so a local day is 46, 48 or 50 half-hours | `uk_to_nz.py` | §9 · §10 |
| 3 | **how much** a household uses — NZ households use roughly twice what London ones did | `gen_metering.py` | §14 |

And one thing must **not** change: **the wall clock itself.** A reading at 18:30
London local becomes a reading at 18:30 Auckland local. People cook dinner at
dinner time in both hemispheres, so preserving the clock is what preserves the
load shape — the whole reason for using real data. That single constraint forces
everything else, including that the shift must be a whole number of **days**:
adding hours would smear the evening peak across the wrong part of the day.

## 3. The whole path, in two diagrams

Every section below takes one decision at a time, which makes the shape of the
whole transform hard to see. Two diagrams answer that on their own page —
**[`figures/lcl-transform-path.html`](figures/lcl-transform-path.html)** — which
opens in any browser and needs nothing installed:

- **The stage chain** — what each script leaves on disk, from the sha256-pinned
  Kaggle zip through `cohort.json` and `scale.json` to the four schemas. Each
  node is an artefact that can be read on its own; the lines between them are
  scripts. Two are marked committed: they *are* the decision, not a cache of
  it, which is why a rebuild reproduces.
- **One reading** — 0.4120 kWh at `2012-02-03 18:30 UTC`, one household's
  dinner-time slot, followed through the five steps to the one or two registers
  it can land on, as bars on a shared kWh scale.

Every node and every step carries its own note on hover, and a table view holds
each value the diagrams encode. The figures there are read from `cohort.json`,
`scale.json` and `gen_metering.pv_half_hour`, so the same staleness warning at
the top of this file applies to them.

**The order of the last three steps is load-bearing, not incidental.**

- **Scale before planting** — a planted appliance is specified in NZ kilowatts
  (a 7.2 kW wallbox, a 3 kW element), so it joins a load that is already at NZ
  level. Scaling afterwards would multiply the wallbox by 1.72 as well.
- **Planting before the solar split** — an appliance joins the *household's*
  load, not the grid, so the three solar+EV hosts charge under their own panels
  and only what the array did not cover reaches the import register.
- **The clamp inside the split** — both registers are `max(0, …)`, which is what
  `ck_kwh_non_negative` exists to enforce: solar is a second register, never a
  negative number on the first.

And three things that deliberately never happen. A missing raw reading stays
missing — a `None` slot is skipped, not interpolated, so a genuine LCL gap is
still a gap in NZ. The wall clock is never moved by hours, only by whole days.
And a household's own load decides nothing about *membership*: not who gets
panels (a hash), not who gets a planted load — except through the screen in
`planted.screen()`, which can only ever leave a household out.

The sections below run in that same order. The clock is §8 and the two
daylight-saving ones after it; the cohort the steps apply to is §12–§13; the
factor is §14; and the two additions are §15 (the planted load) and §16 (the
solar split), in the order the generator applies them.

## 4. Provenance

⚠️ **This table is the tracked copy, and the authoritative one.** The archive
itself lives in `data/raw/lcl/`, which is gitignored (`.gitignore:90` —
`data/raw/*`, with only `.gitkeep` excepted). `.gitignore` says of that rule that
"the fetch script records provenance and a checksum alongside them", and it does
— `raw/lcl/PROVENANCE.md` and `raw/lcl/MANIFEST.sha256` — but both are ignored
too, so they exist only on the machine that ran the fetch. Nobody reviewing this
repository can see them. Hence this copy.

| | |
|---|---|
| Source | https://www.kaggle.com/datasets/jeanmidev/smart-meters-in-london |
| Retrieved | 2026-09-20T04:41:54Z |
| Archive | `smart-meters-in-london.zip` |
| Size | 1.2 G |
| SHA-256 | `8839527a354c28013a025508ffad4ad26822f2aa255447dff1a3a4b3575f56f5` |

Verify a local copy with `shasum -a 256 -c data/raw/lcl/MANIFEST.sha256`.

**As received. Never edited by hand** — `data/README.md:17`, and it is the rule
that makes "can we rebuild this?" answerable. If an extract needs fixing, the fix
is a script.

It is UK data used for a New Zealand product. The transform that makes that
defensible — preserving local clock time, shifting the seasons, and regenerating
NZ daylight saving — is `pipeline/scripts/gen_source/uk_to_nz.py`, and the rest
of this file is its reasoning.

## 5. `tstp` is UTC, not London local

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

## 6. LCL is clean, but not perfectly clean

The design doc calls it "a curated academic release — already cleaned...
very few organic defects". Broadly right, and worth a number. In `block_0`
(50 households, 25,574 household-days — LCL's own block size, unrelated to the
50-household cohort):

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
than on a spotless base. Cohort selection filters on coverage for this reason — §13,
"Why 50 households, and how the 50 are chosen", is that filter.

## 7. The Acorn mapping is an assumption

`Acorn_grouped` is a UK geodemographic classifier with no NZ equivalent. It is
used to vary customer attributes across the cohort — vulnerability flags,
payment method, low-user plan take-up, solar likelihood — so the population is
not uniform. It is **not** evidence about New Zealand households, and nothing
downstream should treat it as such. Flagged in the spirit of design doc §2.2.

It is also half of the cohort's stratification key — see §13, "Why 50 households,
and how the 50 are chosen" — so it decides membership as well as attributes.

## 8. Seasons are flipped on purpose

UK data, NZ product. Readings are shifted a whole number of days so that UK
winter lands in NZ winter — otherwise the bill explainer's weather driver reads
backwards and a cold snap appears in January.

**The shift is derived and asserted, not hardcoded.** `derive_shift_days()`
takes it from the two span *ends* and refuses any value failing one of three
properties:

| Property | Why | Current value |
|---|---|---|
| a whole number of **weeks** | load shape is strongly weekday/weekend; `mart_hourly_profile` keys on `is_weekday` and TOU windows are day-of-week scoped via `days_csv` | 4,928 days = **704.0 weeks** |
| **close to half a year** past a whole year | this is the season flip — the point of the exercise | frac **0.4924** (must be 0.48–0.52) |
| lands on the pinned end date | so the span is a decision, not a side effect | 2027-08-27 |

**The ends anchor it; the start follows.** The shift is `NZ_SPAN_END − LCL_END`,
and the same shift then carries the start:

```
LCL_START  2012-02-03 (Fri)  + 4928 days  ->  2025-08-01 (Fri)
LCL_END    2014-02-28 (Fri)  + 4928 days  ->  2027-08-27 (Fri)
```

**`NZ_SPAN_END` is pinned on purpose — never `date.today()`.** The shift derives
from it, so a moving anchor would give a different shift every day: the cohort
pick, every timestamp, every assertion and every `received_at` would drift, and
a re-run would not reproduce. The season-flip assertion also makes legal end
dates sparse — in a two-year range only four qualify and December is not among
them — so the next bump is **2028-08-25** (+364 days, shift 5,292, frac 0.4890),
not "a few months on".

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

The mechanical proof is in the generator: **`gen_metering.py` takes no temperature
input at all** — there is no HDD term anywhere in it. Weather is real New Zealand
data (`load_niwa_weather.py` loads NIWA daily temperature for four stations, NZ-dated,
as published), and the load beside it is London households responding to London
weather. Any daily HDD-versus-usage correlation in the cohort is incidental.

⚠️ **What this does not explain:** the Sprint 4 backtest's finding that the
champion's ±15% band holds 51%. That is band construction —
`build_bill_forecasts.py` sets `half_band = remaining_cost * 0.15`, so in bill
terms the half-band lands within 0.8–1.1× of the champion's own median error at
every horizon, and a band that wide covers about half by definition. See design
§15.12; do not reach for the weather gap to explain it.

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

⚠️ **Every figure in this subsection comes from the archived `COHORT_SIZE=500`
run, and nothing has been refitted on the 50 households `make rebuild-all`
builds.** 446 fitted ICPs become at most 50 there, and the 0.30 fit floor has far
fewer households to clear. The *rule* carries over regardless — fit and spend the
slope at monthly grain, never daily — but the coverage numbers should not be
quoted for the demo profile.

**So the cohort does not need regenerating**, and the sharper version of this
section's warning is: the shift aligns the seasonal component and only the
seasonal component. Fit — and spend — the slope at monthly grain. A daily
regression on this cohort is measuring London weather against Auckland weather
and will always return noise.

The fixtures' `0.93 kWh/HDD` remains a hand-set constant at
`generate_scenarios.py:51` applied to the nine, never a fitted property of the
500 — the 0.74 above is fitted, and the two agreeing to within 20% is a
coincidence worth not leaning on.

## 9. Daylight-saving days are not energy-normalised

An Auckland local day is 46, 48 or 50 half-hours. On the 50-half-hour day one
UK reading fills two NZ slots, so the day's total sits about 4% above its
neighbours; on the 46 day it sits about 4% below.

**This is deliberate and must not be flagged as an anomaly.** A household
living through a 25-hour day genuinely uses more electricity. Rescaling to
conserve the daily total would be the wrong correction, and it would break the
`interval_count = expected_intervals` check. Preserving the per-interval level
keeps `interval_count` equal to the expected count with no fudge factor.

**The NZ grid is iterated, not the UK one**, and that is what makes both edges
resolve themselves instead of needing special cases. `nz_half_hour_grid()` walks
forward from local midnight **in absolute UTC time**, so a day's length falls out
of the zone (46, 48 or 50) rather than being assumed; each slot then reads its own
wall clock and steps back 4,928 days for its UK reading. On a 46-hour day no slot
ever carries a 02:00 or 02:30 wall clock, so those two UK readings are never asked
for — dropped, correctly. On a 50-hour day two slots share the 02:00 wall clock
(NZDT then NZST), so both ask for the same UK reading and it is duplicated.

Two traps here are worth naming because both are silent:

- Arithmetic is done in **UTC** because Python datetime arithmetic on two aware
  values sharing a tzinfo is **wall-clock** arithmetic, not elapsed time.
  Consecutive local midnights subtract to exactly 1 day even across a transition,
  which yields a plausible-looking 48 half-hours for *every* day.
- **Localising 48 wall clocks independently does not work either.** On the
  spring-forward day local 02:00 and 02:30 do not exist and on the fall-back day
  they exist twice, so you get 48 rows with a hole and a collision instead of a
  clean 46 and 50.

Genuine source gaps are left alone: `nz_day_from_uk_readings()` returns `None`
and the generator skips the row rather than inventing a reading.

## 10. A UK daylight-saving day collides with an NZ one

The shift is a whole number of days, so a UK spring-forward date maps onto an
NZ date whose half-hour grid still asks for 01:00 and 01:30 — wall clocks
London does not have that day.

| UK spring-forward date | NZ date | effect |
|---|---|---|
| 2012-03-25 | 2025-09-21 | two slots estimated |
| 2013-03-31 | 2026-09-27 | two slots estimated |
| 2012-10-28 (fall back) | 2026-04-26 | two readings discarded |
| 2013-10-27 (fall back) | 2027-04-25 | two readings discarded |

So per household **4 slots are estimated and 4 readings discarded**, out of
36,336, on 4 of 757 days. Any analysis filtering on `read_status='A'` loses those
four slots; any analysis that does not is reading a carried-forward value on two
of the days.

Left alone this makes **every household in the cohort two readings short on the
same two days**, which looks exactly like ordinary missing data when viewed one
household at a time. Those slots are filled from the preceding half-hour and
marked `read_status='E'`. They are genuinely estimated, and they are the only
source of estimated reads that is not injected.

The UK *fall-back* day is the mirror image: two readings share one wall clock.
The **first** is kept. Keeping the last — the default behaviour of writing into
a dict — silently substitutes 02:00 GMT's value for 02:00 BST's.

## 11. UK public holidays are not carried across

UK Christmas 2012 maps to an ordinary NZ Tuesday in June. The load anomaly
travels with the reading and lands in `delta_unexplained`. Known artefact,
small, not corrected. NZ holidays would have to be generated fresh.

## 12. The usable window starts in February 2012, not November 2011

LCL recruited in waves, so the published span is not a usable panel. Coverage
of the full 829 days:

| Days held | Households |
|---|---|
| ≥ 828 | 21 |
| ≥ 800 | 317 |
| ≥ 700 | 1,120 |
| ≥ 600 | 4,176 |

Reproduce that table with `cohort.py --report-windows`.

Only **77** households cover 99% of all 829 days, so sampling at random would
have filled the cohort with short histories and left the year-on-year
comparisons nothing to compare. Window length and panel size trade against each
other, and that trade is what pins `LCL_START`. It is currently **2012-02-03**,
where **609** households hold ≥ 99% of the **757** remaining dates.

Moving the start does **not** change the shift: that is derived from the window's
*end* (§8), which has not moved.

⚠️ **But `LCL_START` was chosen backwards, and this is the least obvious part of
the design.** 2012-02-03 means nothing in London. What matters is the NZ date it
lands on: **2025-08-01 is a month boundary**, and bill periods are monthly.
`mart_forecast_input.same_period_last_year_kwh` is a plain SUM over the year-ago
window with no guard for a window only partly covered — and a half-covered window
does not come back NULL, it comes back **half**, so this year reads as a doubling.
That is a fabricated bill shock, on every household at once, in the one feature
whose job is to tell a real one from a false alarm. The previous pin (NZ start
2025-09-17) would have given the September-2026 bills 14 of 30 year-ago days and
done exactly that.

The start also has to keep all four NZ daylight-saving transitions inside the
span — 2025-09-28 (46 h), 2026-04-05 (50 h), 2026-09-27 (46 h), 2027-04-04
(50 h) — two short days *and* two long ones, so neither path is tested only once.
Across the span that is **36,336 half-hour slots per household**: the two 46s and
two 50s cancel exactly, so the total equals 757 × 48.

⚠️ **The trade-off comment in `uk_to_nz.py` is stale.** It reasons that "609
against a cohort of 500 is the margin". The live cohort is **50**, so the margin
is 12× rather than 1.2×, and the "cost is panel size" constraint that justified
not starting earlier has gone slack. The pin is still safe — more headroom, not
less — but anyone re-tuning the span would be reading a binding constraint that
no longer binds.

## 13. Why 50 households, and how the 50 are chosen

⚠️ **Superseded by a decision, not yet by code (2 Oct 2026).**
[`design/kea-cohort-selection.md`](../design/kea-cohort-selection.md) replaces both halves of this
section. It asks what each feature needs from the cohort, and finds that 50 leaves several of them
with one or two examples. It moves to **200 households**: population quotas instead of the eligible
pool's, a sample spread across usage, and a fixed solar count. Everything below still describes the
cohort `cohort.json` holds today, and stays true until the rebuild that implements it.

### Why 50 — a rebuild-time decision, not a statistical one

Nothing about 50 was derived from a power calculation or a variance target. `make
rebuild-all` is the normal rebuild and `data/Makefile` sets `COHORT_SIZE ?= 50`; the
documented reason is in `docs/sprints/sprint-3/data-plan.md` under "Compact demonstration
profile" — the compact path exists to bring a full rebuild in **under 30 minutes**, and
`COHORT_SIZE=500` remains available for a scale run. Design §7 of
`kea-data-engineering-stack.md` carries the operational half of the same fact.

So 50 is a demonstration panel, sized for rebuild time. Distributional claims read off it
carry that caveat; the sections that quote the archived 500-household run mark themselves.

The number is not only a clock decision, though — it is an **input** to the mechanism
below, which is why it comes first here. It sets every stratum's quota, and one of its
consequences shows up only at 50.

### How the 50 are drawn

`cohort.py` makes the pick, and `cohort.json` records it. Four steps, in this order:

**1. Coverage filter.** Of the **5,566** households in the release, the ones kept are
those holding at least `--min-coverage` (default **0.99**) of the window §12
pins — **609** of 5,566, over its 757 dates. Nothing else about a household is
looked at yet, which is why the previous section's table is a table of coverage and not
of anything more interesting.

**2. A loud failure if the pool is too small.** If fewer households clear the threshold
than `--size` asks for, `cohort.py` exits naming both remedies — lower `--min-coverage`,
or move `LCL_START` later and re-check the DST column. It cannot quietly return a short
cohort. At the current pin the pool is far larger than the request, so this guard has
never fired — see the stale `uk_to_nz.py` comment flagged above for what that slack
did to the window trade-off.

**3. Proportional stratification on `(stdorToU, Acorn_grouped)`.** Each stratum
contributes `round(size × members / eligible)`, so the cohort keeps the eligible pool's
proportions rather than the hash's. At the current pin:

| Tariff | Acorn group | Eligible | Share | Taken |
|---|---|---:|---:|---:|
| Std | Affluent | 251 | 41.2% | 21 |
| Std | Comfortable | 130 | 21.3% | 11 |
| Std | Adversity | 127 | 20.9% | 10 |
| ToU | Affluent | 56 | 9.2% | 5 |
| ToU | Comfortable | 22 | 3.6% | 2 |
| ToU | Adversity | 17 | 2.8% | 1 |
| Std | ACORN-U | 5 | 0.8% | **0** |
| ToU | ACORN-U | 1 | 0.2% | **0** |

⚠️ **This makes `Acorn_grouped` a selection key, not only an attribute source.** §7
describes what the classifier varies downstream; it is also half
of what decides who is in the cohort at all, so an objection to the classifier is an
objection to the panel's composition too, not just to a flag.

**4. A seeded hash, not a random sample.** Within a stratum, members are ordered by
the first 12 hex digits of `sha256("LCLid|SEED")` (`cohort._h`) and the first `take` are
kept; `SEED = "kea-stage2-v1"` is fixed and
changing it re-rolls the entire cohort. So the pick is reproducible without storing a
list, and it moves only when the eligible pool moves — which a span change does.

The picks are then re-sorted **globally** by the same hash and truncated to `--size`. That
last sort does two jobs worth knowing about:

- **It decides who an overshoot drops.** Truncation cuts the tail of the global hash order,
  not of any one stratum, so if the rounded quotas sum to more than `--size` the households
  lost come from wherever the hash put them and the proportions step 3 just built are
  slightly off. Not a live problem — see "`--size` is a request" below — but it is why the
  proportions are a target rather than a guarantee.
- **It fixes the generated ICPs.** The position in this list is the last two digits of each
  household's ICP (`enumerate(chosen)` feeds `i % 100`), so the order is load-bearing:
  reordering the cohort renames every household even if membership is identical.

### ⚠️ At size 50, two strata round away entirely

ACORN-U is LCL's "unclassified" bucket: 6 eligible households across the two tariffs, and
`round(50 × 5/609)` and `round(50 × 1/609)` are both **0**. So the compact cohort has no
unclassified household in it, and that is an artefact of the size, not a rule —
at `COHORT_SIZE=500` the same arithmetic takes 4 and 1. Anything that asks "how does the
pipeline handle a household with no Acorn group?" cannot be answered from the demo data,
even though `cohort.py` does handle it (it leaves `acorn_grouped` NULL).

### `--size` is a request, not a guarantee

The per-stratum takes are rounded independently, so their sum need not equal `--size`.
An overshoot is truncated by `chosen[:args.size]`; an **undershoot is not corrected**, and
nothing compares the result to the request — `cohort.json` records `"size": len(cohort)`,
the count actually chosen, and the run prints `chose N households` without asserting
anything about N. Today the rounding happens to land exactly on the requested number at
50, 100, 200, 500 and 609, so the cohort really is 50. That is a property of this eligible
pool, not of the algorithm: a span move changes the strata sizes, and a rebuild could come
back with 49 households and say nothing. Check the printed count after a rebuild.

## 14. Consumption is scaled by a single global factor

`gen_metering.py` runs in two passes: `--stats` measures the cohort's own LCL
mean and writes `scale.json`, and generation refuses to start without it.
Measured, not chosen. Current values:

```
lcl_mean_kwh_day   11.6209      measured over 37,710 household-days, 50 households
nz_target_kwh_day  20.0
scale              1.721042     k, applied as `raw * scale`
```

NZ households use roughly twice what London ones did, so every reading is
multiplied by `k = 20.0 / 11.6209 = 1.721042`. 20.0 kWh/day is 7,300 kWh/yr,
close to the NZ residential average, which is what makes the resulting bills
recognisable to anyone who pays one here.

Both figures move when the cohort or the window does, which is the point of
measuring them: the earlier full-scale run measured 10.3009 kWh/day over 353,754
household-days for `k = 1.941577`. The mean rose and the factor fell, consistent
with both an earlier window start (more UK February) and a differently-composed
50-household cohort — the two have not been separated.

**One global multiplier, not one per household.** Scaling per household would
normalise away the between-household variation, which is the main thing a real
dataset supplies and the whole reason for not generating the load synthetically.

### Why the level matters, and not just for looking plausible

Because **a bill is affine in kWh, not proportional to it**:
`build_bill_forecasts.py` computes `fixed_charges_period + projected_kwh *
rate_per_kwh`. Leaving the LCL level alone would not halve every bill, it would
change what every bill is *made of*. On Anytime ($1.45/day, $0.325/kWh) over 30
days:

| | kWh/month | Bill | Fixed charge | Annual |
|---|---|---|---|---|
| unscaled, 11.62 kWh/day | 348.6 | $156.80 | $43.50 = **27.7%** | 4,242 kWh |
| scaled, 20.0 kWh/day | 600.0 | $238.50 | $43.50 = **18.2%** | 7,300 kWh |

Four things follow, and the first two are the ones that would actually mislead:

1. **The bill explainer's decomposition would be wrong in its proportions**, which
   is its entire subject matter — attributing 28% of a bill to a standing charge
   that should be 18%, a 52% relative overweight on the one line the customer
   cannot act on and a matching underweight on the usage they can.
2. **Solar self-consumption versus export is a ratio to load.** Array size is
   derived independently (1,300 kWh/kWp), so at half load an array covers
   proportionally far more of the house, export share rises, and buy-back starts
   driving the economics. Not hypothetical: the decision-log entry of 2026-09-20
   records the same shape one level down, where a fixed 4 kWp array left the
   smallest household selling back 80% of what it generated.
3. **Dollars are the unit of every downstream threshold** — the forecast's bands,
   bill-shock detection, the hardship signal — all calibrated against what a real
   NZ bill looks like.
4. **Plan ranking tilts toward the lowest daily charge**, because a per-kWh
   advantage scales with volume while a daily-charge penalty does not. Time of Use
   costs $0.05/day more than Anytime, so it has to save 0.250 c/kWh at 20 kWh/day
   but **0.430 c/kWh** at 11.62. ⚠️ Keep this in proportion: both are small against
   these plans' 5.5–21 c/kWh spreads, so the current ranking would largely survive.
   It would *not* survive an offer set with a wide daily-charge spread, such as a
   genuine low-fixed-charge plan.

### ⚠️ The level and the hardcoded user category may disagree

`cohort.py` sets `user_category` to `STANDARD` for every household, and the
catalogue is priced on a Vector / Standard User basis — but 7,300 kWh/yr sits
*below* the 8,000 kWh/yr Low Fixed Charge threshold most NZ networks use. There
is no LFC logic anywhere in the pipeline, so this reads as a deliberate scope
decision rather than a defect. It becomes one the moment low-user pricing is
modelled, and `scale` above is what decides which side of the line the cohort
lands on.

### The tail is inherited, not manufactured

⚠️ Measured on the **archived `COHORT_SIZE=500` run**, and not remeasured on the
50 households `make rebuild-all` builds:

| | kWh/day |
|---|---|
| median | 14.6 |
| 95th percentile | 52.8 |
| 99th percentile | 94.2 |

Mean 20 against a median of 14.6 — right-skewed, as real consumption is. Exactly
one household of the 500 averaged over 100 kWh/day (140.0); that is a single large
all-electric LCL property, not an artefact. The largest single *day* in the data
is 934.8 kWh, and that one **is** an artefact: an injected spike from
`05_defects.sql`.

Consequence to state plainly: UK winters are colder than NZ ones, so the
seasonal amplitude here is larger than NZ-realistic — the loaded cohort shows
NZ winter at 24.05 kWh/day against summer at 14.93, a +61% swing. Heating
sensitivities fitted from this data will exceed the fixtures' 0.93 kWh/HDD.
Derive sensitivity *from* the shifted data; do not carry the fixture value over.

## 15. Eleven households carry loads LCL never had (Sprint 4)

LCL (2011–14) has no EV charging, no household that slides into arrears, and no
generation. One regeneration therefore **adds** synthetic load on top of the scaled LCL
load, in two pools that never overlap:

| Pool | Hosts | What is added |
|---|---:|---|
| `PLAN` — households without panels | 8 | three 7.2 kW wallboxes and one 2.3 kW portable charger, each arriving mid-span; four 3 kW hot-water elements on a 23:00 timer every night |
| `SOLAR_PLAN` — households with panels | 3 | one 7.2 kW wallbox each, on **declared** 8.0 / 5.0 / 3.0 kWp roofs, charging at midday, in the evening and on a 23:00 timer |

The scale factor above is measured **before** anything is added. These households' import
is LCL × k **plus** the planted load, so their daily totals and winter/summer swing are
not pure LCL any more. `gen_audit.planted_load` records every session — kind, pattern,
start, end, kWh — and never reaches serving. Three billing accounts separately get a
sliding payment history (`gen_audit.planted_hardship`). Details:
`pipeline/scripts/gen_source/README.md`, "Planted answers"; the detector side is design
§15.11.

### The hosts are screened, not sampled

Scaled × 1.721042, **a UK electric heater draws like a wallbox**: `detect-v1` finds
EV-shaped runs in **27 of the 50** households' own, untouched load. Plant a charger on one
of those and a detected session cannot be told from a coincidence, which would make the
labels worthless. So `planted.screen()` replays `detect-v1`'s own thresholds — read from
`dbt_project.yml` into `planted.DETECT_V1`, with a test that fails if the two drift —
against each candidate's load before anything is planted. **It can only ever leave a
household out.**

| Place | Requirement on the host's **own** load |
|---|---|
| EV, no panels | an EV-shaped run on at most `MAX_EV_SHAPED_SHARE` = **0.005** of assessed days — 3 of 757 |
| Hot water | the same bound *with the element added*, **and** the element reading as a timer on all but `MAX_TIMER_MISREAD_SHARE` = **2%** of its nights |
| EV with panels | loosened to `MAX_SOLAR_EV_SHAPED_SHARE` = **0.015** — 11 of 757 |

⚠️ **The loosening is a stated trade, not a fudge.** Only 8 households have panels, and
their own-load scores are **0, 0, 4, 9, 44, 102, 105 and 546** EV-shaped days. At 0.005
exactly two clear it for three places — and those two are the lightest households in the
cohort, which would not be running a car. So the solar pool accepts a little label noise
and the evaluation reports each host's own-load background **beside** its recall rather
than treating it as zero. `PLAN`'s eight keep the strict bound, so no earlier Sprint 4
figure moves.

Two details make the screen mean what it says:

- **A solar host is screened on its net import, not its gross load**, because the detector
  only ever sees the import register. Generation both hides real runs and, at the edges of
  the day, carves new ones out of an ordinary load.
- **Array size and eligibility would otherwise chase each other**, since `SOLAR_PLAN`
  re-roofs whoever it picks. Everyone is screened at the largest declared array, 8 kWp —
  the safe direction, because more generation subtracted masks more of the day. It costs
  nothing: the eligible three score 9, 4 and 0 at *every* size from 1.5 to 8 kWp. The
  screen measures their own load, not the roof.

Two more things about *how* the screen is applied while the places are filled
(`planted._fill`, `planted.py:296-314`):

- **The hot-water places are filled before the EV ones**, by sorting the places on
  `plan[i][0].startswith("ev_")`. The element's screen is strictly stronger — it holds the
  EV bound *and* the timer-misread one — so filling EVs first could hand a wallbox to a
  household that only an element could have had, and then leave a hot-water place with
  nobody clean. The order is the fix.
- **If a place runs out of clean candidates, the generator fails.** `_fill` raises
  `no clean candidate left for {kind}/{pattern}`, naming the pool size and how many are
  already planted. It never quietly plants ten loads instead of eleven, so a rebuild that
  silently loses a host is not a failure mode — a tightened threshold or a shrunken cohort
  stops the run instead.

Households the source defects already break are excluded from both pools — the two ICPs
`05_defects.sql` picks and the five accounts `07_billing_defects.sql` orphans — and
`99_assert.sql` checks that they were.

### Why the solar three are ordered by load, and their roofs re-declared

`candidates()` orders the no-panel pool by a hash of the LCL id. `solar_candidates()` is
the one order in the module that is **not** a hash: biggest NZ annual load first. An EV
adds 2,500–4,500 kWh a year and the cohort's smallest solar households use 6.6 and
10.2 kWh/day scaled, so giving one of those a wallbox would plant a household that does
not exist.

Their kWp is declared in `SOLAR_PLAN` rather than taken from `cohort.json`, because
`cohort.py` sizes a roof to the household's load **before** it had a car and cannot know
which households will be planted. A household that buys an EV and charges it off its own
roof sizes the roof for the car: 8 kWp yields 10,400 kWh/yr against this host's ~9,700 kWh
of load with the EV. `gen_metering.py` resolves the two, and the declared size wins.

## 16. Solar arrays are added, and LCL has none

LCL (2011–14) contains no generation at all, so every array in the cohort is
synthetic. Each is sized from its household's **NZ-scaled** annual load, bucketed
to real product sizes (`PV_SIZES_KWP` — 1.5 to 8.0 kWp), at an Auckland-ish
**1,300 kWh/kWp**; a seasonal shape with amplitude 0.45 peaks near the summer
solstice. Sizing from the scaled load is what makes the scale factor above a
determinant of the solar economics rather than a cosmetic multiplier — see "Why
the level matters".

**Who gets panels is a hash, not a load criterion.** `cohort.py:282` is the whole rule:

```python
has_solar = _h(lcl, "solar") % 100 < (14 if acorn == "Affluent" else 6)
```

14% of `Affluent` households and 6% of the rest, which lands on **8 of the 50**. Nothing
about the household's own curve is consulted — not its level, not its shape, not whether
a roof would pay for itself there. `Acorn_grouped` is the only attribute that moves the
odds, which puts solar likelihood on the UK classifier along with everything else in
§7, "The Acorn mapping is an assumption". The load enters only afterwards, at sizing.

⚠️ **Moving a household in or out of solar re-deals every planted load.** `has_solar`
picks from the whole cohort and `planted.candidates()` then draws the EV and hot-water
pool from whoever is left, so the two are coupled by construction: change the
probabilities or the seed and both pools re-deal, taking Sprint 4's comparability with
them (`cohort.py:74-77`).

The cohort is therefore real measured shape carrying **synthetic** distributed
energy resources, which is worth remembering before treating a solar or EV
finding here as evidence about real households.

### Solar is subtracted; the Sprint 4 loads are added

The two synthetic additions work in opposite directions, and only one of them is load.
A planted charger raises the household's demand; an array does not lower it — it is
netted against it, and the leftover becomes a **second register**
(`gen_metering.py:376-384`):

```python
load = raw * scale + added.get(int(instant.timestamp()), 0.0)
if solar:
    g = pv_half_hour(instant, gen_day)
    imp, exp = max(0.0, load - g), max(0.0, g - load)
else:
    imp, exp = load, 0.0
```

| | What is synthetic | Sign | Registers written |
|---|---|---|---|
| EV / hot water | a session schedule — kind, pattern, start, end, kWh | **+** onto the load | `UN` only |
| Solar | a generation curve — 1,300 kWh/kWp, amplitude 0.45, peak near DOY 5 | **−** off the load | `UN` **and** `EX` |

Three consequences worth having written down:

- **A solar household's LCL load becomes GROSS demand, and its import is no longer its
  consumption.** Anything reading `UN` alone on those eight households is reading what the
  array failed to cover, not what the house used.
- **They carry twice the rows.** Every half-hour writes a `UN` row and an `EX` row, so row
  counts and per-ICP volumes are not comparable between solar and non-solar households.
- **Export is modelled by direction, never by sign.** Both values are clamped at zero and
  `ck_kwh_non_negative` (`design/kea_source_schema.sql:326`) enforces it — design §8.2.3.
  A negative read belongs at the file boundary in the Lake, not in this table.

⚠️ **And one injected defect deliberately breaks the split.** `05_defects.sql` §7 adds
two households' `EX` volume onto their `UN` register for November 2025 — the source-side
equivalent of a misdelivered export read. It passes every constraint and inflates the
customer's import; the only way to see it is to notice that a solar household's midday
import **rises** on sunny days instead of falling.

**The order between the two is deliberate**: planted load joins the household *before* the
solar split, as a real appliance would, so the three `SOLAR_PLAN` hosts charge under their
own panels and only what the array did not cover reaches `UN`. That is why the meter is
partly blind at midday, why those hosts are screened on net import, and why `detect-v2`
exists at all — design §15.11.

## 17. Plan assignment, and why it is deliberately not the best plan

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

## 18. What the current catalogue rates rest on

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

## 19. The EA levy line on Bill B cannot be reproduced exactly

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
