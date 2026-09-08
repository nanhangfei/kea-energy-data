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
| `gen_audit` | run log, defect manifest and staging — **not** a `src_*` schema, on purpose |

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
| `gen_metering.py` | `--stats` measures the scale factor, then writes `reads.csv.gz` |
| `gen_reference.py` | connection points, meters, registers |
| `gen_billing.py` | customers, accounts, invoice **periods** — never amounts |
| `01_schemas.sql` | `gen_audit` |
| `02_catalogue.sql` | the three Sprint 3 screenshot plans and their exact rates |
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
