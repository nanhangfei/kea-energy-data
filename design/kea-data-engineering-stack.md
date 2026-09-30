# Kea Energy — Data Engineering Stack

**Owner:** Harvey Nan (Data Engineer)
**Scope:** which tools run the pipeline, which versions, where they run, and the order they get built in.

**Companion:** `kea-data-platform-design.md` (what the pipeline does and why). That document
is the architecture; this one is the platform underneath it. Where the two touch — the
governing rule in §1.1 below, and the batch-delivery assumption in §1.2 — the design doc
cites this file rather than restating it.

**Scope boundary.** Open items and the work that unblocks them live in design doc §12.
This document's §5 is the *order* that work lands in and what has to be true before each
step is called finished. Two lists, one of what and one of when — not two task trackers.

> **Catalogue note, 8 Sep 2026:** figures below that say 64 plans, 500
> households, or name Contact-derived products describe the earlier full-scale
> run. The active compact Sprint 3 catalogue is now three Kea plans on Vector /
> Standard User; current counts and results are in
> `docs/sprints/sprint-3/data-plan.md`.

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
| `kea-source-db` | None: it stands in for the four source systems, which the pipeline reaches rather than hosts. Since §6.4 it holds only their `src_*` schemas and the audit rollups; it was `kea-serving-db` until then |
| `kea-db`, in `../../infra/docker-compose.yml` | RDS PostgreSQL. The application's database, and where the publisher lands |
| Marquez · Prometheus · Grafana · Vault | CloudWatch and Secrets Manager, or lift Marquez and Grafana as-is |
| Metabase | Metabase, or QuickSight. Analysis, not part of the pipeline |
| Kafka + Schema Registry (`streaming` profile) | MSK + Glue Schema Registry |
| Snowflake, database `KEA` | Snowflake — SaaS, no local substitute |

The served tables live in the application's database locally too, so the boundary the
publisher crosses on a laptop is the one it crosses in every shared environment: one
database, with ownership divided by schema.

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
| Pipeline PostgreSQL (the four sources, `kea-source-db`) | 17.10 | `../docker-compose.yml` | Stack |
| PostgreSQL (the application's; where the publisher lands) | 16-alpine | `../../infra/docker-compose.yml` | Serving |
| Apache Airflow | 3.3.0 | `../docker-compose.yml` | Orchestration |
| Apache Spark / PySpark | 3.5.9 (official `apache/spark` image; standalone via `spark-class`), with pyarrow 17.0.0 and openlineage-python 1.53.0 added and held by its `constraints.txt` (Delivery) | `../docker-compose.yml`, `pipeline/spark_jobs` | Lake |
| MinIO / mc | RELEASE.2025-09-07 / 2025-08-13 (community edition archived Apr 2026 — last releases) | `../docker-compose.yml` | Lake |
| PostgreSQL (Airflow + Marquez metadata) | 17.10 | `../docker-compose.yml` | Orchestration |
| Marquez (+ web UI) | 0.50.0 | `../docker-compose.yml` | Quality gates |
| Prometheus / statsd-exporter | v3.13.1 / v0.29.0 | `../docker-compose.yml` | Quality gates |
| Grafana | 13.1.1 | `../docker-compose.yml` | Quality gates |
| Metabase | v0.63.15 (on its own Postgres 17.10 — the bundled H2 corrupts) | `../docker-compose.yml` | now |
| Vault | 1.20.4 | `../docker-compose.yml` | stretch |
| Kafka + Schema Registry (Confluent Platform) | cp 8.3.0 (≈ Kafka 4.x, KRaft-only) | `../docker-compose.yml`, `streaming` | Sprint 5 |
| Apache Flink | 2.3.0 (java 17, Scala-free) | `../docker-compose.yml`, `streaming` | Sprint 5 |
| Snowflake | SaaS (no version) | — | Warehouse |
| dbt (dbt-snowflake) | 1.12.0 with dbt-core 1.12.3; every other package in the image held by its `constraints.txt` (Delivery) | `pipeline/docker/dbt` | Warehouse |
| Soda Core | 3.5.6; every other package in the image held by its `constraints.txt` (Delivery) | `pipeline/docker/soda` | Quality gates |

**Two version facts: one still open, one settled in Delivery.**

The serving store runs Postgres **16** locally while the two metadata databases run
**17**. Design doc §12 carries the matching open item: all three DDL files apply cleanly
on 16.15 and Bill B reconciles to $50.54 there, but PG17 itself is unverified. The
metadata databases are not the thing to verify against — they hold Airflow and Marquez
state, not the serving schema.

A tag like `postgres:17` is a **floating tag**: it tracks the latest patch release, so the
same compose file pulls a different server once Docker Hub moves it, and Hub's had already
moved to 17.11 while every container here ran 17.10. `python:3.11-slim` moves the same way
with each Python and Debian update. So every tag here names a release (`postgres:17.10`,
`python:3.11.16-slim`), and every image built here installs its Python packages from a
constraints file. The dbt, Soda and Spark images each carry a `constraints.txt` that is the
image's own `pip freeze`, and the Airflow image uses Airflow's published constraints for
3.3.0. After pinning, each rebuild resolved exactly what the image it replaced held.
`dataeng/soda:latest` keeps its name: it is a local tag, and what it holds is pinned. The
application's `postgres:16-alpine`, in `../../infra`, is the backend's to pin; CI's throwaway
runs `postgres:16.15-alpine`, the release kea-db runs today.

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
| Monitoring | StatsD → Prometheus → Grafana. Pipeline health only — the serving data is read in Metabase, §9 | — |
| Reconciliation | **$50.54** (TOU + solar, 24 days) recomputed end to end from source. **$248.07** (flat, 31 days) is a documented target with no source data behind it — it stays as the evidence for the catalogue's `[DERIVED]` daily rate | §10.3 |
| Blast radius | `kea_loader` has no grant on `app` — not even `USAGE`. Conversation history is the one thing no re-run can recover | §1.1 |
| A new model is shown to be right | Answers planted into the generated source and scored by `make evaluate-sprint4`, on thresholds written before the first run. The report records misses as misses; two of fourteen are unmet and neither was rewritten | §15.10 |

---

## 5. Build roadmap

One section per step, each with an exit criterion that has to be **demonstrably true** — not "the code
exists". A box is ticked only when there is something in the repo to point at, which is
the standard the rest of `data/` is held to.

> **The fixtures (Sources, part 1) are why every step after them has acceptance criteria
> at all. Every section but Delivery and Streaming is done and verified.** The Warehouse step's criterion was Bill B
> reconciling from source at `$50.54` with a second build proven a no-op;
> `mart_scale_test` stays deferred (§6.5) and Bill A has no source data.
>
> **Streaming was to be built before Delivery; Delivery's CI and deploy came
> first anyway** (15 Sep 2026). Until then a PR could merge with a broken DAG, a dbt
> model with a missing ref or an edited serving migration, and nothing tied the
> served release to a merged commit. Streaming's checks join `make ci` when it
> lands.
>
> **⚠️ Sprint 3's source work sits outside this roadmap and changed its
> premises.** On 2026-09-04/05 the data span moved to 2025-08-01 → 2027-08-27 so
> the newest reading is ahead of today rather than behind it, the four
> Ecotricity plans were replaced (first by Contact-derived products and now by
> three Kea designs), and term
> commitments, payments and plan history were added to the source. None of that
> is a step — the roadmap is about the pipeline's capabilities, not its
> contents — but every figure quoted below is from after it.
> See `docs/sprints/sprint-3/`.
>
> **⚠️ Sprint 4's stretch goals sit outside this roadmap too, and added a step
> the roadmap had no name for.** Detected loads, the solar and EV what-ifs and
> the hardship early warning are new *models*, not new pipeline capabilities:
> migration V005, five new dbt models (three silver, two marts), two more jobs in
> the same publication, and nothing in the stack that was not already there
> (design doc §15). What is new
> to this document is how they were accepted — `make evaluate-sprint4` scores
> the pipeline against answers the source generator planted, on thresholds
> written before the first run, and 2 of 14 came back missed. Neither was
> rewritten. A step's exit criterion asks whether the pipeline *runs*; this
> asks whether what it publishes is *true*, and the two are not the same
> question. See `analysis/sprint4_evaluation/` and design doc §15.10.

### Steps

**The sections below follow the Makefile's step order — the order the platform runs in.**
Orchestration comes last because it triggers the steps before it. Analysis has no section here
(§9 is its home), and Quality gates has no step of its own, so it sits after Serving, whose
gates it wrote. Since 29 Sep 2026 the Makefile, Airflow and the Metabase dashboard titles all
use these names: one `make` command per step, each safe to repeat, and each running the same
scripts in the same order as its Airflow tasks.

Older commit messages, code comments and two scripts' printed output (`assert_quality.sh`,
`rebuild_span.sh`) still number these as build stages, in the order they were built:
0 Stack, 1–2 Sources, 3 Lake, 4 Warehouse, 5 Serving, 6 Orchestration, 7 Quality gates,
8 Delivery, 9 Streaming.

| Step | Command | In Airflow |
|---|---|---|
| Stack | `make up` | — |
| Sources | `make init-sources` | — |
| Lake | `make ingest-date` | `kea_extract_metering` / `_billing` / `_catalogue` / `_weather`, tagged `ingest-date` |
| Warehouse | `make build-warehouse` | `kea_daily_pipeline`, group `build-warehouse`: `snowflake_stage → dbt_run → dbt_test → soda_scan_warehouse` |
| Serving | `make publish-serving` | `kea_daily_pipeline`, group `publish-serving`: `export_warehouse → publish_serving → assert_serving → soda_scan_serving` |
| Quality gates *(no step of its own)* | its gates end Warehouse and Serving; its proof is `make run-pipeline VERIFY=1` | `soda_scan_warehouse`, `assert_serving`, `soda_scan_serving` |
| Analysis | `make dashboards`, `make evaluate-sprint4` | `kea_daily_pipeline`, group `dashboards`: `summarise_lake`, as `kea_lake_audit` |
| Delivery | `make ci`, `make deploy` | — (GitHub Actions, and a person) |
| Orchestration | `make run-pipeline` | triggers `kea_daily_pipeline` |
| Streaming *(with the stack)* | `make up STREAMING=1` | — |

`make rebuild-all` is every step from Stack to Analysis in sequence. `kea_forecast_refresh` is
the Warehouse and Serving groups again, for one mart.

### Stack — `make up` ✓

**Verified 30 August 2026.** The first step to move off "unbuilt".

- [x] `.env.example` documents every variable the compose file interpolates; `.env`
      generated for local dev with the Snowflake block left blank, since the Stack step touches none of it
- [x] `make images` (now `make up BUILD=1`) builds all four tags — `dataeng/airflow:3.3.0` (3.18 GB),
      `dataeng/spark:3.5.9` (2.35 GB), `dataeng/dbt:1.12.0` (1.08 GB), `dataeng/soda:latest`
- [x] `make up` brings 17 containers to running and they **stay** there: restart count 0
      across the board, and 0 error lines in the scheduler log
- [x] Every endpoint answers: Airflow 8080, MinIO 9000/9001, Spark 8082, Spark History 18080, Marquez 3000/5002,
      Prometheus 9090, Grafana 3001, Metabase 3002, statsd-exporter 9102, Vault 8200,
      source-db 5433
- [x] Spark master reports **1 alive worker**
- [x] Airflow admin login issues a token
- [x] `minio-init` creates `kea-lake` and `checkpoints`

**Done.** Every UI answers and the loader's target is reachable.

### Sources, part 1 — `make init-sources`: the nine fixtures ✓

**Done first, before a single container existed.** It comes after the stack
because that is where a first run executes it: its schema and fixtures load into
the Stack's database. The order the work was *done* in still matters, because the
common failure is the opposite one: stand up the stack first and work out what the
data means later.

Read a real power bill → derive the entities and the arithmetic → write the DDL →
generate fixtures from user scenarios. Looking at them is a dashboard's job, and every
dashboard is described in one place, §9.

- [x] **Three real NZ electricity bills analysed.** The bills supplied the entities,
      the charge groups, the rate bases, the TOU bands, line losses, GST and the ICP
      and plan-code structure. Design doc §8.1
- [x] **Two rules found only by reconciling the totals, not by reading the labels** —
      TOU bands are scoped per charge group (§8.2.1), and customers are billed on
      metered volume **plus** losses (§8.2.2). Stopping at the labels would have shipped
      both bugs: the second silently undercharges ~5% on every import line
- [x] `kea_source_schema.sql` and `kea_serving_schema.sql` written, split by **writer**
      rather than by subject — which is what makes "can we rebuild this?" answerable.
      ⚠️ The serving file is a *reference* now: since 2026-09-12 the served tables are
      created by `migrations/serving` in the backend's database, and this file only supplies
      the scaffolding `seed_fixtures.sh` creates and drops inside the fixtures' transaction
- [x] **Nine fixtures generated from user scenarios**, each owning a private block of
      ids and each asserting its own totals. The six `bill_higher_*` files go further
      and assert *which driver wins* — so a scenario that stops teaching its lesson
      fails the transaction instead of loading wrong data

**Done when:** a bill reproduces from the fixture data to the cent, and every fixture
asserts its own totals. Both hold — Bill B is **$50.54**, and all nine load and pass on
PG16 and PG17.

`make init-sources` builds it, and is safe to repeat; `RESEED=1` replaces the fixtures
after a `sample/*.sql` edit and reloads the generated cohort in the same run. A fixture's weather seed
is checked by every `make build-warehouse` and regenerated by `REGEN=1`, in the Warehouse step
because that is what reads it.

What is finished here is the *domain and the tooling*. The analysis those tools exist for
— working through what the fixtures actually show — is §9's, and it gates no step.

**Why this is part of the roadmap and not a preface.** It produced the only *external* ground truth
in the project. Every later step's exit criterion leans on it: Warehouse is done when
Snowflake reproduces **$50.54** from source data, and Serving is done
when the pipeline's output agrees with the nine fixtures. Without the fixtures, "dbt build is
green" would only mean the pipeline agrees with itself.

It is also why `kea.mart_*` could be designed before there was anything to fill it: the
fixtures are a hand-built stand-in for the pipeline's output, and the pipeline is being
built to reproduce them.

### Sources, part 2 — `make init-sources`: the generated cohort ✓

- [x] `src_billing`, `src_metering`, `src_catalogue` and `src_weather` stood up from
      `kea_source_schema.sql` and populated by `make init-sources` — 4 stations and 69,569 daily
      observations among them. ⚠️ Weather is the one source nothing fetches: `load_niwa_weather.py`
      reads zips a person downloaded from NIWA DataHub, because the licence is accepted per
      dataset by a person and not by a scheduler, so `init-sources` loads `src_weather` when
      `raw/niwa/` holds them and says it skipped when it does not
- [x] LCL generated into the `src_*` shapes (design doc §12) — **50 households**, 2,137,752
      half-hourly reads across 757 NZ-local days (2025-08-01 → 2027-08-27), of which
      44,275 are revisions carrying `version_no > 1`. 15 plan versions (three Kea codes at
      two versions each, plus the fixtures' nine), 1,224 invoices, 1,181 payments. The nine
      fixture households add 44,044 reads and 29 invoices on their own 2023-06-01 →
      2024-08-31 span. Counted from `kea-source-db` on 2026-09-27, on what `make
      rebuild-all` builds; the archived `COHORT_SIZE=500` run is not what this line
      describes. `make init-lcl` (now `make init-sources`), proved end to end from a torn-down database
- [x] The UK → NZ transform proved before it touched any data — 100 unit tests, no database
      and no dataset required (part of `make ci ONLY=test`). It caught two silent faults: a UK
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
      how the quality rules get proven. `05_defects.sql`: 7,965 gap intervals in runs of
      2–96, 1,378 estimated household-days, 334 spikes, 45 stuck-meter days, 1,030
      duplicate deliveries, 5 closed accounts carrying `'F'`, 1 ambiguous register; plus
      3 orphan ICPs and 2 orphan plan codes from `07_billing_defects.sql`, which must run
      after rating because an account whose `PLAN_CODE` resolves to nothing cannot be rated
      at all. The manifest lives in `gen_audit`, which has no grant to
      any `kea_extract_*` role — in a `src_*` schema the `ALTER DEFAULT PRIVILEGES` at the
      foot of `kea_source_schema.sql` would hand the pipeline the answer key
- [x] **Negative reads are not among them, and that is deliberate.**
      `src_metering.interval_reads` carries `CHECK (kwh >= 0)`, which is the enforcement of
      design doc §8.2.3 — export is modelled by direction, not by sign. A negative read is
      therefore something the pipeline meets in a *file* and quarantines, never a row in the
      source; it belongs in the Lake's bronze fixture. The source-side equivalent is export
      delivered on the import register: positive values, passes every constraint, corrupts
      the bill, and only a distribution check finds it. **Done** — two households, November
      2025, `src:export_on_import`
- [x] Revision mechanics confirmed: a correction arrives as a new row with a higher
      `version_no`. 44,255 rows at v2, 20 at v3 (so "latest wins" cannot be satisfied by
      accident with "max = 2"), and 44,255 superseded v1 rows still present and unmodified
- [x] **A three-week-old correction is waiting for the Lake.** A 2026-07-20 reading, corrected
      and delivered 2026-08-25. An extract that filters on reading time will never see it;
      only one that watermarks on `received_at` will. That is the Lake's exit criterion, and
      until now there was nothing for it to find

**Done when:** all four source schemas are populated and the extract has something real to read.
**Status: done, 1 Sep 2026.** `make init-sources` builds it and gates it in the same run —
`99_assert.sql`, 16 numbered checks, including that the nine fixtures survive the run and
that every identity sequence is ahead of its table. Both of those exist because the
generator broke them once.

It is safe to repeat: the 1.2 G archive is fetched only when neither the extracted blocks
nor the zip is on disk, and the load clears the previous run's rows before it writes.
`COHORT_SIZE=500` builds the archived full-scale profile instead of the 50-household one.

Verified rather than assumed: `make init-lcl`, now part of `make init-sources`, was run end to end from a torn-down
database (no generated rows, no CSVs, no measured scale factor) and reproduced the cohort,
the reads and every assertion. Defect and revision injection was then run twice
on identical input and compared by an order-independent checksum over full row content —
`setseed()` had not been enough, because it fixes the random stream and not which row
draws from it.

### Lake — `make ingest-date` ✓

- [x] Four independent extract jobs on their own schedules; a billing failure does not block metering.
      `kea_extract_metering`, `kea_extract_billing`, `kea_extract_catalogue`, `kea_extract_weather`
      — separate DagRuns, separate schedules, separate credentials (design §9). Weather is `@monthly`,
      not daily, because NIWA republishes the whole history monthly. The four `kea_extract_*` roles had
      `LOGIN` and no password and could not connect at all; `set_extract_roles.sh` sets them out
      of band and `assert_isolation.sh` proves what the grants buy in 20 checks — `make
      ingest-date` runs both, first and last.
      **The artefact is a forced failure**, because independence is not provable by inspection:
      `make ingest-date VERIFY=1` breaks the billing credential and shows metering completing anyway
- [x] `interval_reads` watermarked on `received_at` and partitioned by `received_at` date range.
      One literal `received_at` range per Spark JDBC predicate — 24 on an ordinary NZ day, 23 or 25
      across a DST transition (design §5.1). **A load date is an NZ calendar day** (design §4.2),
      which is the whole ballgame: the UTC window for 2026-08-25 holds 2,759 rows and *none* of the
      planted late revision; the NZ window holds 2,864 and all 24 of it
- [x] Bronze immutable and as-received, at `bronze/{source}/{entity}/load_date=…`, carrying
      `_extracted_at` / `_source_system` / `_source_file`. `read_status` is still `read_status` in
      bronze: nine source columns plus exactly the three underscore-prefixed ones (design §4.2)
- [x] Parse writes Parquet conforming to the Avro schema — reshaping only, no business logic — for
      **all seventeen entities**. KEA-047 is hand-written; the other sixteen are derived by `gen_avsc.py`
      from `information_schema`, with `--check` catching a contract that has drifted from the source
      it describes. Conformance is checked against the Parquet *footer*, which caught a real defect:
      Spark 3.5 defaults to `INT96` timestamps (design §4.2)

**Done when:** a three-week-old correction is picked up by an ordinary nightly run, and
re-running a load date changes nothing.

**Status: done, 2 Sep 2026.** Both exit conditions are checked by a command, not asserted:
`assert_bronze.py` (12 checks, 13 with `--expect-contiguous`, on top of `assert_isolation.sh`'s
20), which `make ingest-date` ends on — since 29 Sep 2026 on the partition the run wrote, over
the window its manifest records, and over the whole lake only when run without `--load-date`, and the replay proof `assert_idempotent.sh`, which it runs under
`VERIFY=1`.

⚠️ **A lane has to carry its whole source, and the metering one did not until 28 Sep 2026.**
`interval_reads` is the only entity big enough for Spark (design §5.1); the other sixteen are
scripts, and `_kea_common.scripted_dag` gives billing, catalogue and weather a task each.
`kea_extract_metering` was hand-written and held only the Spark pair, so `networks`,
`connection_points`, `meters` and `registers` reached bronze **only when a rebuild script
looped every non-Spark entity**. Nothing reported it: the nightly stage skips an entity with
no sealed partition, counts it as skipped rather than failed and exits 0, so `KEA.RAW` went on
serving whatever the last rebuild had put there. The four are `scripted_task`s in that DAG
now, and its asset moved from `parse` onto a `bronze_ready` join — Airflow marks an asset
updated when **any** task carrying it as an outlet succeeds, so leaving it on `parse` would
have announced metering's bronze while the snapshots were still running, and the four tasks
would have changed nothing.

The manual path had the same shape, and closed with it. **`make ingest-date LOAD_DATE=…` is
the whole Lake step in one command**, and it is safe to repeat: role passwords, a contract drift
check, the Spark extract and parse for `interval_reads`, `extract_all_small.py` over the
other sixteen, then the gate. Every extract step treats a sealed partition as immutable, so a
second run costs the gate and nothing else; `FORCE=1` rewrites the date's partitions and
`VERIFY=1` adds the replay proof, which re-extracts deliberately and is the one step here
that is not free to repeat. It replaces nine separate targets, of which `extract-metering`
drove one entity of seventeen while reading as though it covered the source.

Two of the gate's checks carry most of the weight. Each partition must hold **exactly** what its
window selects at source with no `read_id` in two partitions — if the windows overlapped rows
would be double-counted, if they gapped rows would vanish, and both look like ordinary variation
in a daily row count. And the late-revision check is paired with a **negative control**: a window
computed on `interval_start` must find *none* of it, because without that the positive proves
very little — the wrong watermark still returns ~2,800 perfectly reasonable rows.

**The gate is known to fail, not just to pass.** One manifest's `source_row_count` was changed
from 26,616 to 99,999; it reported `FAIL 9 … sealed 99,999 now 26,616`, named the partition, ran
the other checks anyway, and exited 1. **The backfill is the same DAG, no second code path**
(design §11): 29 runs via `airflow backfill create`, all green over a contiguous
2026-08-02 … 2026-08-30 — **31 partitions, 734,124 rows, every `read_id` distinct** — with
`--expect-contiguous` passing. Both were done on the full-scale lake, and the row counts are
that run's; what they demonstrate is the behaviour, which has not changed.

`make ci ONLY=test` runs 370 checks on the host with no database, no lake and no Spark — 270 over
`pipeline/tests` and 100 over the generator's; `make ingest-date VERIFY=1` runs 22 more against
the lake.

**"All the entities" is a claim about the lake, checked there** (3 Sep 2026), not
inferred from `spec.ENTITIES`. Every one of the seventeen is present in `s3://kea-lake/`
in *both* layers. Weather landed on two load dates — 69,569 observations and 4 stations,
`row_count` equal to `source_row_count` on both — and 69,569 is the same figure the NIWA
dictionary and the loader independently report, so the lake agrees with the prose.

**What the lake holds today** is the demo layout, not a nightly one: a single raw
and a single parsed partition for `interval_reads` at `load_date=2027-08-28`, **2,180,481
rows**, carrying all **757** reading days at **44–58 ICPs a day** (`lake_audit.partitions`
and `lake_audit.reads_daily`, 2026-09-28). Nothing in it comes from the full-scale run —
the lake can only carry what Sources generated, so a 50-household source gives a
50-household lake, and no partition anywhere holds the ~26,900 reads a day the archived one
did.

⚠️ **A parsed partition count one above raw is by design, not drift.** The extra is
`load_date=2000-01-01`, written by `test_lake_conformance.py`, which picks a date of its own
so the fixture never collides with real data. The gate cannot see it —
`_bronze_load_dates()` discovers load dates from bronze, and the fixture writes only parsed
and quarantine.

### Warehouse — `make build-warehouse` ✓

⚠️ **The figures in Warehouse, Serving, Quality gates and Orchestration describe a cohort that is not the one you
build today.** They were measured on the archived full-scale run (`COHORT_SIZE=500`: 499
ICPs, 11,528 bills, 3,229,225 `fct_meter_read` rows). `make rebuild-all` builds **50**
households, and these live in Snowflake, so recounting them needs a full-scale rebuild and a
warehouse connection. Sources and Lake above were recounted on the demo profile on 2026-09-28.

- [x] Staging models + `schema.yml` for all **seventeen** source tables. `stg_<source>__<entity>`
      as views (design §6.1), renaming and casting only — business keys and `version_no` are
      carried through unresolved and undeduped, because both are silver's job and doing either
      here destroys the evidence the Lake's replay proof rests on
- [x] Silver: **key resolution, quarantine routing and temporal plan resolution**. Three
      append-only key maps (§6.2), dedup on `(icp, register, interval_start)` keeping the
      highest `version_no` (§4.1), and all four of §6.3's integrity classes into one
      quarantine table with one vocabulary. **dbt's own validation rejections are not built**
      — `reject_reasons.json`'s `validation` group is still empty — so this box covers three
      of the four words in it
- [x] Gold star schema. `dim_customer`, `dim_icp`, `dim_register`, `dim_plan`, `dim_date`
      and the two facts build and hold together as a star — no orphan fact row against any
      dimension, and `fct_meter_read` unique on `(icp_id, register_id, interval_start)`,
      which is `metering.meter_read_hh`'s serving PK asserted in the warehouse rather than
      discovered half way through a Serving load. `dim_register` was not in §6.4's diagram
      and is not optional — the fact is grained on register
- [x] `mart_*` per consumer — `mart_daily_usage`, `mart_hourly_profile`,
      `mart_account_context`, `mart_bill_explanation`, `mart_forecast_input`, plus
      `mart_load_log`. **`mart_scale_test` is DEFERRED** (§6.5), not missed: it is a
      2.45B-row re-rate against a warehouse holding 9.7M, and 2.45B is Kea's production
      projection rather than anything this cohort has. The box is ticked on the five that
      serve a consumer; the sixth serves a capacity question this environment cannot ask
- [x] `dbt build` green, **including the band/charge-group test**. A foreign key can check
      that `window_id` exists; it cannot check that the window belongs to the SAME
      `charge_group` as the component pointing at it, and the serving schema declares neither
      a composite key nor a trigger. Checked rather than vacuous: 400 of 779 components are
      in scope, all 400 match, and **4,985 same-plan (component, window) pairs have differing
      charge groups** — that many distinct ways for one mis-typed `window_id` to produce a
      plan that loads, balances and is quietly wrong

**Done when:** **Bill B** reproduces from source data at **$50.54** and a second build is a
no-op. **Status: done, 3 Sep 2026.** `dbt build` is **PASS=188 ERROR=0** across staging,
silver, gold and marts; Bill B reconciles group by group against §10.3 — Network `$29.22`,
Energy `$21.10`, Other `$0.22`, across 11 lines — through bronze, `KEA.RAW`, staging,
silver's temporal plan resolution and gold, asserted nowhere along the way; and every mart
and fact hashes identically across two builds.

⚠️ **Narrowed from "both bills", and not for convenience.** Bill A is not in `src_billing` —
no invoice carries its `$248.07`, and its plan `VECT_OFPK_STD_140520` is absent from
`src_catalogue`. Reproducing it would mean inventing a tariff and 31 days of readings that
add up to `$215.71`, then reconciling against them, which tests the invention. It is **not
deleted and must not be**: its `$23.61` of "Other" over 31 days and 1,034.665 kWh supplied
the earlier Contact-derived catalogue's non-levy residual, and it remains a documented
reconciliation target with no source data behind it — different from a target that was
dropped. Making it live would be a Sources change.

**Snowflake is its own account** (AWS ap-southeast-2; the identifier lives in `.env`), not a
database on the sibling project's. Kea connects as `KEA_TRANSFORMER` against `KEA_WH` — never
`ACCOUNTADMIN`, because `dbt build` drops and recreates what it owns and the blast radius of
a wrong `DBT_SCHEMA` should be a permission error. `snowflake/bootstrap.sql` creates all of
it and is run once, by hand: every container authenticates as the role that file creates, so
it cannot bootstrap itself, and a `make` target would need an ACCOUNTADMIN credential in
`.env`.

**Snowflake cannot reach the local MinIO**, so the lake cannot be an external stage and dbt
cannot read Parquet where it lies. `pipeline/scripts/warehouse/load_bronze_to_snowflake.py` (`make
build-warehouse LOAD_DATE=…`) pushes parsed bronze up with PUT + COPY, and the mode split is the one interesting decision in it: a
snapshot's partition is a fresh full copy, so RAW is truncate-and-reload and holds only the
current picture, while an incremental entity's partitions are disjoint by construction, so
RAW accumulates them and idempotency comes from deleting the load date's own rows first.
**That split is what keeps staging honestly 1:1** — otherwise every model needs a "latest
load_date" filter, which is business logic wearing a mechanical disguise.

**The key maps are append-only, proved rather than intended**: all three hashed, the project
rebuilt, hashed again, identical. `incremental` is what enforces it — a `table`
materialisation renumbers every id on each run and satisfies "stable" only while the input
never changes order. Sequential rather than a hash surrogate, because `kea_serving_schema.sql`
types these `bigint GENERATED ALWAYS AS IDENTITY` and Serving has to land in that column.
**`key_map_plan` holds 73 versions across 41 plan codes**, which is design §6.2 rule 2 made
concrete: a map keyed on `plan_code` alone would collapse those 73 to 41 and make temporal
resolution impossible.

**Temporal plan resolution is exercised, and the number is large.** Against the full history
a code-only lookup would mis-rate **2,878 of 11,534 invoices, 25%** — each one a bill rated
against a tariff version that was not in force, with nothing failing and every total still
balancing. §2.1.2 calls this the most dangerous step in the pipeline; it is a measured
quarter of the book rather than a warning.

⚠️ **Comparing a `TIMESTAMP_TZ` to a `DATE` resolves the instant in the SESSION timezone**,
which Snowflake defaults to `America/Los_Angeles`, and every invoice resolved a day early: a
period ending midnight NZ is 05:00 the previous day there. On a period end that lands on a
version boundary, that silently picks the wrong tariff — §2.1.2 exactly.
`int_billing__plan_resolution` converts to an NZ date explicitly; `bootstrap.sql` also sets
the session timezone, but as defence in depth, because a correctness property that depends on
a session parameter is not a property.

**Determinism needs a total ordering.** `int_metering__reads_deduped` ranked on `version_no`
alone, and a duplicate is the same reading at the *same* version, so `row_number()` broke ties
arbitrarily and a different `read_id` survived each build — with the uniqueness test passing
throughout, because exactly one row survives either way. Ranking on
`(version_no desc, read_id desc)` makes the ordering total, and
`assert_dedup_ordering_is_total` guards the property a single build *can* check. The same
model's `version_count` counted **rows**, so a reading delivered twice at v1 reported 2 and
told a bill explanation it had been corrected; it counts distinct versions now.

**The quarantine finds exactly what Sources plants, by an independent path** — three orphan
ICPs and two accounts on `ECO_GONE_VECT_STA`, which `07_billing_defects.sql:58` asserts, the
injector and the detector having never spoken to each other. §6.3's retry rule is demonstrated
too: the three orphans reported `integrity.timing_skew` at misses 1 and 2 and promoted to
`integrity.orphan_icp` on the third. **All four integrity classes fire** — `no_plan_version`
6, `orphan_icp` 3, `unknown_plan_code` 2, `ambiguous_register` 1 — and a further **26 invoices
match two versions**, an overlapping-version defect in the catalogue that the model surfaces
rather than hides.

**The register rule Sources asked for**: filter to meters live at the reading's instant, and
quarantine if more than one survives — never pick. A naive join through `registers` inflates
by **750 rows and 447.05 kWh** on one household, with no error and a plausible grand total.
⚠️ That injection is not idempotent — `05_defects.sql` clears `gen_audit.defects` each run but
not the meters it inserts, so the code says "two meters" and there are six. The rule handles
any number of candidates.

**Two deviations from §6.4, both deliberate.** `dim_date` does **not** carry heating degree
days: HDD is regional, four values per day, so it would force the date dimension to a
`(date, region)` grain and stop it being conformed. It lives in `fct_weather_daily` at its
true grain, and §6.4's "no join at query time" is met by the marts denormalising it. And
`is_public_holiday` is **NULL, not false** — there is no NZ holiday calendar anywhere in this
repo, named in three design documents and backed by no data. NULL says unknown; false would
assert Waitangi Day is an ordinary Tuesday. ⚠️ The nearest-station weather fill is still
unimplemented, so a regional gap in `fct_weather_daily` is a null rather than a substitution;
the rule is specified in `dictionary/niwa-weather-source.md`, and the marts are where it
belongs.

**`mart_forecast_input`, and the invariant it is built around.** §8.6 states that across one
cycle `hdd_to_date + hdd_expected_remaining` is **constant**, which fixes the shape of the
calculation: the expected total is decided **once** per period and the remainder derived as
`total − to_date`, rather than the remaining days being forecast independently, which would
let the sum drift as actuals arrived. A singular test asserts it over all **11,440 cycles**;
the only movers are the **1,645** where a colder-than-normal period pushed `hdd_to_date` past
the expected total and the clamp engaged, named as the exception rather than tolerated as
slack.

⚠️ **§8.6 requires `hdd_expected_remaining` and never says where the expectation comes from.**
A forecast API would cover a fortnight of a month-long cycle and nothing after it.
`int_weather__climate_normals` answers the whole horizon from data already in the warehouse —
mean HDD per `(climate_region, month, day)` over **30 years** of the NIWA series. Thirty and
not the full 73: a normal is a climate baseline, and including the 1950s would bias every
forecast toward a colder past.

⚠️ **A completeness check has to verify the inputs it needs, not the ones it can see.**
`days_without_weather` counts rows that are present and null, so it cannot see a day with **no
row at all**, and a cycle whose metering covered 2 of its 31 days passed the hedge on a
fifteenth of the period. `days_observed = days_elapsed` is required too, which removed 9,636
false positives. This is the third time the same shape has appeared in the pipeline.

⚠️ **`cost_to_date` is an estimate at the prior period's rate, not a rating.** A true one needs
the tariff applied to a partial period — `tou_windows`, `rate_components`, band scoping —
which is the same rating engine `delta_from_tariff` is missing in §8.5. NULL where there is no
prior bill, rather than 0.

**The marts, and what they demonstrate.** `mart_daily_usage` is where the NIWA series reaches
a household-day, and where the NZ day's real length is used rather than assumed:
`expected_intervals` is **46 / 48 / 50**, computed from the calendar, and a hard-coded 48 makes
two days a year look 4% short and 4% over. Energy is conserved through the rollup.
`mart_hourly_profile` converts to **NZ local hours**, and the check that it worked is the
shape — the busiest weekday hour is **19:00**, the NZ residential evening peak. Reading the
hour off the UTC instant would rotate every household's curve by 12–13 hours, and not by a
constant, so the error would read as a seasonal effect rather than a bug.
`mart_account_context` closes design §6.3's last loop: a quarantined account is kept and
flagged rather than dropped, because a missing row makes the agent fail to find the account
while a flagged row lets it decline for a stated reason.

⚠️ **The heating slope has to be fitted at the grain it is spent at.** §8.5 spends it across
**billing periods**, and the first fit was daily: median R² **0.01**, slopes −3.98 to 8.44
where §8.4 expects ~0.9, and a negative slope asserts a house uses more heating when it is
warmer. The cause is the source, not the arithmetic — the cohort is real **London** metering
date-shifted, and **the shift aligns seasons, not weather**, so a household's daily variation
answers London's weather while the HDD it is regressed against is Auckland's. Refitted at
monthly grain on the same data and households:

| | ICPs | median slope | median R² |
|---|---:|---:|---:|
| daily | 499 | 0.45 | **0.069** |
| **monthly** | 446 | **0.74** | **0.467** |

**290 of 446 clear the 0.30 fit floor**, and the cohort does **not** need regenerating: the
earlier conclusion was true of the daily component and false of the seasonal one, which is the
component the bill explainer actually uses.

**`mart_bill_explanation`, and a decomposition that closes.** §8.5's four drivers in
attribution order, with period length removed first so a 43-day move-in bill is not read as a
40% consumption rise, and everything priced at the prior period's own average **variable**
rate — fixed daily charges subtracted first, or every per-kWh driver is inflated by the fixed
component. A singular test asserts the invariant the mart rests on: **the four drivers plus
the remainder equal the amount delta on all 11,019 bills with a prior, none off by more than
two cents.** **2,426 of the 11,528 bills in `fct_bill_line` are explainable**, and weather is the dominant driver at
**68.9% of all movement**, as a seasonal energy bill should be.

⚠️ **`delta_from_tariff` is not re-rated.** §8.5 defines it as re-rating this period's kWh
under both plan versions, which needs the rating engine expressed in SQL. Where the plan
version did not change it is definitionally **0** and asserted so; where it changed it is
**NULL, not 0**, because 0 would push a real tariff move into `delta_unexplained` and make the
decomposition look complete when it is not.

⚠️ **Two traps that returned a number instead of an error.** `flow_direction` is `'IMPORT'` in
billing and `'import'` in metering — the mechanical difference §5 says staging exists to
reconcile — and left alone, a filter on one spelling matches nothing and the sum returns **0
rather than an error**, which made every bill's `total_kwh` zero and took every weather driver
with it. Normalised in `stg_billing__invoice_lines`. And
`write_pandas(auto_create_table=True)` let the **first load date define the schema**, so
invoices' `decimal(12,2)` amounts became `NUMBER(4,2)` and the next date failed out of range —
a narrower *scale* would have been worse, silently truncating cents. The loader now builds
`CREATE TABLE` from the Avro contract (`avro_schema.to_snowflake_ddl`), and reading with
`types_mapper=pd.ArrowDtype` keeps the Parquet's own types end to end, where plain
`to_pandas()` degrades any type pandas has no nullable equivalent for.

**`mart_scale_test` is deferred**, and the arithmetic behind that decision is recorded once in
§6.5's outstanding table rather than repeated here.

### Serving — `make publish-serving`

The target was `kea-serving-db` (now `kea-source-db`) in this project's stack (§6.3), with the application's
database deferred. **Since 12 Sep 2026 it is the application's database**, locally `kea-db`
from `../../infra` — §6.4. The checkboxes below record what was proved against the sandbox.
The publication that replaced the load keeps every one of those properties, and widens the
transaction to take in the Sprint 3 decisions.

The schema already applied there and the nine fixtures already loaded into it — but that was
the fixtures' work (Sources, part 1), not this step's. What the fixtures provide is the **acceptance target**: the
pipeline is finished here when its own output stands where the hand-seeded fixtures stand
now.

- [x] ~~`COPY INTO`~~ → `s3://kea-lake/export/{table}/`, then the loader — **the direction was
      wrong and the design has been corrected.** Snowflake is SaaS in ap-southeast-2 and MinIO
      is local-only, so it cannot `COPY INTO` the lake any more than it could read it as an
      external stage on the way up. `export_warehouse_to_lake.py` **pulls**: runs the query,
      receives Arrow batches, writes the Parquet. 17 tables, 10.7M rows, ~16s
- [x] The pipeline's own output reproduces what the fixtures assert — the point of this step.
      `make assert-serving` (now the gate step of `make publish-serving`): **56 checks, 50 pass, 0 failures.** Bill B comes back at
      **$50.54** from pipeline output, with peak import 34.08 kWh and export 47.48 kWh; the
      13-month household reproduces its 397-day span, 397 daily rows with no holes, 13 profile
      months, and both the 46- and 50-interval DST days. **6 known divergences**, each recorded
      in the gate with its cause — see below
- [x] Per-table strategy honoured: truncate+reload for small tables, DELETE-by-period for
      bills, **partition swap** for `meter_read_hh` (39 monthly partitions, built beside the
      table and attached with a matching CHECK so the swap is metadata-only)
- [x] Load order follows the FK graph, not a sleep — `serving.TABLES` is the single ordered
      list both the exporter and the loader read, including `catalogue.plan` →
      `metering.network`, which reads like a sibling and is not. **Orchestration settled the
      "as task dependencies" half, and not the way the checkbox expected**: the pipeline's
      steps *are* task dependencies now, but the FK order within the load is deliberately
      NOT, because that load is one transaction and splitting it across tasks would give each
      table its own commit. The ordering is a property of the loader; the DAG has one
      `load_serving` task — `publish_serving` since §6.4, still one transaction. See Orchestration
      and design §9
- [x] `synced_at` set per table at commit time, not `DEFAULT now()` per row

**Done when:** a full load runs twice with identical row counts ✅ (identical across all 17
tables), a mid-table failure leaves the table unchanged ✅ (`make publish-serving VERIFY=1` injects a
failure at the last table and proves every count and `synced_at` is untouched; since §6.4 it
plants the failure at the publication's last statement, on a throwaway Postgres 16), and `app`
is untouched ✅ (asserted: the schema does not exist in this database and `kea_loader` holds no
grant on it; now that the served tables share a database with `app`, `assert_serving.sql`
asserts that `kea_loader` holds no privilege on it at all).

**One thing §7.2 asked for turned out to be impossible per table.** "Truncate + reload, one
transaction" cannot work table by table: emptying `metering.property` while `metering.icp`
still references it is a foreign key violation, and no delete order avoids it in general —
some parent is always referenced by a child that has not been reloaded yet. The unit of
atomicity is therefore the whole FK-bound set, with the constraints deferred to commit. That
is **stronger** than what the design asked for, not weaker: the store can never be observed
half-applied across two tables that reference each other.

**The known divergences**, all properties of the source rather than of the loader:

| What the fixture asserts | What the pipeline produces | Why |
|---|---|---|
| `cold_snap` blames weather; three scenarios are `is_explainable` | `unexplained`, and false | The nine fixture households carry ~2 months of reads. `int_metering__heating_sensitivity` reports `fitted_on_months = 2` and returns NULL, so there is no slope to attribute a weather term with. The fixture's 0.93 kWh/HDD is a hand-set constant in `generate_scenarios.py`, not something an estimator recovered from 61 days |
| 13 bills for the 13-month household | 8 | `DEMO_STD_FXTOU_13_MONTH_HISTORY` has one version, effective 2024-01-01; the five 2023 invoices end before it, match no version, and are quarantined as `integrity.no_plan_version`. Design §2.1.2 refusing to re-rate history against a tariff that did not cover it. A catalogue gap, not a loader one |

They are kept as failing-but-explained checks rather than deleted, so the day one starts
passing is visible too.

**The dashboard** is `make dashboards ONLY=serving` — the first one that can see warehouse output
at all, because Metabase is connected to `kea-serving-db` (now `kea-source-db`) and everything the Warehouse step builds lives
in Snowflake. It carries what only this step makes answerable: `mart_load_log` and
`synced_at` (did the load work, how fresh is the replica), and a source-against-serving
comparison naming every deliberate exclusion. Since §6.4 the two sides of that comparison are
in different databases, so it is two aligned cards: *What the sources hold* reads **Kea
pipeline**, and *What was served, and why it differs* reads **Kea serving**, the
application's database as `kea_app`.

⚠️ **A dashboard must name a household by `icp_number`, never by surrogate id**
(`build_dashboard.py`). The load
replaces fixture rows with pipeline output carrying the warehouse's own key maps, so cards
keyed on the old ids stay *valid queries returning nothing* — which is what
`verify-dashboards` exists to catch, and the same fix `assert_serving.sql` needed.

### Quality gates — no step of their own, they end Warehouse and Serving

This section is about the **pipeline's** health — whether a bad load is caught and stopped.
The BI tools that read the serving data belong with the fixtures (Sources, part 1), and §9
draws that line.

- [x] Soda scans after `dbt_test` — ten checks across two files. `warehouse.yml` is the gate
      (Soda exits 2, so the task fails and the export never starts); `serving.yml` reports on
      what was actually served. ⚠️ **The checks are deliberately not the textbook ones** — see
      below
- [x] OpenLineage from Airflow and Spark forms one graph in Marquez, source → serving —
      **0 datasets and 0 edges before, 110 and 239 after**, spanning `postgres://` (source) →
      `s3://kea-lake` → `snowflake://` → `postgres://` (serving). ⚠️ Not the way the checkbox
      implies — see below
- [x] Grafana also carries pipeline health from Prometheus, and an alert fires on a forced
      failure — dashboard "Kea pipeline health" in the Pipeline folder, and an `alert-sink`
      service that keeps what it is sent so a test can assert on it.

      ⚠️ **One bad pattern disables a whole `statsd-exporter` mapping file.** Its glob
      matcher only wildcards complete dot-segments and Airflow puts the operator name inside
      one, so the file was rejected whole, the exporter kept running on defaults, and every
      metric quietly kept its old name. `match_type: regex` fixes it. Panels must also be
      written against metrics Airflow 3.3 actually emits — `dagrun.duration.*` and
      `dag_processing.import_errors` are not among them — so check at the exporter rather
      than assume.

      ⚠️ **If the panels look empty, it is a browser extension, not this dashboard.** Panel
      *plugins* fail to load while Explore's built-in graph renders the same data, which is
      why only dashboards are affected. Open it in incognito, or exclude `localhost:3001`
      from the extension. Nothing here needs changing.

**Done when:** an injected defect lands in quarantine with its reason, raises an alert and
stops the export — and the lineage graph covers source to serving. **`make run-pipeline
VERIFY=1` checks all four**, by poisoning the Soda gate and watching what happens.

⚠️ **THE OBVIOUS SODA CHECKS ARE RED EVERY NIGHT ON THIS DATA.** The source is a fixed
historical extract: its readings stop 2027-08-27 and its arrivals 2027-10-10. So
`freshness(interval_start) < 1d` fails forever, and reads-per-LOAD-date collapses from ~26,900
to ~250 once the extract passes the end of the readings. The Lake dashboard already
recorded the consequence — *"a volume check that did not know this would page someone every
night"* — and a gate that is always red is a gate someone switches off.

So freshness is measured on **the load** (did the date this run staged actually arrive in
RAW), and the ±10% volume band on **consumption days where the cohort is fully present**.
Measured: 369 such days, mean 26,364 reads, and **zero outside the band**. The first sixteen
days of the window carry 6–19 households rather than 500 — that is the extract ramping in, not
a collapse, and a check that could not tell the difference would be worthless.

⚠️ **THE SPARK LISTENER EMITS NO DATASETS, AND THAT IS WHY THE GRAPH IS BUILT DIFFERENTLY.**
The checkbox says lineage "from Airflow and Spark". Both were installed and both fell short:
Airflow's provider emitted 379 events with `inputs: []` and `outputs: []` — `DockerOperator`
has no extractor that can know what ran inside — and the `openlineage-spark` jar, once added,
registered cleanly, reached Marquez, and emitted only application-level START/COMPLETE with no
dataset facets for these job shapes.

What produces the graph is therefore **explicit emission**: every step names what it read and
wrote through `kea_pipeline.lineage`, and `dbt-ol` closes the warehouse middle. The dataset
names are built from `kea_pipeline.lake` on both sides of each hop, so the halves join on a
string neither can invent independently. The JVM listener stays installed for the run-level
events it does provide.

⚠️ **QUARANTINE DOES NOT REACH THE SERVING STORE, AND THE GATE FOUND IT.** Design §6.3 says
*"Quarantine rates surface to the product. If a household … is quarantined, the agent hedges or
declines rather than answering confidently. This is the link most pipelines never make."*

It is not made here either. The warehouse mart holds all five planted defects with their
reasons; `kea.mart_account_context` in serving holds **zero**, and its `is_quarantined` /
`quarantine_reason` columns are always false and null. Not because the export is wrong, but
because **a quarantined account is by definition one the serving schema cannot hold**:
`billing.account` requires `icp_id` and `plan_id` NOT NULL, the three orphans have no ICP at
all, and the other two are on `ECO_GONE_VECT_STA`, which has zero versions in `dim_plan`.

Closing it means a schema decision — a nullable FK, or a separate quarantine table in serving —
and that is design work, not wiring. Recorded here rather than resolved by loosening a
constraint to make a checkbox tick.

### Delivery — `make ci`, `make deploy`

> **Partly built ahead of Streaming.** CI and `make deploy` were built on 15 Sep 2026; see the
> note at the top of this section.

- [x] Unit tests for the parse job: reshaping, quarantine rules, DST-boundary day lengths
      (a local day is 46 or 50 half hours, never assume 48). Reshaping is `test_reshape.py`,
      in CI. The quarantine routing and the two DST days go through the Spark parse itself in
      `make ingest-date VERIFY=1`, which needs Spark and MinIO and so stays out of CI: 5 Apr 2026
      parses to 50 distinct half hours with 02:00 and 02:30 twice, 27 Sep 2026 to 46 with no
      02:xx, every read keeps its instant, and both days match the extract's load-date window
      (15 Sep 2026, 22 tests OK)
- [x] CI green end to end, behind the one required `check`: ruff, the host unit tests, the DAG
      import test, `dbt parse`, the serving migrations and a publication on a throwaway
      Postgres 16, and the Story page's identifier guard. Three jobs in
      `.github/workflows/ci.yml`, each step a `data/Makefile` target, so `make ci` runs the
      same thing locally, without `.env`. Green locally 15 Sep 2026, and **green on GitHub**:
      first on PR #174 (19 Sep), and 8/8 including the `check` gate on both open data PRs,
      #217 and #222, on 27 Sep — DAG import 1m4s, dbt parse + serving publication 1m14s,
      ruff/unit/migrations/story guard 10s. Since #222 (29 Sep) the dbt job also runs the
      43 kNN backtest tests in the dbt image, and fails on any skip. A push alone never runs
      it: `ci.yml` is `push: [main]` plus `pull_request:`, so a topic branch gets checks only
      once it has a PR
- [ ] `dbt build` in an isolated schema, dropped afterwards. It needs Snowflake credentials as
      repository secrets, which only an admin can add, on a trial that converts on 3 Oct 2026.
      Until then CI parses: that catches broken refs and sources, Jinja and YAML errors and
      malformed tests, but **runs no data test**. A failing dbt test stops `make deploy`
      before anything is published; it does not stop the merge
- [ ] `make deploy`: merged code only (a clean checkout at `origin/main`), gated by `make ci`;
      the real `dbt build` with its data tests; migrate, then one atomic publish recorded as
      `git-<sha>`; the fixture checks and the serving scan; every dashboard rebuilt and
      verified. Built, and its gate (`deploy_gate.sh`, step 1 of `make deploy`) refuses a dirty
      tree as designed. It has not run. **The blocker written here for two weeks — PR 156 — is
      gone.** PR 156 was closed unmerged and replaced by **#196** (`2867021`, merged 22 Sep
      2026), which brought `publish_serving.py` and serving migrations V001–V004; the CI jobs
      and `make deploy` itself arrived separately in **#198** (`6e8a6d2`, merged 23 Sep 2026),
      the pushed replacement for the unpushed `data-ci-deploy` branch. So `main` has had both
      halves since 23 Sep. What remains is the bullet above — the real `dbt build` needs the
      Snowflake credentials, so the first deploy waits on those, not on a pull request
      (checked 27 Sep 2026)
- [x] Versions reviewed and pinned (15 Sep 2026; §3). Every image tag names a release:
      `postgres:17.10`, where `postgres:17` floated, for the four pipeline databases and
      every one-off `psql`; `postgres:16.15-alpine` for the throwaway serving databases in
      CI and `assert_load_atomic.sh`; `python:3.11.16-slim` under the dbt and Soda images and
      `alert-sink`. Every image built here installs from a constraints file: dbt (95
      packages), Soda (3.5.6; 52) and Spark (pyarrow and openlineage-python; 20), each its
      image's `pip freeze`, and Airflow from Airflow's own. Each rebuild's freeze matched the
      image it replaced. kea-db's own `postgres:16-alpine` is the backend's to pin

**Done when:** a PR cannot merge with a broken DAG, a failing dbt test, or a failing unit test.
The CI change is on `main` (#198, 23 Sep 2026), so a broken DAG or a failing unit test already
cannot; a failing dbt *data* test still can, and needs the isolated-schema build above.

**The one class of test CI used to skip now runs.** `make ci ONLY=test` is standard-library-only
by its own contract, so the 43 tests in `test_knn_quantile.py` — the backtest's leakage checks
among them — skip on the runner's host. Since #222 (29 Sep 2026) the dbt job runs them inside
the dbt image, which carries numpy, as `make ci ONLY=backtest`, and that step fails on any skip,
so it cannot go green by testing nothing (design doc §15.12).

### Orchestration — `make run-pipeline`

- [x] `kea_daily_pipeline` runs the whole path — ~~on the 02:00 NZT schedule~~ **asset-scheduled**,
      on `AssetAll` of the three daily lanes' bronze. Design §9 and `_kea_common.py` both say
      the pipeline "should be asset-scheduled on what these emit rather than owning their
      tasks", and that beats a fixed hour: 02:00 is a guess about how long an extract takes,
      and a wrong guess reads yesterday's partition and reports success. Six tasks —
      `snowflake_stage → dbt_run → dbt_test → export_warehouse → load_serving → assert_serving`
      — verified green end to end. Today, after Quality gates, §6.4 and the 29 Sep 2026 step
      names, as three task groups named for the Makefile's commands: `build-warehouse`
      (`snowflake_stage → dbt_run → dbt_test → soda_scan_warehouse`) → `publish-serving`
      (`export_warehouse → publish_serving → assert_serving → soda_scan_serving`) →
      `dashboards` (`summarise_lake`). The serving gate runs before the serving report since
      29 Sep, so a report finding can no longer stop the gate or the `SERVING` asset
- [x] Separate DAGs for the different cadences — `kea_forecast_refresh` is new (manual
      trigger; see below). `kea_weather_load` **already existed** as `kea_extract_weather`,
      `@monthly` since the Lake was built. `kea_scale_test` is **deferred with `mart_scale_test`**: a DAG
      whose only task runs a model that does not exist can only fail, and would sit red
      misrepresenting the pipeline's health
- [x] Backfill over a date range is idempotent, through the same DAG — ⚠️ **but not through
      `airflow backfill`**, and the two are in genuine conflict. See below. Proven over
      2026-08-24 → 26, run twice with the DagRuns cleared between so the second round genuinely
      re-ran: **identical row counts across all fifteen serving tables.** ⚠️ Three dates, not
      the thirty the "Done when" names — the loop is the same at any length, and each date costs
      a full `dbt run` + export + load, so a month is ~45 minutes and ~5 Snowflake credits per
      round. The property is structural rather than statistical: staging is DELETE-by-load-date,
      and dbt, the export and the load are full refreshes
- [x] A failing `dbt_test` visibly blocks every export task — **proven by breaking it.** A
      deliberately failing singular test took `dbt_test` to `failed` after its two retries;
      `export_warehouse`, `load_serving` and `assert_serving` never ran, and the serving
      store was byte-for-byte unchanged **including `synced_at`**, which is what proves the
      load did not merely no-op but never started. §11's "Postgres serves yesterday", as data

**Done when:** a month is backfilled, then backfilled again, and nothing changes.

⚠️ **Asset scheduling and `airflow backfill` are mutually exclusive, and checkboxes 1 and 3
asked for both.** Airflow refuses outright:

```
DagNonPeriodicScheduleException: kea_daily_pipeline has a non-periodic schedule
that does not support backfills
```

`airflow backfill` generates runs from a periodic timetable, and an asset-driven DAG has none.
Checked with `--dry-run`, not assumed. The resolution is `load_date` as a first-class **Param**:
a range becomes one triggered run per date, each carrying its own date, through the same six
tasks and the same containers — which is what §11 actually asks for ("no separate backfill code
path"). `make run-pipeline FROM=… TO=…` wraps the loop and waits for it; `max_active_runs=1` keeps a month
from starting thirty Snowflake sessions.

That Param is load-bearing for a second reason. An asset event carries no date, so a cleared
extract re-run for an old day would fire this pipeline, which would then take **today** from
its own fire time and stage the wrong day.

### Streaming — `make up STREAMING=1` (Sprint 5)

The batch path ships first. Kafka, Schema Registry, the topics and Flink all sit behind
the compose `streaming` profile, and none of them run before this is built.

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
project code baked in. §8 covers what follows from that.

**Everything else that carries this project's identity is named for it**: the OpenLineage
namespace `kea_energy`, the StatsD prefix `airflow_kea`, the bucket `kea-lake`, the
Snowflake database `KEA`, the Kafka topics `meter_reads*`.

### 6.2 Where the pipeline code lives: `data/pipeline/`

The pipeline's code lives under **`data/pipeline/`**:

```
data/pipeline/
  airflow/dags/      the DAGs of design doc §9
  spark_jobs/        the metering lane: the partitioned extract and the parse job (§5.1)
  lib/kea_pipeline/  logic shared by the DAGs and the Spark jobs — the extract
                     window above all, so both cannot disagree on what a load
                     date is. Mounted into Airflow and both Spark services
  contracts/         KEA-047 and its siblings, plus the one `_reject_reason`
                     vocabulary. Kept beside `lib/` because the schema loader
                     resolves them as siblings, and OUT of the repo-root
                     `contracts/`, which is the API↔frontend interface with a
                     different lifecycle and a different owner
  tests/             the host suite: no database, no lake, no Spark
  tests/lake/        the suite that needs the lake, run under spark-submit —
                     deliberately outside `unittest discover`'s reach, so the
                     fast one stays honest about what it does not cover
  dbt_project/       staging → silver → gold → marts
  soda/              the runtime quality gate
  scripts/           flat, because the verb prefix already groups them --
                     extract_*, gen_*, set_*, assert_*, and since Sprint 3
                     build_* for the jobs that compute a decision inside the
                     publication: forecasts, plan recommendations, solar and
                     EV estimates, hardship. Its README says who runs each,
                     which the layout cannot: extract_small.py is the only one
                     Airflow imports, and the assert_* scripts are proofs
                     about a RUNNING system rather than tests of code, which
                     is why they are not in tests/
  scripts/gen_source/  the source generator: numbered SQL that runs in order,
                     plus planted.py, which plants the known answers Sprint 4
                     is scored against. A directory rather than a prefix
                     because the order IS the interface
  scripts/reference/ reference data cited rather than computed -- the regional
                     solar yield table and the fetcher that produced it. Kept
                     beside the scripts that read it, so a figure in a served
                     estimate traces to a file
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

`producer/` is deliberately absent for now — it is Sprint 5, and it should be created with
Streaming rather than stubbed empty today.

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
  kea-source-db                       the four source systems (kea-serving-db before §6.4)
  marquez(+db, web)                   namespace kea_energy
  statsd-exporter · prometheus · grafana · vault
  metabase(+db)                       analysis: the served tables and the sources
  kafka · schema-registry · kafka-init · flink x2   (streaming profile, Sprint 5)

infra/docker-compose.yml              THE APPLICATION — untouched
  db -> kea                           `make db`: the API's tables, and the served ones
```

**One database for what is served, locally as everywhere else.** `db` in `infra/` holds the
served tables, as the RDS database does in integration and production: Data owns `common`,
`metering`, `billing`, `catalogue`, `kea` and `data_ops`; the application owns `app`.
Containers here reach it as `host.docker.internal:5432`. `kea-source-db` is the Data team's
own Postgres: the four `src_*` source systems the extracts read, and the `gen_audit` /
`lake_audit` rollups. Until 12 Sep 2026 it also held a sandbox copy of the served schemas;
with two copies, every check could pass against the one the application does not read.
`pipeline/migrations/2026-09-12_drop_serving_copy.sql` removes that copy from an existing
volume.

Two arrangements are **withdrawn** and should not be retried: merging the pipeline into
`infra/docker-compose.yml`, which puts its dependencies in a file another team owns, and
extracting the base services into a shared stack, which saves memory but leaves this repo
unable to start on its own.

**dbt and Soda need an image each.** Not one between them: `dbt-snowflake` and
`soda-core-snowflake` together are unresolvable for pip (ResolutionTooDeep, on conflicting
`snowflake-connector`/`protobuf`/`otel` pins) — the same reason neither lives inside the
Airflow image, which its own Dockerfile documents. Hence `pipeline/docker/dbt` and
`pipeline/docker/soda`.

### 6.4 Publishing pipeline output to the application

`make publish-serving RELEASE=<id>` points the loader at the same PostgreSQL database the
application reads. It loads the sealed warehouse export, computes forecasts, plan
recommendations, solar and EV estimates and hardship outputs, validates them, and records the release in
`data_ops.serving_publication`. These data changes share one transaction, so an error
leaves the prior release visible.

`make publish-serving MIGRATE=1` applies immutable SQL files from `migrations/serving/`, and is
asked for deliberately, before the publish that needs it: a publish connects as `kea_loader`, which can
change no schema, and never migrates as a side effect.
Migration history and checksums live in `data_ops.schema_migration`; the readable
`kea_serving_schema.sql` remains the current bootstrap/reference DDL. Existing databases
are explicitly baselined after review rather than dropped and rebuilt.

The migration identity owns Data schemas. `kea_loader` writes them but has no access to
`app`; the API reads them and manages `app` through its own Alembic history. PostgreSQL
16 and 17 are both supported because publication uses SQL over a connection rather than
database dump compatibility.

Locally the target is `kea-db` from `../../infra`, reached from the pipeline network as
`host.docker.internal`. Docker Desktop defines that name; on Linux,
`--add-host=host.docker.internal:host-gateway` does, and the Makefile, Airflow's
`DockerOperator`s and the rebuild scripts all pass it. The daily DAG's `publish_serving`
task, `make rebuild-all` and `rebuild_span.sh` all call this same publisher.

The current lake export paths are mutable. Scheduled export and publication therefore
must not overlap until exports gain immutable release prefixes and a manifest. Large
publications can also hold table or partition locks long enough to delay API reads, so
production scheduling needs a measured maintenance window. The runbook is
`migrations/serving/README.md`.

### 6.5 What is built, and what is not

**Done, and verified running** (30 Aug 2026 — see Stack in §5). `../docker-compose.yml`, `../Makefile` and `../.env.example` have been rewritten
against §6.1–§6.3, and `../pipeline/` holds the images and config:

- The platform services are gone from this file; what remains is Airflow, its metadata
  Postgres, and `kea-source-db`. Images are `dataeng/*` with **no `build:` stanza**, so
  `up` can only consume them.
- `name: kea` scopes the volumes; the private/`dataeng` network split is in place, and
  `kea-source-db` is the only prefixed service on the shared network.
- OpenLineage namespace is `kea_energy`; the Snowflake database defaults to `KEA` and
  `DBT_SCHEMA` to **`kea`** — a BASE name, not a layer. dbt appends each model's custom
  schema to the target, so the layers land as `kea_staging` / `kea_silver` / `kea_gold` /
  `kea_marts`. It was `kea_staging` while staging was the only layer, which put staging in
  the right place by accident and left nowhere for silver to go.
- `make up` guards on the `dataeng` network, the `dataeng/airflow` image and `.env`,
  failing with an explanation rather than a DNS error inside a container.
- The Make target vocabulary was the conventional one for this stack — `up`, `down`,
  `clean`, `dbt-build`, `dbt-test`, `soda-scan`. `spark-parse` was singular by design:
  §1.1 says one Spark job, so there is no bronze/silver/gold chain. Since 29 Sep 2026 each
  step is one command instead (`init-sources`, `ingest-date`, `build-warehouse`,
  `publish-serving`, `run-pipeline`); the Makefile header maps them.
- `--env-file` replaces shell sourcing, which is also what lets the `&` inside the
  `AIRFLOW_CONN_*` URIs survive — sourcing them in a shell does not.

**Still outstanding:**

| What | Why it matters |
|---|---|
| ~~Basic Plan carries most of the cohort~~ — **closed 2026-09-08** | The Contact-derived catalogue was replaced by Anytime Rate, Time of Use and Free Weekends. See `docs/sprints/sprint-3/plans-and-terms.md`. |
| ~~`ecoWHOLESALE` is approximated~~ — **closed 2026-09-05, by retirement** | The spot plan was retired with the other three Ecotricity products when Sprint 3 adopted Contact's range, none of which is spot-priced. The finding stands and is worth keeping: a spot price is a market outcome per half hour per GXP rather than a tariff, so `rate_components.unit_rate` being a scalar can never express it. If a wholesale product returns, EA's EMI publishes final half-hourly prices per node and `dictionary/ecotricity-tariff-source.md` still carries the schema, URL patterns and GXPs |
| `mart_hourly_profile.is_weekday` carries no signal in the fixtures | Design §8.7 argues weekday and weekend are two different curves, and the column doubles the mart's row count to hold that. But `generate_scenarios.py` applies one hourly shape to every day: across all nine fixtures the largest weekday/weekend difference in any hour is **0.0001 kWh**. Daily totals differ by up to 7%, and that is weather landing on different dates, not a shape difference. Visible on the "Average day, weekday vs weekend" panel as two curves sitting on top of each other. The generated cohort does not have this problem — LCL is real metering, so its day-of-week variation is real |
| ~~Only one of 64 plans is fully bill-sourced~~ — **closed 2026-09-08** | The active generated catalogue has six version rows for three Kea design tariffs. The screenshot values are `[DESIGN]`; internal ex-GST/loss rates are `[DERIVED]`; the common export rate is `[ASSUMED]`. |
| ~~`pipeline/soda/checks/` is still empty~~ — **filled, Quality gates** | It was the last empty code directory. It now holds `warehouse.yml` (the gate, after `dbt_test`) and `serving.yml` (a report, after the load), with `configuration.yml` beside them naming two datasources. ⚠️ The checks are deliberately **not** the textbook ones: this source is a fixed historical extract, so a freshness check on the readings or a ±10% volume check per load date is red every night forever — the file carries the full reasoning |
| **`mart_scale_test` — deferred, 3 Sep 2026** | §6.5 specifies "the 2.45B-row re-rate", and the warehouse holds **9,691,082** reads. 2.45B is Kea's *production* projection for their real customer base; this cohort is 500 households whose source holds 19.1M rows in total, so running it means synthesising ~253× the data before re-rating any of it. On a fixed $400 trial that has spent **2.77 credits** and converts on **3 Oct 2026**, that spends the remaining budget demonstrating a property of Snowflake rather than of this pipeline. **Revisit in a funded environment**, or scale-test at an affordable volume with the extrapolation stated — but not by quietly reinterpreting the number. It is the one mart that is never exported, so nothing downstream waits on it |
| The Airflow image cannot launch Spark directly | It has neither `java` nor `spark-submit`, so `SparkSubmitOperator` is unusable despite the provider being installed. The DAGs use `DockerOperator` against `dataeng/spark:3.5.9` — the sibling-container pattern the compose file already provisions for dbt and Soda |
| ~~Soda Core is unpinned~~ — **closed 15 Sep 2026, Delivery** | 3.5.6 was only what the build happened to resolve. The Dockerfile now names both Soda packages at 3.5.6, and `pipeline/docker/soda/constraints.txt` holds every package they pull in. The rebuilt image's `pip freeze` matched the old one's exactly, so every scan so far ran on what is now pinned |
| ~~No Spark job can be inspected after it finishes~~ — **closed 7 Sep 2026** | The master UI on 8082 still has its proper, narrower role: cluster status and the application list. `spark_session.py` now writes compressed event logs to `s3a://kea-lake/_spark/events`, and `spark-history-server` replays them on host port 18080 with a five-second refresh and 30-day retention. Completed applications now retain their Jobs, Stages and DAG pages after the driver container exits. Jobs completed before event logging was enabled have no log to replay and cannot be reconstructed retroactively |

---

## 7. Rebuilding after the span moves

The generated cohort is real metering from **Low Carbon London**, date-shifted so that
UK winter lands in NZ winter and scaled to NZ consumption. **What the source data is,
what the three conversions do, and why each constant is where it is belong to
`data/dictionary/lcl-source-data.md`** — read that first if the question is about the
data. This section is the operational half: what a span move breaks, and the procedure
that repairs it.

**The pin itself is not repeated here.** The shift, both spans, the three properties
the shift has to satisfy and the arithmetic that derives it are dictionary §8, "Seasons
are flipped on purpose"; the values are `cohort.json`. Two copies of a constant that
moves is how one of them goes stale, and this section is the one that runs *after* the
move. What the procedure below needs from the pin is one number: the span is **757 load
dates**, and the loop runs once per date.

**Live demo profile** (`make rebuild-all`, the normal rebuild): **50 households**,
~2.14M source meter-read rows, 1,224 generated invoice periods; 239 dbt models/tests
passing, inside its 30-minute target. The full-scale run — `COHORT_SIZE=500`, 500
households and 20,277,761 reads — is still available but is not what the repo builds
by default.

Bumping `NZ_SPAN_END` or `LCL_START` changes the shift, and the shift is applied to every
timestamp the generator writes — every reading, every invoice date, every `received_at`, and
the cohort pick itself. So **every row already in the warehouse describes the old shift**.

Everything *else* about a rebuild is reproducible, and deliberately so — which is what makes a
span move a discrete event rather than a drift. `cohort.json` records `seed` (`kea-stage2-v1`),
`min_coverage`, `shift_days` and both spans, so the cohort pick is deterministic: it changes
when the eligible window changes, never at random. `scale.json` records the measured multiplier
rather than recomputing it silently. And `received_at` is derived per `(icp, nz_date)` rather
than from `now()`, so re-running the same span twice produces the same bytes. Rebuild because
the span moved, not because the pipeline ran again.

**This is not a backfill**, and design §11 carries the argument: a backfill's rows land on
the same dates as the ones they replace, while a span move's land on different ones, so the
old rows are left in place carrying the old shift. §11 also holds the replacement-scope table
that goes with it — **only the first step of the chain removes anything it was not handed** —
and the five bugs of that shape the 2027 move surfaced, four of which exited 0.

`pipeline/scripts/ops/rebuild_span.sh` is the procedure. Measured on the move to 2027: **757 load
dates, 6h24m**, plus about five minutes for the once-only tail. It rebuilds; it does not
migrate — add `MIGRATE=1` to its `make publish-serving` line if anything under `migrations/serving` has moved
since the last publish. Two of its choices come straight from §11's rules: it **drops and
recreates RAW** rather than truncating it, and it passes `--force` to the scripted extracts as
well as the Spark ones, because a sealed bronze partition would otherwise hand its old parquet
back to the loader.

**The shape is per-date work per date, once-only work once.** Deliberately not
`run_pipeline.sh` (`make run-pipeline`): that triggers the whole `kea_daily_pipeline` per date, and every dbt
model is `+materialized: table` rebuilt from all of RAW, so it would produce the same tables
757 times. Measured, about 18 hours against about 7.

| | Work | Cost |
|---|---|---|
| per date | extract (Spark, ~11 s) + parse (~10 s) + scripted entities (~4 s) + stage, **incremental entities only** (~6 s) | ~31 s × 757 |
| once | the fourteen snapshots, then `make build-warehouse` (dbt and the Soda gate) and `make publish-serving` (the export, the publish — which computes the decisions as well — and its gate) | ~5 min |

Staging every entity per date instead costs 40 s rather than 6, because all fourteen snapshot
loads are overwritten by the next date's. That one flag is 7.2 hours across the span, for
nothing.

### What the script verifies rather than assumes

- **That RAW was really recreated.** It reads the row count back, because a step can report
  success without having run: `docker run` without `-i` does not attach stdin, so a heredoc
  piped to `python3 -` reads EOF and exits 0.
- **That the snapshots landed.** It counts rows in each snapshot's RAW table, because the
  loader returns 0 for "skipped, no partition" and 1 only for a real failure. Without the
  count, all fourteen can skip, the step can report nothing landed, and `set -e` still sees
  success — after which dbt builds every dimension from an empty table.
- **Incremental billing rows outside the reading span.** The loop's range is the span
  *readings* exist for, and `invoices` is incremental on `issue_dt`, `payments` on `paid_ts`.
  Both can fall outside it at either end: the nine fixture households bill across 2023-07 to
  2024-09, years before the cohort's span and never visited by the loop, and the cohort's last
  125 invoices are issued after its last reading. Without the catch-up, 153 invoices go
  missing from a rebuild that reports no error at all, every fixture bill vanishes from
  serving, and only the serving gate, assert_serving, notices. `payments` needs it for the same reason: a
  late manual
  payment is valid source data that a full rebuild would otherwise never pick up. The catch-up
  asks the source which dates exist rather than looping, because the gap is sparse — 762 empty
  days lie between the fixtures and the span.

### After it finishes

Its tail is `make build-warehouse` and `make publish-serving`, so the Soda gates and
assert_serving have already run. What is left:

```bash
make dashboards ONLY=source  # the gen_audit rollups are matviews -- see 6.5
cd ../services/api && .venv/bin/pytest -q
```

`make dashboards ONLY=source` is not optional and is the easiest to forget: `gen_audit.mv_daily`,
`mv_profile` and `mv_bills` are MATERIALIZED VIEWS, so they go on serving the previous span
indefinitely. The dashboard then looks healthy while showing data that no longer exists, which
is worse than a broken one, because nobody investigates it.

### What is still unresolved

- **The pin will expire.** `NZ_SPAN_END` reaches about 11.7 months past today, which is what
  lets the nightly extracts find real work. Legal end dates are sparse — the season-flip
  assertion is what makes them so — and the next one is named in dictionary §8, verified
  against the assertions rather than guessed. Bumping it means this whole procedure, so it
  is worth doing before the runway is gone rather than under pressure.
- **A sixth partial-replace bug is likely.** Five have been found, all the same shape, four of
  them silent. Nothing in the chain *structurally* prevents another; the defence is the verify
  step and this table, both of which depend on someone reading them.
- **Resolved 2026-09-27: §5's Sources lines and §9's table were still full-scale.** They
  quoted 500 households, 20,277,761 reads, and defect and revision counts from the same
  archived `COHORT_SIZE=500` run. Those were recounted against `kea-source-db` and now
  describe what `make rebuild-all` builds. The figures from Lake through Orchestration were **not** recounted —
  re-measuring them needs a full-scale rebuild and a Snowflake connection — so they carried a
  marker at the head of Lake instead (at the head of Warehouse since the Lake was recounted
  on 2026-09-28), which is the honest form of the same fix. The lesson stands: the catalogue note at the top
  of this document had said so for weeks, 1,400 lines earlier, and in practice protected
  nobody. A figure is only safe where it is read.

## 8. Running alongside other stacks

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
`kea-source-db` 5433, and the application's `kea-db` 5432 from `../../infra`. Only one stack
using these can be up at a time. If that ever
becomes a problem, a `${PORT_OFFSET:-0}` on the published ports is the fix — not before
it is needed.

**Sharing a Snowflake account is safe; sharing a database is not.** `dbt build` drops and
recreates everything it owns, so a single wrong `DBT_SCHEMA` — a stale shell still
exporting an old value, a copied `profiles.yml` — points this project's build at another's
models and destroys them. A database boundary turns that into a permission error instead
of data loss, and Snowflake charges nothing for the extra database: you pay for storage
and compute, which are unchanged. Two consequences: credits are shared, so scope any
resource monitor per database rather than per account; and Delivery's isolated CI schema
has to be isolated from every other consumer of the account, not only from local
development.

**The image tags are deliberately not project-scoped.** `dataeng/airflow:3.3.0`,
`dataeng/spark:3.5.9`, `dataeng/dbt:1.12.0` and `dataeng/soda:latest` are named for what
they contain, because that is what they are — a base image plus libraries, with no project
code in them. Everything project-specific is a bind mount: `airflow/dags`, `dbt_project`,
`soda`, `spark_jobs`, `scripts`. Docker stores an image once per tag, so anything else on
the machine built from the same Dockerfiles reuses one copy rather than carrying a second.

That is also why **`make up` builds an image only when it is missing**. A shared tag means a
rebuild is a side effect on everything using it, so rebuilding is `make up BUILD=1`, run
deliberately. If the
Dockerfiles here are ever edited, rebuild explicitly and be aware of what else consumes
the tag.

## 9. Where analysis lives

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

**Seven dashboards, each answering a different question**, all built from committed
scripts in `../analysis/metabase/` and rebuildable from them:

| Dashboard | Reads | What it is for |
|---|---|---|
| Sample data — the nine fixtures | `kea.mart_*` | The hand-built scenarios, driven by one ICP filter |
| Sources — generated source data | `src_*`, via rollups in `gen_audit` | The 50-household demo cohort: 2.14M reads, 1,224 invoices. ⚠️ The `gen_audit` rollups are MATERIALIZED VIEWS and do not follow their base tables — `make dashboards ONLY=source` after every regeneration (`rebuild-all` does), or the dashboard shows the previous span |
| Lake — extract, bronze, parsed | `lake_audit` | The **pipeline's own behaviour**, not the meter data |
| Serving — export, load, and what crossed | `billing.*`, `metering.*`, `kea.mart_*` | The serving store against its source: what crossed, what did not, and the nine fixtures reproduced |
| Plan analysis & recommendation (Sprint 3) | `kea.plan_recommendation`, `catalogue.*` | A counterfactual, and the only dashboard here that plots an **opinion**. A third of its panels argue against the other two thirds, because a recommendation cannot be checked against an outcome |
| Story (demonstration) | the served marts and decision outputs | The one-page walkthrough, **identifier-free**: no ICP, no account number, no customer name on any card |
| Sprint 4 — what the meter can see, and two what-ifs | `kea.mart_daily_detected_load`, `mart_charging_session`, `solar_estimate`, `ev_scenario`, `hardship_*` + `gen_audit` | The detector's output beside what the generator planted, then the bill driver, the two what-ifs and the early warning. Also identifier-free. The only page reading **both** Metabase databases — and no single card does, because they are two connections and a join across them is not a thing Metabase can run |

**Three rules these are built under** — the first applies to all seven, the second to
the two identifier-free pages, the third to any page over an append-only table. Each
guards a failure that does not announce itself:

- **The builder is the record.** A question edited in the Metabase UI is gone on the
  next `down -v`, and nothing in git would show it had ever differed. Every card is
  upserted by name from the script, and a question in the script's own collection that
  the script no longer builds is **archived** — so a renamed panel cannot linger and be
  mistaken for a live one.
- **Identifiers are refused at build time, not reviewed at read time.** The Story and
  Sprint 4 builders run their SQL through a regex guard and exit rather than publish a
  card that selects an ICP, an account number or a customer name. `make ci ONLY=story-guard`
  runs the same guard in CI. `count(DISTINCT icp_identifier)` is the one documented
  exemption: a count of households is not an identifier.
- **Every append-only table is pinned.** `solar_estimate`, `ev_scenario`,
  `hardship_assessment` and `gen_audit.planted_load` all keep their history, so a card
  without `computed_at = (SELECT max(...))` — `run_id` for `planted_load`, which has no
  `computed_at` — silently doubles every count on the next publication, at whatever
  multiple the next run makes it. The predicate is repeated in
  each card rather than factored into a view, because a reader of the panel has to be
  able to see it.

⚠️ **There is deliberately no Warehouse dashboard.** The warehouse's marts live in
**Snowflake** and Metabase is connected to the serving **Postgres**, which is the design's
intent: design §7 has the Serving step export the marts to Postgres, and that is when they become
visible through the connection Metabase already has. A second connection to Snowflake would
build something Serving makes redundant and leave two dashboards over the same numbers to
keep in agreement. The Serving dashboard is the one that reads the marts.

`make dashboards` ends by checking every card on all seven: that it runs, returns rows, and
**uses every column it selects**. That last clause is not padding — a query returning
`hour, month, kwh` under a viz declaring only `graph.dimensions: ["hour"]` renders as one
averaged curve, because Metabase drops the breakout silently and the panel looks fine. It
also reports each card's row count, which is the only thing that catches the other silent
failure: **Metabase caps a card at 2,000 rows**, so a 2,791-row daily series returned exactly
2,000 and stopped mid-span looking complete. That chart is a weekly mean now.

⚠️ **`lake_audit` is analysis, not pipeline output**, and the schema name is chosen to say so.
Metabase talks JDBC and the lake is Parquet on MinIO, so a dashboard over it needs the
rollups landed somewhere Metabase reaches; `analysis/metabase/summarise_lake.py` rebuilds them
whole from the lake on each run — in `make dashboards`, and nightly as `kea_daily_pipeline`'s
`dashboards` group. Since 29 Sep 2026 it writes as `kea_lake_audit`, which owns `lake_audit` and
reads no source system: never the superuser, which neither Airflow nor the Spark containers
now hold. Putting them in `kea` would make them look like marts — the
same reasoning that keeps `gen_audit` out of `src_*`. **Serving is what lands real pipeline
output in the serving store.**

**Weather is the one thing the Lake dashboard plots as data rather than as pipeline behaviour**, because
nothing else plots it at all: the source arrived after the Sources dashboard was built, and a
wrong-hemisphere series would have survived every existing check. It does not — February is
the warmest month and July–August the coldest, and heating degree days run south to north as
the station-to-region mapping predicts.

Otherwise the Lake dashboard deliberately does **not** re-plot the meter data; that is
the Sources dashboard's, read from the source. What it newly makes visible is arrival lag, which turns
design §4.1's argument into a measurement: **every one of the 16,286 rows arriving eight or
more days late is a correction**, and 10,139 of them land 22 to 46 days out. An extract
watermarked on reading time sees none of them and reports nothing wrong.

**The three opening analysis questions, one of them now half answered:**

- **Whether `base_load_kwh` separates a fault from a new appliance** — half answered, and
  the half that is answered was measured rather than argued. Sprint 4 gives a new
  appliance its own bill driver, and against planted EVs the published step came out at
  100–110% of the planted one on every wallbox (design doc §15.10). The *fault* side of
  the question — §8.7's rising floor — still has no fixture scored against it.
- **Whether the weather decomposition holds against the cold-snap fixture** — still open.
  Sprint 4 changed the heating fit (it is now net of detected loads) and replicated the
  published fit on 49 of 49 ICPs, but that proves the refit reproduces, not that the
  decomposition is right about weather.
- **Whether the forecast band narrows for the reason the design claims** — the comparison
  now exists, and it answers this in the direction the design did not expect.
  `forecast_backtest/` scores the bill forecast against the kNN quantile method of Botman
  et al. (2025) on the sealed export, under a gate whose three thresholds were fixed before
  the runs. ⚠️ **The band is not narrow for that reason; it is just narrow.** On the 4,084
  snapshots both methods forecast, the champion's ±15% of the kWh still to come contains the
  final kWh **51%** of the time — 42% at 20+ days elapsed, when it should be easiest — while
  kNN's P10–P90 contains it **82%** at roughly twice the width. kNN also clears the gate as a
  fallback for the accounts `is_forecastable` turns down (median APE 6.3% against a 10.2%
  limit, mean CRPS 0.88× the champion's, though the bootstrap interval 0.72–1.06 crosses 1 on
  34 accounts). Two things stay open: the champion scored is **`hdd-linear-v2` refitted as at
  each date, not Sprint 4's `hdd-linear-v3`**, because the export ships v3 and the run drops
  that comparison; and a seasonal-neighbour variant was rejected, passing coverage and APE but
  landing at CRPS 1.84× the champion's — narrowing the neighbour pool widened every interval
  instead of sharpening it. Full numbers in §15.12 of the platform design; recorded in
  `docs/decision-log.md`, 27 Sep 2026.

**Two shapes of analysis live in `../analysis/`, and the naming rule bent to fit the second.**
A one-off question is a `YYYY-MM-DD-what-it-answers` script. An evaluation is not:
`sprint4_evaluation/` is a named directory holding a method, a threshold per figure, a
re-runnable harness and the report it produced, because the point of it is that it can be run
again and disagree with itself. Dating it would suggest the answer belongs to the day it was
taken, when what matters is the release it scored.

**`forecast_backtest/` is the second of that shape, which is what makes it a convention
rather than one exception.** The same parts: a method (`pipeline/scripts/knn_quantile.py` —
pure numpy, no I/O), a re-runnable harness (`backtest_bill_forecast.py`, behind
`make backtest-reproduce` and `make backtest-forecast`), a gate fixed before the runs, and
one report per stage naming **the export it scored** — sealed 2026-09-17 23:34 UTC for the
current ones — rather than the day it was run, for the same reason `sprint4_evaluation/`
carries no date. It is offline apart from one read-only `SELECT` on
`KEA.KEA_GOLD.FCT_METER_READ`, holds no DML at all, and writes only inside its own
`results/`. Nothing in the pipeline imports it: `knn_quantile.py` is imported by its own
test and by the harness, no dbt model or DAG reaches it, and the two make targets are
referenced nowhere else — so the recommendation it produces changes no published number
until someone acts on it.

**Its tests run in CI, and a skip fails them.** `test_knn_quantile.py` needs numpy, which the
`dataeng/dbt` image carries and the CI host does not, so the host test step skips all 43. Since
#222 the dbt job runs them inside the image (`make ci ONLY=backtest`), and that step exits 1 on
any skip — the leakage tests are the ones that matter, because a backtest that can see the day
it is predicting scores beautifully and means nothing. Both make targets also run them first.

**When Snowflake joins**, two habits are worth starting with rather than retrofitting:
point questions at `mart_*` rather than raw tables, and turn on Metabase's query caching
before any dashboard auto-refreshes — each refresh is a warehouse query, and a dashboard
left open will keep a warehouse awake that would otherwise auto-suspend.
