# Kea Energy — Data Engineering Stack

**Owner:** Harvey Nan (Data Engineer)
**Scope:** which tools run the pipeline, which versions, where they run, and the order they get built in.

**Companion:** `kea-data-platform-design.md` (what the pipeline does and why). That document
is the architecture; this one is the platform underneath it. Where the two touch — the
governing rule in §1.1 below, and the batch-delivery assumption in §1.2 — the design doc
cites this file rather than restating it.

**Scope boundary.** Open items and the work that unblocks them live in design doc §12.
This document's §5 is the *order* that work lands in and what has to be true before each
stage is called finished. Two lists, one of what and one of when — not two task trackers.

---

## 1. Platform choices

### 1.1 The governing rule

**SQL in Snowflake under dbt by default.** Modelling, validation, key resolution,
aggregation and the marts are all SQL, version-controlled and testable in one place. The
rule exists so that "which engine does this?" is not re-argued per model.

Spark handles two named exceptions, and only two:

| Exception | Why it cannot be SQL | Where it runs |
|---|---|---|
| **Raw parsing / reshaping** of `src_metering.interval_reads` | 2.45B rows/yr. A single-threaded JDBC read does not finish in a batch window; partitioned parallel read is the only practical option | EMR Serverless (locally: Spark standalone). Design doc §5.1 |
| **The rating engine** | Domain logic with per-charge-group banding and loss arithmetic. Its *output* lands in bills; it is not a warehouse model | Run separately from the pipeline |

**Spark therefore runs one job in the daily pipeline — not one per extract.** Billing and
catalogue extracts are plain JDBC scripts — Spark would spend longer starting up than reading. This
is worth naming because the opposite is the common failure: standing up a distributed
engine for every extract because one of them needs it, and paying the startup cost and
operational surface on jobs that would run in seconds locally.

The consequence for this document is that the local stack does **not** need to be sized
for a Spark-everywhere pipeline. Most of §2 is there for one job and for Sprint 5.

### 1.2 Why the ingestion path is batch

NZ retail metering data moves between participants as scheduled **file exchange** — the
Electricity Authority's information-exchange protocols (EIEP) are file-based formats on
an agreed cadence, not a streaming interface. A retailer receives meter data as periodic
files, and revisions arrive as later files rather than as updates in place.

That shapes three decisions:

- The extract is **scheduled and incremental**, not a change stream (design doc §4.1).
- The watermark is **`received_at`**, because a revision's `interval_start` is older than
  the last run's high-water mark and would never be picked up.
- **Streaming is a Sprint 5 capstone, not the ingestion path.** Kafka and Flink
  demonstrate that the same schema can arrive by either route; they are not how the data
  actually arrives.

> **Flagged as an assumption**, in the same spirit as design doc §2.2: the file-exchange
> convention is the reason to expect batch delivery, but Kea's actual meter-feed cadence
> is unconfirmed. `KEA-501`. If delivery turns out to be continuous, the Sprint 5 Flink
> path becomes production-relevant rather than demonstrative, and §1.1's exception list
> gains a fourth entry.

### 1.3 What each tool is here to do

One line each, so the stack can be read without inferring intent:

| Tool | Job in this pipeline |
|---|---|
| **S3** | Bronze (immutable, as-received) and export staging. The re-run boundary — a bug downstream never re-reads a production source |
| **Spark** | The one extract that needs partitioned parallel reads, per §1.1 |
| **Snowflake** | The warehouse. Sees all 2.45B rows; Postgres never will |
| **dbt** | Every model from staging to marts, plus the tests that gate the export |
| **Soda** | Runtime data quality — freshness, volume, distribution drift — as a second gate after dbt's |
| **Airflow / MWAA** | Scheduling and dependencies, including the FK-ordered export (design doc §7.4) |
| **PostgreSQL (RDS)** | The serving store the agent queries in under 500 ms |
| **OpenLineage / Marquez** | Cross-tool lineage, so "where did this number come from" is answerable across Airflow, Spark and dbt |
| **Prometheus / Grafana** | Pipeline health — Airflow metrics via statsd-exporter. **Not** used to analyse the serving data |
| **Metabase** | The single analysis surface: the serving schema now, the Snowflake marts later. It ships drivers for both in the free product; Grafana's Snowflake datasource is an Enterprise plugin, so one tool covering both had to be this one |
| **Kafka + Schema Registry, Flink** | Sprint 5 only. The same `KEA-047` schema arriving by a second route |

---

## 2. The local stack, and what it stands in for

The design doc targets managed AWS. `../docker-compose.yml` brings up local equivalents so
the pipeline can be built and demonstrated without an AWS account. Neither the DAGs nor
the dbt project should be able to tell which one they are running against — that
interchangeability is the whole point of the substitution.

| Local, in `../docker-compose.yml` | AWS target |
|---|---|
| MinIO | S3 — `s3://kea-lake/`, prefixes `bronze` / `silver` / `gold` / `quarantine` / `export` |
| Spark standalone master + worker | EMR Serverless. No autoscaling locally |
| Airflow, self-hosted | MWAA. **Version-sensitive — see below** |
| `kea-serving-db` | RDS PostgreSQL. Where the loader lands |
| Marquez · Prometheus · Grafana · Vault | CloudWatch and Secrets Manager, or lift Marquez and Grafana as-is |
| Metabase | Metabase, or QuickSight. Analysis, not part of the pipeline |
| Kafka + Schema Registry (`streaming` profile) | MSK + Glue Schema Registry |
| Snowflake, database `KEA` | Snowflake — SaaS, no local substitute |

The application's Postgres in `infra/` is a separate thing again, and not part of this
table: it belongs to the API and web teams, not the pipeline. §6.3.

**The substitution is not free in two places.**

**Airflow version.** The local Airflow version is not necessarily the version MWAA
offers. MWAA has no local mode, which is why the design doc (§9) specifies
`aws-mwaa-local-runner` for DAG development, and a failed environment update from an
untested dependency change is a 20-minute recovery. Check the version MWAA currently
offers and pin local Airflow to it **before** DAGs are written, not after.

**Spark sizing.** Standalone gives no autoscaling. The 2.45B-row extract can be *proven*
locally against a slice — the partitioning, the watermark, the idempotency — but it
cannot be *sized* locally. Sizing is an EMR Serverless exercise.

**And one place the transparency claim above does not hold: how a task launches work.**
*Dormant while AWS deployment is deferred (§6.4) — everything runs the local operators
today. It is recorded here because it is the seam that reappears first if that changes.*

The compose file mounts `/var/run/docker.sock` into Airflow so that
`DockerOperator` can launch sibling containers — the pattern that keeps dbt and the
Snowflake connector out of the Airflow image, where they would collide with its pinned
dependencies. It is a good pattern locally and **it cannot be used on MWAA**, which runs
managed workers with no Docker daemon to talk to.

So the operator differs by environment even though nothing else does:

| Work | Local | MWAA |
|---|---|---|
| The Spark parse job | `DockerOperator` / `spark-submit` to the standalone cluster | `EmrServerlessStartJobOperator` |
| dbt, and the Postgres loader | `DockerOperator` on a `kea/dbt` image | `EcsRunTaskOperator`, or the dependency added to the MWAA `requirements.txt` |

Keep the difference in **one place** — a small factory that returns the right operator for
the environment — so the DAG still reads as `extract → parse → stage → run → test → gate →
export` in both. The dependency-isolation problem is real in both environments; only the
mechanism for solving it changes.

---

## 3. Versions

| Component | Version | Pinned in | Needed from |
|---|---|---|---|
| Serving PostgreSQL (pipeline output) | 17, to be added as `kea-serving-db` | `../docker-compose.yml` | Stage 5 |
| PostgreSQL (the application's) | 16-alpine | `../../infra/docker-compose.yml` | now — separate instance, see §6.3 |
| Apache Airflow | 3.3.0 | `../docker-compose.yml` | Stage 6 |
| Apache Spark / PySpark | 3.5.9 (official `apache/spark` image; standalone via `spark-class`) | `../docker-compose.yml` | Stage 3 |
| MinIO / mc | RELEASE.2025-09-07 / 2025-08-13 (community edition archived Apr 2026 — last releases) | `../docker-compose.yml` | Stage 3 |
| PostgreSQL (Airflow + Marquez metadata) | 17 | `../docker-compose.yml` | Stage 6 |
| Marquez (+ web UI) | 0.50.0 | `../docker-compose.yml` | Stage 7 |
| Prometheus / statsd-exporter | v3.13.1 / v0.29.0 | `../docker-compose.yml` | Stage 7 |
| Grafana | 13.1.1 | `../docker-compose.yml` | Stage 7 |
| Metabase | v0.63.15 (on its own Postgres 17 — the bundled H2 corrupts) | `../docker-compose.yml` | now |
| Vault | 1.20.4 | `../docker-compose.yml` | stretch |
| Kafka + Schema Registry (Confluent Platform) | cp 8.3.0 (≈ Kafka 4.x, KRaft-only) | `../docker-compose.yml`, `streaming` | Sprint 5 |
| Apache Flink | 2.3.0 (java 17, Scala-free) | `../docker-compose.yml`, `streaming` | Sprint 5 |
| Snowflake | SaaS (no version) | — | Stage 4 |
| dbt (dbt-snowflake) | 1.12.0, pulling dbt-core 1.12.3 | `pipeline/docker/dbt` | Stage 4 |
| Soda Core | 3.5.6 (resolved by the build; **pin it** — Stage 8) | `pipeline/docker/soda` | Stage 7 |

**Two version facts to settle before they bite.**

The serving store runs Postgres **16** locally while the two metadata databases run
**17**. Design doc §12 carries the matching open item: all three DDL files apply cleanly
on 16.15 and Bill B reconciles to $50.54 there, but PG17 itself is unverified. The
metadata databases are not the thing to verify against — they hold Airflow and Marquez
state, not the serving schema.

`postgres:17` is a **floating tag** that tracks the latest patch release, and dbt and Soda
have no pin at all. Tightening these is Stage 8.

---

## 4. Production behaviours, and where each one lives

Each row is implemented in the design doc section named; this table is the index.

| Concern | How | Design doc |
|---|---|---|
| Secrets | `.env` (gitignored) + `env_var()` in dbt/Soda + GH Actions Secrets → Secrets Manager; Vault local as a stretch | — |
| Contract | The Avro schema (`KEA-047`) defines what a meter reading is. Parquet on S3 and Avro on Kafka are two serialisations of one schema | §5.3 |
| Revisions | Watermark on `received_at`, never `interval_start`; silver dedups on `(icp, register, interval_start)` keeping the highest `version_no` | §4.1 |
| Bad data | One quarantine table, one `_reject_reason` vocabulary, across all three rejection paths. Rows are routed, never dropped | §6.3 |
| Quarantine reaches the product | A household with a quarantined account or a high estimated-read rate gets a hedge or a decline, not a confident answer | §6.3 |
| Idempotency | Bronze immutable; `MERGE` on a stable key in the warehouse; DELETE-by-period and partition swap in Postgres | §11 |
| Stable surrogates | Append-only key maps. Regenerating ids on each load would break every FK in Postgres | §6.2 |
| Backfill | The same DAG with `--start-date` / `--end-date`. No second code path — a second code path is a second set of bugs, and it is the under-tested one | §11 |
| Quality gate | dbt tests, then Soda, both **before** export. A failure leaves Postgres serving yesterday: correct-but-stale beats fresh-but-wrong | §9, §10 |
| Freshness is queryable | `kea.mart_load_log`, one row per mart per load, so staleness is answerable without inspecting the data | §6.5 |
| Lineage | OpenLineage from Airflow and Spark → Marquez | §9 |
| Monitoring | StatsD → Prometheus → Grafana. Pipeline health only — the serving data is read in Metabase, §8 | — |
| Reconciliation | Two real bills recomputed end to end: **$248.07** (flat, 31 days) and **$50.54** (TOU + solar, 24 days) | §10.3 |
| Blast radius | `kea_loader` has no grant on `app` — not even `USAGE`. Conversation history is the one thing no re-run can recover | §1.1 |

---

## 5. Build roadmap

Stages, each with an exit criterion that has to be **demonstrably true** — not "the code
exists". A box is ticked only when there is something in the repo to point at, which is
the standard the rest of `data/` is held to.

> **Stage 0 was done before any of this**, and it is why the stages below have
> acceptance criteria at all. Stage 1 is verified; Stages 2 onward are not started.

### Stage 0 — the domain, the schema and the fixtures ✓

**Done, before a single container existed.** The order matters and is worth stating,
because the common failure is the opposite one: stand up the stack first and work out
what the data means later.

Read a real power bill → derive the entities and the arithmetic → write the DDL →
generate fixtures from user scenarios → look at them.

- [x] **Three real NZ electricity bills analysed.** The bills supplied the entities,
      the charge groups, the rate bases, the TOU bands, line losses, GST and the ICP
      and plan-code structure. Design doc §8.1
- [x] **Two rules found only by reconciling the totals, not by reading the labels** —
      TOU bands are scoped per charge group (§8.2.1), and customers are billed on
      metered volume **plus** losses (§8.2.2). Stopping at the labels would have shipped
      both bugs: the second silently undercharges ~5% on every import line
- [x] `kea_source_schema.sql` and `kea_serving_schema.sql` written, split by **writer**
      rather than by subject — which is what makes "can we rebuild this?" answerable
- [x] **Nine fixtures generated from user scenarios**, each owning a private block of
      ids and each asserting its own totals. The six `bill_higher_*` files go further
      and assert *which driver wins* — so a scenario that stops teaching its lesson
      fails the transaction instead of loading wrong data
- [x] **Metabase** stood up over the serving schema — a schema explorer of 24 questions
      driven by one ICP filter, built by `analysis/metabase/build_dashboard.py` and
      rebuildable from it. It is the analysis surface for this data, and the one that
      reaches the Snowflake marts later too. §8

**Done when:** a bill reproduces from the fixture data to the cent, and every fixture
asserts its own totals. Both hold — Bill B is **$50.54**, and all nine load and pass on
PG16 and PG17.

What is finished here is the *domain and the tooling*. The analysis those tools exist for
— working through what the fixtures actually show — has not started; §8 is where that
lives, and it is not a stage gate.

**Why this is a stage and not a preface.** It produced the only *external* ground truth
in the project. Every later stage's exit criterion leans on it: Stage 4 is done when the
warehouse reproduces **$248.07** and **$50.54** from source data, and Stage 5 is done
when the pipeline's output agrees with the nine fixtures. Without Stage 0, "dbt build is
green" would only mean the pipeline agrees with itself.

It is also why `kea.mart_*` could be designed before there was anything to fill it: the
fixtures are a hand-built stand-in for the pipeline's output, and the pipeline is being
built to reproduce them.

### Stage 1 — the local stack boots ✓

**Verified 30 August 2026.** The first stage to move off "unbuilt".

- [x] `.env.example` documents every variable the compose file interpolates; `.env`
      generated for local dev with the Snowflake block left blank, since Stage 1 touches none of it
- [x] `make images` builds all four tags — `dataeng/airflow:3.3.0` (3.18 GB),
      `dataeng/spark:3.5.9` (2.35 GB), `dataeng/dbt:1.12.0` (1.08 GB), `dataeng/soda:latest`
- [x] `make up` brings 17 containers to running and they **stay** there: restart count 0
      across the board, and 0 error lines in the scheduler log
- [x] Every endpoint answers: Airflow 8080, MinIO 9000/9001, Spark 8082, Marquez 3000/5002,
      Prometheus 9090, Grafana 3001, Metabase 3002, statsd-exporter 9102, Vault 8200,
      serving-db 5433
- [x] Spark master reports **1 alive worker**
- [x] Airflow admin login issues a token
- [x] `minio-init` creates `kea-lake` and `checkpoints`

**Done.** Every UI answers and the loader's target is reachable.

### Stage 2 — source data exists

- [x] `src_billing`, `src_metering`, `src_catalogue` stood up from `kea_source_schema.sql` and populated
- [x] LCL generated into the `src_*` shapes (design doc §12) — 500 households, 18,796,453
      half-hourly reads across 710 NZ-local days (2024-09-18 → 2026-08-28), rising to
      19,125,023 once revisions and duplicate deliveries are injected. 64 plans, 11,506
      invoices. `make init-data`, proved end to end from a torn-down database
- [x] The UK → NZ transform proved before it touched any data — 30 unit tests, no database
      and no dataset required (`make test-transform`). It caught two silent faults: a UK
      spring-forward day that left every household two readings short on the same two days,
      and a fall-back day where the second of two readings sharing a wall clock overwrote
      the first
- [x] A rating engine that prices from `src_catalogue` — the first code here that joins
      `tou_windows` and `rate_components`. It reproduces Bill B line by line from metered
      half-hours; the total carries a one-cent residual on a levy line the printed bill
      contradicts itself on (design §10.3)
- [x] A realistic NZ defect profile injected synthetically — interval gaps, implausible
      spikes, stuck-meter zero runs, estimated and final flags, duplicate deliveries, orphan
      references, DST transitions. LCL is already clean, and §10.2 is explicit that this is
      how the quality rules get proven. `05_defects.sql`: 67,288 gap intervals in runs of
      2–96, 12,506 estimated household-days, 3,367 spikes, 355 stuck-meter days, 9,263
      duplicate deliveries, 20 closed accounts carrying `'F'`, 1 ambiguous register; plus
      3 orphan ICPs and 2 orphan plan codes from `07_billing_defects.sql`, which must run
      after rating because an account whose `PLAN_CODE` resolves to nothing cannot be rated
      at all. The manifest lives in `gen_audit`, which has no grant to
      any `kea_extract_*` role — in a `src_*` schema the `ALTER DEFAULT PRIVILEGES` at the
      foot of `kea_source_schema.sql` would hand the pipeline the answer key
- [x] **Negative reads are not among them, and that is deliberate.**
      `src_metering.interval_reads` carries `CHECK (kwh >= 0)`, which is the enforcement of
      design doc §8.2.3 — export is modelled by direction, not by sign. A negative read is
      therefore something the pipeline meets in a *file* and quarantines, never a row in the
      source; it belongs in Stage 3's bronze fixture. The source-side equivalent is export
      delivered on the import register: positive values, passes every constraint, corrupts
      the bill, and only a distribution check finds it. **Done** — two households, November
      2025, `src:export_on_import`
- [x] Revision mechanics confirmed: a correction arrives as a new row with a higher
      `version_no`. 394,481 rows at v2, 20 at v3 (so "latest wins" cannot be satisfied by
      accident with "max = 2"), and 394,481 superseded v1 rows still present and unmodified
- [x] **A three-week-old correction is waiting for Stage 3.** A 2026-07-20 reading, corrected
      and delivered 2026-08-25. An extract that filters on reading time will never see it;
      only one that watermarks on `received_at` will. That is Stage 3's exit criterion, and
      until now there was nothing for it to find

**Done when:** three source databases are populated and the extract has something real to read.
**Status: done, 1 Sep 2026.** `make init-data` builds it; `make assert-source` gates it —
15 assertions, including that the nine fixtures survive the run and that every identity
sequence is ahead of its table. Both of those exist because the generator broke them once.

Verified rather than assumed: `make init-data` was run end to end from a torn-down
database (no generated rows, no CSVs, no measured scale factor) and reproduced the cohort,
the 18,796,453 reads and every assertion. Defect and revision injection was then run twice
on identical input and compared by an order-independent checksum over full row content —
`setseed()` had not been enough, because it fixes the random stream and not which row
draws from it.

### Stage 3 — extract → bronze → parsed

- [ ] Three independent extract jobs on their own schedules; a billing failure does not block metering
- [ ] `interval_reads` watermarked on `received_at` and partitioned by `received_at` date range
- [ ] Bronze immutable and as-received, at `bronze/{source}/{entity}/load_date=…`, carrying
      `_extracted_at` / `_source_system` / `_source_file`
- [ ] Parse writes Parquet conforming to the Avro schema — reshaping only, no business logic

**Done when:** a three-week-old correction is picked up by an ordinary nightly run, and
re-running a load date changes nothing.

### Stage 4 — warehouse: Snowflake + dbt

- [ ] Staging models + `schema.yml` for all 12 source tables
- [ ] Silver: validation, quarantine routing, and key resolution — including the temporal
      plan resolution of §2.1.2, which is the one that fails silently
- [ ] Gold star schema, then `mart_*` per consumer
- [ ] `dbt build` green, including the test that a `tariff_component.band_id` references a
      `tou_band` of the matching `charge_group` — no SQL constraint can express it

**Done when:** both reconciliation bills reproduce from source data — **$248.07** and
**$50.54** — and a second build is a no-op.

### Stage 5 — export → the local serving Postgres

The target is `kea-serving-db` in this project's stack (§6.3). Reaching the application's database,
or RDS, is deferred — §6.4.

The schema already applies there and the nine fixtures already load into it — but that is
Stage 0's work, not this stage's. What Stage 0 provides is the **acceptance target**: the
pipeline is finished here when its own output stands where the hand-seeded fixtures stand
now.

- [ ] `COPY INTO` → `s3://kea-lake/export/{table}/`, then the loader
- [ ] The pipeline's own output reproduces what the fixtures assert — the point of this stage
- [ ] Per-table strategy honoured: truncate+reload for small tables in one transaction,
      DELETE-by-period for bills, **partition swap** for `meter_read_hh`
- [ ] Load order follows the FK graph as task dependencies, not a sleep — including
      `catalogue.plan` → `metering.network`, which reads like a sibling and is not
- [ ] `synced_at` set per table at commit time, not `DEFAULT now()` per row

**Done when:** a full load runs twice with identical row counts, a mid-table failure leaves
the table unchanged, and `app` is untouched.

### Stage 6 — orchestration

- [ ] `kea_daily_pipeline` runs the whole path on the 02:00 NZT schedule
- [ ] Separate DAGs for the different cadences: `kea_weather_load`, `kea_forecast_refresh`, `kea_scale_test`
- [ ] Backfill over a date range is idempotent, through the same DAG
- [ ] A failing `dbt_test` visibly blocks every export task

**Done when:** a month is backfilled, then backfilled again, and nothing changes.

### Stage 7 — quality gates & observability

This stage is about the **pipeline's** health — whether a bad load is caught and stopped.
The BI tools that read the serving data are Stage 0's, and §8 draws that line.

- [ ] Soda scans after `dbt_test`: freshness, volume within ±10% of trailing average, orphan rate
- [ ] OpenLineage from Airflow and Spark forms one graph in Marquez, source → serving
- [ ] Grafana also carries pipeline health from Prometheus, and an alert fires on a forced failure

**Done when:** an injected defect lands in quarantine with its reason, raises an alert and
stops the export — and the lineage graph covers source to serving.

### Stage 8 — testing & CI

- [ ] Unit tests for the parse job: reshaping, quarantine rules, DST-boundary day lengths
      (a local day is 46 or 50 half hours, never assume 48)
- [ ] CI green end to end: ruff, pytest, DAG import test, `dbt build` in an isolated schema dropped afterwards
- [ ] Versions reviewed and pinned — `postgres:17`, dbt, Soda

**Done when:** a PR cannot merge with a broken DAG, a failing dbt test, or a failing unit test.

### Stage 9 — streaming (Sprint 5)

The batch path ships first. Kafka, Schema Registry, the topics and Flink all sit behind
the compose `streaming` profile, and none of them run before this stage.

- [ ] A replay producer encodes the **same** `KEA-047` schema as Avro binary through Schema Registry
- [ ] Downstream cannot tell which path a row arrived by — the property that makes the two interchangeable
- [ ] Reconciliation between the two paths, with a stated tolerance

**Done when:** a day of meter reads replayed through Kafka produces the same marts as the
batch path, within tolerance.

---

## 6. How the stack is laid out

Four decisions about `../docker-compose.yml`, `../Makefile` and `../pipeline/` that are
easier to read here than to infer from the files: what things are named, where the code
lives, what this stack is and is not responsible for, and what is still unbuilt.

### 6.1 Naming

Locally-built images and the compose project need names, and two different jobs are worth
keeping apart.

**The compose project is `kea`.** That is what isolates this stack: Docker scopes named
volumes and the default network to the project name. It is set explicitly in the compose
file rather than derived from the directory, so renaming the checkout cannot orphan a
volume. It matches every infrastructure identifier already in the repo — the `kea`
database and role, `kea-db`, `kea-pgdata`, `kea_loader`, `kea_app`, `KEA_PROVIDER`, the
`kea` schema, `KEA-047`.

**The images are `dataeng/*`, deliberately not project-scoped** — `dataeng/airflow:3.3.0`,
`dataeng/spark:3.5.9`, `dataeng/dbt:1.12.0`, `dataeng/soda:latest`. They are named for
what they contain because that is all they contain: a base image plus libraries, with no
project code baked in. §7 covers what follows from that.

**Everything else that carries this project's identity is named for it**: the OpenLineage
namespace `kea_energy`, the StatsD prefix `airflow_kea`, the bucket `kea-lake`, the
Snowflake database `KEA`, the Kafka topics `meter_reads*`.

### 6.2 Where the pipeline code lives: `data/pipeline/`

The pipeline's code lives under **`data/pipeline/`**:

```
data/pipeline/
  airflow/dags/      the DAGs of design doc §9
  spark_jobs/        the one parse job of §1.1
  dbt_project/       staging → silver → gold → marts
  soda/              the runtime quality gate
  scripts/           the JDBC extracts and the Postgres loader
  monitoring/        prometheus.yml, grafana provisioning
  docker/dbt/        the dbt image, kept out of the Airflow image
```

Why `data/` rather than the top level or `services/`:

- **One owner.** The whole pipeline is the Data team's, and `data/` is already routed to
  them in CODEOWNERS. A top-level `pipeline/` needs a second ownership rule for the same
  people.
- **It sits next to what it reads.** The DDL it applies, the design that specifies it and
  the fixtures it is checked against are all in `data/`.
- **It is not a service.** `services/` holds things that serve traffic and each have a
  Makefile the root delegates to; the pipeline is a scheduled job.

**Why the names are not tidied.** `spark_jobs` and `dbt_project` would read better as
`spark` and `dbt`. They keep the conventional names for a stack of this shape, so anyone
who has worked on an Airflow-plus-Spark-plus-dbt layout recognises where things are
without reading a map. A marginally nicer name is not worth spending that recognition on.

`producer/` is deliberately absent for now — it is Sprint 5, and it should be created at
Stage 9 rather than stubbed empty today.

**One consequence to accept.** The root README says `data/` is "for the things that are
not runtime code". That stops being true and the line needs amending — the same way the
repo already accepts the Data team owning runtime code inside `services/api/`. Ownership
is a convention, not a directory.

### 6.3 Self-contained, and separate from the application's stack

This stack is **self-contained**: everything it needs is defined in this repo and built
from files in it.

```
data/docker-compose.yml               THE PIPELINE — name: kea
  minio · minio-init                  the lake, one bucket: kea-lake
  spark-master · spark-worker         built from pipeline/spark_jobs
  airflow x4 · airflow-postgres       orchestration
  kea-serving-db                      where the loader lands
  marquez(+db, web)                   namespace kea_energy
  statsd-exporter · prometheus · grafana · vault
  metabase(+db)                       analysis over the serving schema
  kafka · schema-registry · kafka-init · flink x2   (streaming profile, Stage 9)

infra/docker-compose.yml              THE APPLICATION — untouched
  db -> kea                           `make db` for the API and web teams
```

**Two Postgres instances, on purpose.** `kea-serving-db` here is the pipeline's output —
the local stand-in for RDS. `db` in `infra/` is the application's, on port 5432, owned by
the AWS team. They hold the same schemas and nothing reconciles them yet; that is a
deliberate deferral, §6.4.

An earlier draft merged the pipeline into `infra/docker-compose.yml`, and a later one
extracted the base services into a separate shared stack. Both are **withdrawn**. The
first put the pipeline's dependencies into a file another team owns. The second saved
memory but left this repo unable to start on its own, which is worse than the memory it
saved.

**dbt and Soda need an image each.** Not one between them: `dbt-snowflake` and
`soda-core-snowflake` together are unresolvable for pip (ResolutionTooDeep, on conflicting
`snowflake-connector`/`protobuf`/`otel` pins) — the same reason neither lives inside the
Airflow image, which its own Dockerfile documents. Hence `pipeline/docker/dbt` and
`pipeline/docker/soda`.

### 6.4 Getting pipeline output to the application — deferred

The pipeline lands processed data in its own local Postgres. Getting it from there to the
database the agent actually reads is **out of scope for now** and is not designed here.

When it is picked up, the shape to reach for is almost certainly *not* a database-to-database
sync. The loader already writes over a connection string, and §7.3 of the design doc
specifies the mechanics — transactional truncate+reload, DELETE-by-period, partition swap,
`synced_at` at commit time — so pointing the same loader at a second target is a config
change rather than a new mechanism.

**A version detail that constrains the mechanism.** This stack's Postgres containers run
**17**; the application's `db` in `infra/` runs **16-alpine**. They are independent
instances, so the difference is harmless today — and both DDL files are verified on both
(16.15 earlier, 17 on 30 Aug 2026). But `pg_dump` output is forward-compatible only: a
dump taken from 17 will not restore into 16. A dump-and-restore sync would be running in
the wrong direction and would fail. Either the loader writes to a second target over a
connection string — where server versions need not match — or `infra` moves to 17 first.
The former is cheaper, and is what this section already recommends.

**One constraint to carry forward whatever gets built:** the `app` schema must never be
copied. Conversation history is written by the agent at runtime, and design doc §1.1 calls
it the one thing in the system no re-run can recover. `kea_loader` has no grant on `app`,
not even `USAGE` — but that protection only holds if the mechanism is *the loader*. A
whole-database `pg_dump` and restore, or physical replication, would go straight through
it. That is the strongest reason to prefer a second loader target over a sync when the
time comes.

### 6.5 What is built, and what is not

**Done, and verified running** (30 Aug 2026 — see Stage 1). `../docker-compose.yml`, `../Makefile` and `../.env.example` have been rewritten
against §6.1–§6.3, and `../pipeline/` holds the images and config:

- The platform services are gone from this file; what remains is Airflow, its metadata
  Postgres, and `kea-serving-db`. Images are `dataeng/*` with **no `build:` stanza**, so
  `up` can only consume them.
- `name: kea` scopes the volumes; the private/`dataeng` network split is in place, and
  `kea-serving-db` is the only prefixed service on the shared network.
- OpenLineage namespace is `kea_energy`; the Snowflake database defaults to `KEA` and the
  dbt schema to `kea_staging`.
- `make up` guards on the `dataeng` network, the `dataeng/airflow` image and `.env`,
  failing with an explanation rather than a DNS error inside a container.
- The Make target vocabulary is the conventional one for this stack — `up`, `down`,
  `clean`, `dbt-build`, `dbt-test`, `soda-scan`. `spark-parse` is singular by design:
  §1.1 says one Spark job, so there is no bronze/silver/gold chain.
- `--env-file` replaces shell sourcing, which is also what lets the `&` inside the
  `AIRFLOW_CONN_*` URIs survive — sourcing them in a shell does not.

**Still outstanding:**

| What | Why it matters |
|---|---|
| `ecoANYTIME` carries most of the cohort | Ecotricity positions it at homes *without* a smart meter; that is read as marketing, not eligibility. LCL's `stdorToU='Std'` maps to it directly, and a fixed rate gives the bill explainer a case where a rise can only be volume or weather |
| `ecoWHOLESALE` is approximated, not modelled | Spot changes every half hour; `rate_components.unit_rate` is a scalar. A truthful spot plan needs ~17,500 rows a year. No reconciliation bill is on a spot plan |
| Only one of 64 plans is fully bill-sourced | Retail rates are quoted per address. Provenance is marked `[BILL]` / `[PUBLISHED]` / `[DERIVED]` / `[ASSUMED]` inline in `02_catalogue.sql` and summarised in `data/dictionary/` |
| `pipeline/` holds code directories that are still empty | The DAGs, the parse job, the dbt project, the Soda checks and the loader are Stages 3–6. The directory names are fixed now so nothing has to move later |
| Soda Core is unpinned | 3.5.6 is what the build resolved. Pinning it is Stage 8 |

---

## 7. Running alongside other stacks

This stack claims fifteen host ports, a Docker daemon, and a Snowflake account. If
anything else on the machine runs a data platform — another project, a colleague's
checkout, a course exercise — five things keep them apart. All are cheap to get right up
front and awkward to retrofit.

| | How this stack is scoped | Why it matters |
|---|---|---|
| **Compose project** | `name: kea`, set explicitly rather than derived from the directory | Docker scopes named volumes and the default network to it. A `down -v` elsewhere cannot reach this project's data, and renaming the checkout cannot silently orphan it |
| **Snowflake database** | `KEA`, not a schema inside a shared one | The important one — see below |
| **MinIO bucket** | one `kea-lake` bucket, with the design doc's prefixes inside it | Distinct from any bare `bronze`/`silver`/`gold` convention |
| **Metric prefix** | `airflow_kea` | If a Prometheus ever scrapes more than one Airflow, the series stay distinguishable |
| **Lineage namespace** | `kea_energy` | Marquez separates producers by namespace |

**Host ports are the one thing not isolated.** Airflow 8080, MinIO 9000/9001, Spark 8082,
Marquez 3000/5002, Prometheus 9090, Grafana 3001, Metabase 3002, Vault 8200,
`kea-serving-db` 5433. Only one stack using these can be up at a time. If that ever
becomes a problem, a `${PORT_OFFSET:-0}` on the published ports is the fix — not before
it is needed.

**Sharing a Snowflake account is safe; sharing a database is not.** `dbt build` drops and
recreates everything it owns, so a single wrong `DBT_SCHEMA` — a stale shell still
exporting an old value, a copied `profiles.yml` — points this project's build at another's
models and destroys them. A database boundary turns that into a permission error instead
of data loss, and Snowflake charges nothing for the extra database: you pay for storage
and compute, which are unchanged. Two consequences: credits are shared, so scope any
resource monitor per database rather than per account; and Stage 8's isolated CI schema
has to be isolated from every other consumer of the account, not only from local
development.

**The image tags are deliberately not project-scoped.** `dataeng/airflow:3.3.0`,
`dataeng/spark:3.5.9`, `dataeng/dbt:1.12.0` and `dataeng/soda:latest` are named for what
they contain, because that is what they are — a base image plus libraries, with no project
code in them. Everything project-specific is a bind mount: `airflow/dags`, `dbt_project`,
`soda`, `spark_jobs`, `scripts`. Docker stores an image once per tag, so anything else on
the machine built from the same Dockerfiles reuses one copy rather than carrying a second.

That is also why **`make up` does not pass `--build`**. A shared tag means a rebuild is a
side effect on everything using it, so building is `make images`, run deliberately. If the
Dockerfiles here are ever edited, rebuild explicitly and be aware of what else consumes
the tag.

## 8. Where analysis lives

Metabase is the workbench; this repo is the record. The distinction matters because a
saved question lives in **Metabase's own application database** — not in git, not in a
pull request, and not surviving a `docker compose down -v`.

`../README.md` sets the standing rule: every figure traces to a source, and `analysis/`
holds notebooks and scripts named `YYYY-MM-DD-what-it-answers`. So:

| Use | Where |
|---|---|
| Exploring, ad-hoc questions, browsable dashboards | **Metabase** — the only tool for this |
| Any number that reaches a slide, a demo line or the deck | A committed query or notebook in `../analysis/` |
| Pipeline health — DAG duration, failures, freshness | Grafana, from Prometheus. **Never** the serving data |
| Anything a customer sees | `services/web`. Not a BI surface |

**One tool for the data, one for the pipeline.** Grafana was used for the first look at
the fixtures and is not used for analysis from here — two dashboards over the same tables
drift, and only one of them would be the one anyone trusts. Grafana keeps the job it is
good at: time-series metrics about whether the pipeline ran. Metabase is also the only one
of the two that reaches Snowflake without an Enterprise licence, so the split holds when
the warehouse arrives.

**The analysis itself has not started.** What exists is a schema explorer — 24 questions,
one per table, plus the six forecast panels. Useful for looking at what is there, and not
the same thing as having asked the questions the fixtures were built to answer: whether
`base_load_kwh` really separates a fault from a new appliance, whether the weather
decomposition holds against the cold-snap fixture, whether the forecast band narrows for
the reason the design claims. That work is next, it happens in Metabase, and anything it
concludes lands in `../analysis/` as a committed query.

**When Snowflake joins**, two habits are worth starting with rather than retrofitting:
point questions at `mart_*` rather than raw tables, and turn on Metabase's query caching
before any dashboard auto-refreshes — each refresh is a warehouse query, and a dashboard
left open will keep a warehouse awake that would otherwise auto-suspend.
