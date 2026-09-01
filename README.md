# data/ — Data Engineering / Data Analysis team

The meter data, what it means, and the analysis done on it.

## Why the analytics code is not in here

`app/analytics/` and `app/db/` live in `services/api/` because the API imports them at
request time. The Data team **owns those paths** via `.github/CODEOWNERS` — ownership is
a review rule, not a directory. Every headline number in the pitch comes out of
`app/analytics/engine.py`, and the Data team reviews changes to it.

This directory is for the things that are not runtime code.

| Path | What goes in it |
|---|---|
| `raw/` | Source extracts as received. **Never edited by hand** — if it needs fixing, the fix is a script |
| `analysis/` | Notebooks and one-off scripts. Name them `YYYY-MM-DD-what-it-answers` |
| `dictionary/` | What each column means, its units, and its known quirks |
| `design/` | The data platform design doc and the DDL it describes. The DDL is split by **writer**, not by subject — a file's owner is who writes the data, which is what makes "can we rebuild this?" answerable |
| `sample/` | Runnable seed fixtures — nine of them, each idempotent and each asserting its own totals. See [The sample fixtures](#the-sample-fixtures) below |

## Rules

**Every figure traces to a source.** This is the brief's standing rule and it applies to
a notebook cell exactly as much as to a slide. If a number leaves this directory and
lands in a deck or a demo line, someone must be able to follow it back to the row it
came from.

**Dollar figures are illustrative.** Kea's real tariff is not public. Everything costs
out at **30c/kWh** and any surface showing a `$` has to say so.

**Never name an appliance.** Half-hourly interval data cannot identify appliances. No
amount of clustering changes that — the refusal is deliberate and it is a demo line, not
a limitation to hide.

**No respondent data in this repo.** The customer survey contains data collected for
analysis, not publication. Aggregates and derived figures are fine; raw responses are
not. See the absolute rule in [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

## The sample fixtures

Nine runnable seeds in `sample/`. Apply `design/kea_source_schema.sql` and
`design/kea_serving_schema.sql` first, then run any file — or all of them, in any order,
into the same database. Each one owns a private block of ids and its own network,
household and plan, so they never collide.

| File | Household | Period | Bills | What it is for |
|---|---|---|---|---|
| `01-bill_printed_invoice_2024-08.sql` | `0165841877LCC54` | 24 days | 1 | The real printed invoice, reproduced to the cent (**$50.54**). The only fixture with solar — two registers, import and export |
| `02-bill_higher_full_house.sql` | `DEMO…0201AA` | 2 months | 2 | Two more people at home. Daytime occupancy has no column either, so again **unexplained** |
| `03-bill_higher_cold_snap.sql` | `DEMO…0301AA` | Mar + Jun | 2 | A mild March against a cold June. Nothing changed in the house, so **weather** should take almost all of it |
| `04-bill_higher_faulty_appliance.sql` | `DEMO…0401AA` | 2 months | 2 | A cylinder element stuck on from 8 June. A fault draws the same power at 3am as at 3pm, so it lands in **base load** |
| `05-bill_higher_new_appliance.sql` | `DEMO…0501AA` | 2 months | 2 | A dryer on a 9pm timer. Real, deliberate, and still **unexplained** — no driver column measures "bought a dryer" |
| `06-bill_higher_estimated_reads.sql` | `DEMO…0601AA` | 2 months | 2 | The meter stopped reporting and the estimates run high. The decomposition looks clean and `is_explainable` is still **false** |
| `07-bill_higher_longer_period.sql` | `DEMO…0701AA` | 30 + 43 days | 2 | A delayed billing run. **Period length**, and the trap: the daily rate barely moved |
| `08-bill_13_month_history.sql` | `DEMO…0801AA` | **397 days** | 13 | More than a year of half-hourly reads, so the same calendar month appears twice and a year-on-year comparison is possible at all |
| `09-bill_mid_cycle_forecast.sql` | `DEMO…0901AA` | **in flight** | 2 | The only fixture whose period has not ended. Reads stop 10 days in, and `mart_forecast_input` / `bill_forecast` carry a daily snapshot each — the band narrowing as fact displaces forecast |

### Fixture ids

Every seeded primary key is `<table><fixture><serial>`, so an id read out of a
query says which table it came from without a lookup.

```
  23 9 002
  |  | +--- serial within this table and fixture
  |  +----- fixture number, 1..9
  +-------- table; the first digit is the schema
```

| | metering | | billing | | catalogue |
|---|---|---|---|---|---|
| `11` | network | `21` | customer | `31` | plan |
| `12` | property | `22` | account | `32` | tou_band |
| `13` | icp | `23` | bill | `33` | tariff_component |
| `14` | meter | `24` | bill_line | | |
| `15` | register | | | | |

So `239002` is the second bill of fixture 9, and `329011` is that fixture's
eleventh TOU band.

**The fixture number is also the file's `NN-` prefix**, so there is one number
to remember rather than two: `06-bill_higher_estimated_reads.sql` seeds
`DEMO…0601AA`, account `226001` and bills `236001`/`236002`, and a row carrying
any of those came from that file and no other. `generate_scenarios.py` derives
the filename from the fixture number rather than keeping a second table, so the
two cannot drift apart. `01` is the hand-written printed invoice, which the
generator does not produce.

Before this, every table in a fixture shared one number — `900001` was at once
the customer, the ICP, the meter, the plan, the account and the bill. A stray id
in a result set could not be attributed, and it let a `DELETE` key on the wrong
table without anyone noticing; one did. `generate_scenarios.py` allocates these
through `oid()`, so the generated fixtures cannot drift from the scheme.

### What makes them safe to trust

**Every file asserts its own totals.** The closing `DO` block re-derives the bill from
the reads and raises if the two disagree, so a fixture that has quietly stopped
demonstrating what it claims fails the transaction instead of loading wrong data. The
six `bill_higher_*` files go further and assert *which driver wins* and *whether
`is_explainable` comes out true* — a scenario that no longer teaches its lesson is a
failure, not a surprise.

**Charges are rated from the meter, not typed.** Except for the printed invoice, every
bill line is derived by banding the half hours and applying the rate, so the bill
reconciles to the reads by construction and retuning the read shape flows straight
through to the charges.

**Re-running changes nothing.** Every write is a scoped `DELETE`-then-`INSERT` or an
`ON CONFLICT DO UPDATE`.

**Times are New Zealand local.** `interval_start` is `timestamptz` stored as UTC; every
date and hour is derived through `AT TIME ZONE 'Pacific/Auckland'`. This matters more
than it sounds: a local day is **46 or 50 half hours** across a DST change, never
assume 48. `expected_intervals` is computed from the actual local day length for exactly
that reason, and `08-bill_13_month_history.sql` is the fixture that crosses both
transitions — 24 September 2023 and 7 April 2024.

### What the fixtures do *not* license the explainer to say

Each file names a cause, because a generator knows what it generated. **The pipeline
does not know and must not claim to.** Four drivers are measurable — period length,
weather, base load, tariff — and everything else lands in `delta_unexplained`. Two of the
six fixtures make **unexplained** the largest driver, and a third comes out not
explainable despite a clean-looking decomposition. That is deliberate: the honest answer
there is "I can account for $X of the $Y increase; the rest is daytime usage — has
something changed at home?" rather than a confident wrong cause.

### Regenerating

`generate_scenarios.py` writes all seven generated files; the printed invoice is
hand-maintained and is not generated.

```
cd data/sample && python3 generate_scenarios.py
```

Standard library only, deterministic — same input, byte-identical output. Edit the
`SCENARIOS` list or `HISTORY` config and regenerate; do not hand-edit the generated
`.sql`.

## The July measurement

The analytics engine is validated against real measured July values, not fixtures —
`services/api/tests/data/july_2026_daily.json` is the reference, and
`test_analytics_july.py` fails if the engine drifts from it. Treat that file as a
fixed point: if a change makes that test fail, the change is wrong until proven
otherwise.

## Related

- [`../docs/erd.md`](../docs/erd.md) — the data model
- [`../docs/adr/0006-postgres-in-sprint-2.md`](../docs/adr/0006-postgres-in-sprint-2.md) — why Postgres
- [`../docs/teams/data.md`](../docs/teams/data.md) — team onboarding
