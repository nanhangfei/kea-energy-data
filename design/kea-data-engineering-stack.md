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
| Pipeline PostgreSQL (the four sources, `kea-source-db`) | 17.10 | `../docker-compose.yml` | Stage 0 |
| PostgreSQL (the application's; where the publisher lands) | 16-alpine | `../../infra/docker-compose.yml` | Stage 5 |
| Apache Airflow | 3.3.0 | `../docker-compose.yml` | Stage 6 |
| Apache Spark / PySpark | 3.5.9 (official `apache/spark` image; standalone via `spark-class`), with pyarrow 17.0.0 and openlineage-python 1.53.0 added and held by its `constraints.txt` (Stage 8) | `../docker-compose.yml`, `pipeline/spark_jobs` | Stage 3 |
| MinIO / mc | RELEASE.2025-09-07 / 2025-08-13 (community edition archived Apr 2026 — last releases) | `../docker-compose.yml` | Stage 3 |
| PostgreSQL (Airflow + Marquez metadata) | 17.10 | `../docker-compose.yml` | Stage 6 |
| Marquez (+ web UI) | 0.50.0 | `../docker-compose.yml` | Stage 7 |
| Prometheus / statsd-exporter | v3.13.1 / v0.29.0 | `../docker-compose.yml` | Stage 7 |
| Grafana | 13.1.1 | `../docker-compose.yml` | Stage 7 |
| Metabase | v0.63.15 (on its own Postgres 17.10 — the bundled H2 corrupts) | `../docker-compose.yml` | now |
| Vault | 1.20.4 | `../docker-compose.yml` | stretch |
| Kafka + Schema Registry (Confluent Platform) | cp 8.3.0 (≈ Kafka 4.x, KRaft-only) | `../docker-compose.yml`, `streaming` | Sprint 5 |
| Apache Flink | 2.3.0 (java 17, Scala-free) | `../docker-compose.yml`, `streaming` | Sprint 5 |
| Snowflake | SaaS (no version) | — | Stage 4 |
| dbt (dbt-snowflake) | 1.12.0 with dbt-core 1.12.3; every other package in the image held by its `constraints.txt` (Stage 8) | `pipeline/docker/dbt` | Stage 4 |
| Soda Core | 3.5.6; every other package in the image held by its `constraints.txt` (Stage 8) | `pipeline/docker/soda` | Stage 7 |

**Two version facts: one still open, one settled in Stage 8.**

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
| Monitoring | StatsD → Prometheus → Grafana. Pipeline health only — the serving data is read in Metabase, §8 | — |
| Reconciliation | **$50.54** (TOU + solar, 24 days) recomputed end to end from source. **$248.07** (flat, 31 days) is a documented target with no source data behind it — it stays as the evidence for the catalogue's `[DERIVED]` daily rate | §10.3 |
| Blast radius | `kea_loader` has no grant on `app` — not even `USAGE`. Conversation history is the one thing no re-run can recover | §1.1 |

---

## 5. Build roadmap

Stages, each with an exit criterion that has to be **demonstrably true** — not "the code
exists". A box is ticked only when there is something in the repo to point at, which is
the standard the rest of `data/` is held to.

> **Stage 1 is why the stages after it have acceptance criteria at all.**
> **Stages 0–7 are done and verified.** Stage 4's criterion was Bill B
> reconciling from source at `$50.54` with a second build proven a no-op;
> `mart_scale_test` stays deferred (§6.5) and Bill A has no source data.
>
> **Stage 9 was to run before Stage 8; Stage 8's CI and deploy came first
> anyway** (15 Sep 2026). Until then a PR could merge with a broken DAG, a dbt
> model with a missing ref or an edited serving migration, and nothing tied the
> served release to a merged commit. Stage 9's checks join `make ci` when it
> lands. The numbers stay: eight code comments and five commit messages name
> "Stage 9", and git history cannot be renamed to match.
>
> **⚠️ Sprint 3's source work sits outside this roadmap and changed its
> premises.** On 2026-09-04/05 the data span moved to 2025-08-01 → 2027-08-27 so
> the newest reading is ahead of today rather than behind it, the four
> Ecotricity plans were replaced (first by Contact-derived products and now by
> three Kea designs), and term
> commitments, payments and plan history were added to the source. None of that
> is a stage — the roadmap is about the pipeline's capabilities, not its
> contents — but every figure quoted below is from after it.
> See `docs/sprints/sprint-3/`.

### Stage 0 — the local stack boots ✓

**Verified 30 August 2026.** The first stage to move off "unbuilt".

- [x] `.env.example` documents every variable the compose file interpolates; `.env`
      generated for local dev with the Snowflake block left blank, since Stage 0 touches none of it
- [x] `make images` builds all four tags — `dataeng/airflow:3.3.0` (3.18 GB),
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

### Stage 1 — the domain, the schema and the fixtures ✓

**Done first, before a single container existed.** It is numbered after the stack
because that is where a first run executes it: its schema and fixtures load into
Stage 0's database. The order the work was *done* in still matters, because the
common failure is the opposite one: stand up the stack first and work out what the
data means later.

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
warehouse reproduces **$50.54** from source data, and Stage 5 is done
when the pipeline's output agrees with the nine fixtures. Without Stage 1, "dbt build is
green" would only mean the pipeline agrees with itself.

It is also why `kea.mart_*` could be designed before there was anything to fill it: the
fixtures are a hand-built stand-in for the pipeline's output, and the pipeline is being
built to reproduce them.

### Stage 2 — source data exists ✓

- [x] `src_billing`, `src_metering`, `src_catalogue` stood up from `kea_source_schema.sql` and populated
- [x] LCL generated into the `src_*` shapes (design doc §12) — 500 households, 20,277,761
      half-hourly reads across 757 NZ-local days (2025-08-01 → 2027-08-27), of which
      413,233 are revisions carrying `version_no > 1`. 64 plans, 12,279 invoices, 11,708
      payments. The nine fixture households add 42,556 reads and 28 invoices on their own
      2023-06 → 2024-09 span. `make init-data`, proved end to end from a torn-down database
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
the reads and every assertion. Defect and revision injection was then run twice
on identical input and compared by an order-independent checksum over full row content —
`setseed()` had not been enough, because it fixes the random stream and not which row
draws from it.

### Stage 3 — extract → bronze → parsed ✓

- [x] Four independent extract jobs on their own schedules; a billing failure does not block metering.
      `kea_extract_metering`, `kea_extract_billing`, `kea_extract_catalogue`, `kea_extract_weather`
      — separate DagRuns, separate schedules, separate credentials (design §9). Weather is `@monthly`,
      not daily, because NIWA republishes the whole history monthly. The four `kea_extract_*` roles had
      `LOGIN` and no password and could not connect at all; `make source-roles` sets them out of
      band, `make assert-isolation` proves what the grants buy in 20 checks.
      **The artefact is a forced failure**, because independence is not provable by inspection:
      `make demo-independence` breaks the billing credential and shows metering completing anyway
- [x] `interval_reads` watermarked on `received_at` and partitioned by `received_at` date range.
      One literal `received_at` range per Spark JDBC predicate — 24 on an ordinary NZ day, 23 or 25
      across a DST transition (design §5.1). **A load date is an NZ calendar day** (design §4.2),
      which is the whole ballgame: the UTC window for 2026-08-25 holds 26,623 rows and *none* of the
      planted late revision; the NZ window holds 26,925 and all 48
- [x] Bronze immutable and as-received, at `bronze/{source}/{entity}/load_date=…`, carrying
      `_extracted_at` / `_source_system` / `_source_file`. `read_status` is still `read_status` in
      bronze: nine source columns plus exactly the three underscore-prefixed ones (design §4.2)
- [x] Parse writes Parquet conforming to the Avro schema — reshaping only, no business logic — for
      **all fourteen entities**. KEA-047 is hand-written; the other thirteen are derived by `make avsc`
      from `information_schema`, with `--check` catching a contract that has drifted from the source
      it describes. Conformance is checked against the Parquet *footer*, which caught a real defect:
      Spark 3.5 defaults to `INT96` timestamps (design §4.2)

**Done when:** a three-week-old correction is picked up by an ordinary nightly run, and
re-running a load date changes nothing.

**Status: done, 2 Sep 2026.** Both exit conditions are checked by a command, not asserted:
`make assert-bronze` (13 checks over the lake, on top of `make assert-isolation`'s 20) and
`make assert-idempotent`.

Two of the gate's checks carry most of the weight. Each partition must hold **exactly** what its
window selects at source with no `read_id` in two partitions — if the windows overlapped rows
would be double-counted, if they gapped rows would vanish, and both look like ordinary variation
in a daily row count. And the late-revision check is paired with a **negative control**: a window
computed on `interval_start` must find *none* of it, because without that the positive proves
very little — the wrong watermark still returns ~26,900 perfectly reasonable rows.

**The gate is known to fail, not just to pass.** One manifest's `source_row_count` was changed
from 26,616 to 99,999; it reported `FAIL 9 … sealed 99,999 now 26,616`, named the partition, ran
the other checks anyway, and exited 1.

**The backfill is the same DAG, no second code path** (design §11). 29 runs via `airflow backfill
create`, all green: a contiguous 2026-08-02 … 2026-08-30, **31 partitions, 734,124 rows, every
`read_id` distinct**, with `--expect-contiguous` passing over that range.

`make test` (then named `make test-parse`) runs 83 checks on the host with no database, no lake and no Spark;
`make test-conformance` runs 15 more against the lake.

**"All fourteen entities" is a claim about the lake, checked there** (3 Sep 2026), not
inferred from `spec.ENTITIES`. Every one of the fourteen is present in `s3://kea-lake/`
in *both* layers. Weather landed on two load dates — 69,569 observations and 4 stations,
`row_count` equal to `source_row_count` on both — and 69,569 is the same figure the NIWA
dictionary and the loader independently report, so the lake agrees with the prose.

`interval_reads` shows **32 parsed partitions against 31 raw**, which is by design and not
drift. The extra is `load_date=2000-01-01`, written by `test_lake_conformance.py`, which
picks a date of its own "so the fixture never collides with real data". The gate cannot
see it: `_bronze_load_dates()` discovers load dates from **bronze**, and the fixture writes
only parsed and quarantine. The 31 real raw dates are 2026-08-02 … 08-30 contiguous, plus
09-01 and **09-27** — the 23-hour DST day.

### Stage 4 — warehouse: Snowflake + dbt ✓

- [x] Staging models + `schema.yml` for all **fourteen** source tables. `stg_<source>__<entity>`
      as views (design §6.1), renaming and casting only — business keys and `version_no` are
      carried through unresolved and undeduped, because both are silver's job and doing either
      here destroys the evidence Stage 3's replay proof rests on
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
      discovered half way through a Stage 5 load. `dim_register` was not in §6.4's diagram
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
no-op.

⚠️ **Narrowed from "both bills" on 3 Sep 2026, and the reason is not convenience.** Bill A
is not in `src_billing` — no invoice carries its `$248.07`, and its plan
`VECT_OFPK_STD_140520` is absent from `src_catalogue`. Reproducing it would mean inventing a
tariff and 31 days of readings that add up to `$215.71`, then reconciling against them —
which tests the invention, not the pipeline. §10.3 already concedes Bill A's line detail was
never published, so that is the ceiling for it regardless.

**Bill A is not deleted, and must not be.** Its `$23.61` of “Other” charges
over 31 days and 1,034.665 kWh supplied the earlier Contact-derived catalogue's
non-levy residual. The three current Kea plans no longer use that derivation.
Its plan code remains a worked fixture example and a documented reconciliation
target with no source data behind it, which is different from a target that was
dropped.

**Status: done, 3 Sep 2026.** Both halves of the criterion above are met by a command, not
asserted. `make dbt-build` is **PASS=188 ERROR=0** across staging, silver, gold and marts.

**Bill B reproduces from source at `$50.54`**, matching §10.3 group by group — Network
`$29.22`, Energy `$21.10`, Other `$0.22`, across 11 lines — through bronze, `KEA.RAW`,
staging, silver's temporal plan resolution and gold, asserted nowhere along the way.

**And a second build is a no-op**, which took a bug fix to make true: every mart and fact
hashed across two full builds, excluding build timestamps, now comes back identical.

**"A second build is a no-op" is now true, and it was not.** Testing it — hashing every
mart and fact's content across two full builds, excluding build timestamps — found
`fct_meter_read` changing between runs at an identical row count.

The cause was in `int_metering__reads_deduped`, which ranked on `version_no` alone and
asserted in a comment that ties were impossible. **There are 4,775 tie groups**: Stage 2
injects 9,263 duplicate deliveries, and a duplicate is the same reading at the *same*
`version_no`, not a revision. `row_number()` broke them arbitrarily, so a different
`read_id` survived each build. No number was wrong — the tied rows are identical in `kwh`
and `quality`, measured, 0 of 4,775 differing — but the warehouse was not reproducible, and
**the uniqueness test passed throughout**, because exactly one row survives either way.
Ranking on `(version_no desc, read_id desc)` makes the ordering total.

The same fix exposed a second error in that model: `version_count` counted **rows**, so a
reading delivered twice at v1 reported 2 and told a bill explanation it had been corrected
when nothing of the sort happened. It counts distinct versions now.

`assert_dedup_ordering_is_total` guards it. Determinism needs two builds to test; what one
build can check is the property it depends on — that the full ordering key is unique — and
that is the test the original bug should have run into.

**If Bill A is ever wanted as a live target**, what it needs is construction rather than
extraction: an invoice in `src_billing` carrying the figures page 1 actually states —
`charges_exgst 215.71`, `gst_amt 32.36`, `total_amt 248.07`, `late_fee 37.21`,
`total_import_kwh 1034.665`, issued 2023-04-08 — with a plan version and 31 days of readings
derived to land on §10.3's published group totals. That is a Stage 2 change, and the result
would test the derivation rather than the pipeline, which is why the criterion no longer
depends on it. Page 1 says as much itself: *"Electricity charges (please see over for
details)"*, and the "over" we hold is page 2 — a different bill, on a different plan, a year
later.

⚠️ **The checkbox above said "12 source tables" until now, and there are fourteen.** Same
stale count the weather source left everywhere else; the earlier sweep missed it because it
searched for the spelled-out "twelve".

**Snowflake is its own account** (AWS ap-southeast-2; the identifier lives in `.env`), not a database on
the sibling project's. Kea connects as `KEA_TRANSFORMER` against `KEA_WH` — never
`ACCOUNTADMIN`, because `dbt build` drops and recreates what it owns and the blast radius of
a wrong `DBT_SCHEMA` should be a permission error. `snowflake/bootstrap.sql` creates all of
it and is run once, by hand: every container here authenticates as the role that file
creates, so it cannot bootstrap itself, and a `make` target would need an ACCOUNTADMIN
credential in `.env` — a far larger thing to leave lying around than the one it would make.

**Snowflake cannot reach the local MinIO**, so the lake cannot be an external stage and dbt
cannot read Parquet where it lies. `pipeline/scripts/load_bronze_to_snowflake.py` pushes
parsed bronze up with the connector's PUT + COPY, and the mode split is the only interesting
decision in it: a snapshot's partition is a fresh full copy, so RAW is truncate-and-reload
and holds only the current picture, while an incremental entity's partitions are disjoint by
construction so RAW accumulates them and idempotency comes from deleting the load date's own
rows first. **That split is what keeps staging honestly 1:1** — otherwise every model would
need a "latest load_date" filter, which is business logic wearing a mechanical disguise.

Measured rather than asserted: `make load-snowflake LOAD_DATE=2026-08-25` lands **14/14
entities**, `make dbt-build` is **PASS=75 ERROR=0** (14 views, 61 tests), and re-running both
leaves all 16 row counts identical. `METERING__INTERVAL_READS` holds **26,925** rows — the
same figure §Stage 3 records for that NZ window, so the warehouse agrees with the lake.

**Second pass, 3 Sep 2026 — silver.** `make dbt-build` is **PASS=108 ERROR=0**.

**The key maps are append-only, proved rather than intended.** All three were hashed, the
whole project rebuilt, and hashed again: identical. `incremental` is what enforces it — a
`table` materialisation would renumber every id on each run and satisfy "stable" only while
the input never changed order. Sequential rather than a hash surrogate because
`kea_serving_schema.sql` types these `bigint GENERATED ALWAYS AS IDENTITY`, and Stage 5 has
to land in that column.

**`key_map_plan` holds 73 versions across 41 plan codes**, which is design §6.2 rule 2 made
concrete: a map keyed on `plan_code` alone would collapse those 73 to 41 and make temporal
resolution impossible. 32 of the 41 codes have two versions.

**The quarantine found exactly what Stage 2 planted, by an independent path.** Three orphan
ICPs and two accounts on `ECO_GONE_VECT_STA`; `07_billing_defects.sql:58` announces "3 orphan
ICPs, 2 orphan plan codes" and asserts five. The injector and the detector have never spoken
to each other. §6.3's retry rule is demonstrated too: the three orphans reported
`integrity.timing_skew` at misses 1 and 2 and promoted to `integrity.orphan_icp` on the third.

**Temporal plan resolution is EXERCISED, and the number is large.** This paragraph
previously said the opposite: with 41 invoices loaded, all ending August 2026, a naive
code-only lookup agreed on every one and the machinery was insurance not yet claimed on.
The billing backfill settled it — **695 issue dates, 11,534 invoices** — and against the
full history a code-only lookup would mis-rate **2,878 of 11,534 invoices, 25%**. Every one
of those is a bill rated against a tariff version that was not in force, with nothing
failing and every total still balancing. §2.1.2 calls this the most dangerous step in the
pipeline; it is now a measured quarter of the book rather than a warning.

⚠️ **Comparing a `TIMESTAMP_TZ` to a `DATE` resolves the instant in the SESSION timezone**,
which Snowflake defaults to `America/Los_Angeles`. Measured: all 41 invoices resolved one day
earlier than their NZ calendar date, because a period ending midnight NZ is 05:00 the previous
day in Los Angeles. On a bill whose period end lands on a version boundary that silently picks
the wrong tariff — §2.1.2 exactly. `int_billing__plan_resolution` now converts to an NZ date
explicitly; `bootstrap.sql` also sets the user's session timezone, but as defence in depth,
because a correctness property that depends on a session parameter is not a property.

**Third pass, 3 Sep 2026 — gold.** `make dbt-build` is **PASS=148 ERROR=0**.

**The register rule Stage 2 asked for.** `05_defects.sql` plants an ICP whose
`register_code` maps to several meters and says so: *"no rule is documented for picking
one … Stage 4 needs the rule."* The rule is **filter to meters live at the reading's
instant, and quarantine if more than one survives** — never pick. Measured why: a naive
join through `registers` inflates by **750 rows and 447.05 kWh**, all on one household,
with no error and a plausible grand total. `fct_meter_read` excludes those 150 readings
rather than attributing them, and `integrity.ambiguous_register` records the subject.

⚠️ **That injection is not idempotent** — `05_defects.sql` clears `gen_audit.defects` each
run but not the meters it inserts, so every run appends another `SM-SECOND-01`. The code
says "two meters"; there are six. Not fixed here (regenerating source is a Stage 2 change),
and the rule handles any number of candidates.

**Two deviations from §6.4, both deliberate.** `dim_date` does **not** carry heating degree
days: HDD is regional, four values per day, so it would force the date dimension to a
`(date, region)` grain and stop it being conformed. It lives in `fct_weather_daily` at its
true grain, and §6.4's "no join at query time" is met by the marts denormalising it. And
`is_public_holiday` is **NULL, not false** — there is no NZ holiday calendar anywhere in
this repo, named in three design documents and backed by no data. NULL says unknown; false
would assert Waitangi Day is an ordinary Tuesday.

⚠️ **The nearest-station weather fill is still unimplemented.** `fct_weather_daily` reports
what NIWA published, so a regional gap is a null rather than a substitution — 128 of 69,569
rows. The dictionary specifies the rule; the marts are where it belongs.

**`mart_forecast_input`, and the invariant it is built around.** §8.6 states that across
one cycle `hdd_to_date + hdd_expected_remaining` is **constant** — "fact displacing
forecast, day by day, over a fixed total" — and the fixture asserts it. That fixes the shape
of the calculation: the expected total is decided **once** per period and the remainder
derived as `total − to_date`, rather than the remaining days being forecast independently,
which would let the sum drift as actuals arrived. A singular test now asserts it over all
**11,440 cycles**, not one: the only cycles where the sum moves are the **1,645** where a
colder-than-normal period pushed `hdd_to_date` past the expected total and the clamp
engaged, which is named as the exception rather than tolerated as slack.

⚠️ **§8.6 requires `hdd_expected_remaining` and never says where the expectation comes
from.** A forecast API would cover a fortnight of a month-long cycle and nothing after it.
`int_weather__climate_normals` answers the whole horizon from data already in the warehouse
— mean HDD per `(climate_region, month, day)` over **30 years** of the NIWA series. Thirty
and not the full 73: a normal is a climate baseline, and including the 1950s would bias
every forecast toward a colder past.

⚠️ **A third bug of the same family, caught by a flat number.** A worked cycle showed
`hdd_to_date` frozen at 11.20 across six consecutive days while `is_forecastable` stayed
true. `days_without_weather` counts rows that are present and null — it cannot see a day
with **no row at all**, so a cycle whose metering covered 2 of its 31 days passed the hedge
with a forecast built on a fifteenth of the period. `days_observed = days_elapsed` is now
required too, which removed 9,636 false positives. This is the third time the same shape has
appeared: a completeness check that verifies the inputs it can see rather than the ones it
needs.

**97,544 of 349,310 snapshots are forecastable**, and a cycle now reads as §8.6 describes:
`to_date` 10.00 → 21.10 → 30.50 while `remaining` falls 203.16 → 192.06 → 182.66, total
holding at 213.16.

⚠️ **`cost_to_date` is an estimate at the prior period's rate, not a rating.** A true one
needs the tariff applied to a partial period — `tou_windows`, `rate_components`, band
scoping — which is the same rating engine `delta_from_tariff` is missing in §8.5. Priced at
the prior bill's own average variable rate so the two marts at least agree with each other,
and NULL where there is no prior bill rather than 0.

**`mart_scale_test` is deferred**, and the arithmetic behind that decision is recorded once
in §6.5's outstanding table rather than repeated here: a 2.45B-row re-rate against a
warehouse holding 9.7M, where 2.45B is Kea's production projection and not this cohort.

**The marts, and what they demonstrate.** (The current build count lives once, in Stage 4's
status above — repeating it here is how it came to read 168 long after it was not.)

`mart_daily_usage` is where the NIWA series finally reaches a household-day, and where the
NZ day's real length is used rather than assumed: `expected_intervals` is **46 / 48 / 50**,
computed from the calendar, and the spine confirms 6 short days and 6 long ones — with
2025-09-28 at 46 and 2026-04-05 at 50. A hard-coded 48 would make two days a year look 4%
short and 4% over. Energy is conserved through the rollup: 21,328.11 kWh in the mart equals
21,328.11 in `fct_meter_read`.

**74% of household-days have no HDD, and that is the dictionary being right rather than a
bug.** Weather ends 2026-08-17 and 976 of the mart's 1,424 rows fall on 08-24 and 08-25 —
inside the documented 11-day publication tail. Every date before the tail has HDD on every
row. The decision recorded in `niwa-weather-source.md` — take the gap rather than move the
span — is now visible in the data instead of only in prose.

`mart_hourly_profile` converts to **NZ local hours**, and the check that it worked is the
shape: the busiest weekday hour is **19:00**, the NZ residential evening peak. Reading the
hour off the UTC instant would have rotated every household's curve by 12–13 hours — and
not by a constant, so the error would have read as a seasonal effect rather than a bug.

`mart_account_context` closes design §6.3's last loop, the one it calls the link most
pipelines never make: **5 of 509 accounts carry `is_quarantined`** — the 3 orphan ICPs and
2 unknown plan codes Stage 2 planted — with their reasons attached. They are kept and
flagged rather than dropped, because a missing row makes the agent fail to find the
account, while a flagged row lets it decline for a stated reason.

**The billing backfill, and half the exit criterion.** `invoices` is incremental on
`issue_dt`, so the warehouse only ever held the load dates that had been extracted — two of
them. The source has **695 distinct issue dates**; all were extracted (23s, zero failures)
and loaded (695 landed, zero skipped, zero failed). RAW now holds **11,534 invoices** and
`fct_bill_line` **101,236 lines across 11,528 invoices**, up from 372 across 41.

**Bill B reconciles end to end: `$50.54`**, and group by group against §10.3 —
Network `$29.22`, Energy `$21.10`, Other `$0.22`, across 11 lines. That is the warehouse
reproducing a real published invoice from source data, through staging, silver's temporal
resolution and gold. **Bill A remains unconstructible** — it is not in the source at all
(see below), so the other half of the criterion is still open.

**All four integrity classes now fire**, where before only two had data to catch:
`no_plan_version` 6, `orphan_icp` 3, `unknown_plan_code` 2, `ambiguous_register` 1. The six
`no_plan_version` invoices are ones no catalogue version brackets — exactly the case §6.3
says must be quarantined rather than resolved to the nearest. A further **26 invoices match
two versions**, which is an overlapping-version defect in the catalogue that the model
surfaces rather than hides.

**`mart_bill_explanation`, and a decomposition that closes.** §8.5's four drivers in
attribution order, with period length removed first so a 43-day move-in bill is not read as
a 40% consumption rise, and everything priced at the prior period's own average **variable**
rate — fixed daily charges subtracted first, or every per-kWh driver is inflated by the
fixed component. 11,528 bills built, and a singular test asserts the invariant the mart
rests on: **the four drivers plus the remainder equal the amount delta on all 11,019 bills
with a prior, none off by more than two cents.**

**The metering backfill, and what it revealed.** 120 load dates (2026-04-20 → 08-17,
autumn into winter for HDD range) extracted and parsed through Spark — 120/120, no failures,
32 minutes — then loaded to Snowflake, 120/120. `fct_meter_read` went from 53,519 to
**3,229,225** rows and `mart_daily_usage` from 1,424 to **61,300** household-days across 499
ICPs. The heating slope now fits for every one of them.

⚠️ **THE FIT WAS WORTHLESS AT DAILY GRAIN, AND THE FIX WAS THE ESTIMATOR, NOT THE DATA.**
The first fit was daily: median R² **0.01**, slopes from −3.98 to 8.44 where §8.4 expects
~0.9, and a negative slope asserts a house uses more heating when it is warmer.

The cause is in the source, not the arithmetic. The cohort is LCL — real **London** metering
date-shifted so UK winter lands in NZ winter. **The shift aligns SEASONS, not WEATHER:** a
household's day-to-day variation responds to London's weather that day, while the HDD it is
regressed against is Auckland's. The seasonal envelope matches; the daily wiggle inside it
cannot.

**§8.5 spends the slope across BILLING PERIODS, not days** — so fitting it daily measured
exactly the component that is noise. Refitted at monthly grain on 12 months, same data, same
households:

| | ICPs | median slope | median R² |
|---|---:|---:|---:|
| daily | 499 | 0.45 | **0.069** |
| **monthly** | 446 | **0.74** | **0.467** |

0.74 against §8.4's ~0.9 for a typical NZ home on electric heating, and **290 of 446 clear
the 0.30 fit floor**. The lesson is general: fit an estimator at the grain it is spent at.

**So the cohort does NOT need regenerating.** The earlier conclusion — that London
households cannot respond to NZ weather — was true of the daily component and false of the
seasonal one, which is the component the bill explainer actually uses.

**The bill explainer now works.** **2,426 of 11,528 bills are explainable**, and weather is
the dominant driver as a seasonal energy bill should be:

```
avg |delta_from_weather|        $25.73     68.9% of all movement
avg |delta_from_period_length|   $9.70
avg |delta_from_base_load|       $8.65
```

⚠️ **Two bugs this exposed, both mine.** An earlier `is_explainable` checked that *this*
period had full weather cover but never the *prior* period, and reported 1,269 bills as
explainable whose `delta_from_weather` was NULL. It now requires `prior_hdd`,
`prior_base_load_kwh_day` and `rate` too.

And **`flow_direction` is `'IMPORT'` in billing but `'import'` in metering** — two systems
built by different teams, which is exactly the mechanical difference §5 says staging exists
to reconcile. Left alone it is invisible: a filter on one spelling matches nothing and the
sum returns **0 rather than an error**. Every bill had `total_kwh = 0`, which made the
variable rate null and took every weather driver with it. Normalised in
`stg_billing__invoice_lines`.

⚠️ **`delta_from_tariff` is not re-rated.** §8.5 defines it as re-rating this period's kWh
under both plan versions, which needs the rating engine expressed in SQL. Where the plan
version did not change it is definitionally **0** and asserted so; where it changed it is
**NULL, not 0**, because 0 would push a real tariff move into `delta_unexplained` and make
the decomposition look complete when it is not.

⚠️ **`write_pandas(auto_create_table=True)` let the FIRST load date define the schema.**
Invoices' `decimal(12,2)` amounts became `NUMBER(4,2)` because 2026-08-25 held only two-digit
values; 2026-08-26 failed with "Number out of representable range … value 135.95". A narrower
*scale* would have been worse — silently truncated cents. The loader now builds
`CREATE TABLE` from the Avro contract (`avro_schema.to_snowflake_ddl`), and all 14 tables were
checked column by column against it. Related: plain `to_pandas()` degrades any type pandas has
no nullable equivalent for, which turned an all-null `date` column into a number and widened
`rel_humidity_pct` to float. Reading with `types_mapper=pd.ArrowDtype` keeps the Parquet's own
types end to end and removes the class rather than the instances.

### Stage 5 — export → the served tables in Postgres

The target was `kea-serving-db` (now `kea-source-db`) in this project's stack (§6.3), with the application's
database deferred. **Since 12 Sep 2026 it is the application's database**, locally `kea-db`
from `../../infra` — §6.4. The checkboxes below record what was proved against the sandbox.
The publication that replaced the load keeps every one of those properties, and widens the
transaction to take in the Sprint 3 decisions.

The schema already applied there and the nine fixtures already loaded into it — but that was
Stage 1's work, not this stage's. What Stage 1 provides is the **acceptance target**: the
pipeline is finished here when its own output stands where the hand-seeded fixtures stand
now.

- [x] ~~`COPY INTO`~~ → `s3://kea-lake/export/{table}/`, then the loader — **the direction was
      wrong and the design has been corrected.** Snowflake is SaaS in ap-southeast-2 and MinIO
      is local-only, so it cannot `COPY INTO` the lake any more than it could read it as an
      external stage on the way up. `export_warehouse_to_lake.py` **pulls**: runs the query,
      receives Arrow batches, writes the Parquet. 17 tables, 10.7M rows, ~16s
- [x] The pipeline's own output reproduces what the fixtures assert — the point of this stage.
      `make assert-serving`: **56 checks, 50 pass, 0 failures.** Bill B comes back at
      **$50.54** from pipeline output, with peak import 34.08 kWh and export 47.48 kWh; the
      13-month household reproduces its 397-day span, 397 daily rows with no holes, 13 profile
      months, and both the 46- and 50-interval DST days. **6 known divergences**, each recorded
      in the gate with its cause — see below
- [x] Per-table strategy honoured: truncate+reload for small tables, DELETE-by-period for
      bills, **partition swap** for `meter_read_hh` (39 monthly partitions, built beside the
      table and attached with a matching CHECK so the swap is metadata-only)
- [x] Load order follows the FK graph, not a sleep — `serving.TABLES` is the single ordered
      list both the exporter and the loader read, including `catalogue.plan` →
      `metering.network`, which reads like a sibling and is not. **Stage 6 settled the
      "as task dependencies" half, and not the way the checkbox expected**: the pipeline's
      stages *are* task dependencies now, but the FK order within the load is deliberately
      NOT, because that load is one transaction and splitting it across tasks would give each
      table its own commit. The ordering is a property of the loader; the DAG has one
      `load_serving` task — `publish_serving` since §6.4, still one transaction. See Stage 6
      and design §9
- [x] `synced_at` set per table at commit time, not `DEFAULT now()` per row

**Done when:** a full load runs twice with identical row counts ✅ (identical across all 17
tables), a mid-table failure leaves the table unchanged ✅ (`make assert-load-atomic` injects a
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

**The known divergences**, all properties of the source rather than of the loader. There
were six at Stage 5; the sixth, fixture 09 publishing no forecast snapshots, went when #143
gave that household a May bill and a plan version covering last June:

| What the fixture asserts | What the pipeline produces | Why |
|---|---|---|
| `cold_snap` blames weather; three scenarios are `is_explainable` | `unexplained`, and false | The nine fixture households carry ~2 months of reads. `int_metering__heating_sensitivity` reports `fitted_on_months = 2` and returns NULL, so there is no slope to attribute a weather term with. The fixture's 0.93 kWh/HDD is a hand-set constant in `generate_scenarios.py`, not something an estimator recovered from 61 days |
| 13 bills for the 13-month household | 8 | `DEMO_STD_FXTOU_13_MONTH_HISTORY` has one version, effective 2024-01-01; the five 2023 invoices end before it, match no version, and are quarantined as `integrity.no_plan_version`. Design §2.1.2 refusing to re-rate history against a tariff that did not cover it. A catalogue gap, not a loader one |

They are kept as failing-but-explained checks rather than deleted, so the day one starts
passing is visible too.

**The dashboard** is `make stage5-dashboard` — the first one that can see warehouse output
at all, because Metabase is connected to `kea-serving-db` (now `kea-source-db`) and everything Stage 4 built lives
in Snowflake. It carries what only this stage makes answerable: `mart_load_log` and
`synced_at` (did the load work, how fresh is the replica), and a source-against-serving
comparison naming every deliberate exclusion. Since §6.4 the two sides of that comparison are
in different databases, so it is two aligned cards: *What the sources hold* reads **Kea
pipeline**, and *What was served, and why it differs* reads **Kea serving**, the
application's database as `kea_app`.

⚠️ **Stage 5 broke the fixtures dashboard, and that was the point of `verify-dashboards`.**
`build_dashboard.py` named its nine households by surrogate id; the load replaced every
fixture row with pipeline output carrying the warehouse's own key maps, and all twelve cards
became valid queries returning nothing. It now resolves them by `icp_number` at build time,
the same fix `assert_serving.sql` needed. All four dashboards pass.

### Stage 6 — orchestration

- [x] `kea_daily_pipeline` runs the whole path — ~~on the 02:00 NZT schedule~~ **asset-scheduled**,
      on `AssetAll` of the three daily lanes' bronze. Design §9 and `_kea_common.py` both say
      the pipeline "should be asset-scheduled on what these emit rather than owning their
      tasks", and that beats a fixed hour: 02:00 is a guess about how long an extract takes,
      and a wrong guess reads yesterday's partition and reports success. Six tasks —
      `snowflake_stage → dbt_run → dbt_test → export_warehouse → load_serving → assert_serving`
      — verified green end to end. Today, after Stage 7 and §6.4: `… dbt_test →
      soda_scan_warehouse → export_warehouse → publish_serving → soda_scan_serving →
      assert_serving`
- [x] Separate DAGs for the different cadences — `kea_forecast_refresh` is new (manual
      trigger; see below). `kea_weather_load` **already existed** as `kea_extract_weather`,
      `@monthly` since Stage 3. `kea_scale_test` is **deferred with `mart_scale_test`**: a DAG
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
path"). `make backfill-pipeline FROM=… TO=…` wraps the loop; `max_active_runs=1` keeps a month
from starting thirty Snowflake sessions.

That Param is load-bearing for a second reason. An asset event carries no date, so a cleared
extract re-run for an old day would fire this pipeline, which would then take **today** from
its own fire time and stage the wrong day.

### Stage 7 — quality gates & observability

This stage is about the **pipeline's** health — whether a bad load is caught and stopped.
The BI tools that read the serving data are Stage 1's, and §8 draws that line.

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

      ⚠️ **The mapping file was silently disabled and the dashboard could never have had
      data.** `statsd-exporter` rejected it whole — *"invalid match:
      airflow_kea.operator_failures_*"* — because its glob matcher only wildcards complete
      dot-segments, and Airflow puts the operator name in the same one. It logged the error,
      kept running on defaults, and every metric quietly kept its old name. One bad pattern,
      the entire file ignored. Fixed with `match_type: regex`; metrics now arrive as
      `airflow_task_duration_ms{dag_id=…, task_id=…}` and all five panel queries return
      series through Grafana's own datasource proxy.

      ⚠️ **Two panels queried metrics Airflow 3.3 does not emit** — `dagrun.duration.*` and
      `dag_processing.import_errors`, checked at the exporter rather than assumed. They now
      read task duration and per-operator failures, which do arrive.

      ⚠️ **If the panels look empty, it is a browser extension — not this dashboard.**
      Confirmed by elimination: the panels render completely in an incognito window, while a
      normal profile shows every panel stuck on *"Loading plugin panel…"*. Explore renders
      the same Prometheus data fine either way, because its graph is a built-in component
      rather than a panel *plugin* — which is why only dashboards are affected. A
      Grafana-authored dashboard holding one markdown panel fails the same way, as does
      Grafana 12.1.0, with every asset returning 200 and a clean console.
      **Open it in incognito, or exclude `localhost:3001` from the extension.** Nothing here
      needs changing.

**Done when:** an injected defect lands in quarantine with its reason, raises an alert and
stops the export — and the lineage graph covers source to serving. **`make assert-quality`
checks all four**, by poisoning the Soda gate and watching what happens.

⚠️ **THE OBVIOUS SODA CHECKS ARE RED EVERY NIGHT ON THIS DATA.** The source is a fixed
historical extract: its readings stop 2027-08-27 and its arrivals 2027-10-10. So
`freshness(interval_start) < 1d` fails forever, and reads-per-LOAD-date collapses from ~26,900
to ~250 once the extract passes the end of the readings. The Stage 3 dashboard already
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

### Stage 8 — testing, CI & deploy

> **Partly built ahead of Stage 9.** CI and `make deploy` were built on 15 Sep 2026; see the
> note at the top of this section.

- [x] Unit tests for the parse job: reshaping, quarantine rules, DST-boundary day lengths
      (a local day is 46 or 50 half hours, never assume 48). Reshaping is `test_reshape.py`,
      in CI. The quarantine routing and the two DST days go through the Spark parse itself in
      `make test-conformance`, which needs Spark and MinIO and so stays out of CI: 5 Apr 2026
      parses to 50 distinct half hours with 02:00 and 02:30 twice, 27 Sep 2026 to 46 with no
      02:xx, every read keeps its instant, and both days match the extract's load-date window
      (15 Sep 2026, 22 tests OK)
- [ ] CI green end to end, behind the one required `check`: ruff, the host unit tests, the DAG
      import test, `dbt parse`, the serving migrations and a publication on a throwaway
      Postgres 16, and the Story page's identifier guard. Three jobs in
      `.github/workflows/ci.yml`, each step a `data/Makefile` target, so `make ci` runs the
      same thing locally, without `.env`. Built, and `make ci` is green locally
      (15 Sep 2026); this ticks when GitHub runs it green on a PR
- [ ] `dbt build` in an isolated schema, dropped afterwards. It needs Snowflake credentials as
      repository secrets, which only an admin can add, on a trial that converts on 3 Oct 2026.
      Until then CI parses: that catches broken refs and sources, Jinja and YAML errors and
      malformed tests, but **runs no data test**. A failing dbt test stops `make deploy`
      before anything is published; it does not stop the merge
- [ ] `make deploy`: merged code only (a clean checkout at `origin/main`), gated by `make ci`;
      the real `dbt build` with its data tests; migrate, then one atomic publish recorded as
      `git-<sha>`; the fixture checks and the serving scan; every dashboard rebuilt and
      verified. Built, and `make deploy-check` refuses a dirty tree as designed. It has not
      run: it deploys `main`, which gets this target and the serving migrations only with
      PR 156
- [x] Versions reviewed and pinned (15 Sep 2026; §3). Every image tag names a release:
      `postgres:17.10`, where `postgres:17` floated, for the four pipeline databases and
      every one-off `psql`; `postgres:16.15-alpine` for the throwaway serving databases in
      CI and `assert_load_atomic.sh`; `python:3.11.16-slim` under the dbt and Soda images and
      `alert-sink`. Every image built here installs from a constraints file: dbt (95
      packages), Soda (3.5.6; 52) and Spark (pyarrow and openlineage-python; 20), each its
      image's `pip freeze`, and Airflow from Airflow's own. Each rebuild's freeze matched the
      image it replaced. kea-db's own `postgres:16-alpine` is the backend's to pin

**Done when:** a PR cannot merge with a broken DAG, a failing dbt test, or a failing unit test.
Once the CI change reaches `main`, a broken DAG or a failing unit test cannot; a failing dbt
*data* test also needs the isolated-schema build above.

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
                     extract_*, gen_*, set_*, assert_*. Its README says who
                     runs each, which the layout cannot: extract_small.py is
                     the only one Airflow imports, and the assert_* scripts
                     are proofs about a RUNNING system rather than tests of
                     code, which is why they are not in tests/
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
  kea-source-db                       the four source systems (kea-serving-db before §6.4)
  marquez(+db, web)                   namespace kea_energy
  statsd-exporter · prometheus · grafana · vault
  metabase(+db)                       analysis: the served tables and the sources
  kafka · schema-registry · kafka-init · flink x2   (streaming profile, Stage 9)

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

### 6.4 Publishing pipeline output to the application

`make publish-serving RELEASE=<id>` points the loader at the same PostgreSQL database the
application reads. It loads the sealed warehouse export, computes forecasts, plan
recommendations and hardship outputs, validates them, and records the release in
`data_ops.serving_publication`. These data changes share one transaction, so an error
leaves the prior release visible.

`make migrate-serving` applies immutable SQL files from `migrations/serving/`, and is run
deliberately, before a publish that needs it: a publish connects as `kea_loader`, which can
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
task, `make rebuild-demo` and `rebuild_span.sh` all call this same publisher.

The current lake export paths are mutable. Scheduled export and publication therefore
must not overlap until exports gain immutable release prefixes and a manifest. Large
publications can also hold table or partition locks long enough to delay API reads, so
production scheduling needs a measured maintenance window. The runbook is
`migrations/serving/README.md`.

### 6.5 What is built, and what is not

**Done, and verified running** (30 Aug 2026 — see Stage 0). `../docker-compose.yml`, `../Makefile` and `../.env.example` have been rewritten
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
- The Make target vocabulary is the conventional one for this stack — `up`, `down`,
  `clean`, `dbt-build`, `dbt-test`, `soda-scan`. `spark-parse` is singular by design:
  §1.1 says one Spark job, so there is no bronze/silver/gold chain.
- `--env-file` replaces shell sourcing, which is also what lets the `&` inside the
  `AIRFLOW_CONN_*` URIs survive — sourcing them in a shell does not.

**Still outstanding:**

| What | Why it matters |
|---|---|
| ~~Basic Plan carries most of the cohort~~ — **closed 2026-09-08** | The Contact-derived catalogue was replaced by Anytime Rate, Time of Use and Free Weekends. See `docs/sprints/sprint-3/plans-and-terms.md`. |
| ~~`ecoWHOLESALE` is approximated~~ — **closed 2026-09-05, by retirement** | The spot plan was retired with the other three Ecotricity products when Sprint 3 adopted Contact's range, none of which is spot-priced. The finding stands and is worth keeping: a spot price is a market outcome per half hour per GXP rather than a tariff, so `rate_components.unit_rate` being a scalar can never express it. If a wholesale product returns, EA's EMI publishes final half-hourly prices per node and `dictionary/ecotricity-tariff-source.md` still carries the schema, URL patterns and GXPs |
| `mart_hourly_profile.is_weekday` carries no signal in the fixtures | Design §8.7 argues weekday and weekend are two different curves, and the column doubles the mart's row count to hold that. But `generate_scenarios.py` applies one hourly shape to every day: across all nine fixtures the largest weekday/weekend difference in any hour is **0.0001 kWh**. Daily totals differ by up to 7%, and that is weather landing on different dates, not a shape difference. Visible on the "Average day, weekday vs weekend" panel as two curves sitting on top of each other. The Stage 2 cohort does not have this problem — LCL is real metering, so its day-of-week variation is real |
| ~~Only one of 64 plans is fully bill-sourced~~ — **closed 2026-09-08** | The active generated catalogue has six version rows for three Kea design tariffs. The screenshot values are `[DESIGN]`; internal ex-GST/loss rates are `[DERIVED]`; the common export rate is `[ASSUMED]`. |
| ~~`pipeline/soda/checks/` is still empty~~ — **filled, Stage 7** | It was the last empty code directory. It now holds `warehouse.yml` (the gate, after `dbt_test`) and `serving.yml` (a report, after the load), with `configuration.yml` beside them naming two datasources. ⚠️ The checks are deliberately **not** the textbook ones: this source is a fixed historical extract, so a freshness check on the readings or a ±10% volume check per load date is red every night forever — the file carries the full reasoning |
| **`mart_scale_test` — deferred, 3 Sep 2026** | §6.5 specifies "the 2.45B-row re-rate", and the warehouse holds **9,691,082** reads. 2.45B is Kea's *production* projection for their real customer base; this cohort is 500 households whose source holds 19.1M rows in total, so running it means synthesising ~253× the data before re-rating any of it. On a fixed $400 trial that has spent **2.77 credits** and converts on **3 Oct 2026**, that spends the remaining budget demonstrating a property of Snowflake rather than of this pipeline. **Revisit in a funded environment**, or scale-test at an affordable volume with the extrapolation stated — but not by quietly reinterpreting the number. It is the one mart that is never exported, so nothing downstream waits on it |
| The Airflow image cannot launch Spark directly | It has neither `java` nor `spark-submit`, so `SparkSubmitOperator` is unusable despite the provider being installed. The DAGs use `DockerOperator` against `dataeng/spark:3.5.9` — the sibling-container pattern the compose file already provisions for dbt and Soda |
| ~~Soda Core is unpinned~~ — **closed 15 Sep 2026, Stage 8** | 3.5.6 was only what the build happened to resolve. The Dockerfile now names both Soda packages at 3.5.6, and `pipeline/docker/soda/constraints.txt` holds every package they pull in. The rebuilt image's `pip freeze` matched the old one's exactly, so every scan so far ran on what is now pinned |
| ~~No Spark job can be inspected after it finishes~~ — **closed 7 Sep 2026** | The master UI on 8082 still has its proper, narrower role: cluster status and the application list. `spark_session.py` now writes compressed event logs to `s3a://kea-lake/_spark/events`, and `spark-history-server` replays them on host port 18080 with a five-second refresh and 30-day retention. Completed applications now retain their Jobs, Stages and DAG pages after the driver container exits. Jobs completed before event logging was enabled have no log to replay and cannot be reconstructed retroactively |

---

### 6.6 Rebuilding after the span moves

`pipeline/scripts/rebuild_span.sh`. Run it when `uk_to_nz.NZ_SPAN_END` or `LCL_START` moves —
the source is regenerated with a new shift, so **every row in the warehouse is wrong until
this finishes**. Measured on the 2027 move: 757 load dates, **6h24m**, then about five
minutes for the once-only tail.

The shape is per-date work per date, once-only work once:

| | Work | Cost |
|---|---|---|
| per date | extract + parse (Spark), scripted entities, stage INCREMENTAL ONLY | ~31 s × 757 |
| once | the twelve snapshots, `dbt build`, export, serving load | ~5 min |

Staging snapshots per date instead costs 40 s rather than 6, because eleven of twelve are
overwritten by the next day's — 7.2 hours across the span, for nothing.

#### ⚠️ Everything here is a partial replace, and a partial replace leaves the rest behind

The 2027 move surfaced **five** bugs. All five were the same shape and **four of the five
exited 0**. They are listed in the decision log with their measurements; what matters here is
the pattern, because the next person will meet a sixth:

```
truncate RAW          total          <- the only total operation in the chain
extract               per partition  <- sealed partitions are skipped without --force
stage                 per load date  <- replaces the dates it is given, keeps the rest
partition swap        per month      <- replaces months the export carries, keeps the rest
delete+insert (dbt)   per key        <- deletes keys in the incoming batch, keeps the rest
materialized views    never          <- do not follow their base tables at all
```

Reading down that column: **only the first line removes anything it was not explicitly handed.**
Every other step's idea of "replace" is scoped to what arrived, so anything that used to be
there and no longer arrives simply stays — carrying the old shift, invisible to every per-date
check, and wrong in every aggregate.

The truncate is what made this visible. It is also why a span move is harder than a backfill:
a backfill's new data covers the old, and a span move's does not.

#### What the script now verifies rather than assumes

- **The truncate happened.** It reads the row count back. The first version printed
  "truncated" from a process that had never received its own script — `docker run` without
  `-i` does not attach stdin, so the heredoc piped to `python3 -` read EOF and exited 0 —
  and the rebuild ran ten minutes mixing shifts before anyone looked.
- **The snapshots landed.** Counting rows in each snapshot's RAW table, because the loader
  returns 0 for "skipped, no partition" and 1 only for a real failure.
- **Invoice dates outside the reading span.** `FROM`/`TO` are the dates *readings* exist for;
  `invoices` is incremental on `issue_dt` and falls outside at both ends.

#### After it finishes

```bash
make dbt-test && make assert-serving && make soda-scan
make source-dashboard        # the gen_audit rollups are matviews -- see 6.5
cd ../services/api && .venv/bin/pytest -q
```

`make source-dashboard` is not optional and is the easiest to forget: `mv_daily` and
`mv_bills` are MATERIALIZED VIEWS, so they keep serving the previous span indefinitely and
the dashboard looks fine while showing data that no longer exists.

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

**Four dashboards, each answering a different question**, all built from committed
scripts in `../analysis/metabase/` and rebuildable from them:

| Dashboard | Reads | What it is for |
|---|---|---|
| Sample data — the nine fixtures | `kea.mart_*` | The hand-built scenarios, driven by one ICP filter |
| Stage 2 — generated source data | `src_*`, via rollups in `gen_audit` | The 500-household cohort: 20.3M reads, 12,279 invoices. ⚠️ The `gen_audit` rollups are MATERIALIZED VIEWS and do not follow their base tables — `make source-dashboard` after every regeneration, or the dashboard shows the previous span |
| Stage 3 — extract, bronze, parsed | `lake_audit` | The **pipeline's own behaviour**, not the meter data |
| Stage 5 — export, load, and what crossed | `billing.*`, `metering.*`, `kea.mart_*` | The serving store against its source: what crossed, what did not, and the nine fixtures reproduced |

⚠️ **There is deliberately no Stage 4 dashboard yet.** The warehouse's marts live in
**Snowflake**, and Metabase is connected to the serving **Postgres** — which is the design's
intent, not an oversight: §7 has Stage 5 export the marts to Postgres, and that is when they
become visible through the connection Metabase already has. Wiring a second Metabase
connection to Snowflake now would build something Stage 5 makes redundant, and would mean
two dashboards over the same numbers to keep in agreement. The Stage 3 dashboard was
refreshed instead and still passes on the grown lake — 757 load dates against the 31 it was
built on. Stage 5's dashboard, added when the export landed, is the one that reads the marts.

`make verify-dashboards` checks every card on all four: that it runs, returns rows, and
**uses every column it selects**. That last clause is not padding. A query returning
`hour, month, kwh` under a viz declaring only `graph.dimensions: ["hour"]` renders as one
averaged curve, Metabase drops the breakout silently, and the panel looks fine — five
panels were wrong that way before the check existed.

⚠️ **`lake_audit` is analysis, not pipeline output**, and the schema name is chosen to say
so. Metabase talks JDBC and the Stage 3 lake is Parquet on MinIO, so a dashboard over it
needs the rollups landed somewhere Metabase reaches; `analysis/metabase/summarise_lake.py`
rebuilds them whole from the lake on each run. **Stage 5 is what lands real pipeline output
in the serving store** — per-table strategy, FK-ordered dependencies, `synced_at` at commit
time. Putting these rollups in `kea` would make them look like marts. The same reasoning
keeps `gen_audit` out of `src_*`.

**Weather is the one thing Stage 3 does plot as data rather than as pipeline behaviour**,
because nothing else anywhere plots it: the source arrived after the Stage 2 dashboard was
built. A wrong-hemisphere series would have survived every existing check. It does not —
February is the warmest month at 18.6 °C and July–August the coldest at 9.2 °C, and heating
degree days run Christchurch 3,049 → Wellington 1,903 → Waikato 1,772 → Auckland 939, the
south-to-north gradient the station-to-region mapping predicts. Auckland's mean of 16.5 °C
sits above the base-16 threshold, which is why it barely accumulates any.

⚠️ **The daily version of that chart was silently truncated**, and it is worth recording
because the failure is invisible: Metabase caps a card at 2,000 rows, the daily series is
2,791, and the panel returned exactly 2,000 and stopped mid-2026 looking complete. It is a
weekly mean now — ~404 rows, with room as the span grows. `make verify-dashboards` reports
the row count per card, which is the only reason it was caught.

Otherwise the Stage 3 dashboard deliberately does **not** re-plot the meter data; that is
Stage 2's, read from the source. What Stage 3 newly makes visible is arrival lag — and it turns
design §4.1's argument into a measurement: of 734,127 rows, **every one of the 16,286
arriving eight or more days late is a correction**, and 10,139 of them land 22 to 46 days
out. An extract watermarked on reading time sees none of them, and reports nothing wrong.

**The analysis itself has not started.** What exists is instrumentation and schema
exploration, not the questions the fixtures were built to answer: whether `base_load_kwh`
really separates a fault from a new appliance, whether the weather decomposition holds
against the cold-snap fixture, whether the forecast band narrows for the reason the design
claims. That work is next, it happens in Metabase, and anything it concludes lands in
`../analysis/` as a committed query.

**When Snowflake joins**, two habits are worth starting with rather than retrofitting:
point questions at `mart_*` rather than raw tables, and turn on Metabase's query caching
before any dashboard auto-refreshes — each refresh is a warehouse query, and a dashboard
left open will keep a warehouse awake that would otherwise auto-suspend.
