# Building the source databases from Low Carbon London

Stage 2 of `design/kea-data-engineering-stack.md`. Turns the Kaggle
[Smart Meters in London](https://www.kaggle.com/datasets/jeanmidev/smart-meters-in-london)
dataset — the Low Carbon London trial, ~5,567 UK households on half-hourly
metering, Nov 2011 – Feb 2014 — into three populated NZ source systems.

    make init-data          # fetch, generate, load
    make test-transform     # the UK -> NZ transform; no database, no dataset
    make assert-source      # the Stage 2 gate

## What it produces

| | |
|---|---|
| `src_metering` | 500 households, **18,796,453** reads over 2024-09-18 → 2026-08-28, becoming 19,125,023 after revisions and duplicate deliveries |
| `src_catalogue` | 64 plans (4 Ecotricity products × 4 networks × 2 user categories × 2 versions), 624 windows, 704 rate components |
| `src_billing` | 500 customers and accounts, 11,506 invoices priced from the catalogue — ecoANYTIME 328, ecoSAVER 74, ecoSOLAR 53, ecoWHOLESALE 45 |
| `gen_audit` | run log, 92,808-row defect manifest, staging — **not** a `src_*` schema, on purpose |

## Order

| File | Does |
|---|---|
| `fetch_lcl.sh` | download + sha256 + `PROVENANCE.md`; skips if unchanged |
| `probe_tstp.py` | settles empirically whether `tstp` is UTC or London local |
| `uk_to_nz.py` | derives the shift, builds NZ local days, handles both zones' DST |
| `cohort.py` | coverage scan, window choice, deterministic 500-household pick |
| `gen_metering.py` | `--stats` measures the scale factor, then writes `reads.csv.gz` |
| `gen_reference.py` | connection points, meters, registers |
| `gen_billing.py` | customers, accounts, invoice **periods** — never amounts |
| `01_schemas.sql` | `gen_audit` |
| `02_catalogue.sql` | the four plans, with provenance marked per rate |
| `03_reference.sql` | resolves staged business keys to local ids |
| `04_rate_bills.sql` | the rating engine, and the driver over the cohort |
| `99_assert.sql` | the gate |
| `load_source.sh` | runs all of the above against `kea-serving-db` |

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
`[DERIVED]` or `[ASSUMED]`. Only one of the 64 plans rests entirely on a
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
