# data/ — Data Engineering / Data Analysis team

The meter data, what it means, the pipeline that moves it, and the analysis done on it.

## Why the analytics code is not in here

`app/analytics/` and `app/db/` live in `services/api/` because the API imports them at
request time. The Data team **owns those paths** via `.github/CODEOWNERS` — ownership is
a review rule, not a directory. Every headline number in the pitch comes out of
`app/analytics/engine.py`, and the Data team reviews changes to it.

Most of this directory is not runtime code — the exception is `pipeline/`, which the
Data team also owns. Ownership is a convention, not a directory.

| Path | What goes in it |
|---|---|
| `raw/` | Source extracts as received. **Never edited by hand** — if it needs fixing, the fix is a script |
| `analysis/` | Notebooks and one-off scripts. Name them `YYYY-MM-DD-what-it-answers` |
| `dictionary/` | What each column means, its units, and its known quirks |
| `design/` | The two platform documents and the DDL they describe. The DDL is split by **writer**, not by subject — a file's owner is who writes the data, which is what makes "can we rebuild this?" answerable |
| `migrations/serving/` | Immutable, ordered changes to the Data-owned schemas in the backend's database, where the served tables live |
| `sample/` | Runnable seed fixtures — nine of them, each idempotent and each asserting its own totals. See [The sample fixtures](#the-sample-fixtures) below |
| `pipeline/` | The pipeline itself — DAGs, the Spark parse job, dbt models, Soda checks, extract and loader scripts |
| `docker-compose.yml` · `Makefile` · `.env.example` | This project's half of the local stack: Airflow and the pipeline's own Postgres, which holds the four source systems. The served tables are in the backend's `kea-db` (`../infra`) — see [The pipeline](#the-pipeline) |

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

## The pipeline

Two documents in `design/`, and they answer different questions:

| Document | Answers |
|---|---|
| [`kea-data-platform-design.md`](design/kea-data-platform-design.md) | What the pipeline does, and why each non-obvious choice was made — extraction, the warehouse layers, the export, the domain model |
| [`kea-data-engineering-stack.md`](design/kea-data-engineering-stack.md) | What runs it — the tools, their versions, the local stand-ins for AWS, and the build order with an exit criterion per stage |

The shape, in one line:

```
4 source DBs ─► S3 bronze ─► Snowflake (staging → silver → gold → marts) ─► S3 export ─► the backend's Postgres ─► the agent
```

**As of 4 Sep 2026 the chain runs itself, and says so when it does not.** Stages 0–7 are done: bronze on MinIO,
Snowflake, staging → silver → gold → five of §6.5's six marts plus `mart_load_log`, and
the export back down into the backend's Postgres, and Airflow running the whole path
unattended. The compact Sprint 2/3 profile runs `dbt build` with no errors,
and Bill B reproduces from
source at **$50.54** — now at both ends, in the warehouse and in the served tables.
`mart_scale_test` is deferred: a 2.45B-row re-rate against a warehouse holding 9.7M.

**The served tables are not filled by hand.** Every row in them is pipeline output —
22 tables (20 before Sprint 4) and about 1.14M rows in the compact profile, `make export-warehouse && make
publish-serving RELEASE=<id>`, which writes them into the backend's database in one
transaction with the Sprint 3 decisions computed from them. The nine fixtures in
`sample/` are what that output is checked *against*: their assertions were lifted into
`pipeline/scripts/assert_serving.sql`, re-pointed at business keys, because the pipeline
mints its own surrogates and the fixtures' hand-assigned ids no longer exist.

⚠️ **Five of those checks are known divergences, not passes** — three scenarios cannot
blame weather (the fixture households carry ~2 months of reads, so no heating slope fits),
and five 2023 invoices predate their tariff and are quarantined by design §2.1.2. Each is
a property of the source data, recorded in the gate with its cause rather than deleted.

### Running it

`docker-compose.yml` here is the local pipeline stack — MinIO, Spark, Airflow, Marquez,
Prometheus/Grafana, Metabase, and the pipeline's own Postgres on port 5433, which holds the
four source systems and the audit rollups. Its service is `kea-source-db`, renamed
from `kea-serving-db` on 12 Sep 2026. The served tables live in the backend's database, locally
as well as in every shared environment: the Data publisher writes the Data-owned schemas and
the API reads them while owning `app`. Locally that is `kea-db` from `../infra` (`make db` at
the repository root), which containers reach as `host.docker.internal:5432`. Start it first.

```bash
make workflow                    # print the execution order; changes nothing
cp .env.example .env             # then fill it in
make demo-bootstrap              # first run only, against empty volumes
make demo-refresh                # rebuild Sprint 2/3 data and all five dashboards
```

After the warehouse export is ready, publish it into the backend's database with:

```bash
make migrate-serving             # only when migrations/serving has changed; a publish never migrates
make publish-serving RELEASE=<unique-release-id>
```

See [`migrations/serving/README.md`](migrations/serving/README.md) for first-time
migration, existing-database baseline and recovery instructions.

`demo-bootstrap` executes Stage 0 startup, the Stage 1 source schema and fixtures,
the extract roles, the backend's served schemas and publisher roles (`migrate-serving`,
`configure-serving-roles`), and Metabase provisioning, in the required order.
After the first run, `demo-refresh` is the normal entry point; use
`demo-dashboards` when only code-managed Metabase questions changed. The
individual Stage targets remain available for debugging and are grouped by
stage in `make help`.

Once the source databases exist, the pipeline can read them:

```bash
make source-roles                # give the four extract roles a password (once)
make assert-isolation            # prove a cross-system read FAILS -- 20 checks

make extract-metering LOAD_DATE=2026-08-25   # one NZ day -> bronze
make spark-parse      LOAD_DATE=2026-08-25   # bronze -> bronze/parsed, conforming to KEA-047

make assert-bronze               # THE STAGE 3 GATE -- every claim, checked
make assert-idempotent LOAD_DATE=2026-08-25  # replay a load date, compare the content hash
make demo-independence           # break billing, show metering unaffected
```

Spark has two different UIs. `http://localhost:8082` is the standalone cluster
master, useful for worker status and the application list. Completed applications'
Jobs, Stages and SQL DAG views are retained at `http://localhost:18080`. Every
session built by `spark_session.py` writes its compressed event log to
`s3a://kea-lake/_spark/events`; the History Server refreshes that prefix every five
seconds and keeps logs for 30 days. Applications completed before this configuration
was enabled cannot be reconstructed unless their event log was captured separately.

The other sixteen entities are plain scripts, not Spark — design §5.1 sizes the
tool to the job, and standing up a distributed engine for a 73-row table costs
more in startup than the read costs at all. They run inside the Airflow image,
which already has psycopg2 and pyarrow:

```bash
make avsc                        # regenerate the sixteen derived Avro contracts
pipeline/scripts/gen_avsc.sh --check    # ... or fail if any has drifted
```

KEA-047 is hand-written and stays that way: it is the contract §5.3 argues
over, and its `quality` enum encodes a decision. The other sixteen are derived
from `information_schema`, so the Postgres→Avro mapping lives in one reviewable
place instead of sixteen hand-typed files — but they carry §5's reshapes as
**overrides**, because a contract taken straight from the source would be
satisfied by a parse job that did nothing at all.

⚠️ **`make source-seed` is a one-shot seed for an empty database, not a refresh.** It
refuses once the fixtures are present. **After changing a fixture, run `make reseed-fixtures`
and then `make demo-refresh`.** Each file deletes its own hand-assigned ids before inserting
them, but the generated cohort hangs foreign keys off those rows, so the reseed removes the
generated rows first, in the same transaction, and the refresh loads them back. Both keep only
the fixtures' *source* half: each file also writes a hand-built expected
output into the served schemas, which the pipeline's database no longer has, so the seed
creates them as scaffolding inside its own transaction and drops them before it commits. The
expected numbers live on in `assert_serving.sql`. To refresh what is served, the answer is
`make publish-serving RELEASE=<id>`, or `make demo-refresh`.

`make source-roles` is not optional and is easy to miss: `kea_source_schema.sql` creates
the four `kea_extract_*` roles with `LOGIN` and **no password**, deliberately — "set it
out of band from the secret store". Until they have one they cannot connect at all, and
the only working credential is the superuser, which can read all four schemas and would
make the extract's independence a fiction.

**`LOAD_DATE` is a New Zealand calendar day**, and it matters more than it looks. The UTC
window for 2026-08-25 holds 26,623 rows and *none* of the planted 36-day-late revision;
the NZ window holds 26,925 and all 48 of it. A UTC load date still returns thousands of
plausible rows, so nothing looks wrong. The default is NZ *yesterday*.

Two test suites, kept apart on purpose:

```bash
make test                        # 153 checks: no database, no lake, no Spark. Milliseconds
make test-conformance            # 15 checks against the lake, under spark-submit
```

The first runs on the host with the standard library alone, which is what lets the
riskiest logic — the extract window — be proved before any infrastructure exists. Keeping
the lake tests out of `unittest discover`'s reach is what stops it quietly acquiring a
dependency it claims not to have.

**Order matters between the last two data steps.** The nine fixtures and the generated
cohort share `src_metering.networks` — the generator upserts on the network *code*, so
generated connection points attach to fixture 01's own VECT row rather than a duplicate.
That is right, but it means `sample/01-*.sql` can no longer delete that row while
generated data is present, which is why **`make reseed-fixtures` removes the generated
rows first** and `make demo-refresh` has to follow it.

`make init-data` downloads the Kaggle dataset (`KAGGLE_USERNAME` / `KAGGLE_KEY` in
`.env`), picks the default deterministic **50-household** cohort, converts it to NZ local
time, injects the complete defect/revision and hardship-demo profile, and prices roughly
1,200 invoices from the catalogue. Set `COHORT_SIZE=500` only for a full scale run.

The compact generated cohort deliberately uses **one lines company (Vector), one customer
category (Standard User), and three retail plan families**: Anytime Rate, Time of Use and
Free Weekends. That produces three active choices instead of multiplying the
same choices by network and customer category. The nine fixed Sprint 2 fixtures still
retain their original networks and tariff edge cases, but they are not recommendation
candidates for the generated cohort.

`make rebuild-demo` is the normal demo path. It moves the compact cohort's complete two-year
history through the lake in one 12-way Spark range job: the second year supports plan
comparison, while the overlap with NIWA observations is required to fit the heating response
used by bill forecasts. It then rebuilds Snowflake and publishes into the backend's database:
the served tables, forecasts, recommendations, the bill-reproduction gate, hardship
assessments and pending in-app alerts, in one transaction. Because it first truncates the six
decision tables, it refuses a `KEA_MIGRATION_DSN` whose host is not local. The profile retains
all nine fixed Sprint 2 fixtures and has an explicit **under-30-minute** target.
The complete 50-household run measured about **7 minutes on 8 Sep 2026**.
Two cheaper targets are worth knowing:

```bash
make test-transform              # 30 unit tests, no database and no dataset needed
make assert-source               # the Stage 2 gate: 13 assertions
```

`data/pipeline/scripts/gen_source/README.md` explains what is generated and what is
assumed; `data/dictionary/lcl-source-data.md` records where every rate came from.

Analysis runs in **Metabase** at `http://localhost:3002` — `make metabase-setup` creates
the admin user and connects two databases: **Kea serving**, the backend's, read as `kea_app`,
and **Kea pipeline**, the sources and audit rollups. It is the workbench, not
the record: a saved question lives in Metabase's own database, so anything whose number
reaches a slide also needs a committed query or notebook in `analysis/`.

Five dashboards, each rebuilt in place from a committed script:

```bash
make metabase-dashboard          # the nine households, over kea.mart_*
make source-dashboard            # Stage 2: the 50-household demo cohort, over src_*
make lake-dashboard              # Stage 3: the pipeline's own behaviour
make stage5-dashboard            # Stage 5: the load, and what did not cross
make plans-dashboard             # Sprint 3: recommendation, hardship signals,
                                 #   outreach, and the reasons to doubt them
make verify-dashboards           # every card on all five: runs, returns rows,
                                 # and uses every column it selects
```

⚠️ **`make plans-dashboard` is the first one that plots a decision.** Every other dashboard
here shows something the pipeline *observed* — readings that arrived, rows that crossed. This
one reads `kea.plan_recommendation`, `kea.hardship_assessment`, `kea.hardship_signal` and
`kea.alert`: what another plan *would* cost, which explainable difficulty signals were found,
and which pending outreach records were created. It shows both the decisions and their bounds
— how close second place is, what was excluded, the closed hardship reason vocabulary, and
whether alert creation stayed idempotent. Since Sprint 4 it is also the staff queue for the
early warning: each signal's staff-only explanation, the warning signs counted apart from
*flagged*, and each open outreach marked flagged or early with its `kea.hardship_outlook`
projection. It carries account numbers, so it is for staff only; the Story page counts the
five v1 signals and shows no identifier.

It needs at least one published release: `make publish-serving` writes the recommendations.
Every panel pins the newest `computed_at`, because the table is append-only and a panel that
forgets doubles every figure on the page without erroring.

**There is deliberately no Stage 7 dashboard here.** Stage 7 is about the *pipeline's* health
— DAG duration, failures, freshness — and that lives in Grafana, from Prometheus. §8 draws the
line and `datasources.yml` enforces it by actively deleting the serving datasource an earlier
version provisioned: two dashboards over the same tables drift, and only one ends up trusted.
What Stage 7 did add to Metabase is one panel on the **Stage 5** dashboard, because its
finding is about served data: representable quarantines now reach the product decision
boundary with their reason, while orphan or unknown references remain upstream.

⚠️ **`make metabase-dashboard` used to name its households by surrogate id**, and Stage 5
broke it: the load replaced every fixture row with pipeline output carrying the warehouse's
own key maps, so all twelve cards became valid queries returning nothing — the exact failure
`verify.py` exists to catch, and the reason it is worth running. It now resolves the nine by
`icp_number` at build time, which is what `assert_serving.sql` does and for the same reason.

`make lake-dashboard` first runs `make lake-summary`, which is doing something worth
knowing about. **Metabase talks JDBC and the Stage 3 lake is Parquet on MinIO**, so there
is nothing there for a dashboard to point at. `analysis/metabase/summarise_lake.py` reads
the lake with Spark and rebuilds four rollups in a `lake_audit` schema — what landed per
partition, what each load date contained, how late corrections arrive, and the NIWA
temperature series as the lake holds it.

⚠️ `lake_audit` is **analysis, not pipeline output**. The real thing lands in the served
tables, via Stage 5's export. The rollups are derived, disposable and rebuilt whole on every
run.

`make help` lists every target. The stack doc's §6.3 explains the split and §6.5 tracks
what is still unbuilt.

## Stage 4 — the warehouse

Snowflake is its own account. `snowflake/bootstrap.sql` creates the database, warehouse and
role and is run **once, by hand** — every container here authenticates as the role that file
creates, so it cannot bootstrap itself.

```bash
make dbt-debug                               # connection check (key-pair)
make load-snowflake LOAD_DATE=2026-08-25     # bronze/parsed -> KEA.RAW, all 17 entities
make dbt-build                               # staging -> silver -> gold -> marts
make dbt-test                                # tests only
```

**Snowflake cannot reach the local MinIO**, so the lake cannot be an external stage.
`load_bronze_to_snowflake.py` pushes parsed Parquet up with the connector's PUT + COPY. Two
things it does that are easy to get wrong and hard to notice:

- the table is created **from the Avro contract**, not from the data. `auto_create_table`
  infers types from whatever arrived first, and invoices' `decimal(12,2)` became
  `NUMBER(4,2)` because day one held only two-digit values
- it reads with `types_mapper=pd.ArrowDtype`, because plain `to_pandas()` degrades any type
  pandas cannot hold nulls in — an all-null `date` column reaches Snowflake as a number

The layers land in `KEA` as `kea_staging` / `kea_silver` / `kea_gold` / `kea_marts`.
`DBT_SCHEMA` is a **base name**, not a layer: dbt appends the custom schema to the target.

⚠️ **Backfilling is per load date.** `invoices` is incremental on `issue_dt` and
`interval_reads` on `received_at`, so the warehouse only ever holds what was extracted.
Sweeping many dates in a shell loop needs `</dev/null` on each `make` — otherwise `make`
consumes the loop's stdin and it silently processes one date and reports success.

## Stage 5 — the served tables

The warehouse and the backend's Postgres finally meet. Two hops, and the first one is not the
one design §7 originally drew:

```bash
make migrate-serving             # migrations/serving -> the backend's database (once, then per change)
make configure-serving-roles     # give kea_loader and kea_app a password (once)
make export-warehouse            # Snowflake -> lake export/   22 tables, ~1.14M demo rows
make publish-serving RELEASE=<id>  # lake export/ -> kea-db, with the Sprint 3 decisions,
                                 #   in ONE transaction; a release id publishes once
make assert-serving              # THE STAGE 5 GATE -- the fixtures' own numbers, reproduced
make assert-load-atomic          # prove a failed publish changes nothing (throwaway Postgres)
```

The Sprint 3 jobs (forecasts, recommendations, the bill check, hardship, alerts) have no
targets of their own. They run only inside `publish-serving`, in its transaction, so nothing
reaches the backend without a release. [`pipeline/scripts/README.md`](pipeline/scripts/README.md)
shows how to dry-run one.

⚠️ **`kea.plan_recommendation` is NOT one of the 17.** It is an output of the platform, not an
export of the warehouse: `build_plan_recommendations.py` writes it, `serving.TABLES` must never
learn it exists, and every run appends a generation stamped with one `computed_at` so "what did
we tell this customer in August" stays answerable. Same argument as `kea.bill_forecast`. The
pricing comes from `services/api/app/analytics/tariff.py` — imported, never copied, which is
why that target mounts `../services/api` read-only.

⚠️ **`COPY INTO` cannot work, and the design said `COPY INTO`.** Snowflake is SaaS in
ap-southeast-2 and MinIO is local-only, so Snowflake can no more write to the lake than it
could read it on the way up — the same constraint that makes the bronze loader push.
`export_warehouse_to_lake.py` **pulls**: runs the query, takes Arrow batches, writes the
Parquet itself. §7's diagram has been corrected.

The reshaping lives in `pipeline/export/<schema>.<table>.sql`, one file per target table,
so the enum translations, the temporal plan resolution and the retention window are
reviewable as SQL. `load_export_to_serving.py` is then only transport. Both read
`kea_pipeline/serving.py`, the single ordered list of what crosses and in what order —
`billing.customer` first because it depends on nothing, `catalogue.plan` after
`metering.network` because those two read like siblings and are not.

Three things worth knowing before changing any of it:

- **The unit of atomicity is the whole FK-bound set, not one table.** §7.2 asks for
  "truncate + reload, one transaction" per table, and per table it cannot work: emptying
  `metering.property` while `metering.icp` references it is a foreign key violation, and no
  delete order avoids that in general. So everything is staged, deleted children-first,
  re-inserted parents-first, and committed once with the constraints deferred. That is
  stronger than the design asked for — the store is never half-applied across two tables
  that reference each other. The publication widens it once more: the decisions computed
  from those tables commit in the same transaction, and recording the release is its last
  statement.
- **The read window is a per-household rule.** 13 months for the cohort, the entire span
  for the nine fixture households. A single global window would exclude their 2023–24 reads
  and take the acceptance target with them.
- **Partition bounds are NZ instants, not dates.** A date is resolved in the session
  timezone, and this container runs UTC, so NZ midnight sits twelve hours before the bound
  and the readings in between belong to no month. That cost 48 rows silently — visible only
  because a 9,732,560-row export produced a 9,732,512-row table.

## Stage 6 — orchestration

Six DAGs. The four Stage 3 extracts, the pipeline, and a forecast refresh.

```bash
docker compose exec -T airflow-scheduler airflow dags unpause kea_daily_pipeline
make backfill-pipeline FROM=2026-08-24 TO=2026-08-26   # re-run a date range
```

**`kea_daily_pipeline` does not own the extracts.** It is scheduled on `AssetAll` of the three
daily lanes' bronze, so it starts when its inputs exist rather than when a clock says they
probably do. Design §9 and `_kea_common.py` both call for this: one DagRun has one failure
state, so a billing failure inside a combined DAG would block metering however the edges were
drawn — which is the property Stage 3 built four separate DAGs to guarantee.

`kea_extract_weather` is deliberately **not** an input. It is `@monthly`, and gating a nightly
pipeline on a monthly source would block it for twenty-nine nights in thirty.

Inside the publication, `build_bill_forecasts.py` consumes only rows whose warehouse
`is_forecastable` gate is true and upserts `kea.bill_forecast`. The manual
`kea_forecast_refresh` DAG rebuilds and re-exports the forecast mart, then publishes a release
through the same `publish_serving.py` the nightly DAG ends with, so the new inputs and the
forecasts computed from them change together; there is no second projection formula.

⚠️ **`airflow backfill` cannot be used, and that is a conflict between two of Stage 6's own
checkboxes.** Airflow refuses an asset-scheduled DAG outright — *"has a non-periodic schedule
that does not support backfills"* — because it generates runs from a timetable and there is
none. So `load_date` is a first-class Param and a range becomes one triggered run per date,
through the same DAG. That is what design §11 actually asks for: no separate backfill
code path.

**The gate is an edge.** `dbt_test` sits between the build and every export task, so a failing
test leaves them `upstream_failed` and Postgres keeps serving yesterday. Proven by breaking it
on purpose: with a deliberately failing test, `export_warehouse`, `load_serving` and
`assert_serving` never ran and the serving store was unchanged **including `synced_at`** —
which is what distinguishes "the load did nothing" from "the load never started". The load
task is `publish_serving` now, and `make assert-quality` re-proves the same edge against it.

## Stage 7 — quality gates and observability

```bash
make soda-scan                   # THE RUNTIME GATE (warehouse), after dbt_test
make soda-scan-serving           # a report on what was actually served
make assert-quality              # THE STAGE 7 GATE -- all four claims (~10 min)
```

Grafana carries **Kea pipeline health** (Prometheus only — the serving data is read in
Metabase, §8), Marquez holds the lineage graph on :5002, and `alert-sink` on :9099 keeps every
alert so a test can assert one fired.

⚠️ **If Grafana's panels look empty, open it in an incognito window.** A browser extension
blocks Grafana's panel-plugin loading, and every panel then sits on *"Loading plugin panel…"*.
Explore is unaffected — its graph is a built-in component, not a panel plugin — which is the
tell. Confirmed by elimination across two Grafana versions and a Grafana-authored test
dashboard.

⚠️ **The Soda checks are deliberately not the textbook ones.** This source is a fixed
historical extract whose readings stop in Aug 2026, so `freshness(interval_start) < 1d` and a
±10% band on reads-per-load-date are red every night forever. Freshness is measured on *the
load*; the volume band on *consumption days where the cohort is fully present* — 369 of them,
mean 26,364 reads, zero outside the band.

⚠️ **The lineage graph is built by explicit emission, not by the listeners.** Airflow's
provider emitted 379 events with no inputs or outputs at all, and the `openlineage-spark` jar
registered and emitted only application-level events. Every step now names what it read and
wrote through `kea_pipeline.lineage`, and `dbt-ol` closes the warehouse middle: **0 datasets
before, 110 and 239 edges after.**

⚠️ **Quarantine does not reach the served tables, and the gate is what found it.** The
warehouse holds all five planted defects with their reasons; serving holds none, because
`billing.account` requires `icp_id` and `plan_id` NOT NULL and a quarantined account is
precisely one that has neither. Design §6.3's "quarantine rates surface to the product" is not
achieved end to end — recorded rather than fixed by loosening a constraint.

## The sample fixtures

Nine runnable seeds in `sample/`. Each writes two halves, source rows and the hand-built
output expected from them, so a file on its own needs `design/kea_source_schema.sql` and
`design/kea_serving_schema.sql` applied first; then run any file — or all of them, in any
order, into the same database. Each one owns a private block of ids and its own network,
household and plan, so they never collide. That is for a scratch database. The pipeline's own
is seeded by `make source-schema && make source-seed`, which keeps the source half only.

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
| `09-bill_mid_cycle_forecast.sql` | `DEMO…0901AA` | **in flight** | 3 | The only fixture whose period has not ended. The hand-seeded fixture demonstrates the narrowing band; after a pipeline load, `mart_forecast_input` is the retained nightly input and the separate Stage 6 job owns `bill_forecast` output |

### Fixture ids

⚠️ **These ids are not what is served, and that is not a bug.** The served rows
are pipeline output carrying the warehouse's own `key_map_*` surrogates, and the
fixtures' own served half is not kept anywhere. The nine households are the same and
their `icp_number`s are unchanged; only the surrogate keys differ:

| Household | `sample/*.sql` | served |
|---|---|---|
| `0165841877LCC54` | 131001 | **1** |
| `DEMO0000000801AA` | 138001 | **508** |

Editing `sample/` to match would be wrong twice over: these files must
still work on an empty database, and a surrogate is whatever the next load
assigns, so the numbers would go stale again immediately.

**`icp_number` is the stable handle**, and everything that has to find a
household by name uses it — the Metabase explorer resolves ids at build time,
and `assert_serving.sql` asserts on business keys throughout. Hard-coding ids is
what silently emptied all twelve cards of that dashboard when Stage 5 first ran.

The scheme below still describes what `sample/*.sql` inserts, and is what you
are reading when you open one of those files.

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

**Generated rows start at `1,000,000`**, so they never sit inside these blocks.
`gen_audit.sync_sequences()` used to hand out the table's max + 1, which put the cohort
straight after the fixtures, and fixture 09's third bill, `239003`, was already a generated
invoice when #143 added it. `make assert-source` (check 0c) fails if a generated
connection point, account or invoice is below the line.

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

**Stage 5 made that concrete.** Run against pipeline output rather than hand-seeded rows,
**all six** scenarios come back not explainable — three of them as the fixtures intended,
three that were meant to be explainable and are not — and `cold_snap`, the one built to be
a weather story, blames `unexplained` instead. The reason is visible in
`int_metering__heating_sensitivity`: these households carry about two months of reads,
`fitted_on_months = 2`, and no slope is fitted, so there is no weather term to attribute
with. The fixture's 0.93 kWh/HDD is a hand-set constant in `generate_scenarios.py`, not
something an estimator recovered from 61 days. The model declining to name weather when it
cannot measure weather is the behaviour this section asks for, arriving unprompted.

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

- [`design/kea-data-platform-design.md`](design/kea-data-platform-design.md) — the pipeline architecture
- [`design/kea-data-engineering-stack.md`](design/kea-data-engineering-stack.md) — the stack that runs it, and the build order
- [`../docs/erd.md`](../docs/erd.md) — the data model
- [`../docs/adr/0006-postgres-in-sprint-2.md`](../docs/adr/0006-postgres-in-sprint-2.md) — why Postgres
- [`../docs/teams/data.md`](../docs/teams/data.md) — team onboarding
