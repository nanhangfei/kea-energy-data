# NIWA weather — what it is, and what it is not

The temperature series behind `mart_daily_usage.heating_degree_days`, and
therefore behind every weather figure the agent quotes: `delta_from_weather` in
`mart_bill_explanation` (design §8.5) and `hdd_expected_remaining` in
`mart_forecast_input` (§8.6).

Until this landed there was **no weather source anywhere in the project**. Ten
columns across three marts depended on one, `connection_points.climate_region`
was documented as the join key for it, and the fixtures' `0.93 kWh/HDD`
sensitivity was a hand-set constant in `generate_scenarios.py` with nothing
behind it.

---

## Provenance

**Source:** NIWA National Climate Database (CLIDB), via **DataHub** at
`data.niwa.co.nz`.

⚠️ **CliFlo is retired.** Every instruction written against `cliflo.niwa.co.nz`
is obsolete, and the replacement differs in ways that change what can be asked
for:

| CliFlo | DataHub |
|---|---|
| Query a date range | **Whole series only** — "the minimum download unit is all data from one parameter / one station" |
| One form, many stations | One dataset = **one station × one parameter** |
| Free with registration | Free for **non-commercial use, limited to 50 datasets**; commercial is priced |
| Near-current data | **Archived only** — published monthly, running roughly two weeks behind |

**Licence:** Non-commercial Use, $0. This is a non-commercial
project, which is what makes that the applicable option. Anything
commercial needs a different licence and a fee.

**Redistribution:** the CSVs live in `data/raw/niwa/`, which is gitignored
(`.gitignore:64`). They are **not** committed, and must not be — the licence
covers use, not redistribution.

---

## The four stations

One per `climate_region` that carries a meaningful share of the cohort. 502 of
the 509 ICPs sit in these four; the remainder are one or two fixture households
each.

| `climate_region` | ICPs | Station | Agent | Temperature record |
|---|---:|---|---:|---|
| Auckland | 125 | `AUCKLAND AERO` | 1962 | 1 Jan 1966 — 17 Aug 2026 |
| Waikato | 127 | `HAMILTON AERO AWS` | 2112 | 1 Jun 1990 — 17 Aug 2026 |
| Wellington | 140 | `WELLINGTON, KELBURN AWS` | 25354 | 29 Apr 2004 — 17 Aug 2026 |
| Christchurch | 110 | `CHRISTCHURCH AERO` | 4843 | 1 Jan 1954 — 17 Aug 2026 |

**`AWS` does not mean "the current station", and each one had to be checked.**
The suffix marks an automatic weather station, not a live one:

- `AUCKLAND AERO AWS` (10330) and `CHRISTCHURCH AERO AWS` (10332) hold **Rain
  only, ending 25 Jul 2013**. The live temperature records are on the plain
  names.
- `WELLINGTON, KELBURN` (3385) is the opposite: temperature ends **1 Sept
  2005**, and the live record is on `KELBURN AWS`.

Picking by name alone would have produced a source with a decade-wide hole and
no error anywhere.

---

## A station is not a region — `[ASSUMED]`

Mapping one station's temperature onto every household in a region is an
**assumption**, in the same class as Stage 2's Acorn mapping, and it is
recorded here rather than presented as measurement.

Kelburn is 140 metres above sea level and windier than most of Wellington;
Auckland Aero is at Māngere, on the coast, seven metres up. A household in the
Waitākere hills does not experience Māngere's temperature. What the mapping
buys is a **defensible regional signal** — the seasonal swing and the cold
snaps are real and correctly timed — not a per-household one.

The households are synthetic (LCL, shifted from London). The temperatures are
not. The join between them is the assumption, and any claim that a particular
household's heating response is realistic is confined to that caveat.

**Fixtures keep their own weather.** Fixtures 02–09 were written against
synthesised temperatures — 845 fixture-days between 2023-06-01 and 2024-08-31
— and their expected bill explanations are computed from them; fixture 03's
`$61.92` weather attribution is one. Those temperatures sit in each fixture's
hand-built `kea.mart_daily_usage` insert, the half `seed_fixtures.sh` drops, so
the warehouse never saw them and explained the fixtures against NIWA instead:
the wrong winter for fixture 03, and no weather at all outside the four station
regions (issue #143).

`pipeline/scripts/gen_fixture_weather.sh` (`make fixture-weather`) loads the
nine files into a throwaway Postgres and writes those temperatures to the dbt
seed `fixture_weather_daily`, keyed per ICP because fixtures 05 and 08 share
Otago and overlapping dates. `mart_daily_usage` takes a fixture's own
temperature where the seed has that ICP and day, NIWA otherwise, and applies
the same base-16 HDD to either. All four NIWA records reach back before 2023,
so it is this precedence, not a gap between the date ranges, that keeps real
weather off a fixture's bill. Fixture 01 is a real property with no synthetic
weather; it is not in the seed and stays on NIWA Auckland.

The six minor regions — Otago, Hawke's Bay, Canterbury, Tasman, Taranaki,
Northland, one or two fixture ICPs each — are mapped to the nearest of the four
rather than given stations of their own. Four datasets against the 50-dataset
allowance is cheap; ten would still be an assumption, just a more expensive one.
The map is the seed `climate_region_weather_region`, surfaced as
`dim_icp.weather_region`, which every weather join uses:

| Region | Station | Measured from | Distance |
|---|---|---|---:|
| Canterbury | Christchurch | Riccarton | 6 km |
| Tasman | Wellington | Richmond | 133 km |
| Taranaki | Waikato | New Plymouth | 172 km |
| Northland | Auckland | Kerikeri | 212 km |
| Hawke's Bay | Waikato | Napier | 227 km |
| Otago | Christchurch | Dunedin | 310 km |

Otago's 310 km is the weakest link: the mapping supplies a season and the
timing of its cold snaps, not Otago's climate. For the fixtures it reaches only
what the seed does not cover — the forecast's climate normals, and any reading
day outside a fixture's own weather.

---

## The gaps, and the rule for them

`mart_daily_usage` needs a temperature for **every household-day**, or
`heating_degree_days` goes null and §8.5's whole weather driver silently drops
out of the bill explanation. So the gaps are not incidental.

### 1. The tail — 11 days, structural

Every series ends **17 Aug 2026**. The cohort's readings run to **28 Aug 2026**.
DataHub publishes monthly and runs about two weeks behind, so the last 11 days
of the span have no temperature and will not until the next release.

**Decision: take the gap rather than move the span.** `NZ_SPAN_END` is a pinned
literal that the entire UK→NZ shift derives from (`uk_to_nz.py`), so narrowing
it to 2026-08-17 means regenerating 19M readings, re-rating every invoice and
re-running Stage 3 end to end — to recover eleven days at the very edge of the
window. Taken 2 Sep 2026.

### 2. In-series gaps — measured from the delivered files

Counted by `load_weather.py --report` over the 710-day span. A day counts as
present only if it has a **mean**: fifteen rows across the four files exist
with no mean and no recoverable max/min, and counting those as present would
overstate coverage by exactly the days the fill rule has to handle.

| Region | Usable | Coverage | Inner gaps | Tail |
|---|---:|---:|---:|---:|
| Auckland | 694 | 97.7% | 5 | 11 |
| Waikato | 690 | 97.2% | 9 | 11 |
| Wellington | 695 | 97.9% | 4 | 11 |
| Christchurch | 697 | 98.2% | 2 | 11 |

```
Auckland      2024-11-11, 2025-03-30, 2025-06-20, 2025-06-21, 2025-06-22
Waikato       2024-10-26, 2024-11-16, 2025-03-07, 2026-07-16, 2026-07-17,
              2026-07-27, 2026-07-28, 2026-07-30, 2026-08-01
Wellington    2024-11-02, 2024-11-03, 2024-11-04, 2024-11-05
Christchurch  2024-12-18, 2025-05-10
```

**The four never miss the same inner day**, so nearest-station fill genuinely
works and the rule below is sufficient. `load_weather.py` re-checks that on
every run and says so explicitly if it ever stops being true — a later NIWA
release could introduce a day nothing covers, and that would change the rule
rather than merely widen a gap.

⚠️ An earlier version of this file listed Auckland's gaps as `2025-03-29` and
`2025-06-20 — 21`, read off DataHub's catalogue preview. Those were **wrong by
a day**, for the same 9am/UTC reason described below: the preview and the file
do not use the same date convention. The table above comes from the files.

### The rule

1. A missing day is **filled from the nearest of the other three stations**,
   and the filled row is marked as such — never silently interpolated, and
   never left null.
2. A day with no temperature at *any* station leaves `heating_degree_days`
   null, and `mart_bill_explanation.is_explainable` must be false for any
   period containing one. A bill explained from a weather driver with holes in
   it is exactly the confident-but-wrong answer §6.3 exists to prevent.
3. The fill is recorded per row, so "how much of this bill's weather figure was
   substituted" is answerable rather than a matter of trust.

---

## The date rule — the one thing that fails silently

NIWA stamps each row with the instant of the **9am observation, in UTC**:

```
1965-12-31T21:00:00Z  ->  09:00 NZST on 1966-01-01   DataHub labels it "1 Jan 1966"
2026-08-16T21:00:00Z  ->  09:00 NZST on 2026-08-17   DataHub labels it "17 Aug 2026"
```

So **the NZ date is the `Pacific/Auckland` date of that instant** — one day
later than the UTC date. Confirmed against DataHub's own labelling at both ends
of the Auckland series rather than assumed.

The two clock times in the files, `21:00Z` and `20:00Z`, are **daylight saving**
(NZST +12, NZDT +13), not two readings a day. Every row is `Frequency = D` and
no station has more than one row for any date.

Reading the UTC date instead would shift every temperature back a day. No
constraint would catch it, and every cold snap in every bill explanation would
move. `src_weather.daily_observations` keeps `observed_at_utc` beside the
derived `obs_date` so the conversion stays auditable.

## Where to look at it

The Stage 3 dashboard (`make lake-dashboard`) plots it from the **parsed lake**, not from
`src_weather` — so what is on screen is the pipeline's copy, not the loader's. Four panels:
the weekly mean by region, monthly heating degree days, the gaps below, and the stations.

Two things it confirms at a glance. February is the warmest month at 18.6 °C and July–August
the coldest at 9.2 °C, so the series is southern-hemisphere. And HDD runs Christchurch 3,049
→ Wellington 1,903 → Waikato 1,772 → Auckland 939 — the south-to-north gradient the mapping
predicts, with Auckland's 16.5 °C mean sitting above the base-16 threshold and so barely
accumulating any.

⚠️ It cannot show the 9am/UTC day shift. At any resolution the curve looks right; that one
is settled against DataHub's own labelling, below, not by eye.

## Where it is computed, now that it is

`gold.fct_weather_daily` is the **only** place in the warehouse that applies
§8.4's `GREATEST(16.0 - temp_c, 0)`. One row per (climate_region, day), and
`mart_daily_usage` denormalises it onto the household-day through
`dim_icp.climate_region`.

**NULL, never 0, where there is no temperature.** `greatest()` skips nulls in
Snowflake, so the obvious `greatest(16 - temp, 0)` returns `0.00` for a day with
no reading — indistinguishable from a warm day, and it sums straight into a
monthly total. That is the arithmetic behind the rule below, so it is enforced
in SQL and not left to convention.

⚠️ **The nearest-station fill is still not implemented.** `fct_weather_daily`
reports what NIWA published, so a regional gap is a null rather than a
substitution. Measured on the current load: **128 of 69,569 rows** have no mean,
and in `mart_daily_usage` the missing days are overwhelmingly the 11-day
publication tail described above rather than inner gaps. The rule below is still
the specification; nothing implements it yet.

## What HDD is computed from

Design §8.4 fixes the definition: `GREATEST(16.0 - temp_c, 0)`, base 16°C, one
definition throughout.

**NIWA supplies the mean; it is not computed here.** Across 69,422 rows the
`Mean Temperature` column equals `(Tmax + Tmin) / 2` within 0.05 °C on all but
one. That is the meteorological convention for degree days — not an
approximation worth improving on with hourly data, and it is what the fixtures'
`0.93 kWh/HDD` was fitted against. Where the column is blank the arithmetic is
the fallback; where max or min is missing too, the day is absent.

**Hourly data was considered and rejected.** Every temperature column in the
schema is daily or per-period, and `mart_hourly_profile` — the one place it
could land — has no temperature column at all; it describes load shape. Hourly
would cost four more of the fifty free datasets, need ~68,000 rows aggregated
down to the 2,840 daily values already present, and produce a true 24-hour mean
that agrees with neither the convention nor the fixtures.

The API's `heating_degree_days()` in `services/api/app/analytics/engine.py`
defaulted to base **18** against §8.4's 16, with an unsourced docstring calling
18 "the NZ convention". Corrected to 16 on 2 Sep 2026.

It was never live — `routes.py` serves HDD from `fixtures.heating_degree_days`,
a different function of the same name — so the two bases had never disagreed in
production, and an earlier note in this file overstated that they had. The fix
still mattered: the first caller to reach for it would have got 18 and a
docstring telling them it was right, and the test asserted 18, so the suite
would have failed anyone who corrected it.
