# Building the source databases from Low Carbon London

Stage 2 of `design/kea-data-engineering-stack.md`. Turns the Kaggle
[Smart Meters in London](https://www.kaggle.com/datasets/jeanmidev/smart-meters-in-london)
dataset — the Low Carbon London trial, ~5,567 UK households on half-hourly
metering, Nov 2011 – Feb 2014 — into three populated NZ source systems.

    make init-data          # default 50-household source profile
    make init-data COHORT_SIZE=500   # optional full scale profile
    make test-transform     # the UK -> NZ transform; no database, no dataset
    make assert-source      # the Stage 2 gate

## What it produces

| | |
|---|---|
| `src_metering` | 50 generated households and about **2.14M** delivered rows over the complete two-year source span, plus the nine fixed Sprint 2 fixtures |
| `src_catalogue` | 8 generated plan versions (4 products × Vector × Standard User × 2 versions), explicit solar buy-back rates, plus 2 term-credit rows |
| `src_billing` | 50 generated customers/accounts, about 1,224 invoices priced from the catalogue, about 1,184 payment events, and one `account_plan_history` row each |
| `gen_audit` | run log, defect manifest, Sprint 4's planted answers and staging — **not** a `src_*` schema, on purpose |

⚠️ **`make init-data` does not refresh the `gen_audit` rollups, and nothing else does either.**
`mv_daily`, `mv_bills` and `mv_profile` are MATERIALIZED VIEWS behind the Stage 2 dashboard;
they keep serving the previous generation's data indefinitely, and the dashboard looks
perfectly healthy while showing a span that no longer exists. After the 2027 span move
`mv_bills` still held 11,506 rows from the old span — a figure this repo's own documents were
still quoting. Run **`make source-dashboard`** after every regeneration.

## Order

| File | Does |
|---|---|
| `fetch_lcl.sh` | download + sha256 + `PROVENANCE.md`; skips if unchanged |
| `probe_tstp.py` | settles empirically whether `tstp` is UTC or London local |
| `uk_to_nz.py` | derives the shift, builds NZ local days, handles both zones' DST |
| `cohort.py` | coverage scan, window choice, deterministic 50-household demo pick |
| `planted.py` | pure: which households get a planted EV or hot-water load, and their sessions |
| `gen_metering.py` | `--stats` measures the scale factor; then screens the planting candidates' own load and writes `reads.csv.gz` (planted loads added) and `planted_loads.csv.gz` |
| `gen_reference.py` | connection points, meters, registers |
| `gen_billing.py` | customers, accounts, invoice **periods** — never amounts |
| `01_schemas.sql` | `gen_audit` |
| `02_catalogue.sql` | the three Sprint 3 screenshot plans and their exact rates |
| `03_reference.sql` | resolves staged business keys to local ids |
| `05_defects.sql` | source defects, and the run row everything after it is stamped with |
| `09_planted.sql` | `gen_audit.planted_load` from the staged sessions |
| `06_revisions.sql` | late read revisions |
| `04_rate_bills.sql` | the rating engine, and the driver over the cohort |
| `08_payments.sql` | payment profiles, including Sprint 4's sliding accounts (`gen_audit.planted_hardship`) |
| `07_billing_defects.sql` | orphan ICPs and plan codes |
| `99_assert.sql` | the gate |
| `load_source.sh` | runs the SQL above, in that order, against `kea-source-db`; stops at the first failed step |

## Planted answers (Sprint 4)

LCL has no EVs and no household that slides into arrears, so the generator
plants known answers for the Sprint 4 detectors to find. Design §15 and
`docs/sprints/sprint-4/data-plan.md` give the why; the rules here:

- **Loads** (`planted.py`, added in `gen_metering.py` before the solar split):
  3 households with a 7.2 kW wallbox (2 evening, 1 on a 23:00 timer) and 1 with
  a 2.3 kW portable charger, each arriving on a date in the served window at
  least six months before the span ends; 4 more with a 3 kW hot-water element
  on a 23:00 timer every night, drawing more in winter. Households are picked
  by a hash of the LCL id from those **without solar** and **not targeted by
  any source defect** (05 §8/§9, 07's five orphans) — 35 candidates in the
  50-household profile. Sessions depend only on the calendar, never on the
  reads, so a day LCL has no reading for still has its session in the answer
  key. `--stats` plants nothing: `scale.json` is measured on LCL alone.
- **Clean labels** (`planted.screen()`): scaled ×1.72, a UK heater or
  cylinder already looks like a 5 kW charger, so a planted EV in such a
  household could not be told from its own load. Before planting,
  `gen_metering.py` reads the candidates' blocks once more (about 20 s) and
  runs the detect-v1 run rules (`planted.DETECT_V1`, which a test keeps equal
  to the dbt vars) over each household's own NZ-dated load:
  - an **EV** place needs EV-shaped runs (`high_power`, or the `low_power`
    shape) on at most 0.5% of the assessed days;
  - a **hot-water** place needs the same of the load with the element
    added, not counting the element's own runs, and its element read as a
    23:00 timer (not high-power, not moved an hour early) on at least 98% of
    the assessed nights.

  Hot-water places are filled first, from the pool order above, then the EV
  places; a household that fails is skipped, and too few clean ones stop the
  run. In the 50-household profile 10 candidates are clean for an EV and 7
  for an element. The screen is the one choice that reads LCL, and it only
  ever leaves a household out.
- **Payments** (`08_payments.sql`): 3 `SLIDING` accounts (on time; a bill at
  ≥ 130% of the median of the previous six; payments drifting later; then only
  about the usual amount paid, so arrears build) and 1 `SLIDING_SUDDEN` account
  (on time, then stops at an ordinary bill — the early warning is expected to
  miss it). Taken only from `PAY_ON_TIME`, so every Sprint 3 profile keeps its
  accounts, and never from a defect target.
- **Answer keys**: `gen_audit.planted_load` (one row per session) and
  `gen_audit.planted_hardship` (trigger bill, first drifted payment, arrears
  onset = the first date a bill is still ≥ 5% unpaid 30 days after due). Both
  are replaced on each load and never reach serving. `99_assert.sql` §15
  checks the counts and the exclusions.

## Dry-running the load

Two environment variables move the whole load off the real database and
directory; their defaults are today's values.

| Variable | Default | Used by |
|---|---|---|
| `KEA_GEN_OUT` | `raw/lcl` | the three generators' output, `load_source.sh`'s input |
| `KEA_SOURCE_PSQL` | `docker compose exec -T kea-source-db psql …` | `load_source.sh`, `pipeline/scripts/seed_fixtures.sh` |

`KEA_SOURCE_PSQL` replaces the whole command, so it must carry
`-v ON_ERROR_STOP=1` itself. The LCL input (`raw/lcl/extracted`) and the
committed `cohort.json`/`scale.json` are always read where they are.

```bash
export KEA_GEN_OUT=/some/scratch/lcl
export KEA_SOURCE_PSQL="docker exec -i <throwaway> psql -X -v ON_ERROR_STOP=1 -U kea -d kea"
docker exec -i <throwaway> psql -U kea -d kea < design/kea_source_schema.sql
pipeline/scripts/seed_fixtures.sh
python3 pipeline/scripts/gen_source/gen_metering.py    # then gen_reference.py, gen_billing.py
pipeline/scripts/gen_source/load_source.sh
```

## Four things that are easy to get wrong and silent when wrong

**A local day is 46, 48 or 50 half-hours.** Never 48 by assumption. Python's
arithmetic on two aware datetimes sharing a `tzinfo` is *wall-clock*, not
elapsed: subtracting consecutive local midnights returns exactly 1 day even
across a transition. That produced 48 for every day in the span — plausible
enough to ship. Everything in `uk_to_nz.py` converts to UTC before doing
arithmetic, which is what makes the 46 and the 50 appear.

**The shift is derived and asserted, never hardcoded.** It must be a whole
number of weeks (day-of-week drives load shape and TOU windows), must land
half a year off (the season flip), and must be positive. `NZ_SPAN_END` is a
pinned literal: `date.today()` would give a different shift every day and
nothing would reproduce.

**`received_at` is not `now()`.** The entire incremental-extract design keys
off it. Stamping rows with the load time collapses 710 days of arrivals into
one and makes the watermark untestable. Rows arrive the morning after, with
jitter and a late tail — 714 distinct arrival days.

**Pricing does not happen in Python.** `gen_billing.py` decides who is billed
and for which periods; every dollar comes from `04_rate_bills.sql` joining
`src_catalogue`. Amounts computed in Python could not then be what the
pipeline is tested against.

## Provenance

Rates are marked inline in `02_catalogue.sql` as `[BILL]`, `[PUBLISHED]`,
`[DERIVED]` or `[ASSUMED]`. Only one of the 8 plan versions rests entirely on a
printed invoice. See `data/dictionary/lcl-source-data.md` — it also records the
scale factor, the UK/NZ daylight-saving collision, and why the EA levy line
cannot be reproduced exactly.

## Re-running

Everything is idempotent and deterministic. The cohort is a hash against a
fixed seed; reads are deleted by `source_file LIKE 'lcl-%'` (which cannot reach
the nine fixtures); reference, catalogue and billing rows upsert on their
business key; generated catalogue ids are ≥ 1,000,000, so a re-run's `DELETE`
cannot touch a fixture id (≤ 339,999).

**Defect and revision injection hashes the row, and `setseed()` is not enough.**
The first version of `05_defects.sql` and `06_revisions.sql` used
`SELECT setseed(...)` before `random()`. That fixes the random *stream*, not
which row draws from it: `random()` is evaluated per row in scan order, and
Postgres may parallelise a sequential scan over 19 M rows, giving each worker
its own state. Two runs on byte-identical input produced 359,160 and 359,377
revised rows — close enough to look correct, never the same twice.

Every selection now goes through `gen_audit.h(key)`, an `IMMUTABLE` hash of the
row's own business key returning `[0, 1)`. The draw becomes a property of the
row rather than of the plan, so scan order, parallelism and worker count stop
mattering. It is the SQL equivalent of what `cohort.py` already does to pick
households.

**`05_defects.sql` is still not idempotent against itself** — it DELETEs reads
and UPDATEs kwh, so running it twice cuts twice the gaps and re-multiplies the
spikes. Deterministic and idempotent are different properties: it must run
against a fresh load, which is the order `load_source.sh` uses.

`raw/` and `coverage.json` are gitignored. `cohort.json` and `scale.json` are
committed: they *are* the selection and the measured constant.
