# NIWA weather — the temperature series behind every weather figure

Daily temperature from NIWA drives `heating_degree_days` in `mart_daily_usage`, and through it the
weather driver in bill explanations (design §8.5) and the expected weather in forecasts (design §8.6).
This document records where the data comes from, how it maps onto households, how gaps are handled, and
the one date rule that fails silently.

## Provenance

**Source:** NIWA National Climate Database, via **DataHub** (`data.niwa.co.nz`). The older CliFlo
service is retired, and DataHub differs in ways that matter:

| | DataHub |
|---|---|
| Unit of download | the whole series for one station × one parameter; no date-range queries |
| Freshness | archived monthly, about two weeks behind |
| Licence | free for **non-commercial use**, up to 50 datasets; commercial use is priced |

**Redistribution:** the licence covers use, not redistribution. The archives live in `raw/niwa/`
(gitignored), are downloaded by a person who accepts the licence per dataset, and are **never
committed**. `make init-sources` loads them when present and reports that it skipped weather when not.

### The four archives

Retrieved 2026-10-14 (DataHub version 0.1, updated 18 Sep 2026). This table is the only tracked
record of what was fetched; verify a local copy with `shasum -a 256 raw/niwa/*.zip`.

| Archive | Station agent | SHA-256 |
|---|---:|---|
| `1962__Temperature__daily.zip` | 1962 | `6788e13f6b5892951e843436a8c69486c0ac40fc6c8e11590c89bb088957f50c` |
| `2112__Temperature__daily.zip` | 2112 | `6365f171265e471c45c5232694e07acf63903a5c198c632db1d9d983633d3cc9` |
| `25354__Temperature__daily.zip` | 25354 | `1d75e4cc09135079bb20ddbd7a5aa88e4ae03643e437f78a81f6c550491b2252` |
| `4843__Temperature__daily.zip` | 4843 | `16498bec9dc01f7ccb7c3f808d8d2a6974af971f84df455c007f74e8ce5c02b5` |

The loader reads the one zip per station in `raw/niwa/`, so a re-download replaces the old archives
there. The previous retrieval (2 Sep 2026, ending 17 Aug 2026) is kept beside them in
`raw/niwa/archive-2026-09-02/`, with its delivered names (`…_AUCKLAND.zip`, and a Hamilton name with
a space before `.zip`).

## The four stations

| Climate region | Station | Agent | Temperature record |
|---|---|---:|---|
| Auckland | `AUCKLAND AERO` | 1962 | 1966 – 16 Sep 2026 |
| Waikato | `HAMILTON AERO AWS` | 2112 | 1990 – 16 Sep 2026 |
| Wellington | `WELLINGTON, KELBURN AWS` | 25354 | 2004 – 16 Sep 2026 |
| Christchurch | `CHRISTCHURCH AERO` | 4843 | 1954 – 16 Sep 2026 |

The generated cohort is entirely in the Auckland region; the other three stations serve the fixtures
and keep the model general.

**Station names had to be checked one by one.** `AWS` marks an automatic station, not the live one:
`AUCKLAND AERO AWS` and `CHRISTCHURCH AERO AWS` hold only rainfall ending in 2013, while for Wellington
the live temperature record is on `KELBURN AWS` and the plain `KELBURN` ended in 2005. Picking by name
would have produced a decade-wide hole with no error.

## A station is not a region `[ASSUMED]`

Mapping one station onto every household in a region is an assumption. Auckland Aero sits on the coast
seven metres above sea level; a household in the Waitākere hills is colder. The mapping gives a
**defensible regional signal** (real seasons, correctly timed cold snaps), not a per-household one.

Minor regions (used only by fixtures) map to the nearest of the four stations through the dbt seed
`climate_region_weather_region`, surfaced as `dim_icp.weather_region`:

| Region | Station | Distance |
|---|---|---:|
| Canterbury | Christchurch | 6 km |
| Tasman | Wellington | 133 km |
| Taranaki | Waikato | 172 km |
| Northland | Auckland | 212 km |
| Hawke's Bay | Waikato | 227 km |
| Otago | Christchurch | 310 km |

**Fixtures keep their own weather.** Fixtures 02–09 were written against synthesised temperatures, and
their expected explanations depend on them. `gen_fixture_weather.sh` extracts those temperatures into
the dbt seed `fixture_weather_daily` (checked on every warehouse build, regenerated with
`make build-warehouse REGEN=1`). `mart_daily_usage` uses a fixture's own temperature where the seed has
that ICP and day, and NIWA otherwise.

## The date rule — the one thing that fails silently

NIWA stamps each daily row with the instant of the **9am observation, in UTC**:

```
2026-08-16T21:00:00Z  ->  09:00 NZST on 2026-08-17   (DataHub labels it 17 Aug 2026)
```

So the observation's date is the **`Pacific/Auckland` date of that instant**, one day after its UTC
date. Confirmed against DataHub's own labels at both ends of a series. The two clock times in the files
(`21:00Z` and `20:00Z`) are standard and daylight time, not two readings a day.

Reading the UTC date would move every temperature back a day. No constraint would catch it, and every
cold snap in every bill explanation would shift. `src_weather.daily_observations` keeps
`observed_at_utc` beside the derived `obs_date` so the conversion stays auditable.

## Computing heating degree days

`gold.fct_weather_daily` is the **only** place that applies design §8.4's definition,
`GREATEST(16.0 − mean_temp_c, 0)`, at one row per region and day; `mart_daily_usage` joins it to each
household-day.

- **NIWA supplies the mean.** It equals `(max + min) / 2` within 0.05 °C on all but one of 69,422 rows,
  the meteorological convention for degree days. Where the mean is blank the arithmetic is the
  fallback; where max or min is missing too, the day is absent.
- **NULL, never 0, without a temperature.** Snowflake's `greatest()` skips nulls, so the obvious
  expression returns 0 for a missing day, indistinguishable from a warm one. The model guards against it
  explicitly.
- **Hourly data was rejected.** Nothing in the model consumes hourly temperature, it would cost four
  more of the fifty free datasets, and its true 24-hour mean would disagree with the convention.

## Gaps, and the rule for them

**The publication tail.** Every series ends on **16 Sep 2026**, while the cohort's readings run to the
pinned 27 Aug 2027. Days after the last observation have no temperature:
- **forecasts** use 30-year climate normals per region and day of year (`int_weather__climate_normals`);
- **bill explanations** for periods without complete weather are marked not explainable.

The span is not narrowed to fit the weather, because the whole UK→NZ shift derives from its pinned end.

**Inner gaps.** Measured at the first retrieval over Sept 2024 – Aug 2026: coverage 97.2–98.2% per
station, with 2 to 9 missing inner days each. The 14 Oct 2026 retrieval added a month with none. The four stations **never miss the same day**, and
`load_niwa_weather.py --report` re-checks that on every load.

**The rule:**
1. A day with no temperature leaves `heating_degree_days` NULL.
2. A bill period containing one is **not explainable**. A weather driver with holes in it is the
   confident-but-wrong answer the platform exists to avoid.
3. Specified but not yet implemented: fill a missing inner day from the nearest other station, marked
   per row as filled. Until then, gaps stay NULL (128 of 69,689 rows).

## Where to see it

The Lake dashboard (`make dashboards ONLY=lake`) plots weather from the parsed lake: weekly means by
region, monthly HDD, gaps and stations. It confirms the series is southern-hemisphere (February warmest
at 18.6 °C, July–August coldest at 9.2 °C) and that HDD runs south to north as expected: Christchurch
3,049, Wellington 1,903, Waikato 1,772, Auckland 939. It cannot show the 9am/UTC date shift; that rule is
settled against DataHub's labels, not by eye.
