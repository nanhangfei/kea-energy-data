# Kea Energy — Data Engineering Stack

**Scope:** which tools run the pipeline, which versions, where they run, and the order the platform
was built in, with the exit criterion for each step.
**Companion:** [`kea-data-platform-design.md`](kea-data-platform-design.md) covers what the pipeline
does and why. Section numbers in both documents are stable and cited from code.

---

## 1. Platform choices

### 1.1 The governing rule

**SQL in Snowflake under dbt by default.** Modelling, validation, key resolution, aggregation and marts
are SQL: version-controlled, tested, in one place. The rule exists so "which engine does this?" is
not re-argued per model.

Spark has two named exceptions:

| Exception | Why not SQL | Where |
|---|---|---|
| Extract and parse of `interval_reads` | 2.45B rows/yr; a single-threaded JDBC read does not finish in a batch window | Spark standalone locally, EMR Serverless on AWS (design §5.1) |
| The production rating engine | domain logic whose *output* lands in bills | outside this pipeline |

So Spark runs **one job** in the daily pipeline. Every other extract is a Python script; a cluster
would spend longer starting than reading.

### 1.2 Why the ingestion path is batch

NZ retail meter data moves between participants as scheduled **file exchange** (the Electricity
Authority's EIEP formats), and corrections arrive as later files. That drives three decisions:

- Extracts are **scheduled and incremental**, not a change stream (design §4.1).
- The watermark is **`received_at`**, so late corrections are picked up.
- **Streaming is a demonstration path, not the ingestion path.** Kafka and Flink would show the same
  contract arriving by a second route; they are provisioned but not yet built.

This is an assumption (design §2.2). If the production feed turns out to be continuous, the streaming
path becomes production-relevant.

### 1.3 What each tool is here to do

| Tool | Job |
|---|---|
| **S3 / MinIO** | bronze (immutable, as received) and export staging; the re-run boundary |
| **Spark** | the one extract that needs partitioned parallel reads |
| **Snowflake** | the warehouse; sees the full history the serving store never holds |
| **dbt** | every model from staging to marts, and the tests that gate publishing |
| **Soda Core** | runtime checks (freshness, volume, orphan rate) after dbt's |
| **Airflow** | scheduling and dependencies; MWAA on AWS |
| **PostgreSQL** | the serving store the application queries |
| **OpenLineage + Marquez** | lineage from source to serving across Airflow, Spark and dbt |
| **Prometheus + Grafana** | pipeline health; **not** used to analyse served data |
| **Metabase** | the analysis surface; reaches Postgres and Snowflake without an enterprise licence |
| **Kafka + Schema Registry, Flink** | planned streaming path for the same `KEA-047` contract |

---

## 2. The local stack, and what it stands in for

The design targets managed AWS. Docker Compose brings up local equivalents, so the pipeline can be
built and demonstrated without an AWS account. DAGs and dbt models cannot tell which they run against.
How to deploy it there, in phases and at what cost: [kea-aws-deployment-plan.md](kea-aws-deployment-plan.md).

| Local | AWS target |
|---|---|
| MinIO (`kea-lake` bucket) | S3 |
| Spark standalone master + worker | EMR Serverless |
| Airflow 3.3, self-hosted | MWAA |
| `kea-source-db` | the four source systems (reached, not hosted, in production) |
| `kea-db` ([`docker-compose.serving.yml`](../docker-compose.serving.yml)) | RDS PostgreSQL, the application's database |
| Marquez, Prometheus, Grafana, Vault | CloudWatch and Secrets Manager, or Marquez and Grafana as-is |
| Metabase | Metabase or QuickSight |
| Kafka + Schema Registry (`streaming` profile) | MSK + Glue Schema Registry |
| Snowflake database `KEA` | Snowflake (SaaS, no local substitute) |

**Where the substitution is not free:**
- **Airflow version.** MWAA has no local mode and lags upstream releases. Pin local Airflow to an
  MWAA-supported version before deploying; develop against `aws-mwaa-local-runner`.
- **Spark sizing.** Standalone has no autoscaling. Partitioning, watermark and idempotency are proven
  locally on a slice; sizing is an EMR Serverless exercise.
- **How tasks launch work.** Locally, `DockerOperator` launches sibling containers so dbt and the
  Snowflake connector stay out of the Airflow image. MWAA has no Docker daemon, so there it becomes
  `EmrServerlessStartJobOperator` and `EcsRunTaskOperator`. Keep the switch in one small factory so
  the DAGs read the same in both.

---

## 3. Versions

| Component | Version | Pinned in |
|---|---|---|
| PostgreSQL, sources (`kea-source-db`) and metadata DBs | 17.10 | `docker-compose.yml` |
| PostgreSQL, serving (`kea-db`) | 16-alpine (CI: 16.15-alpine) | `docker-compose.serving.yml` |
| Apache Airflow | 3.3.0 (Python 3.11) | `pipeline/airflow/Dockerfile` |
| Apache Spark | 3.5.9, with pyarrow and openlineage-python | `pipeline/spark_jobs/` |
| dbt | dbt-core 1.12.3, dbt-snowflake 1.12.0 | `pipeline/docker/dbt/` |
| Soda Core | 3.5.6 | `pipeline/docker/soda/` |
| MinIO / mc | RELEASE.2025-09-07 / 2025-08-13 | `docker-compose.yml` |
| Marquez | 0.50.0 | `docker-compose.yml` |
| Prometheus / statsd-exporter | v3.13.1 / v0.29.0 | `docker-compose.yml` |
| Grafana | 13.1.1 | `docker-compose.yml` |
| Metabase | v0.63.15, on its own Postgres (the bundled H2 corrupts) | `docker-compose.yml` |
| Vault | 1.20.4 | `docker-compose.yml` |
| Kafka + Schema Registry / Flink | Confluent Platform 8.3.0 / 2.3.0 | `docker-compose.yml` (`streaming`) |

**Every tag names a release, and every built image installs from a constraints file.** Floating tags
such as `postgres:17` or `python:3.11-slim` silently change what a rebuild pulls. The dbt, Soda and
Spark images each carry a `constraints.txt` that is their own `pip freeze`; Airflow uses its published
constraints. Publishing is SQL over a connection, so serving works on PostgreSQL 16 and 17.

---

## 4. Production behaviours, and where each one lives

| Concern | How | Design doc |
|---|---|---|
| Secrets | `.env` (gitignored), `env_var()` in dbt and Soda, Airflow connections; Secrets Manager on AWS | — |
| Data contracts | Avro schemas; Parquet on S3 and Avro on Kafka are two serialisations of one schema | §5.3 |
| Late corrections | watermark on `received_at`; silver keeps the highest `version_no` | §4.1 |
| Bad data | one quarantine table, one reason vocabulary; rows routed, never dropped | §6.3 |
| Idempotency | immutable bronze, stable keys, atomic publish; replays compared by content hash | §11 |
| Stable surrogates | append-only key maps | §6.2 |
| Backfill | the same DAG, one run per date; no second code path | §9, §11 |
| Quality gate | dbt tests and Soda **before** publishing; a failure leaves the previous release live | §9, §10 |
| Freshness | `kea.mart_load_log` per mart per load | §6.5 |
| Lineage | explicit OpenLineage emission → Marquez | this doc, §5 Quality gates |
| Monitoring | StatsD → Prometheus → Grafana, with alerting | — |
| Reconciliation | a real printed bill reproduced from source: **$50.54** | §10.3 |
| Blast radius | the loader has no grant on `app` | §1.1 |
| Model acceptance | planted answers scored on thresholds written before the run | §15.10 |

---

## 5. Build roadmap

The platform was built in this order. A step counts as done only when its exit criterion is shown by
a command, not when the code exists.

| Step | Command | Airflow | Exit criterion | Status |
|---|---|---|---|---|
| Stack | `make up` | — | every service healthy and reachable | done |
| Sources | `make init-sources` | — | four source schemas populated; a printed bill reproduces from fixtures to the cent | done |
| Lake | `make ingest-date` | `kea_extract_*` | a three-week-old correction is picked up by a normal run; re-running a date changes nothing | done |
| Warehouse | `make build-warehouse` | `kea_daily_pipeline` · `build-warehouse` | Bill B reproduces from source at $50.54; a second build is a no-op | done |
| Serving | `make publish-serving` | `kea_daily_pipeline` · `publish-serving` | the pipeline's output reproduces the fixtures; a failed publish changes nothing | done |
| Quality gates | (inside Warehouse and Serving) | Soda scans, `assert_serving` | an injected defect is quarantined, alerts and stops publishing; lineage covers source to serving | done |
| Orchestration | `make run-pipeline` | triggers `kea_daily_pipeline` | a date range is backfilled twice and nothing changes | done |
| Delivery | `make ci`, `make deploy` | — | a change cannot ship with a broken DAG, model or test | partly |
| Streaming | `make up STREAMING=1` | — | a day replayed through Kafka produces the same marts as batch | not started |

`make rebuild-all` runs every step from Stack to Analysis in sequence.

### Stack
Every service starts and stays up (restart count 0); each UI answers; the Spark master reports a live
worker. `make up` builds an image only when it is missing (see §8).

### Sources
- **Fixtures first, before any infrastructure.** Three real bills were analysed into the entities and
  arithmetic, the DDL was written, and nine fixtures were generated from user scenarios. Each fixture
  asserts its own totals, and the six "why did my bill go up" fixtures also assert which driver wins.
  These are the project's only external ground truth: without them, a green `dbt build` would only
  mean the pipeline agrees with itself.
- **The generated cohort.** Low Carbon London metering, re-timed to NZ (see
  [`lcl-source-data.md`](../dictionary/lcl-source-data.md)): 200 households, ~8.1M half-hourly reads
  over 757 days, priced into bills from the catalogue, with payments and plan history.
- **Defects injected synthetically** (gaps, spikes, stuck meters, estimated and final reads, duplicates,
  orphans, an ambiguous register) because the source data is already clean. The answer key lives in
  `gen_audit`, which no extract role can read.
- **The UK → NZ transform was proven before it touched data** (unit tests, no database). It caught two
  silent DST faults.
- **Gate:** `99_assert.sql`, including that fixtures survive the run and identity sequences stay ahead
  of their tables.

### Lake
- Separate extract DAGs, credentials and least-privilege roles per source. **Independence is proven by
  a forced failure:** `make ingest-date VERIFY=1` breaks the billing credential and shows metering
  completing.
- `interval_reads` is watermarked on `received_at`, one Spark JDBC predicate per hour of the NZ day
  (23–25 across DST).
- **Gate:** each partition holds **exactly** what its window selects at source, with no `read_id` in two
  partitions. The late-correction check is paired with a **negative control**: a window on
  `interval_start` must find none of it.
- The gate is known to fail as well as pass: a tampered manifest count is reported, named and exits 1.

### Warehouse
- Snowflake is the project's own account. dbt connects as a least-privilege role, so a wrong
  `DBT_SCHEMA` is a permission error rather than data loss. [`snowflake/bootstrap.sql`](../snowflake/bootstrap.sql)
  creates the role, warehouse and database and is run once, by hand.
- **Snowflake cannot reach a local MinIO**, so parsed Parquet is pushed with PUT + COPY. Snapshot
  entities are truncate-and-reload; incremental entities accumulate and replace their own load date.
  That keeps staging honestly 1:1.
- Key maps are **proved** append-only: hashed, rebuilt, hashed again, identical.
- Findings worth carrying into any Snowflake project:
  - Comparing `TIMESTAMP_TZ` to `DATE` uses the **session** timezone (default `America/Los_Angeles`),
    which resolved every invoice a day early. Convert explicitly; don't rely on a session parameter.
  - Deduplication needs a **total** ordering (`version_no desc, read_id desc`), or a different row
    survives each build while uniqueness tests still pass.
  - Letting `write_pandas` infer types let the first load date define the schema. Tables are now
    created from the Avro contract.
  - A case mismatch (`'IMPORT'` vs `'import'`) returned 0 instead of an error and zeroed every weather
    driver. Staging normalises it.

### Serving
- The export **pulls** from Snowflake into the lake; the publisher loads it into the application's
  database in one transaction (design §7.3).
- **Gate:** `assert_serving.sql` reproduces the fixtures' expected numbers from pipeline output,
  including Bill B at $50.54. Known divergences are kept as failing-but-explained checks with their
  cause (the fixtures' two-month histories cannot fit a heating slope; five 2023 invoices predate their
  tariff and are correctly quarantined).
- `make publish-serving VERIFY=1` injects a failure at the publication's last statement on a throwaway
  Postgres and proves every count and `synced_at` is untouched.

### Quality gates
- **Soda checks are adapted to a fixed historical source.** Freshness is measured on the load, and the
  volume band on days where the cohort is fully present (design §10.1).
- **Lineage by explicit emission.** Airflow's provider emitted events with no inputs or outputs
  (`DockerOperator` cannot see inside the container) and the Spark listener emitted no datasets. Each
  step now names what it read and wrote through `kea_pipeline.lineage`, and `dbt-ol` covers the
  warehouse: from 0 datasets to **110 datasets and 239 edges**, source to serving.
- `make run-pipeline VERIFY=1` poisons the Soda gate and checks that the defect is quarantined, an
  alert fires, publishing stops and lineage is complete.
- One bad pattern in a `statsd-exporter` mapping file silently disables the whole file; regex matching
  fixed it.

### Orchestration
- `kea_daily_pipeline` is **asset-scheduled** on all three daily sources landing, rather than at a fixed
  hour (design §9).
- **A failing `dbt test` blocks every publish task**, proven by breaking it: the serving store was
  unchanged, including `synced_at`, which shows the load never started rather than ran and did
  nothing.
- **Asset scheduling and `airflow backfill` are mutually exclusive** (`DagNonPeriodicScheduleException`).
  `load_date` is a DAG param, and `make run-pipeline FROM=… TO=…` triggers one run per date through
  the same DAG. A backfilled range, re-run, produced identical row counts in every serving table.

### Delivery
- **Done:** `make ci` runs lint, the host unit tests, migration checks, `dbt parse`, a publication into a
  throwaway Postgres, DAG import tests, the dashboard identifier guard, and the backtest tests inside
  the dbt image (failing on any skip). It needs no `.env` or credentials.
- **Done:** every image tag and Python dependency is pinned (§3).
- **Not done:** a CI `dbt build` in an isolated schema, which needs warehouse credentials as repository
  secrets. Until then CI parses models but runs no data test; `make deploy` runs the real build.
- **Done:** `make deploy` publishes only merged code, gated by `make ci`, as release `git-<sha>`.
  First run end to end on 14 Oct 2026 for release `git-ce12778a0c68`, in about four minutes; an
  earlier attempt had already published that release, so the run changed nothing in kea-db.

### Streaming
Not started. Kafka, Schema Registry and Flink are provisioned behind the `streaming` profile; the replay
producer and the batch/stream reconciliation are not built.

---

## 6. How the stack is laid out

### 6.1 Naming

- **Compose project `kea`**, set explicitly so renaming the checkout cannot orphan its volumes. The
  serving database is a separate project, `kea-serving`.
- **Images are `dataeng/*`** (`dataeng/airflow:3.3.0`, `dataeng/spark:3.5.9`, `dataeng/dbt:1.12.0`,
  `dataeng/soda`). They hold a base image plus libraries and no project code, so they are named for
  what they contain.
- **Everything project-specific is named for the project:** the OpenLineage namespace `kea_energy`, the
  StatsD prefix `airflow_kea`, the bucket `kea-lake`, the Snowflake database `KEA`.

### 6.2 Where the pipeline code lives

```
pipeline/
  airflow/dags/      the DAGs (design §9)
  spark_jobs/        the meter-read extract and parse
  lib/kea_pipeline/  logic shared by DAGs and Spark jobs, above all the extract window,
                     so the two can never disagree on what a load date is
  contracts/         Avro contracts and the reject-reason vocabulary
  dbt_project/       staging → silver → gold → marts
  export/            one SQL query per served table
  soda/              runtime quality checks
  scripts/           extract, warehouse, serving, decision, gate and ops scripts;
                     gen_source/ is the source generator, whose numbered order is its interface
  tests/             host tests: no database, lake or Spark
  tests/lake/        lake tests, run under spark-submit, kept out of unittest discovery
  monitoring/        Prometheus, Grafana and alerting configuration
  docker/            the dbt and Soda images
```

The conventional names (`spark_jobs`, `dbt_project`) are kept so anyone who knows an
Airflow-Spark-dbt layout can navigate without a map.

### 6.3 Self-contained, and separate from the application's stack

```
docker-compose.yml           THE PIPELINE (project: kea)
  minio, minio-init          the lake
  spark-master, spark-worker, spark-history-server
  airflow ×4, airflow-postgres
  kea-source-db              the four source systems and audit rollups
  marquez (+db, web), statsd-exporter, prometheus, grafana, alert-sink, vault
  metabase (+db)
  kafka, schema-registry, flink ×2   (streaming profile)

docker-compose.serving.yml   THE APPLICATION'S DATABASE (project: kea-serving)
  kea-db                     the served tables, and the application's own `app` schema
```

**One database for what is served, locally as in every other environment.** The pipeline owns
`common`, `metering`, `billing`, `catalogue`, `kea` and `data_ops`; the application owns `app`.
Containers reach it as `host.docker.internal:5432`. Keeping it in its own Compose project means
`make clean` on the pipeline never deletes served data, and the application keeps reading the last
release while the pipeline is down. Setting `KEA_DB_VOLUME` in `.env` reuses an existing
database's volume instead of starting empty.

**dbt and Soda each get an image.** `dbt-snowflake` and `soda-core-snowflake` cannot be resolved
together by pip, and neither belongs in the Airflow image.

### 6.4 Publishing pipeline output to the application

`make publish-serving RELEASE=<id>` loads the sealed warehouse export, computes forecasts, plan
recommendations, solar and EV estimates and hardship outputs, validates them, and records the release
in `data_ops.serving_publication`, all in one transaction. An error leaves the previous release
visible. A release id publishes once; repeating it only re-checks.

Schema changes are separate and deliberate: `MIGRATE=1` applies immutable migrations from
[`migrations/serving/`](../migrations/serving/README.md) as the owner. The publisher connects as
`kea_loader`, which can change no schema and cannot read `app`.

**Operational caveats.** Export paths are mutable, so scheduled exports and publishes must not overlap
until exports get immutable release prefixes and a manifest. Large publications hold locks that can
delay application reads, so production needs a measured maintenance window.

### 6.5 What is built, and what is not

| Outstanding | Why it matters |
|---|---|
| CI `dbt build` in an isolated schema | CI cannot catch a failing data test |
| A CI workflow in this repository | `make ci` is ready; no workflow runs it on pull requests yet |
| Streaming | the batch/stream equivalence is unproven |
| `mart_scale_test` | deferred; it is a 2.45B-row capacity test, not a feature (design §6.5) |
| Immutable export releases | needed before export and publish can be scheduled independently |
| Snowflake access | the first trial account ended in October 2026, after the warehouse was archived to Parquet on 30 Sep 2026 with `pipeline/scripts/ops/archive_warehouse.py`. A second trial account has run the pipeline since 10 Oct 2026 and ends about 9 Nov 2026; after that the pipeline needs a paid account |

---

## 7. Rebuilding after the span moves

The demo data is London metering shifted onto NZ dates, ending on a pinned `NZ_SPAN_END`
(2027-08-27). The shift, and why it must be a whole number of weeks and about half a year off a
whole number of years, is documented in [`lcl-source-data.md`](../dictionary/lcl-source-data.md) §8;
the values live in `cohort.json`.

Moving the span moves **every** timestamp the generator writes. Everything else is deterministic
(seeded cohort pick, recorded scale factor, `received_at` derived per ICP-day rather than from
`now()`), so a span move is a discrete event, not drift.

**It is not a backfill** (design §11): new rows land on different dates, and old rows survive every
per-date step. [`rebuild_span.sh`](../pipeline/scripts/ops/rebuild_span.sh) is the procedure:

| | Work | Cost (757 load dates) |
|---|---|---|
| per date | extract, parse, scripted entities, stage **incremental entities only** | ~31 s each |
| once | the snapshots, then `make build-warehouse` and `make publish-serving` | ~5 min |

It drops and recreates RAW rather than truncating, and forces re-extraction of sealed partitions.
Running `make run-pipeline` per date instead would rebuild every dbt model 757 times (~18 h against
~7 h).

**What the script verifies rather than assumes:**
- that RAW was really recreated, by reading the row count back;
- that every snapshot landed, because the loader reports "skipped" as success;
- that incremental billing rows outside the reading span (fixture invoices, late invoices and payments)
  are caught up, or about 150 invoices go missing without an error.

Afterwards, run `make dashboards ONLY=source`: the source rollups are materialized views and keep
showing the old span until refreshed.

---

## 8. Running alongside other stacks

| | Scope | Why |
|---|---|---|
| Compose projects | `kea`, `kea-serving` | volumes and networks are scoped to them |
| Snowflake | its own database `KEA` | `dbt build` drops what it owns; a database boundary turns a wrong schema into a permission error |
| MinIO | one `kea-lake` bucket | no collision with generic `bronze`/`silver` buckets |
| Metrics | prefix `airflow_kea` | series stay distinct if one Prometheus scrapes several Airflows |
| Lineage | namespace `kea_energy` | Marquez separates producers by namespace |

**Host ports are not isolated:** Airflow 8080, MinIO 9000/9001, Spark 8082 and 18080, Marquez
3000/5002, Prometheus 9090, Grafana 3001, Metabase 3002, Vault 8200, `kea-source-db` 5433, `kea-db`
5432. Only one stack using them can run at a time.

**Image tags are shared on purpose**, and `make up` builds only missing images. Rebuilding a shared tag
affects everything using it, so it is explicit: `make up BUILD=1`.

---

## 9. Where analysis lives

**Metabase is the workbench; the repository is the record.** A question saved in Metabase lives in
Metabase's own database, not in git. Anything that reaches a slide or a decision needs a committed
query or script in [`analysis/`](../analysis/).

| Use | Where |
|---|---|
| Exploration and browsable dashboards | Metabase |
| Any figure that is quoted | a committed script or query in `analysis/` |
| Pipeline health (duration, failures, freshness) | Grafana, from Prometheus |

**Eight dashboards, each built from a committed script** in
[`analysis/metabase/`](../analysis/metabase/) (`make dashboards`):

| Dashboard | Reads | For |
|---|---|---|
| Sample fixtures | `kea.mart_*` | the nine hand-built households |
| Sources | `src_*` via `gen_audit` rollups | the generated cohort |
| Lake | `lake_audit` rollups | the pipeline's own behaviour: partitions, arrival lag |
| Serving | `billing.*`, `metering.*`, `kea.mart_*` | what crossed into serving, what did not, and why |
| Plans | recommendations, hardship, alerts | decisions and their bounds; staff-only |
| Advanced analytics | detected loads, what-ifs, hardship outlook, `gen_audit` | detector output against planted answers |
| Demo walkthrough | one household's served outputs | every feature in order, no identifiers |
| Forecast backtest | committed results files, no table | the backtest gate and results |

**Three rules, each guarding a failure that does not announce itself:**
- **The builder is the record.** Cards are upserted by name from the script, and cards the script no
  longer builds are archived.
- **Identifiers are refused at build time.** The public-facing builders run every query through a
  guard that rejects ICP numbers, account numbers and names; `make ci ONLY=story-guard` runs it too.
- **Append-only tables are always pinned** to the latest `computed_at`, or every count doubles on the
  next publication without an error.

`make dashboards` ends by checking every card: it runs, returns rows, and **uses every column it
selects**. Metabase silently drops an undeclared breakout, and silently caps a card at 2,000 rows.

**There is deliberately no Warehouse dashboard.** Marts become visible in Metabase when they are
published to Postgres; a second connection to Snowflake would create two dashboards over the same
numbers.

**Evaluations are directories, not dated scripts.**
[`sprint4_evaluation/`](../analysis/sprint4_evaluation/README.md) and
[`forecast_backtest/`](../analysis/forecast_backtest/README.md) each hold a method, thresholds fixed
before the run, a re-runnable harness and the report it produced, named by the release or export it
scored. One-off questions are `YYYY-MM-DD-what-it-answers` scripts.
