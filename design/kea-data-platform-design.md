# Kea Energy — Data Platform Design

**Owner:** Harvey Nan (Data Engineer)
**Scope:** four source databases → S3 → Snowflake/dbt → RDS PostgreSQL serving store → the agent

**DDL files, in run order:**

| # | File | Schemas | Written by | Regenerable? |
|---|---|---|---|---|
| 1 | `kea_source_schema.sql` | `src_billing`, `src_metering`, `src_catalogue` | Kea's existing systems | n/a — read only |
| 2 | `kea_serving_schema.sql` | `common`, `metering`, `billing`, `catalogue`, `kea` | the pipeline (`kea_loader`) | **yes** — re-run from bronze |
| — | `kea_deferred_schema.sql` | Sprint 3–4 additions | mixed | — |

**`app` is not here.** The `app` schema — conversation history, written by the agent (`kea_app`) at runtime — is owned by the application and defined in the application's repo, not this one. It is out of scope for the pipeline. It still appears throughout this document wherever the pipeline's boundary depends on it.

Column-level detail lives in those files as comments and is not repeated here. This document covers what SQL comments structurally cannot: how the pieces relate, and why the non-obvious choices were made.

> **Catalogue note, 8 Sep 2026:** historical sections below may still describe
> the earlier 64-plan Contact-derived run. The active compact Sprint 3
> catalogue is six version rows for three Kea plans on Vector / Standard User;
> see `docs/sprints/sprint-3/plans-and-terms.md` and `data-plan.md`.

**Companion:** `kea-data-engineering-stack.md` (platform and tooling decisions).

---

## 1. What this platform is for

From the brief:

> "Kea's billing system, half-hourly meter-data feed, and plan catalogue all exist as separate systems with no intelligent layer connecting them."

This pipeline **is** that connecting layer. It does not replace any source system. Its job is to extract from four independent databases, reconcile them into one coherent model, and land a serving copy the agent can query in under 500 ms.

**Non-goals:** writing back to any source system; being the system of record for billing, metering or tariff data.

### 1.1 Why the files are split by writer

Not by subject — by who writes the data. That is what makes the *Regenerable?* column answerable: anything the pipeline owns can be rebuilt from bronze, and anything it does not own cannot. Conversation history is the clearest case, and nothing recovers a lost one — so `kea_loader` has no grant on `app`, not even `USAGE`, and a pipeline bug cannot reach it even by fully-qualified name.

**The agent computes nothing.** It reads pre-computed marts and writes conversation turns. Bill-increase attribution, plan re-rating, forecasting and aggregation all happen in the warehouse, where they are version-controlled, testable, and identical for every customer.



---

### 1.2 What this project is building — and what it is not

From the brief:

> "Kea's billing system, half-hourly meter-data feed, and plan catalogue all exist as separate systems with no intelligent layer connecting them."

**This project builds the intelligent layer. It does not build a billing system.** Most of file 2 is therefore a *read model* — a projection of data mastered elsewhere — not a system of record.

| Namespace | System of record | `kea_app` | `kea_loader` |
|---|---|---|---|
| `common` | shared enum types | — | — |
| `metering` | the half-hourly meter-data feed | SELECT | write |
| `billing` | the billing system | SELECT | write |
| `catalogue` | the plan catalogue | SELECT | write |
| `kea` | derived marts and forecasts | SELECT | write |
| `app` | conversation history | **write** | **no access** |

Physically all six live in one RDS database. The namespace records where each table's data is **mastered**, not a remote connection.

`billing.account` is the join across all three source systems — customer, connection, plan. **That join is the layer the brief says is missing**, and `kea.mart_account_context` is it made concrete: one row the agent reads in a single lookup.

#### Why a rating engine exists at all

A fair question, given billing already rates bills. Because billing rates only the plan the customer is **on**, after the fact. Two things it will never do:

- **Counterfactual rating** — what would plan X have cost? → plan comparison (Sprint 3)
- **Forward rating** — what will this cycle land at? → bill-shock warning (Sprint 2)

The engine is not a reimplementation of billing; it is the part billing cannot do. That makes the reconciliation targets in §4 more than test fixtures: they prove our engine agrees with Kea's on the one case both can compute, before we trust it on the cases only ours can.

#### Not in the serving database, deliberately

- **`weather_daily`** — warehouse-only. NIWA data is an *input* to the marts; the figures the agent needs are already denormalised into `mart_daily_usage` and `mart_bill_explanation`. Exporting it would add a table nobody queries.
- **Materialized views** — an earlier draft computed `mv_daily_usage` and `mv_hourly_profile` inside Postgres. Replaced by `mart_daily_usage` / `mart_hourly_profile`, loaded from the warehouse. Postgres holds only a 13-month subset of meter reads, so anything computed locally could see only that subset; the warehouse sees all 2.45B rows.

---

## 2. Source systems

Three separate databases. Each is a distinct extract, with its own connection, credentials, schedule and failure mode.

Modelled in `kea_source_schema.sql` as `src_billing`, `src_metering`, `src_catalogue`.

**Source names differ from serving names, deliberately.** The billing system calls them `invoices` / `invoice_lines`; the serving layer calls them `billing.bill` / `billing.bill_line`. The catalogue calls them `price_plans` / `tou_windows` / `rate_components`; serving calls them `catalogue.plan` / `tou_band` / `tariff_component`. Three systems built by different teams at different times is the realistic case, and reconciling the naming is the staging layer's job — see §8.3 for the full mapping. In production these are three databases; there they are three schemas so the project can be developed without three servers — but the pipeline must still treat them as separate, with three connections and no cross-schema joins at extract time.

| Source | Contains | Volume (140k customers) | Change rate |
|---|---|---|---|
| **Billing DB** | `customers`, `accounts`, `invoices`, `invoice_lines` | ~1.7M invoices/yr, ~12M lines/yr | Daily, append-heavy |
| **Meter-data feed** | `networks`, `connection_points`, `meters`, `registers`, `interval_reads` | **2.45B rows/yr** | Continuous append; revisions re-issued with a higher `version_no` |
| **Plan catalogue** | `price_plans`, `tou_windows`, `rate_components` | Hundreds of rows | Rare, but versioned |

### 2.1 Cross-system references

**The association is already recorded — the source systems store it.** When billing creates an account it calls the metering system's API to validate the connection, and stores the **ICP number** (`0160647002LCFCF`). Same for the plan code. So the warehouse is not *discovering* relationships; they arrive with the data.

| Relationship | Joined on |
|---|---|
| account → connection | `ICP_NUMBER` (text) |
| account → plan | `PLAN_CODE` + effective date |
| meter read → register | `icp_identifier` + `register_code` |

**Business keys only — no denormalised snapshots.** Billing stores the identifier, not a copy of the address or the plan's rates. A stored copy would go stale the moment the owning system changed, and the warehouse would then have to decide which version is authoritative — a bug class that is hard to spot because both values look plausible. One key means one source of truth per fact.

**Key conforming is mechanical.** Mapping `'0160647002LCFCF'` → `icp_id = 4471` via an append-only key map is bookkeeping, not cleverness. It is worth doing for one concrete reason: `meter_read_hh` keys on `register_id`, and a register has **no globally unique business key** — `register_code` is unique only within a meter. The natural key would be the composite `(icp_number, register_code, interval_start)`, putting a text column in the primary key of a 2.45-billion-row table. Surrogates buy real index size and scan cost there.

What the source systems storing the keys does **not** solve is the following two problems, which are inherent to separate systems.

### 2.1.1 Orphan references

No foreign key is possible across databases, so nothing guarantees that a stored `ICP_NUMBER` exists in metering. A typo, a deletion, or extracts taken an hour apart all produce a reference that will not resolve.

Handled in §6.3. The mitigating property is that orphans **fail loudly** — resolution simply returns nothing, and the account quarantines.

### 2.1.2 Temporal resolution — the dangerous one

`src_billing.invoices` stores `PLAN_CODE` only. But `src_catalogue.price_plans` is `UNIQUE (code, valid_from)`: one code has **many versions**, because a tariff change inserts a new row rather than overwriting.

So resolving a bill to its plan is not a lookup by code. It is a lookup by code *as at the bill's period end*:

```sql
plan_code  = invoice.PLAN_CODE
AND valid_from <= invoice.PERIOD_END
AND (valid_to IS NULL OR valid_to > invoice.PERIOD_END)
```

**Get this wrong and a historical bill is re-rated against today's tariff.** The bill still balances, the arithmetic is internally consistent, and the number is plausible — it is simply wrong. Unlike an orphan, this fails silently, which makes it the more dangerous of the two.

It is also not hypothetical: plan comparison re-rates twelve months of usage across the catalogue, and rates change within twelve months.

**Mitigations:**

- `dim_plan` is type-2 versioned in the warehouse (§6.4), so the correct version is always available
- `billing.bill.plan_id` in the serving store points at the **specific plan version rated**, not at the account's current plan
- A dbt test asserts that every bill's resolved plan version brackets its period end
- The reconciliation target (§10.1) catches it: reproducing $50.54 is only possible with the correct tariff versions — and on the full invoice history a code-only lookup mis-rates **2,878 of 11,534** bills, 25%

### 2.2 Assumptions, flagged as such

The brief describes these systems but not their interfaces. The following are **team assumptions**, to be confirmed via `KEA-501`:

| Assumption | Rationale | If wrong |
|---|---|---|
| Extract is by **read replica / snapshot**, not CDC | Simplest that works; no evidence either way | CDC would replace §4's snapshot logic with change-stream consumption |
| Meter feed delivers in **scheduled batches** | NZ EIEP file-exchange convention (see stack doc §1.2) | Streaming delivery would make the Sprint 5 Flink path production-relevant rather than demonstrative |
| Cross-system referential integrity is **not guaranteed** | Separate databases cannot enforce it | If guaranteed, §6.3's orphan handling becomes dead code — harmless |
| Meter reads may be **revised and re-issued** | Common in retail metering | If append-only, reprocessing simplifies |

The third assumption is the one to design for regardless: separate databases *cannot* enforce referential integrity between them, so the pipeline must handle orphans whether or not they are expected.

---

## 3. Architecture

```
  Billing DB        Meter feed DB      Plan catalogue DB
      │                   │                    │
      └───────────────────┼────────────────────┘
                          │  EXTRACT — 3 independent jobs, own schedules
                          ▼
              S3 bronze/  (raw, immutable, partitioned by source + load date)
                 ├── billing/
                 ├── metering/
                 └── catalogue/
                          │
                          │  parse / reshape — Spark ONLY for meter reads,
                          │  plain scripts for billing and catalogue (§5.1)
                          ▼
              S3 bronze/parsed/  (Parquet, conforming to the Avro schema)
                          │
                          │  external stage
                          ▼
     ┌──────────────── Snowflake + dbt ─────────────────┐
     │  staging   stg_<source>__<entity>   1:1, typed   │
     │  silver    validation, quarantine, key resolution│
     │  gold      dim_* / fct_*  conformed star schema  │
     │  marts     mart_*  purpose-built per consumer    │
     └───────────────────────┬──────────────────────────┘
                             │  COPY INTO → S3 parquet
                             ▼
                       S3 export/
                             │  loader (Airflow task)
                             ▼
     ┌─────────────── RDS PostgreSQL ────────────────────┐
     │  metering / billing / catalogue   mirrored, RO    │
     │  kea                              owned, RW       │
     └───────────────────────┬───────────────────────────┘
                             ▼
                   Agent tools / API
```

**Governing rule, carried from the stack doc:** SQL in Snowflake under dbt by default. Spark handles only two named exceptions — raw parsing/reshaping, and the rating engine.

---

## 4. Extraction

Four independent jobs. Independence is the point: a billing extract failure must not block metering.

### 4.1 Per-source strategy

| Source table | Method | Watermark | Frequency |
|---|---|---|---|
| `src_catalogue.*` | Full snapshot | none | Daily |
| `src_billing.customers`, `.accounts` | Full snapshot | none | Daily |
| `src_billing.invoices`, `.invoice_lines` | Incremental | `ISSUE_DT` | Daily |
| `src_metering.networks`, `.connection_points`, `.meters`, `.registers` | Full snapshot | none | Daily |
| `src_metering.interval_reads` | Incremental | **`received_at`** — not `interval_start` | Daily |
| `src_weather.stations`, `.daily_observations` | Full snapshot | none | **Monthly** |

**Small tables get full snapshots.** Under ~1M rows, a snapshot is simpler, self-correcting, and removes an entire class of missed-delete bugs. Plans, ICPs and registers all qualify.

⚠️ **A snapshot has no window, so "re-running a load date changes nothing"
needs a rule.** `interval_reads` can prove it: its window is a pure function of
the load date, so the same predicate always selects the same rows. A snapshot
takes whatever the table holds *at the moment it runs* — re-running
`load_date=2026-08-25` tomorrow captures tomorrow's table, the replay
legitimately differs, and the partition stops being a record of what the source
held that day.

**The rule: a sealed snapshot partition is immutable.** Once `_SUCCESS` is
written, a re-run is a no-op unless `--force` is passed — a decision someone
takes rather than a default. The exit condition then means the same thing for
all seventeen entities instead of for the one that was hardest.

**`invoice_lines` is a snapshot, not an incremental read**, which the table
above states the other way round. It has no timestamp column at all, so
`ISSUE_DT` can only be reached by joining to `invoices` — at which point the
extract is no longer a single-table read and a bug in the join silently drops
billing lines. At 101,284 rows the paragraph above applies. **In production it
does not**: ~12M lines/yr is over the threshold, and there the join, or a real
timestamp on the table, has to be faced.

**`invoices` stays incremental, and its watermark is a different shape.**
`ISSUE_DT` is a naive `timestamp`, not a `timestamptz`, so the window cannot be
expressed as an instant. It is NZ **wall clock** — the same calendar day the
metering windows cover, in the only terms that column has.

**The meter-read watermark is `received_at`, not `interval_start`.** This is the one extract decision most likely to be got wrong, and the failure is silent.

Meter reads are revised: a correction to a reading from three weeks ago arrives today, as a new row with a higher `version_no`. Watermarking on `interval_start` — the period the reading describes — would never pick that row up, because its `interval_start` is older than the watermark. The revision is simply lost, and the customer's bill is explained from superseded data.

Watermarking on `received_at` — when the row landed in the source — catches every revision naturally, whatever period it corrects. **No lookback window is needed.** An earlier draft specified a 7-day trailing re-extract; that was a workaround for the wrong watermark, and it both missed revisions older than 7 days and re-read a week of data every night for nothing.

Silver then deduplicates on `(icp_identifier, register_code, interval_start)`, keeping the highest `version_no`.

### 4.2 Landing in bronze

```
s3://kea-lake/bronze/{source}/{entity}/load_date=YYYY-MM-DD/part-*.parquet
```

Bronze is **immutable and as-received**. No cleaning, no type coercion, no renaming. It exists so any downstream logic can be re-run against exactly what arrived, and so a bug in silver never requires re-extracting from a production source system.

Each file carries `_extracted_at`, `_source_system`, and `_source_file` columns.

**`load_date` is a New Zealand calendar day.** Every date in this platform is
derived through `Pacific/Auckland`, and the load date is no exception — but it
is worth stating rather than assuming, because the failure is silent and was
measured. The planted late revision arrives at `2026-08-24 21:15 UTC`, which is
`2026-08-25 09:15` in Auckland:

| window for `load_date=2026-08-25` | rows | the 36-day-late revision |
|---|---:|---:|
| UTC day | 26,623 | **0** |
| NZ day | 26,925 | **48** |

A UTC window still returns 26,623 perfectly reasonable rows, so nothing looks
wrong. The extract window is therefore `[midnight D, midnight D+1)` in
`Pacific/Auckland`, half-open so consecutive dates partition the timeline
exactly, and **derived from the load date alone** — never from the clock. 0.06%
of source rows carry a `received_at` in the future (a revision lands 3–45 days
after the reading it corrects), so any `AND received_at <= now()` upper bound
makes a replay return more rows as time passes, which reads like data arriving
rather than like a bug. There is no stored watermark for the same reason: it
would make the output a function of run history and break `--start-date`
backfill.

A New Zealand day is 23, 24 or 25 hours, and the extract's partitioning follows
it — 24 hourly `received_at` ranges on an ordinary day, 23 or 25 across a DST
transition.

**Two naming rules the layout depends on.**

`parsed` is a **reserved source name.** Raw keeps the path above, quoted in
the Lake step's exit criteria, so parsed nests under it as `bronze/parsed/{source}/…`
rather than the two sitting symmetrically under `bronze/raw/` and
`bronze/parsed/`. The cost is that a source called `parsed` would
collide silently, so the path builder refuses the name.

`_source_file` is pipeline provenance; `source_file` is the retailer's delivery
filename, and `interval_reads` already has one. Both land in the same Parquet
file and the underscore is the only thing distinguishing them: **underscore
means pipeline metadata, bare means as-received data.**

**Parquet timestamps are `TIMESTAMP_MICROS`, set explicitly.** Spark 3.5 still
defaults `spark.sql.parquet.outputTimestampType` to `INT96` — the Impala-era
encoding, which carries no logical type and no `isAdjustedToUTC` flag, so a
reader gets a zoneless nanosecond timestamp and has to guess. Snowflake
external tables read it poorly, and the Avro contract says `timestamp-micros`.

---

## 5. Parsing: bronze raw → bronze parsed

One step, one job: reshape what the extract dropped into the conformed schema, and write Parquet that Snowflake can read as typed columns. No business logic — validation, quarantine and key resolution all happen in dbt.

Reshaping covers the mechanical differences between three systems built by different teams:

| From the source | To the conformed schema |
|---|---|
| `read_status 'A'` / `'E'` / `'F'` | `quality 'actual'` / `'estimated'` / `'final'` |
| `MED_DEPENDENT 'Y'` / `'N'` | `is_medically_dependent` boolean |
| `days_csv '1,2,3,4,5'` | `days_of_week smallint[]` |
| `CHG_GROUP 'NET'` / `'ENE'` / `'OTH'` / `'CRD'` | `charge_group 'network'` / `'energy'` / `'other'` / `'credit'` |
| `timestamp` (billing) vs `timestamptz` (metering) | `timestamptz`, UTC |

⚠️ **`read_status = 'F'` has no target in the serving enum.** The source
produces it — 1,010 rows, from accounts closing — and `common.read_quality` is
`('actual','estimated','validated','missing')`, with no `final`. The Avro
contract carries the third symbol, because mapping `'F'` onto `'actual'` would
be parse *deciding* that a final read is an ordinary one, which is business
logic and not reshaping. Resolving the clash belongs to the Serving loader; it is
in the open items.

**Naive billing timestamps are read as `Pacific/Auckland`,** not UTC. The
billing system is a New Zealand retailer's and the dates it stores are the ones
printed on the bill. Reading them as UTC would move every issue date by up to
13 hours — enough to shift a period end across a plan-version boundary and rate
the bill on the wrong tariff, which is §2.1.2's silent failure.

### 5.1 Which extracts actually need Spark

Not all of them. Sizing the tool to the job rather than using one engine everywhere:

| Extract | Volume | Tool | Why |
|---|---|---|---|
| `src_catalogue` | hundreds of rows | **plain JDBC → Parquet** | Spark would spend longer starting up than reading |
| `src_billing` | ~1.7M invoices, ~12M lines/yr | **plain JDBC → Parquet** | Comfortably single-node. Chunk by `ISSUE_DT` if memory is tight |
| `src_metering.connection_points`, `registers`, `networks` | thousands | **plain JDBC → Parquet** | Small reference data |
| `src_metering.interval_reads` | **2.45B rows/yr** | **Spark on EMR Serverless** | A single-threaded JDBC read would not finish in a batch window. Partitioned parallel read is the only practical option |

**So Spark runs one job in this pipeline, not three.** Everything else is a script.

That is worth stating plainly because the opposite is the common failure: standing up a distributed engine for every extract because one of them needs it, and paying the startup cost and operational surface on jobs that would run in seconds locally. One tool for all three would be simpler to template, but the marginal simplicity is not worth the marginal cost here.

**Partitioning the meter-read extract.** Split by `received_at` date range rather than by row id — it aligns with the incremental watermark, keeps partitions evenly sized as history grows, and makes a failed partition re-runnable on its own.

In practice that is `spark.read.jdbc(predicates=[...])`, one literal
`received_at` sub-range per predicate — **not**
`partitionColumn`/`lowerBound`/`upperBound`, which slices a numeric range into
equal parts, cannot express "by date range", and needs a min/max probe that
moves as history grows. One predicate is one Spark partition, one JDBC
connection, one index range scan on `ix_reads_received`, and one independently
re-runnable unit.

**Spark owns the metering lane end to end — the extract *and* the parse.** The
table above assigns the `interval_reads` extract to Spark; the sentence above
it says Spark runs "one job, not three". Both are true and they count different
things: **one SOURCE, two steps.** `extract_interval_reads.py` and
`parse_interval_reads.py` are two entry points sharing one module, and
`spark-parse` is singular because there is no bronze/silver/gold chain here,
not because the lane has one step. The other sixteen entities are scripts, as
the table says.

Note this means the Spark image needs a PostgreSQL JDBC driver as well as the
S3A jars. It shipped with only the latter, which is why the partitioned read
had nothing to point at until the Lake was built.

### 5.2 Where Spark's other workloads live

The stack doc names two exceptions to the SQL-in-Snowflake rule. Only the first appears in this pipeline:

| Exception | In this pipeline? |
|---|---|
| Raw parsing / reshaping | **Yes** — the meter-read extract above |
| The rating engine | No. Domain logic, run separately; its *output* lands in bills |

### 5.3 Schema contract vs file format — two separate choices

These are independent, and conflating them is easy:

| | Choice | Why |
|---|---|---|
| **Schema contract** | **Avro schema** (`KEA-047`) | Field names, types, nullability. Defines what a meter reading *is* |
| **File format on S3** | **Parquet** | How those rows are stored for analytical reading |
| **Wire format on Kafka** | **Avro binary** (Sprint 5) | How a single row is encoded in a message |

**One schema, two serialisations.** The batch path writes Parquet files that conform to the Avro schema. The Sprint 5 replay producer encodes the same schema as Avro binary for Kafka, because Schema Registry and per-message compactness require it. Downstream, neither can tell which path a row arrived by — which is the property that makes the ingestion paths interchangeable.

**One schema is hand-written; sixteen are derived.** `KEA-047` is written by hand because it
is the one carrying an argument — its `quality` enum encodes the decision that `'F'` means
`final` and not `actual`, and it is the schema Sprint 5 re-serialises onto a Kafka topic. The
other sixteen entities carry no such argument: they are the source tables' shapes, typed. They
are generated from `information_schema` (`gen_avsc.py`, run by `make ingest-date REGEN=1`), so the Postgres→Avro mapping is stated
once in a reviewable place instead of sixteen times by hand, and `--check` fails when a
committed contract has drifted from the table it claims to describe. The generator refuses an
unmapped type rather than guessing, which is how `time without time zone` got a deliberate
answer: a TOU window runs from a time of *day* that recurs, not an instant, so flattening it
to a timestamp would invent a date and a zone the source does not have.

⚠️ **A derived contract must carry §5's reshapes as overrides, or it certifies nothing.**
Taken straight from `information_schema`, the contract would describe the **source** — and a
parse job that did nothing whatsoever would then satisfy its own conformance check.
`days_csv` would stay a string instead of becoming `days_of_week`; `charge_scope` would never
become `charge_group`. The four reshapes are therefore applied to the generated schema, so the
contract describes what parse is supposed to *produce*.

**Why Parquet rather than Avro files on S3.** Everything that reads bronze reads it analytically — Snowflake external tables scanning a subset of columns across many rows. Parquet is columnar, so it prunes unread columns and pushes predicates down; Avro is row-oriented and would force a full read of every row to get one field. At 2.45B rows that difference is not marginal. Avro's advantages — compact single-row encoding, embedded schema per message — matter on a Kafka topic, not in an analytical scan.

**Conformance is checked against the Parquet footer, not the rows.** Names, order, types and
nullability are all decidable from the `.avsc` and the file's own metadata with no rows read
at all, which is what makes the check cheap enough to run on every partition
(`kea_pipeline/avro_schema.py`). That is the layer catching the failure that costs money — a
column Snowflake reads as the wrong type — and it caught one: Spark 3.5 writes `INT96`
timestamps by default. Value-level checks, a `quality` outside its symbol list say, need
fastavro and actual rows, and belong with the parse job instead.

**The contract outlives the parse job.** The same file gives parse its Spark `StructType`,
the bronze gate its expected shape, the Snowflake loader its `CREATE TABLE`
(`to_snowflake_ddl`) and the pyarrow path its types (`to_arrow`). The warehouse use is not
incidental: letting `write_pandas(auto_create_table=True)` infer types instead meant the
**first load date defined the schema**, and invoices' `decimal(12,2)` amounts became
`NUMBER(4,2)` because that one day happened to hold only two-digit values.

**The pipeline metadata is a second schema, deliberately.** Every parsed dataset is its
entity's schema followed by exactly the `_pipeline_metadata` block, and the conformance check
enforces that composition rather than a reviewer remembering it. Keeping the block separate is
what lets `KEA-047` go onto a Kafka topic unchanged, where a load date and an S3 key mean
nothing.

---

## 6. Warehouse (Snowflake + dbt)

### 6.1 Layer conventions

| Layer | Naming | Materialisation | Rule |
|---|---|---|---|
| staging | `stg_<source>__<entity>` | view | 1:1 with source. Rename, cast, no business logic, no joins |
| silver | `int_<domain>__<purpose>` | ephemeral / table | Validation, quarantine routing, **key resolution** |
| gold | `dim_<entity>`, `fct_<event>` | incremental table | Conformed star schema, source-agnostic |
| marts | `mart_<consumer>` | table | Purpose-built for one consumer; denormalised |

The double underscore in `stg_billing__bill` separates source from entity. It survives the whole lineage graph, so provenance is readable at a glance rather than lost at the first join.

### 6.2 Key resolution

Mechanical, but with two rules that are easy to break.

**Rule 1 — surrogate keys must be stable across runs.** If they are regenerated each load, every FK in Postgres breaks and anything that stored a reference becomes wrong.

Implementation: a persistent key-map table per entity, appended to but never rewritten.

```
key_map_icp        (icp_number    TEXT PK, icp_id     BIGINT, first_seen)
key_map_customer   (customer_number TEXT PK, customer_id BIGINT, first_seen)
key_map_plan       (plan_code, effective_from PK, plan_id INTEGER, first_seen)
```

New business keys get the next id; existing keys always resolve to the same one. dbt snapshots can serve this role, or a plain incremental model with an identity column — either is fine, but **it must be append-only**.

**Rule 2 — plan resolution is temporal, not a code lookup.** Note that `key_map_plan` is keyed on `(plan_code, effective_from)`, not on `plan_code` alone. One code has many versions. Resolving a bill means finding the version whose validity brackets the bill's period end, per §2.1.2. Resolving by code alone re-rates history against current tariffs, and fails silently.

Gold and marts carry only surrogate keys. Business keys are retained as attributes (they appear on the bill, and customers quote them), but never used for joining downstream.

### 6.3 Cross-system integrity

Separate databases cannot enforce referential integrity between them. Four failure classes, all real:

| Class | Example | Handling |
|---|---|---|
| **Orphan reference** | `accounts.ICP_NUMBER` has no match in the metering extract | Quarantine the account. **The agent must not answer for it** — an answer would be built on a connection we cannot see. Fails loudly, which is the good case |
| **Timing skew** | Billing extracted at 02:00, metering at 03:00; an account created between them | Retry at the next metering extract; only quarantine after N consecutive extracts miss it (default 3). Counted per load date, not per run, so rebuilding over the same extract never counts again |
| **Unknown plan code** | `accounts.PLAN_CODE` not in the catalogue extract | Quarantine. Cannot rate without the tariff, and a bill explanation without rating is worthless |
| **No plan version covers the period** | Code exists, but no row's `valid_from`/`valid_to` brackets the bill's period end | Quarantine the bill. **Do not fall back to the nearest version** — that is the silent-failure mode in §2.1.2 |

All four land in **one quarantine table with one `_reject_reason` vocabulary** — the same table that receives Spark parse failures and dbt validation rejections. Three rejection paths, one audit trail. Fragmenting it across systems is exactly what makes a data issue undiagnosable at 9am on demo day.

**Quarantine rates surface to the product.** If a household has 4% estimated reads, or its account is quarantined, the agent hedges or declines rather than answering confidently. This is the link most pipelines never make.

⚠️ **AND IT IS NOT MADE HERE EITHER — measured 4 Sep 2026, by the warehouse quality gate.** The warehouse
mart holds all five planted defects with their reasons. `kea.mart_account_context` in the
serving store holds **none**: `is_quarantined` is false on all 504 rows and
`quarantine_reason` is null throughout, so the columns the agent would gate on are dead.

Not an export bug. **A quarantined account is by definition one the serving schema cannot
represent.** `billing.account` requires `icp_id` and `plan_id` NOT NULL; the three orphan ICPs
have no connection point at all, and the two `ECO_GONE_VECT_STA` accounts sit on a plan code
with zero versions in `dim_plan`. The export drops them because there is nowhere to put them,
and the alternative — a placeholder ICP or plan — would be inventing the very reference whose
absence is the defect.

Closing it is a schema decision, not wiring: a nullable FK on `billing.account`, or a separate
quarantine table in the serving store that does not pretend a quarantined account is a normal
one. Recorded here rather than resolved by loosening a constraint, because the constraint is
what makes every *other* row trustworthy.

### 6.4 Gold model

```
dim_customer   dim_icp   dim_register   dim_plan   dim_date
     │            │            │            │          │
     └────────────┴──────┬─────┴────────────┴──────────┘
                         │
            fct_meter_read      fct_bill_line
            (grain: icp ×       (grain: bill ×
             register ×          line)
             interval)
```

⚠️ **`dim_register` was missing from this diagram and is not optional.** The fact's own
grain line names `register`, and `metering.meter_read_hh`'s primary key is
`(icp_id, register_id, interval_start)` — so a register dimension exists whether or not it
was drawn. Added when the Warehouse step built the star and had nothing for `register_id` to point at.

It carries only **unambiguous** registers, and that is what makes joining to
`src_metering.registers` safe at all. A read carries no `meter_id` (KEA-047), so resolving
one goes icp → meters → registers on the code — and where an ICP has had more than one
meter that returns several. **Filter to meters live at the reading's instant first; if more
than one still survives, quarantine rather than pick.** Picking is not a small inaccuracy:
it is a fan-out that multiplies every reading at that ICP, measured at +750 rows and
+447.05 kWh on one household, with no error and a grand total that still looks reasonable.
`05_defects.sql` plants exactly this case and says "Stage 4 needs the rule".


`dim_date` carries the NZ holiday calendar and heating degree days joined from NIWA, so the explainer's weather comparison needs no join at query time. The NIWA series, the four stations chosen and the station-is-not-a-region assumption are recorded in `data/dictionary/niwa-weather-source.md`.

### 6.5 Marts

| Mart | Grain | Consumer |
|---|---|---|
| `mart_account_context` | account | Postgres `kea.mart_account_context` |
| `mart_daily_usage` | icp × day | Postgres `kea.mart_daily_usage` |
| `mart_hourly_profile` | icp × month × hour × weekday | Postgres `kea.mart_hourly_profile` |
| `mart_bill_explanation` | bill | Postgres `kea.mart_bill_explanation` — drivers pre-decomposed in attribution order |
| `mart_forecast_input` | account × cycle × day | Postgres `kea.mart_forecast_input` — one snapshot per day, so a past forecast can be replayed |
| `mart_scale_test` | icp × interval | The 2.45B-row re-rate. **Never exported** — Snowflake-only. ⚠️ **Deferred, 3 Sep 2026** — see below |

⚠️ **`mart_scale_test` is deferred, and the reason is arithmetic rather than effort.**
The 2.45B figure is Kea's **production** projection for their real customer base. The demo
cohort is 500 households: the source holds 19.1M reads and the warehouse 9.7M, so running
this mart as specified means synthesising roughly **253× the data** before re-rating any of
it. Doing that on a fixed trial budget would spend it demonstrating a property of Snowflake
rather than of this pipeline — and this is the one mart that is **never exported**, so
nothing downstream waits on it.

It is deferred to a funded environment, or to a run at a volume that environment can afford
with the extrapolation stated. What is not acceptable is quietly reinterpreting 2.45B as
whatever happened to be loaded and calling the box ticked.

Plus one table the pipeline writes about itself: **`kea.mart_load_log`**, one row per mart per load, carrying `started_at` / `completed_at` / `row_count` / `warehouse_run_id` / `status`. It makes "how fresh is this, and did it succeed" answerable without inspecting the data, and traces a serving row back through dbt to the bronze extract that produced it. The agent can check staleness before answering; ops can diagnose a partial load.

---

## 7. Export to PostgreSQL

Snowflake cannot write to Postgres directly. Two hops:

```
Snowflake  --SELECT, pulled-->  s3://kea-lake/export/{table}/  --loader-->  Postgres
```

⚠️ **This said `COPY INTO` until Serving was built, and `COPY INTO` cannot work
here.** Snowflake is SaaS in `ap-southeast-2`; MinIO runs on the developer's
machine with no public endpoint, so Snowflake can no more write to the lake than
it could read it as an external stage on the way up — the same constraint that
makes `load_bronze_to_snowflake.py` push with `write_pandas` instead of letting
Snowflake pull. Serving therefore **pulls**: a local process runs the export
query, receives Arrow batches, and writes the Parquet itself.

The two hops are unchanged, and so is everything below about strategy, order and
`synced_at`. Only the arrow's direction was wrong. `pipeline/export/*.sql` holds
one query per target table, and `export_warehouse_to_lake.py` is the transport.

### 7.1 Shape decision

The Postgres namespaces are **source-shaped**, not query-shaped — deliberately. The rating engine needs exact tariff structure; denormalising `plan → tou_band → tariff_component` would destroy the ability to re-rate. Denormalisation happens only in `kea`.

| Namespace | Shape | Source | File |
|---|---|---|---|
| `metering`, `billing`, `catalogue` | faithful replica | gold, unflattened | 2 |
| `kea` | denormalised, pre-joined | marts | 2 |
| `app` | runtime writes | not loaded | — (application repo) |

`kea.mart_daily_usage` and `kea.mart_hourly_profile` are **plain tables loaded from the warehouse**, not materialized views computed in Postgres. Postgres holds only a 13-month subset of meter reads for the demo cohort, so anything computed locally could see only that subset; the warehouse sees all 2.45B rows.

### 7.2 Per-table strategy

| Target | Volume | Strategy |
|---|---|---|
| `catalogue.*` | hundreds | Truncate + reload, one transaction |
| `metering.network`, `property`, `icp`, `meter_register` | thousands | Truncate + reload |
| `billing.customer`, `account` | 140k | Truncate + reload |
| `billing.bill`, `bill_line` | incremental | DELETE by period → INSERT |
| `metering.meter_read_hh` | **subset** | Partition swap per month |
| `kea.mart_*` | full or incremental | Truncate + reload, or MERGE |
| `kea.bill_forecast` | incremental | MERGE on (account, period, forecast_date) |
| `app.*` | — | **Never.** `kea_loader` has no grant on the schema, not even `USAGE` |


**The subset decision matters most.** At 140k customers you would not put 2.45 billion raw intervals in Postgres. Production keeps daily aggregates in Postgres and fetches half-hourly from the warehouse for the rare deep query.

For this delivery: **13 months of half-hourly for the demo cohort** (~9.5M rows). Thirteen rather than twelve so year-over-year comparison works at every point in the year.

### 7.3 Load mechanics

Small tables — full refresh inside one transaction, so readers never see an empty table:

```sql
BEGIN;
CREATE TEMP TABLE _stg_plan (LIKE catalogue.plan INCLUDING DEFAULTS);
-- bulk load into _stg_plan
DELETE FROM catalogue.plan;
INSERT INTO catalogue.plan SELECT * FROM _stg_plan;
UPDATE catalogue.plan SET synced_at = now();
COMMIT;
```

`meter_read_hh` — never a broad DELETE. Build the month standalone, then swap:

```sql
CREATE TABLE metering.meter_read_hh_2024_03_new (LIKE metering.meter_read_hh INCLUDING ALL);
ALTER TABLE metering.meter_read_hh_2024_03_new
  ADD CONSTRAINT chk_range CHECK (interval_start >= '2024-03-01' AND interval_start < '2024-04-01');
-- bulk COPY into the new table

BEGIN;
ALTER TABLE metering.meter_read_hh DETACH PARTITION metering.meter_read_hh_2024_03;
ALTER TABLE metering.meter_read_hh ATTACH PARTITION metering.meter_read_hh_2024_03_new
  FOR VALUES FROM ('2024-03-01') TO ('2024-04-01');
COMMIT;
DROP TABLE metering.meter_read_hh_2024_03;
```

The matching CHECK constraint makes `ATTACH` a metadata-only operation instead of a full validation scan.

### 7.4 Two details that are easy to get wrong

**Load order follows the FK graph**, encoded as Airflow dependencies rather than a sleep. Note that `catalogue` is **not** independent of `metering` — plans are network-specific, so `catalogue.plan` references `metering.network`:

```
metering.network ─┬─► metering.icp ─► metering.meter_register
metering.property ┘        │
                  │        │
                  └─► catalogue.plan ─► catalogue.tou_band ─► catalogue.tariff_component
                              │                                        │
billing.customer ─────────────┴──► billing.account ─► billing.bill ────┴─► billing.bill_line
                                          │
                                          └─► kea.bill_forecast

metering.meter_read_hh    no FKs — can load any time, in parallel
kea.mart_*                no FKs — load after their sources are current
```

Three dependencies that are easy to miss:

- **`catalogue.plan` → `metering.network`.** Loading the catalogue before the network table fails. The two are not siblings.
- **`billing.bill_line` → `catalogue.tariff_component`**, not just `billing.bill`. The whole catalogue chain must be current before bill lines land.
- **`billing.customer` has no dependencies at all** and can load first, in parallel with everything.

`metering.meter_read_hh` deliberately carries no foreign keys (see §7.3) — referential integrity for it is enforced in the pipeline, which is what makes the partition swap safe to run concurrently with the rest.

**`synced_at` is set per table at commit time**, not `DEFAULT now()` per row. A slow load would otherwise give rows different timestamps within one table, and "how fresh is this replica?" becomes unanswerable.

---

## 8. The domain model

### 8.1 Where the design came from

Three real NZ electricity bills (Ecotricity), cross-checked against the brief.
The plans they priced were retired on 2026-09-05, but the bills are unchanged as
evidence: Vector's network rates and the daily "other" charges are transcribed
from them and are still what the catalogue charges.

- **The bills supplied the entities and the arithmetic** — charge groups, rate bases, TOU bands, line losses, GST, late fees, the ICP and plan-code structure.
- **The brief supplied what a bill cannot show** — half-hourly interval data, weather, forecasting, conversation.
- **Two rules were only discovered by reconciling the totals**, not by reading the labels. Both are in §8.2 — per-charge-group TOU bands (§8.2.1) and billing on volume plus losses (§8.2.2).

The methodological point, worth keeping: reading a bill tells you the entities; reconciling its totals tells you the rules. Stopping at the labels would have shipped both bugs below.

---

### 8.2 The decisions worth knowing

### 8.2.1 TOU bands are scoped per charge group

The first version keyed `tou_band` to `plan_id` alone. That is wrong.

On plan `NZSZ_ZERO_STD_FXTOU_240901_T1`, the same consumption is split differently by charge group:

| Group | Bands | Split |
|---|---|---|
| Network | 2 | 38.22 peak / 142.22 off-peak = 180.44 kWh |
| Energy | 3 | 34.08 peak / 74.96 off-peak / 71.39 shoulder = 180.43 kWh |

Same kWh, same period, same plan, different windows. Keying bands to the plan alone cannot express this, and the rating engine would mis-band every TOU network line.

**Consequence for Sprint 3:** re-rating against a candidate plan is one re-banding pass *per charge group*, not one per plan. This is the most likely source of a wrong savings figure.

### 8.2.2 Customers are billed on metered volume **plus** losses

The bill prints "34.08 kWh, 1.74 kWh losses @ 0.26" and charges $9.31.

```
34.08 × 0.26          = 8.86   ✗ does not match
(34.08 + 1.74) × 0.26 = 9.31   ✓ matches
```

Storing `loss_kwh` without making the relationship explicit would have let the engine bill on the metered figure and undercharge roughly 5% on every import line. Hence `billed_quantity` as a generated column.

### 8.2.3 Solar export is modelled by direction, not by sign

Solar is a Sprint 4 feature, but the sign convention was decided now on purpose.

Standalone tables (`alert`, `plan_recommendation`, `solar_installation`) can be added later at zero cost — a single `CREATE TABLE`, no migration. But relaxing a CHECK constraint on `meter_read_hh` later means **revalidating every partition of a 2.45-billion-row table**.

So: `flow_direction` gains `export`, registers and tariff components carry `direction`, and `kwh`/`rate` stay strictly non-negative. Nothing needs to change when solar arrives.

> **The rule:** decisions that touch the big table are made now. Standalone tables are added when the feature is built.

### 8.2.4 Three tables were cut for Sprint 2

An earlier draft carried 18 tables. Three were removed because neither Sprint 2 feature reads them:

| Cut | Why it was safe | What replaces it for now |
|---|---|---|
| `meter` | Serial numbers and install dates are unused by the explainer or the forecast | `meter_register` references `icp` directly |
| `payment` | Sprint 2 needs only the single "Payment Received" figure the bill prints | `bill.payments_applied` |
| `account_plan_history` | Sprint 2 has static plan assignments | `bill.plan_id` records the plan as rated |

All three are standalone additions — additive, with no `ALTER` to an existing table and no downtime. Two have since been built: `payment` and `account_plan_history` graduated into `kea_serving_schema.sql` on 2026-09-05 and ship in V001. Only `meter` is still deferred, and adopting it now means a migration rather than a hand-run `CREATE TABLE` — see that file's header.

**What was deliberately not cut on the same reasoning:**

- `meter_register`, because `meter_read_hh` keys on `register_id`
- The solar sign convention (§8.2.3), because relaxing a CHECK on a 2.45-billion-row partitioned table means revalidating every partition
- `conversation` / `conversation_message`, because deflection measurement cannot reconstruct sessions that were never logged

> **The rule:** decisions that touch the big table are made now. Standalone tables are added when the feature is built.

### 8.2.5 The forecast is two tables, split by writer

`kea.mart_forecast_input` and `kea.bill_forecast` serve one feature (Outcome 3)
at one grain — a daily snapshot per account per billing cycle. They are still two
tables, because two different jobs write them.

| Table | Key | Written by | Answers |
|---|---|---|---|
| `kea.mart_forecast_input` | `(account_id, period_start, as_at_date)` | the nightly pipeline | what do we know about this period so far? |
| `kea.bill_forecast` | `(account_id, period_start, forecast_date)` | the forecasting job | what will the bill be? |

This is §1.1's split-by-writer rule applied inside one schema rather than across
files. Collapsed into a single table, the feature refresh and the model would
write the same row, and a wrong number would stop being attributable to one of
them.

What each input column is for is set out in §8.6, beside the values the fixture
actually produces — a column list argued for in the abstract and then listed
again with numbers is two half-explanations.

**What the output enforces.** The confidence bounds are stored rather than
derived at read time:

```sql
CONSTRAINT forecast_bounds_ordered
    CHECK (projected_low <= projected_amount AND projected_amount <= projected_high)
```

The agent has to be able to say "between $180 and $220". A single number is false
precision on a half-finished period, and a model that emits an incoherent
interval fails the insert instead of reaching a customer. Alongside it,
`is_anomalous` carries a partial index (`WHERE is_anomalous`) so finding the
accounts worth alerting scans only the flagged rows, and `model_version` keeps a
bad forecast attributable to the model that produced it.

**Why this is replayable.** The input row is a dated snapshot, not mutable
current state, so any historical forecast can be reproduced: take the input for
that `as_at_date`, run a new `model_version`, compare the two. That is only
possible because yesterday's features were never overwritten.

**The gap, named.** Nothing enforces that a `bill_forecast` row has a matching
`mart_forecast_input` row — same grain, no foreign key. `mart_forecast_input` is
a rebuildable mart, and a FK would let a drop-and-reload cascade into forecast
history, which is the half of the pair that cannot be reproduced. The omission is
deliberate, and it makes the join the job's responsibility rather than the
database's.

> **The rule:** a mart may be rebuilt at any time, so nothing durable may depend on one by reference.

---

### 8.3 Source-to-serving mapping

What the warehouse does between file 1 and file 2. The four **resolved** rows are the integration; everything else is renaming and type coercion, handled mechanically in staging.

| Source | Serving | Transformation |
|---|---|---|
| `src_billing.customers` | `billing.customer` | `CUST_NO` → conformed `customer_id`; `MED_DEPENDENT 'Y'/'N'` → boolean |
| `src_billing.accounts` | `billing.account` | `ICP_NUMBER` text → **`icp_id` bigint FK — resolved**<br>`PLAN_CODE` text → **`plan_id` FK — resolved by period end, not by code alone** (§2.1.2 — this fails silently if done wrong)<br>`PAY_METHOD 'DD'` → `payment_method 'direct_debit'` |
| `src_billing.invoices` | `billing.bill` | No `period_days` at source → **GENERATED** column |
| `src_billing.invoice_lines` | `billing.bill_line` | `QTY` + `LOSS_QTY` → `quantity` + `loss_kwh`, plus **generated** `billed_quantity`<br>`CHG_GROUP` → `charge_group`, **all four values including `'CRD'` → `'credit'`** — the export mapped only NET and ENE and sent the rest to `other`, so Sprint 3's term credits arrived indistinguishable from a fee |
| `src_metering.connection_points` | `metering.icp` + `metering.property` | One source table split into two; `address_full` and `climate_region` move to `property` |
| `src_metering.registers` | `metering.meter_register` | Reparented to `icp_id` (the `meter` table is deferred) |
| `src_metering.interval_reads` | `metering.meter_read_hh` | `icp_identifier` + `register_code` → **`register_id` — resolved**<br>`read_status 'A'/'E'` → `quality`<br>`version_no` → latest wins, then dropped |
| `src_catalogue.price_plans` | `catalogue.plan` | `network_code` text → **`network_id` — resolved**<br>`user_category 'LOW'` → `user_type 'low_user'` |
| `src_catalogue.tou_windows` | `catalogue.tou_band` | `days_csv '1,2,3,4,5'` → `days_of_week smallint[]`<br>`charge_scope 'NET'` → `charge_group 'network'` |
| `src_catalogue.rate_components` | `catalogue.tariff_component` | `window_id` → **`band_id` — resolved**<br>`rate_uom 'KWH'` → `basis 'per_kwh'`<br>`component_desc` → `component_name`. **This is the table the rating engine reads** |
| `src_metering.meters` | *(not exported)* | Used only to resolve `registers.meter_id` → `icp_id`, because the `meter` table is deferred from serving (§8.2.4) |
| `src_billing.payments` | *(deferred)* | Commented out at source. Sprint 2 needs only `invoices.PAYMENTS_APPL`; transaction detail arrives with hardship triage in Sprint 4 |

---

### 8.4 The four columns that model a household, and are easily confused

Both marts model a household the same way:

```
daily kwh  ≈  baseline_daily_kwh  +  heating_sensitivity_kwh_per_hdd × HDD
              └── intercept ───┘      └────────── slope ──────────┘
```

Fitting it is the warehouse's job; nothing in the DDL enforces it. What the DDL
does is keep the coefficients together, in both places they are used, so that
explaining a bill and forecasting one cannot drift apart.

**This section defines quantities. It does not define the arithmetic that
consumes them** — that is §8.5 for explaining a closed bill and §8.6 for
forecasting an open one. The four columns are defined here rather than beside
their consumers for one reason, and it is not that they are shared. Only
`heating_sensitivity_kwh_per_hdd` genuinely is:

| Column | Table | Serves |
|---|---|---|
| `heating_sensitivity_kwh_per_hdd` | both marts | §8.5 **and** §8.6 — the same number, deliberately |
| `heating_degree_days` | `mart_daily_usage` | summed into both |
| `baseline_daily_kwh` | `mart_forecast_input` | §8.6 only — nothing in §8.5 reads it |
| `base_load_kwh` | `mart_daily_usage` | §8.5, and the fault detection in §8.7 |

They sit together because **`baseline_daily_kwh` and `base_load_kwh` are trivially
confusable and mean nothing alike** — and they belong to *different* consumers.
Filing each beside the section that uses it would put the one pair a reader must
compare in two different places, and delete the only page where the difference is
visible. The comparison is below, and it is the reason this section exists.

#### Heating degree days

`heating_degree_days` in `mart_daily_usage`, summed into `hdd_this_period` /
`hdd_prior_period` in `mart_bill_explanation` and into `hdd_to_date` /
`hdd_expected_remaining` in `mart_forecast_input`. One definition throughout — the
one the sample fixtures use:

```sql
GREATEST(16.0 - temp_c, 0)
```

16°C is the base temperature: the outdoor mean above which a house needs no
heating. An 8°C day is 8 degree days; a 22°C day is zero rather than negative,
because warm weather does not run a heater backwards. Cooling is the mirror
measure (CDD) and is deliberately not modelled — NZ households heat far more than
they cool, so summer air conditioning lands in `delta_unexplained` instead of
being misattributed to something the data cannot see.

**Why not store mean temperature instead.** Usage is not linear in temperature,
but is roughly linear in degree days. 20°C → 18°C changes nothing; 10°C → 8°C is
the same two degrees and moves real load. The `GREATEST(…, 0)` floor is what
straightens the curve: it collapses the whole no-heating range to zero and leaves
a segment a coefficient can multiply. `0.93 × mean_temp` would mean nothing.

**The period sums.** `mart_bill_explanation` carries the daily figure summed two
ways: `hdd_this_period` over the bill being explained, `hdd_prior_period` over the
bill named by `prior_bill_id`. Both are totals in degree-days across the whole
period — *how much heating weather this bill contained*, against *how much the last
one did*.

Only their **difference** drives the arithmetic (§8.5). Both endpoints are stored
anyway, because a difference cannot be quoted back to a customer. Fixture 03 holds:

| | period | HDD | mean temp |
|---|---|---|---|
| this | 2024-06-01 → 06-30 | 216.42 | 8.79°C |
| prior | 2024-03-01 → 03-31 | 1.47 | 18.00°C |

which is what lets the agent say *"June needed 216 degree-days of heating; March
needed essentially none"* rather than *"weather explains $61.92"*. The pair exists
for the sentence; the subtraction would have been enough for the sum.

⚠️ **They are period totals, not daily rates**, so the two sides are not directly
comparable when the periods differ in length — 216.42 against 1.47 is a genuine
cold snap, but 108.66 against 17.57 is partly just thirteen extra days. The second
limitation in §8.5 works that case through.

`mart_forecast_input` splits the same quantity on a different seam — `hdd_to_date`
/ `hdd_expected_remaining` — because it describes one **open** period whose second
half has not happened yet (§8.6), not two closed ones.

#### `baseline_daily_kwh`  — used by §8.6, forecasting

The intercept — what this household uses on a day that needs no heating.
Refrigeration, hot water, lighting, cooking, devices.

The trap is computing it as a plain historical average. Ninety days of winter
divided by ninety already contains that winter's heating; multiplying it across
the remaining days and *then* adding `hdd_expected_remaining × sensitivity` counts
weather twice and biases every projection high. It has to be the weather-stripped
figure:

```
baseline for a past day = that day's kwh − that day's HDD × sensitivity
baseline_daily_kwh      = the average of those
```

| Day | kwh | HDD | weather part | baseline part |
|---|---|---|---|---|
| 05-20 | 26.5 | 7.0 | 6.51 | 19.99 |
| 05-21 | 22.0 | 2.0 | 1.86 | 20.14 |
| 05-22 | 31.0 | 12.0 | 11.16 | 19.84 |
| 05-23 | 20.9 | 1.0 | 0.93 | 19.97 |

Totals swing from 20.9 to 31.0 while the baseline holds near 20. That stability is
the column's entire reason for existing.

#### `heating_sensitivity_kwh_per_hdd`  — used by §8.5 *and* §8.6

The slope — kWh added per degree day. Every sample fixture carries `0.93`
(`generate_scenarios.py:51`) so that weather is never what distinguishes one
scenario from another, except in the season fixture where the degree-day swing is
the whole story.

The magnitude is itself a finding. Roughly: `~0.2` is a house that barely responds
to cold (wood or gas heat, off-meter — or an empty house); `~0.9` is a typical NZ
home with electric heating; `~2.5` is a poorly insulated house on resistive
heaters. The last case is a heat-pump or insulation conversation, reached from
meter data without a site visit.

⚠️ **FIT IT AT MONTHLY GRAIN. This section did not say so, and the omission cost a
wrong answer.** The Warehouse step first fitted the slope daily and got noise — median R²
**0.069** across 499 households, slopes from **−3.98 to 8.44**, where a negative
slope asserts a house uses more heating when it is warmer.

The cause is in the cohort, not the arithmetic. LCL is real **London** metering,
date-shifted so UK winter lands in NZ winter (`dictionary/lcl-source-data.md`).
**The shift aligns seasons, not weather:** a household's day-to-day variation
responds to London's weather that day, while the HDD it is regressed against is
Auckland's. The seasonal envelope matches; the daily wiggle inside it cannot.

And §8.5 spends this slope across **billing periods**, not days — so a daily fit
was measuring precisely the component that is noise. Refitted monthly on 12
months, same data, same households:

| | households | median slope | median R² |
|---|---:|---:|---:|
| daily | 499 | 0.45 | 0.069 |
| **monthly** | 446 | **0.74** | **0.467** |

`0.74` against the `~0.9` above, and 290 of 446 clear a 0.30 fit floor. The rule
generalises past this cohort: **fit an estimator at the grain it is spent at.**

Publish `fit_r2` alongside the slope and gate on it. A slope with no explanatory
power must not price a weather driver — the alternative is a confident, wrong
weather attribution on every bill, which is the exact failure §6.3 and §8.5 exist
to prevent.

**It appears in both marts on purpose.** `mart_bill_explanation` runs it backwards
across two closed periods:

```sql
delta_from_weather = sensitivity × (hdd_this_period − hdd_prior_period) × rate
```

and `mart_forecast_input` runs it forwards across the unfinished one:

```
remaining weather kwh = hdd_expected_remaining × sensitivity
```

Same household, same coefficient. Fitted separately, the agent could say "the cold
cost you $61 last month" and "expect $79 more this month" off the same temperature
swing and be visibly inconsistent. One number stored in both marts makes that a
data guarantee rather than a convention two jobs are trusted to honour.

The two lines above are shown together because that symmetry is the point; neither
is stated in full here. `rate` and the three drivers beside weather are in §8.5,
the forward pass in §8.6.

**It is not spread evenly across the day.** At day grain it is a single number, but
heating load concentrates morning and evening. The shape the fixtures use
(`generate_scenarios.py:45`) sums to exactly the sensitivity:

| Hours | Per half hour | Half hours | Subtotal |
|---|---|---|---|
| 06–09 | 0.040 | 6 | 0.24 |
| 17–22 | 0.045 | 10 | 0.45 |
| 22–06 | 0.005 | 16 | 0.08 |
| 09–17 | 0.010 | 16 | 0.16 |
| | | | **0.93** |

The shape is not decoration. A near-zero overnight term is what makes a cold snap
and a fault separable at all.

#### `base_load_kwh` is a different thing entirely  — used by §8.5, explanation

Two columns invite confusion and mean nothing alike:

| | `baseline_daily_kwh` | `base_load_kwh` |
|---|---|---|
| Table | `mart_forecast_input` | `mart_daily_usage` |
| Computed | weather-stripped average over many days | **quietest half hour of one day × 48** |
| Scale | a statistic across days | one value per day |
| Answers | what does this house use on a mild day | what does this house draw *continuously* |
| Used for | forecasting | explanation, fault detection |

`base_load_kwh` is `ROUND(MIN(x.kwh) * 48, 4)`: take the smallest of the day's 48
intervals and scale it to 24 hours. At 3am the household is asleep, so whatever is
still drawing draws always. The schema comment states the product line outright —
*"you draw 350W around the clock, NZD 480/yr before you switch anything on"*.

This is the column that separates a fault from an appliance. A stuck cylinder
element draws the same at 3am as at 3pm, so `base_load_kwh` steps up and stays up.
A new dryer runs in the evening, leaves the quiet interval untouched, and does not
move it. The `faulty_appliance` fixture lands 98.0% of its increase in base load
while `cold_snap` lands 94.8% in weather — cleanly separated, and only because the
heating shape above goes near zero overnight where a fault does not.

#### ⚠️ On a generating ICP the minimum is taken in the dark

`MIN` over all 48 intervals is right only where every interval shows the whole
load, and behind a solar meter it does not. `import = max(0, load - pv)`, so a
household whose panels cover its draw imports **exactly 0** in that half-hour —
on the *import* register, with no export row involved. Filtering to import
registers, which the model does and which the comment above claims is enough,
does not help: the zero is already on the import side. `base_load_kwh` therefore
collapsed to 0 whenever the panels covered the draw at any point in the day, and
the column measured the sun rather than the house.

**Measured against release `demo-20260918T113420`, 19 Sep 2026: 5,807 of 5,978
complete ICP-days — 97.1%.** Nine ICPs carry an export register (the 8 cohort
households with panels, plus fixture 01), spanning 2024-08-08 to 2027-08-26. This
was not an edge case that struck on sunny days: a household's midday load is low
enough that some half-hour clears it on nearly every day of the year, so the
column read zero almost always, and the 2.9% that survived are the exception.

The definition is unchanged — *the smallest half-hour in which the whole load is
observable*. What changes is the window that qualifies. For an ICP with no export
register that is still all 48. For one with panels it is **22:00–05:00 NZ local**,
in which no NZ roof generates on any day of the year: the southernmost region
carried is Christchurch (−43.5°), which at midsummer sets 21:22 and rises 05:47
NZDT, clearing the window by 38 and 47 minutes. This also puts the implementation
back where the prose always was — *"at 3am the household is asleep"* above, and
`kea_serving_schema.sql`'s *"overnight percentile"*.

Two consequences worth stating. The zero propagated: `base_load_kwh / 48` is the
detection floor in §15.2, so every solar household's detected load was measured
against a floor of 0 and carried its whole standing draw as detected kWh, into
`mart_daily_detected_load` and the bill driver. And **non-generating ICPs are
deliberately left alone** — narrowing their window too would match the schema
comment more closely but would move every published base load, every
`delta_from_base_load` and every detection floor at once. That is a decision, not
a bug fix; it is open in §12.

#### Nullability is deliberate

A coefficient that cannot be fitted is a property of the model, not of either
consumer, which is why this stays here rather than moving to §8.6 with the column
it mostly affects.

`baseline_daily_kwh` and `heating_sensitivity_kwh_per_hdd` are both nullable. A new
connection has no history to fit; a household whose occupancy just changed has
history that no longer applies. NULL is the correct answer in both cases, and the
consumer is expected to fall back to a cruder projection and widen
`projected_low`/`projected_high` accordingly. The stored-bounds decision (§8.2.5)
is what makes "I know less about this customer" expressible rather than hidden.

The linearity is an approximation, flagged here in the same spirit as §2.2:
heat-pump efficiency falls in extreme cold, so sensitivity is not truly constant
across the range. It is good enough over the range NZ bills live in.

### 8.5 How a bill increase is attributed

`mart_bill_explanation` decomposes the change between two closed bills into four
named drivers plus a residual. This is the arithmetic behind Outcome 1, and until
now it existed only in `data/sample/generate_scenarios.py` — the fixture
generator, whose job is to produce sample data, not to be the specification the
warehouse is built from. The Warehouse step rebuilds this mart in dbt and needs the rule
here.

**Order matters, and it is not a preference.** `delta_from_period_length` is
computed first and removed before anything else is attributed. A 43-day move-in
bill looks like a 40% consumption increase and is not one; attributing it to cold
weather would be confidently wrong. Each driver is measured after the ones above
it have been taken out, so nothing is counted twice.

**Everything is converted to dollars at the prior period's own average variable
rate**, not at a headline tariff — that way a driver measured in kWh becomes a
figure the customer can check against the bill they already have:

```
rate = (prior_amount − daily_rate × prior_days) / prior_kwh
```

Subtracting the fixed daily charges first is what makes it a *variable* rate.
Leaving them in would inflate every per-kWh driver by the fixed component.

**The four drivers.** `days`, `hdd` and `base` are this period; `prior_*` is the
comparison period; `sens` is `heating_sensitivity_kwh_per_hdd` (§8.4).

```
delta_from_period_length = (days − prior_days)
                           × (prior_kwh / prior_days × rate + daily_rate)

delta_from_weather       = sens × (hdd − prior_hdd) × rate

delta_from_base_load     = (base − prior_base) × days × rate

delta_from_tariff        = re-rate this period's kWh under both plan versions;
                           the difference, usage held constant

delta_unexplained        = amount_delta − (the four above)
```

Period length carries `+ daily_rate` because an extra day costs both the energy an
average day uses **and** that day's fixed charge. The other three are per-kWh only.

**Worked, against fixture 03 (`03-bill_higher_cold_snap.sql`).** A cold month
after a mild one, $97.78 → $163.19:

```
rate     = (97.78 − 0.60 × 31) / 255.61              = 0.309769
period   = (30 − 31) × (255.61/31 × 0.309769 + 0.60) =  −3.15   one day shorter
weather  = 0.93 × (216.42 − 1.47) × 0.309769         = +61.92   1.47 → 216 HDD
base     = (6.3286 − 4.6095) × 30 × 0.309769         = +15.98   always-on rose
tariff                                                =   0.00   same plan
                                                        ───────
                                                          74.75
unexplained = 65.41 − 74.75                           =  −9.34
```

The drivers overshoot and the residual is negative, which is the honest outcome:
the model attributes slightly more to weather and base load than the bill actually
moved. A schema assertion requires the five to sum to `amount_delta` exactly, so
the residual absorbs the difference rather than being quietly dropped.

**`delta_unexplained` is a feature, not a leftover.** Degree days measure cold
only, so summer air conditioning lands here rather than being misattributed
(§8.4). "I can account for $34 of the $61 increase" is an honest and useful
answer; inventing a fifth driver to reach $61 is not.

**`is_explainable` gates the claim, not the arithmetic.** It is true only when the
measured drivers cover at least 60% of the movement **and** the period contains no
estimated or missing intervals — a bill rated on estimates cannot be explained
from meter data, however well the numbers add up.

**Two limitations to carry into the Warehouse step.** `delta_from_tariff` is `0.00` in all
nine fixtures because both periods sit on one plan version at one set of rates,
so the column has never been exercised; the generated catalogue (Sources) is the first thing
that can move it, with 64 plan codes at two versions each and a levy that changes
mid-version. And the decomposition is sequential rather than simultaneous: where
two drivers interact — a longer period *and* a colder one — the interaction lands
wherever the ordering puts it.

Fixture 07 (`07-bill_higher_longer_period.sql`, the 43-day delayed-run bill) shows
the size of that. `hdd_this_period` and `hdd_prior_period` are period totals, so
they carry the length difference inside them:

```
this   43 days, 108.66 HDD  = 2.527 HDD/day
prior  30 days,  17.57 HDD  = 0.586 HDD/day
```

`delta_from_weather` differences the raw totals, so the thirteen extra days arrive
at the full gap: `0.93 × (108.66 − 17.57) × 0.311289 = 26.37` — the rate here is
fixture 07's own prior-period variable rate, not fixture 03's. Hold those days at
the prior period's *own* degree-day rate — attributing them to length, where they
have at least as good a claim — and the weather term is `24.17` instead. The
`$2.20` between the two is heating that `delta_from_period_length` already
extrapolated, since `prior_kwh / prior_days` contains the prior period's own
heating rather than its baseline alone.

Nothing is lost: the five still sum to `amount_delta` exactly, because
`delta_unexplained` (`−5.42` here) absorbs the overlap. What is at stake is which
named driver takes the credit — defensible for explanation, and not a variance
decomposition.

### 8.6 A forecast cycle, worked through

The columns above are easier to justify against real numbers, and there is now
a fixture that produces them: **`data/sample/09-bill_mid_cycle_forecast.sql`**.
Every figure below is what that file actually seeds, not an illustration.
Account 900801, period 2024-06-01 to 06-30, as at **10 June** — day 10 of 30,
with three completed bills behind it: the same period last year, April as a
mild month, and May as the latest bill.

**Who writes what, and when.** Two DAGs (§9), which is the practical reason the
tables are split at all: `kea_daily_pipeline` writes the input at 02:00 NZT
after the quality gate, while `kea_forecast_refresh` writes the output and may
run more often, since it only reads the mart and does arithmetic. Input first,
always — the model never touches source tables. (As built, both DAGs write
through the same publication, `publish_serving.py`: the refresh rebuilds the
input mart and publishes a release that recomputes the output with it, so
inputs and forecasts never land in separate commits.)

**The input row.**

| Column | Value |
|---|---|
| `days_elapsed` / `days_total` | 10 / 30 |
| `kwh_to_date` | 179.9008 |
| `cost_to_date` | 62.19 |
| `baseline_daily_kwh` | 9.2002 |
| `hdd_to_date` | 94.52 |
| `hdd_expected_remaining` | 187.90 |
| `heating_sensitivity_kwh_per_hdd` | 0.9300 |
| `fixed_charges_period` | 18.00 |
| `same_period_last_year_kwh` / `_amount` | 404.72 / 147.61 |

Four groups, and the last is the one worth arguing about:

- **Cycle position** — `days_elapsed` / `days_total`, `kwh_to_date`, `cost_to_date`.
- **Anchors to extrapolate from** — `baseline_daily_kwh` (§8.4), and
  `same_period_last_year_kwh` / `_amount`. Two honest baselines: this household's
  own recent daily rate, and this household a year ago.
- **Weather** — `hdd_to_date` (fact), `hdd_expected_remaining` (forecast), and
  `heating_sensitivity_kwh_per_hdd`, the per-household conversion between them.
  This is what makes the projection better than
  `kwh_to_date / days_elapsed × days_total`. The sensitivity is the same column
  `mart_bill_explanation` carries (§8.4), so explaining a bill and forecasting one
  share a single notion of how weather-driven a house is.
- **Charges already certain** — `fixed_charges_period`. Daily charges are
  `daily_rate × days_total` on day one of the cycle; only the variable part needs
  forecasting. What that buys is worked through below.

It is an INSERT, not an UPDATE — `as_at_date` is in the key, so this one cycle
accumulates a row per elapsed day. Across the ten of them `hdd_to_date` climbs
from 8.18 to 94.52 while `hdd_expected_remaining` falls from 274.24 to 187.90,
and **their sum stays at 282.42 throughout**: fact displacing forecast, day by
day, over a fixed total. The fixture asserts that invariant.

**Deriving the rate from what is already stored.** `cost_to_date` and
`fixed_charges_period` together give the variable rate without joining the
tariff:

```
fixed so far = 18.00 × 10/30       = 6.00
variable     = 62.19 − 6.00        = 56.19
rate         = 56.19 / 179.9008    = 0.312339 /kWh
```

April's bill was $112.22 for 301.64 kWh over 30 days, so its own blended rate
is `(112.22 − 18.00) / 301.64 = 0.312360`. The two agree to a fifth of a cent,
and the fixture asserts that too — a re-derived rate that drifts from the
billed one means `cost_to_date` and `fixed_charges_period` have stopped
describing the same thing.

**Projecting the remainder — split, not extrapolated.**

```
remaining baseline = 20 days × 9.2002        = 184.0040 kwh
remaining weather  = 187.90 HDD × 0.93       = 174.7470 kwh
projected_kwh      = 179.9008 + 358.7510     = 538.6518 kwh
projected_amount   = 18.00 + 538.6518 × 0.312339 = 186.24
```

Naive extrapolation gives `179.9008 / 10 × 30 = 539.7` — within a kWh here, and
right for the wrong reason. It assumes the remaining twenty days are as cold as
the first ten. Split into baseline and weather, a forecast warm spell pulls the
projection down on its own.

**The band, and why `fixed_charges_period` earns its place.** Uncertainty
applies only to the 358.75 kWh that has not happened yet. The 179.90 kWh is
metered and the $18.00 of daily charges was fixed on day one. At ±15% on the
remainder:

```
remaining variable cost = 358.7510 × 0.312339 = 112.06
±15%                                          = ±16.81
projected_low / projected_high                = 169.43 / 203.05
```

The agent says **"between $169 and $203"**. Had the fixed charges not been
separated out, ±15% on the whole $186.24 gives ±$27.94 and a band of
$158–$214 — 66% wider, and wider by an amount that is pure fiction.

The narrowing is structural. On 1 June the same arithmetic over 29 unhappened
days gives a band $48.90 wide; by 10 June it is $33.62. Nothing about the
model's confidence changed — there is simply less period left to be wrong
about.

⚠️ **The ±15% was a judgement when it was written, and it has since been scored against
a published method — it does not hold as a range.** Over the 4,084 snapshots of §15.12's
backtest, this band contains the final kWh **51%** of the time, falling to **42%** at 20+
days elapsed, when the remaining period is shortest and it should be easiest. The
narrowing above is real arithmetic; what it is not is a calibrated interval. Read §15.12
before quoting "between $169 and $203" as a range.

**Comparison and flag.**

```
baseline_amount = 147.61      (same period last year, 404.72 kWh)
variance_pct    = +26.17%
is_anomalous    = true
```

Nothing in this household changed: the fixture generates all three windows from
the same 9.20 kWh/day. Last June averaged 11.2°C and this June 6.4°C, and the
whole 26% is that. Which is the useful kind of bill shock — one the agent can
name a cause for.

**What the agent reads.** Only `bill_forecast`, latest `forecast_date` for the
account, via `idx_forecast_account`. `mart_forecast_input` exists for the model
and for diagnosis; no answer path reads it.

**What happens when something fails.** If the quality gate blocks the pipeline,
no input row is written and the model forecasts from yesterday's features —
correct-but-stale, per §9. If the model job fails instead, input rows keep
accruing and the missed days can be forecast retroactively once it is fixed.
Neither recovery is possible if features are overwritten in place, which is the
practical payoff of the snapshot grain.

**One caveat the fixture is explicit about.** Its weather forecast is perfect —
the remaining days run through the same formula the reads were built from. That
is why the HDD total is exactly constant and why `projected_kwh` barely moves
across the ten snapshots (538.6560 to 538.6518). A real NIWA feed carries error,
the point estimate wanders and converges, and the band is wider at long horizons
than ±15% of the remainder alone. The fixture trades realism for a single cause
of narrowing, so that a fixture which stops narrowing has exactly one thing
wrong with it.

### 8.7 The hourly profile, and what shape buys over volume

`kea.mart_hourly_profile` is the only mart with a printed antecedent. The schema
comment names it outright: *the "Average Weekday Breakdown" chart from the bill,
split by direction so import renders above the axis and export below.* It is not
an aggregate someone invented — it is a picture the customer has already seen,
made queryable.

**The grain is four columns**, `(icp_id, profile_month, hour_of_day,
is_weekday)`, which fixes a month at exactly 48 rows. Each part of that key is
load-bearing:

- **`profile_month`** — shape is recomputed monthly, not once over all history,
  because shape itself moves with the season. A winter month has sharper
  morning and evening peaks than a summer one. Storing a profile per month is
  what makes "the same hour, two months apart" a comparison you can run.
- **`hour_of_day`** — hours, not half hours, with `CHECK (hour_of_day BETWEEN 0
  AND 23)`. The 48 intervals underneath are deliberately collapsed to 24: the
  printed chart has 24 bars, and half-hourly resolution here would add noise
  rather than information. Anything that genuinely needs the half hour —
  `base_load_kwh` needs the quietest one — reads `mart_daily_usage` instead.
- **`is_weekday`** — weekday and weekend are two different curves, and a
  household that is empty 9-to-5 on weekdays has no such trough on Saturday.
  Averaged together they flatten each other into a shape neither day has, which
  is why the split is in the key rather than a filter applied later.

**`sample_days` is the column that keeps the mart honest.** An average carries
no record of how much evidence it rests on: 0.8 kWh from three days and 0.8 kWh
from thirty are indistinguishable once stored.

```sql
-- How many days went into the average. A profile built from three
-- days is not the same claim as one built from thirty, and the
-- agent should be able to tell.
sample_days         smallint    NOT NULL,
```

`NOT NULL`, so no row may decline to say. It belongs to the same family as
`is_explainable` and `estimated_read_pct` on `mart_bill_explanation`: columns
whose job is to make "I do not know enough to say that" expressible instead of
letting an average imply confidence it has not earned.

**Import and export are separate columns**, matching the chart's two halves and
§8.2.3's decision to model direction rather than sign — `kwh` stays non-negative
everywhere and the direction lives in the column. Both default to 0 and are
`NOT NULL`, so a household without solar needs no NULL handling downstream.

**What shape buys.** This is the mart that separates scenarios `mart_daily_usage`
cannot. Daily totals say the usage rose; hourly profile says *where in the day*
it rose:

| Fixture | Change against the prior month | `base_load_kwh` |
|---|---|---|
| `bill_higher_faulty_appliance` | every hour of the 24 lifts | steps up and stays |
| `bill_higher_new_appliance` | only 21:00–23:00 lifts; 3am untouched | unmoved |

Two increases of similar size, indistinguishable at day grain, obvious the
moment the same hour is compared across months.

The line the agent may not cross is unchanged. A load appearing between 9pm and
11pm is measured; a dryer is a guess. `data/README.md` is explicit that
half-hourly interval data cannot identify an appliance and calls that refusal
deliberate — describing the shape and asking is the correct move, naming the
appliance is not.

**Why it is a table and not a materialized view.** An early draft computed
`mv_hourly_profile` inside Postgres; the schema comment still records the
replacement. Postgres holds only a 13-month subset of reads for the demo cohort
(§7.1), so a view computed locally could only ever see that subset — and habitual
shape is exactly the thing that wants more history than that, not less. The
warehouse computes it across all 2.45B rows and the export loads the result.
Another instance of the writer rule: this mart's writer is the export job, not
Postgres.

---

## 9. Orchestration

MWAA. DAGs developed against `aws-mwaa-local-runner` — MWAA has no local mode, and a failed environment update from an untested dependency change is a 20-minute recovery.

```
kea_daily_pipeline  (schedule: 02:00 NZT)

  extract_billing ───► parse_billing ────┐
  extract_catalogue ─► parse_catalogue ──┼─► snowflake_stage ─► dbt_run ─► dbt_test
  extract_metering ──► parse_reads ──────┘
                       (Spark; the others are scripts)                                                   │
                                                                        ▼
                                                          soda_scan (quality gate)
                                                                        │
                    ┌───────────────────────────────────────────────────┘
                    ▼
  export_catalogue ─┬─► export_billing_customer ─► export_billing_account ─┐
  export_metering  ─┘                                                       │
                                                                             ▼
                                                        export_billing_bill ─► export_kea
                                                                             │
                                                                             ▼
                                                                    reconciliation_check
```

⚠️ **The export fan-out above is lineage, not a task graph, and Serving is why.**
A replica load deletes a row and re-inserts it with the same id; a foreign key checked at
DELETE time fails on that even though the row returns before COMMIT, and no delete order
avoids it in general — some parent is always referenced by a child not yet reloaded. So
`load_export_to_serving.py` stages every table, deletes children-first, inserts parents-first
and commits **once** with the constraints deferred. Splitting that across Airflow tasks would
give each table its own commit and destroy the atomicity `assert_load_atomic.sh` proves.

`kea_daily_pipeline` therefore has **one** `load_serving` task. The FK order lives in
`kea_pipeline.serving.TABLES`, inside that transaction, where it is a property of the load
rather than of the scheduler.

**The quality gate is a real gate.** If `dbt_test` or `soda_scan` fails, the export tasks do not run. Postgres keeps serving yesterday's data, which is correct-but-stale rather than fresh-but-wrong. For a product whose whole premise is trustworthy explanations, that trade is the right way round.

**Separate DAGs** for anything with a different cadence: `kea_weather_load` (NIWA, **monthly** — the source publishes the full record on a monthly cycle, so a daily run would land twenty-nine identical partitions a month and claim each was a day's news; built as `kea_extract_weather`), `kea_forecast_refresh` (mid-cycle, could be more frequent than the main pipeline), `kea_scale_test` (manual trigger, Snowflake-only).

⚠️ **The four extracts are separate DAGs too, which the diagram above does not
show.** Drawing them inside `kea_daily_pipeline` makes "four independent
extract jobs on their own schedules" a convention rather than a fact: one
DagRun means one failure state, and a billing failure would block metering
however the tasks are wired. They are `kea_extract_billing`,
`kea_extract_metering` and `kea_extract_catalogue` — separate DagRuns, separate
schedules, separate connections, separate alerts. The diagram survives as a
**lineage** picture; Orchestration's `kea_daily_pipeline` should be scheduled on the
assets those DAGs emit rather than owning their tasks.

**Two Airflow 3 details that decide whether the load date is right.**

`@daily` maps to `CronTriggerTimetable`, whose interval is `timedelta(0)`: in
Airflow 3 `data_interval_start`, `data_interval_end` and `run_after` are all
the same instant, the moment the run fires. There is no covered period to read
off — a change from Airflow 2's interval semantics. A DAG that takes the fire
time's own date extracts a day that has barely begun, writes its success marker
over a nearly empty partition, and never returns to it: a permanent, silent,
one-day-wide gap, every night, with every run green.

So the DAG steps back one **calendar** day itself. Not a duration: going back
24 hours from midnight on 2026-09-28 lands at 23:00 on the 26th, because
2026-09-27 is 23 hours long — which emits that date twice and skips the DST day
entirely. Same rule as the extract window (§4.2): build from calendar dates,
and never let a `timedelta` stand in for a day.

**One rule, one implementation.** Every task in every DAG takes its load date
from `run_after` through the same macro (`_kea_common.LOAD_DATE`), and `make`
calls the same function, `kea_pipeline.window.load_date_for_fire_time`, for its
default. `data_interval_end` is not used anywhere: a manually triggered run has
no data interval at all, and until 29 Sep 2026 the metering DAG's two Spark
tasks read it while its four snapshot tasks read `run_after` — so under
`airflow dags test` one DAG could extract two different days.

**Spark is launched with `DockerOperator`, not `SparkSubmitOperator.`** The
Airflow image has neither `java` nor `spark-submit`, so the provider is
installed and unusable; a sibling container is the pattern already provisioned
for dbt and Soda. Bind mounts are resolved by the host daemon, so the paths
come from `HOST_PROJECT_DIR`, not from anything inside the Airflow container.
Source credentials come from the Airflow **connection** rather than an
environment variable — `environment` is a templated field, so the lookup
happens when the task runs rather than when the DAG is parsed, and naming the
connection keeps a metering job from ever holding billing's password.

**Weather is warehouse-only.** NIWA data lands in the warehouse and is consumed by the marts, but is never exported to Postgres. The figures the agent needs — `temp_mean_c`, `heating_degree_days` in `mart_daily_usage`, and the period comparisons in `mart_bill_explanation` — are already denormalised in. Exporting the raw table would add one nobody queries.

---

## 10. Data quality

Three gates at different points:

| Gate | Where | Blocks |
|---|---|---|
| Schema validation | Spark parse | Malformed rows → quarantine |
| dbt tests | after `dbt_run` | Build fails → no export |
| Soda scans | after `dbt_test` | Freshness, volume, orphan rate → no export. **Built with the quality gates**: `pipeline/soda/checks/warehouse.yml`, five checks, `soda scan` exits 2 so the task fails and nothing downstream starts. A second, non-gating scan reports on the serving store after the load |

### 10.1 Tests that matter most

**Reconciliation.** Recompute a real bill end to end and assert it line by line: **$50.54** (TOU + solar, 24 days). If the pipeline reproduces it from source data, the tariff model and rating logic are correct. This is worth more than any synthetic assertion. Bill B carries a one-cent residual on its levy line that no rate can remove — see §10.3.

⚠️ **Bill A ($248.07, flat, 31 days) is a target with no source data behind it.** Checked 3 Sep 2026: no invoice in `src_billing` carries that total, and its plan `VECT_OFPK_STD_140520` is not in `src_catalogue`. Reproducing it means inventing a tariff and 31 days of readings that sum to $215.71 and then reconciling against the invention — which tests the invention. This section already concedes its line detail was never published, so derived-and-labelled was always its ceiling.

**It stays in this document and in the fixture schema as historical evidence.**
Its published group totals once supplied the Contact-derived catalogue's
non-levy residual, but that derivation is no longer used by the three active
Kea demo plans. Its plan code remains a worked fixture example. A documented
target without source data is different from a dropped target, and Bill A is
the first.

**Plan version brackets the bill period.** For every bill, assert that the resolved plan version's `valid_from`/`valid_to` contains `period_end`. This is the §2.1.2 failure, and it is the one that produces a plausible wrong number rather than an error.

**Not expressible as a SQL constraint, so it must be a dbt test:** `tariff_component.band_id` must reference a `tou_band` whose `charge_group` matches the component's. Postgres cannot check across tables; nothing else will catch it; and getting it wrong silently mis-bands every TOU network line.

**Volume anomaly.** Meter reads per day should be within ±10% of trailing average. A collapse usually means a partial extract, which is worse than no extract because it looks like a real drop in consumption.

⚠️ **Built differently than written, because the obvious reading is red every night.** This
source is a fixed historical extract: readings stop 2027-08-27 and arrivals 2027-10-10, so
reads-per-LOAD-date collapses from ~26,900 to ~250 the moment the extract passes the end of
the readings — the corrections-only tail, which is correct behaviour rather than a partial
extract. The Lake dashboard already recorded the consequence: *"a volume check that did not
know this would page someone every night."*

So the ±10% band is measured **per consumption day, over days where the cohort is fully
present**. Measured 4 Sep 2026: 369 such days, mean 26,364 reads, **zero outside the band**.
The window's first sixteen days carry 6–19 households rather than 500 — that is the extract
ramping in, and a check that could not tell it from a collapse would be worthless. Freshness
is likewise measured on **the load** (did the date this run staged reach RAW), not on the
recency of the readings, which are frozen.

**Orphan rate.** Cross-system unresolved references should be near zero. A spike means the extracts are skewed in time, not that customers vanished.

**Near zero is not zero, and the check is banded accordingly.** The source generator plants five defects on
purpose — three orphan ICPs and two unknown plan codes — and the quarantine catching them is
the system working. The measured baseline is 12 rejects against 509 ICPs, 2.4%, so the band
sits well above that and well below anything indicating skew: it fires on a change of regime
rather than on the fixtures.

### 10.2 An honesty note

LCL is a curated academic release — already cleaned. It contains very few organic defects. So quality rules here are **built against a realistic NZ production defect profile** (interval gaps, implausible spikes, stuck-meter zero runs, estimated and final flags, duplicate deliveries, orphan references, DST transitions) and **validated by injecting those defects synthetically**.

**Negative reads are the exception, and the reason is worth stating.** An earlier draft listed them here. `src_metering.interval_reads` carries `CHECK (kwh >= 0)`, which is how §8.2.3 is enforced — export is modelled by direction, not by sign — so a negative read cannot be a row in the source by construction. It is something the pipeline meets at the *file* boundary and quarantines, which is where the defect belongs. The source-side equivalent, and the harder one, is export delivered on the import register: the values are positive, they pass every constraint, they overstate import on the bill, and nothing but a distribution rule catches them.

That is the honest thing to say in review: the source data is clean; here is how the pipeline is proven against the defects production data would actually have.

**Sprint 4 applies the same pattern to a second question**, because LCL cannot answer it either: London households in 2011–14 have no EVs, and every payment profile in the generated billing is fixed from the first bill, so neither the detector (§15.2) nor the early warning (§15.7) could be scored against anything. The generator plants known answers — EV and hot-water sessions in 8 households, 4 sliding payers — records them in `gen_audit`, and `gen_audit` never reaches serving. What that proves is bounded in the same way: the pipeline finds what was planted, not that real households behave that way. §15.10 is the result.

⚠️ **Planting into real data needs the label checked against the data it lands in.** LCL's half-hours are scaled ×1.72 to reach NZ consumption, which lifts ordinary UK electric heating into the 5 kW band the detector calls `ev` — on 27 of 50 candidate households. A household planted with an EV that already produces EV-shaped days would have scored the detector against its own noise. `planted.py` therefore screens each candidate's *own* load with the detector's run rules before planting (≤ 0.5% EV-shaped days, ≤ 2% timer-misread days), which left 10 EV-clean and 7 element-clean candidates out of 35. A clean label is part of the test, not preparation for it.

---

### 10.3 Reconciliation targets

If the rating engine reproduces these from the underlying quantities and plan definitions, it is correct. Better acceptance criteria than anything synthetic, and directly usable for QA (`KEA-231`).

**Bill A — flat/2-band, 31 days, plan `VECT_OFPK_STD_140520`**

| Group | Amount |
|---|---|
| Network | $75.55 |
| Energy | $116.55 |
| Other | $23.61 |
| **Ex GST** | **$215.71** |
| GST @ 15% | $32.36 |
| **Total** | **$248.07** |

**Bill B — TOU/3-band with solar, 24 days, plan `NZSZ_ZERO_STD_FXTOU_240901_T1`**

| Group | Amount |
|---|---|
| Network | $29.22 |
| Energy | $21.10 = import $27.70 − export $6.60 |
| Other | $0.22 |
| **Total ex GST** | **$50.54** |

Bill B exercises: partial period (24 days, prorated daily charges), three TOU bands, per-charge-group band scoping, line losses on import, export credits, and the EA levy charged on import only (180.43 kWh).

**The levy line is internally inconsistent on the printed bill, and the criterion is stated to account for it.** Rating Bill B from `src_metering.interval_reads` through `src_catalogue` reproduces ten of its eleven lines to the cent, including the 5.11% line losses. The eleventh is the levy, where the document's own rate and amount cannot both be true:

| To reproduce | The rate must lie in |
|---|---|
| the printed **amount**, $0.22 | `[0.0011916, 0.0012470)` |
| the printed **rate**, 0.0013 | `[0.0012500, 0.0013500)` |

The intervals do not overlap, so no catalogue can satisfy both: 180.43 kWh × 0.0013 = $0.2346, which rounds to $0.23 and puts the total at **$50.55**.

The catalogue therefore stores **0.0013, the rate the bill prints**, and the back-solved 0.0012193 is recorded in `data/dictionary/` as evidence rather than used. Storing the derived value would make the bill reproduce exactly, but the test would then prove only that an input had been tuned until the output matched — which is the one thing a reconciliation test exists to rule out.

**The criterion is therefore:** every line reproduces exactly, and the total reproduces to within the one cent attributable to a levy line the source document contradicts itself on. A discrepancy anywhere else, or of any other size, is a failure.

---

## 11. Failure and recovery

| Failure | Behaviour |
|---|---|
| One extract fails | Other two proceed. Downstream waits; Postgres serves yesterday |
| dbt test fails | Export blocked. Alert. Postgres serves yesterday |
| Export fails mid-table | Transaction rolls back. Table unchanged |
| Partition swap fails | Old partition still attached. No data loss |
| Bad data reached Postgres | Re-run from bronze — never re-extract from source |

**Reprocessing is idempotent by construction.** Re-running any period must produce identical results: DELETE-then-load per period in Postgres, `MERGE` on a stable key in the warehouse, partition swap for reads. This is a correctness property, not an optimisation, and it is why bronze is immutable.

**"Identical results" needs defining, or it cannot be checked.** It cannot mean
byte-identical files: `_extracted_at` records when the pull happened and so
legitimately differs between two runs, and Parquet part-files carry a fresh
UUID each time. The working definition, and what `make ingest-date VERIFY=1`
compares:

> Two materialisations of a load date are identical when their **row multisets
> over every column except `_extracted_at`** are equal.

Multiset rather than set, because 9,263 duplicate deliveries are legitimate
source rows and a comparison that collapsed them would call a real change no
change. The digest is therefore a **sum** of per-row hashes: order-independent,
because an ordered comparison needs a unique sort key and picking a non-unique
one is how the source generator's first determinism test failed; and summed rather than
XORed, because under XOR a duplicated row cancels itself out and a file with
two copies hashes the same as one with none.

The check also fails when `_extracted_at` did **not** move — a hash that
matches because nothing re-ran proves nothing.

**Backfill** uses the same DAG with `--start-date` / `--end-date`. No separate backfill code path — a second code path is a second set of bugs, and it is always the one that is under-tested.

**A span move is not a backfill, and the difference is where the new rows land.** A
backfill's rows land on the same dates as the ones they replace, so they cover them. When the
span itself moves, the new rows land on *different* dates — the old ones are not overwritten,
they are left sitting there carrying the old shift, invisible to every per-date check and
wrong in every aggregate. The procedure that repairs it is the stack doc's §7; the property
that makes one necessary is here.

⚠️ **Only the first step of the chain removes anything it was not handed.** Reprocessing is
idempotent per key, per date, per partition — never globally, and that is the whole
difficulty:

```
drop + recreate RAW   total          <- the only total operation in the chain
extract               per partition  <- a sealed partition is skipped unless forced
stage                 per load date  <- replaces the dates it is given, keeps the rest
partition swap        per month      <- replaces months the export carries, keeps the rest
delete+insert (dbt)   per key        <- deletes keys in the incoming batch, keeps the rest
materialized views    never          <- do not follow their base tables at all
```

Every step below the first scopes "replace" to what arrived, so after a span move the
non-overlapping old dates survive all of them. The 2027 move surfaced five bugs of exactly
this shape, and **four of the five exited 0**: a step that replaces less than you assumed has
nothing to complain about, so it reports success.

**Two rules follow.** Reset RAW by **DROP and recreate, not TRUNCATE** — DROP also applies the
current Avro schema evolution, and a truncate leaves an old *column* behind exactly as
reliably as skipping a date leaves an old row. And resetting RAW is **necessary but not
sufficient**, because the stale copy also lives one level lower: `spec.py` treats a sealed
bronze partition as immutable, which is right for a re-run and wrong after a span move, so
every date that already had a partition hands its **old** parquet back to the loader. Cost of
finding that out: 6,649 invoice rows carrying 3,210 duplicate `inv_id`s, caught by a dbt
uniqueness test after a 6h24m loop had already finished.

---

## 12. Open items

A struck-through row is settled, with the date and what was decided kept rather than deleted
— several of them record a rejected option, which is the part worth keeping. **As of
27 Sep 2026: 23 items, 11 settled, 12 open.** Of the twelve, three are waiting on Kea rather
than on us (the extract interfaces, the feed cadence, the `retain_until` policy), three are
Sprint 4 measurements that need real data before they can be closed (§15.10), and the
remaining six are ours — the newest being whether the kNN fallback of §15.12 gets wired in.

| Item | Owner | Blocks |
|---|---|---|
| ~~Write dbt staging models + schema.yml for all 12 source tables~~ — **done** | Harvey | 17 staging models across `billing`, `catalogue`, `metering` and `weather`, each domain with its own `_*__sources.yml` and `_*__models.yml`. More than the 12 this item counted: weather was not a source system when it was written |
| ~~Weather source~~ — **NIWA station data, selected 2 Sep 2026** | Harvey | §8.4's `heating_degree_days`, and therefore `delta_from_weather` (§8.5) and the forecast (§8.6). Loaded by `scripts/gen_source/load_niwa_weather.py` and staged in `models/staging/weather/` |
| ~~`heating_degree_days()` in `engine.py` defaults to base 18~~ — **fixed 2 Sep 2026** | Data | Now 16, matching §8.4. It was never live: `routes.py` serves HDD from `fixtures.heating_degree_days`, a different function of the same name, so the two bases had never disagreed in production. Worth correcting anyway — the first caller to reach for it would have got 18 and an unsourced docstring saying that was the NZ convention, and `test_analytics_extra.py` asserted 18 and so *protected* the discrepancy |
| ↳ Fill rule for missing station-days, and `is_explainable` when a bill period contains one — **half settled, and the settled half is not written down** | Harvey | The `is_explainable` half is implemented: `mart_bill_explanation.sql` requires `days_without_weather = 0` **and** that the prior period is covered too, after an earlier version called 1,269 bills explainable whose `delta_from_weather` was NULL. So the fill rule in force is *do not fill — decline to explain*, which is the right default but was never recorded in `docs/decision-log.md`. A weather driver with holes is the confident-but-wrong answer §6.3 exists to prevent |
| ~~Generate LCL data into `src_*` shapes~~ — **done** | Harvey | `scripts/gen_source/`: the UK→NZ transform, the cohort pick, the three generators and the numbered SQL. 500 households and 20,277,761 reads at full scale; 50 in the demo profile |
| Confirm extract interface for all four sources (replica / API / file / CDC) | Adi + Harvey | §4 strategy |
| Confirm meter-feed delivery cadence | Adi (`KEA-501`) | Streaming relevance |
| Confirm revision mechanics — that a revision arrives as a new row with a higher `version_no` | Harvey | §4.1 `received_at` watermark, and the silver dedup that keeps the highest `version_no` |
| ~~Agree quarantine `_reject_reason` vocabulary~~ — **parse group settled, Lake** | Harvey + Stephen | `pipeline/contracts/reject_reasons.json` holds one file with three groups. Parse's five codes are populated; `validation` and `integrity` are declared and empty, so the second and third writers extend one vocabulary rather than starting their own. The rule for the parse group: **a reject is decidable from one row of one file** — which is why `parse.duplicate_in_file` is deliberately absent |
| ~~**`read_status = 'F'` has no target in `common.read_quality`**~~ — **settled 3 Sep 2026: mapped, not widened** | Data | `final` → `validated` in `pipeline/export/metering.meter_read_hh.sql`. Both mean "confirmed, not an estimate", so only the source's spelling is lost, and the enum stays four values for the agent. Mapping at EXPORT rather than at parse keeps §5's rule intact: the contract still carries `final`, and the decision is visible in one `case` expression instead of buried in a Spark job. ⚠️ The 1,010 rows are not yet in the warehouse — see the note below §12 |
| Verify BRIN `pages_per_range` propagates to partitions | Harvey | Query performance |
| ~~Decide Postgres retention for `meter_read_hh`~~ — **decided and implemented** | Harvey + Adi | 13 months for the cohort, **and the full span for the nine fixture households** — those reads are 2023-06 → 2024-08 and any window ending today would exclude them, taking the Serving step's acceptance target with them. Implemented as a per-household rule in `pipeline/export/metering.meter_read_hh.sql` |
| ~~Execute against a live **PG17** instance~~ — **done 30 Aug 2026** | Data | Files 1 and 2 apply cleanly on PG17, the nine fixtures load and pass their assertions, and Bill B still reconciles to $50.54 there. The deferred schema (file 3) remains unapplied |
| Set `app.conversation.retain_until` policy | Adi + Product — **owner not settled** | Consumer Care decision, not a technical default |
| **Quarantined accounts cannot reach the serving store** — found by the warehouse quality gate | Data + Product | §6.3's "quarantine rates surface to the product" does not hold end to end. `billing.account` requires `icp_id` and `plan_id` NOT NULL, and a quarantined account has neither, so all five are dropped by the export and `is_quarantined` is false on every served row. Needs a schema decision — a nullable FK, or a separate quarantine table in serving — not a loader change |
| ~~Split `alert` ownership before Sprint 3 builds it~~ — **settled 2026-09-05: not split** | Data | `kea.alert` graduated into `kea_serving_schema.sql` as an **append-only** table, so the pipeline inserts and never rewrites and the runtime UPDATE to `sent_at` / `delivered_at` / `opened_at` / `acted_at` needs no separate `app.alert_delivery`. The split would have been the wrong fix: the real hazard was the nightly rewrite destroying delivery state, not two writers. See §13.1 |
| **A second import register splits `base_load_kwh` from the detection floor** | Data + Adi | §15.2. Either `base_load_kwh` narrows to the uncontrolled registers — a served column, read by §8.7's fault detection — or the detection models compute their own floor. Nothing depended on the answer before Sprint 4; the models are correct only while every ICP has exactly one import register, which is true of this data and is not a property of the world |
| **Whether `base_load_kwh` narrows to an overnight window for *every* ICP** | Data + Adi | §8.4. Generating ICPs were fixed on 19 Sep 2026 — their minimum is now taken over 22:00–05:00, because behind a solar meter the all-day minimum measured the sun. Non-generating ICPs still use all 48. `kea_serving_schema.sql` calls the column an *"overnight percentile"* and §8.4's prose says *"at 3am the household is asleep"*, so the narrower window is arguably what was always meant — but widening it now moves every published base load, every `delta_from_base_load` and every detection floor at once, and would invalidate the Sprint 4 before/after table (§15.10). Needs a rebuild and a re-measurement, not an edit |
| **The detected-load band's 90% containment threshold is unmet and not retired** | Data | §15.10 measured 65.2%. Narrowing the band, or replacing the hard edge with a tolerance, needs real EV metering to score against — planted answers cannot say which of the two is right |
| **The hardship lead threshold (≥ 21 days) is unmet and not retired** | Data + Product | §15.10. Three planted accounts cannot establish a median. Revisit with real arrears history; until then the early outreach stays staff-queue-only (§15.7) |
| ~~dbt test: `tariff_component.band_id` charge_group must match~~ — **done** | Data | Not expressible as a SQL CHECK, so it is a singular dbt test: `dbt_project/tests/assert_rate_component_band_matches_charge_group.sql` |
| ~~Generate the full partition set~~ — **done 3 Sep 2026** | Data | `load_export_to_serving.py` creates one partition per month over the loaded span and swaps each in, per §7.3. The three examples in the DDL are now just the starting state |
| Whether the kNN quantile fallback serves the accounts `is_forecastable` turns down — **measured 27 Sep 2026, not decided** | Harvey | §15.12. The backtest clears its own gate for exactly that population (median APE 6.3% against a 10.2% limit, mean CRPS 0.88× the champion's), so the evidence for a fallback exists (champion there = `hdd-linear-v2`; see §15.12 on why) and nothing consumes it: `knn_quantile.py` is imported only by its test and its harness, and `hdd-linear-v3`, `kea.bill_forecast` and `mart_forecast_input` are untouched. Wiring it in is a serving decision, not a measurement one — it would give some accounts a band where today the UI says the forecast is unavailable, and that is a product call about what an unexplained number costs. The leakage tests run in the required CI check since #222, failing on any skip (§15.12) |

> ⚠️ **The 1,010 `'F'` reads are not in the warehouse, and the mapping is
> therefore unexercised.** They exist in `src_metering.interval_reads` with
> `received_at` on 2026-08-27, 08-28, 08-31 and 10-11 — all of which fall in NZ
> load dates whose bronze partitions were sealed *before* the defect generator
> wrote those rows. Re-extracting would need `EXTRACT_ARGS=--force`, which
> rewrites a sealed partition and changes the content hash the Lake's
> idempotency gate asserts on, so it was not done as part of Serving.
>
> This is the case `lake.manifest()`'s `source_row_count` exists to detect:
> *"a disagreement means the source changed under a sealed load date — which
> really happens here, because re-running `make init-sources` rewrites
> `received_at` for every revision."* The mapping is correct and costs nothing
> to carry; it simply has no rows to act on until those partitions are
> deliberately re-extracted.

---## 13. Deferred

### 13.1 Mixed-ownership tables

One table in `kea_deferred_schema.sql` still has mixed ownership and needs a decision when
it is built:

| Table | Problem | Options |
|---|---|---|
| `hardship_case` | Created by the agent on escalation, then updated by a human | Belongs in `app` |

**`alert` was the other one, and it is settled rather than deferred.** It graduated into
`kea_serving_schema.sql` on 2026-09-05 and ships in V001. The deferred file proposed
splitting it into a loaded table plus `app.alert_delivery`; append-only settled it more
simply. Every strategy in `kea_pipeline.serving` rewrites its table, so an alert on the
nightly load path would lose its delivery state every night — a message that was sent,
landed and was opened comes back `pending` the next morning, and the engagement data the
feature exists to measure is destroyed on a schedule. An alert is an **event**: the
pipeline inserts and never rewrites, so a runtime UPDATE to `sent_at` / `delivered_at` /
`opened_at` / `acted_at` is safe without a split. `kea.bill_forecast` and
`kea.mart_load_log` are the precedent. ⚠️ It must never be added to `serving.TABLES`.

---

### 13.2 Deferred tables

`kea_deferred_schema.sql` holds the tables not yet built, following the same namespace split. Four graduated in Sprint 3; Sprint 4 graduated none and built its features from metered evidence instead (§15), so its drafts stay here. Note it cuts across features — `metering.solar_installation` is source data, while `kea.load_decomposition`, our inference from it, is ours.

**One has mixed ownership and still needs a decision when built:** `hardship_case` is created by the agent on escalation and belongs in `app`. `alert` was the other, and graduated on 2026-09-05 with the split avoided rather than made — see §13.1.

Run `kea_serving_schema.sql` first.

---

## 14. Summary of decisions

Every decision this document argues for, in one place. Decisions 1–12 summarise §1–§13
above. **Decisions 13, 14 and 15 summarise §15, which follows this section** — Sprint 4 is
numbered last because it was built last, and its number is cited from the V005 migration,
the dbt models and the stack doc, so it stays put rather than being renumbered into
reading order.

1. **Four independent extracts**, because four independent source systems. A failure in one must not block the others.
2. **The source systems already record the associations** — billing stores `ICP_NUMBER` and `PLAN_CODE` at data entry. Conforming those to surrogate keys is mechanical; key maps are append-only so surrogates stay stable.
3. **Plan resolution is temporal.** One plan code has many versions, and a bill must be rated against the version in force at its period end. This fails silently if done wrong, which makes it the most dangerous step in the pipeline.
4. **Cross-system referential integrity cannot be assumed** and is handled explicitly — orphans quarantine, and the agent declines rather than guessing.
5. **One quarantine table, one reason vocabulary**, across all three rejection paths.
6. **Bronze is immutable**, so reprocessing never touches a production source system.
7. **Spark runs one job, not three.** Only the 2.45B-row meter-read extract needs distributed compute; billing and catalogue are plain scripts. Sizing the tool to the job rather than standing up one engine everywhere.
8. **Mirrored namespaces stay source-shaped**; only `kea` denormalises. The rating engine needs exact tariff structure.
9. **Postgres gets a subset of meter reads**, not the full history. 13 months for the demo cohort; production would keep aggregates and fetch detail on demand.
10. **Quality gates block export.** Correct-but-stale beats fresh-but-wrong for a product built on trustworthy explanations.
11. **The `app` schema is never touched by the loader.** `kea_loader` has no grant on it — not even `USAGE`. Conversation history is written by the application at runtime and is the one thing in the system no re-run can recover.
12. **The forecast input and the forecast output are separate tables**, because the nightly pipeline writes one and the model writes the other. Same grain, no FK between them — a rebuildable mart must not be able to cascade into forecast history.
13. **Appliance loads are published as daily evidence with a low and a high bound, not as a breakdown.** Half-hours can show a charging run; daily totals cannot. The pipeline publishes what it can see, per ICP per day, and the backend composes the customer-facing breakdown — which is why `load_decomposition` stays deferred. A figure is always given as its range (§15.2).
14. **A new model is accepted against a known answer, with the thresholds written first.** The source generator plants EV sessions, hot-water timers and sliding payers; the evaluation scores the pipeline against them and the report records misses as misses (§15.10). Two of the fourteen thresholds are unmet and neither was rewritten — one of them overturned the pre-written rule about what to do next, and that is recorded too.
15. **A model that survives its own tests is then measured against someone else's method, and the measurement is allowed to contradict the design.** Planted answers say the pipeline found what was put there; they cannot say the forecast is any good, so the bill forecast was scored head-to-head against a published kNN quantile method with the gate fixed in advance (§15.12). It came back against us: the ±15% band §8.6 argues for contains the final kWh 51% of the time, 42% late in the cycle, where kNN's P10–P90 holds 82% at roughly twice the width. The band is narrow because ±15% was chosen, not because the weather term explains the variance. Two further limits are part of the decision, not footnotes to it — the champion scored is `hdd-linear-v2`, so §15.4's change is still unscored, and the head-to-head win has a bootstrap interval crossing 1 on 34 accounts. A seasonal variant was rejected on sharpness while passing every accuracy clause. Measuring a method is not adopting it: nothing is wired up, and that is §12's newest open item.

---

## 15. Sprint 4: the stretch goals

Scenario 10's three stretch goals are an appliance-level breakdown, solar and EV
what-ifs, and a hardship early warning. The schema is migration `V005`; the
readable form is the Sprint 4 section of `kea_serving_schema.sql`. Decisions are
in `docs/decision-log.md` (17–18 Sep 2026), and the staged plan, with the backend
briefs, is `docs/sprints/sprint-4/data-plan.md`. §15.1–§15.9 are the design;
**§15.10 is what it measured when it ran**, and it qualifies two of the claims
above. **§15.11 is a supplement added after that measurement** (20 Sep 2026,
migration `V006`): the case §15.1–§15.9 never covered, a household that has
both panels and an EV, and what the meter cannot tell us about it. **§15.12 is a
second supplement** (27 Sep 2026): an offline backtest against a published method, which
scores the forecast's **predecessor** rather than §15.4's version, and which corrects what
§8.6 implies about the band.

### 15.1 What is measured and what is estimated

| Table | Kind | Writer | Loaded |
|---|---|---|---|
| `kea.mart_daily_detected_load` | **measured**: what the half-hourly import shows, per ICP per NZ day | dbt → export | TRUNCATE, nightly |
| `kea.mart_charging_session` | **measured**: the EV-pattern runs themselves | dbt → export | TRUNCATE, nightly |
| `kea.solar_estimate` | **estimated**: a what-if priced on the real import | job in the publication | append-only |
| `kea.ev_scenario` | **estimated**: a what-if priced on the real import | job in the publication | append-only |
| `kea.hardship_outlook` | **projected**: an arrears date and amount, with its arithmetic | the hardship job | append-only |

Every new figure is computed in the pipeline and published in the one
publication transaction; the backend reads it. Planted test answers
(`gen_audit`) never reach serving.

### 15.2 Detected loads are evidence, not a breakdown

Daily totals cannot show an EV session or a hot-water timer; half-hours can.
The pipeline publishes what it can see and the backend composes the breakdown,
which is why `load_decomposition` stays deferred (§13.2).

- **Per day, not per month.** Bills do not follow calendar months, so any
  billing period must be a plain sum over days, as it is for `mart_daily_usage`.
- **One row for every assessed day,** with zeros when nothing was detected. An
  assessed day has all `expected_intervals` half-hours on the uncontrolled import
  register. No row means "not assessed", which is not the same as "nothing there".
- **Always a range.** For each half-hour of a published run:
  - `low` = import − the mean import just before and after the run;
  - `high` = import − the day's floor, `base_load_kwh / 48` (§8.4).

  The floor belongs to base load, so `high` never counts a kWh the base-load
  driver counts. A run across midnight is split between the two dates.

  ⚠️ **`low` is not a floor.** It is the least the run can have used under
  *that* subtraction. §15.10 measured a third of planted days falling below it,
  by a median 2.3% of the day. Nothing may word it as a minimum or a guarantee.

  ⚠️ **The floor and the series it is subtracted from are built from different
  register sets, and only today's data makes them the same.** `base_load_kwh`
  (§8.4) is `MIN(kwh)` over **every** import register; the detection series is
  the sum over import registers that are **not controlled**. Every ICP in this
  data has exactly one import register (UN, uncontrolled), so the two are
  identical and the floor is right. Add a controlled night-rate import
  register and they part: the minimum falls to that register's idle
  half-hours, the floor approaches zero, and every uncontrolled half-hour
  above `detect_min_excess_kwh` becomes a run candidate. Whoever adds a second
  import register must decide then whether `base_load_kwh` narrows to the
  uncontrolled registers — which changes a served column and the §8.7 fault
  detection that reads it — or whether the detection models compute their own
  floor from their own series. Sprint 4 is the first thing that depends on the
  answer; before it, nothing did.
- **Classification** (first match wins): a night block that runs only in the
  cold months is **not published** and stays with the heating slope; ≥ 2
  half-hours at ≥ 5 kW is `ev`/`high_power` whatever the start time; a block
  starting in the same slot on ≥ 60 % of nights, warm months included, is
  `timed_night`; a long steady 1.8–4 kW run is `ev`/`low_power` (medium
  confidence at most); anything else is not published.
- **Wording.** "A load with the charging pattern of an EV", never "your EV";
  "a load that switches on at the same time most nights (often a water heater on
  a timer)". Figures are always given as the range.

### 15.3 Daily completeness counts each import half-hour once

`mart_daily_usage.interval_count` used to be `count(*)` over every register, so
a solar household read 96 on a 48-half-hour day and a day with gaps could look
complete. Sprint 4 fixes it in place: `interval_count` and `estimated_count`
count distinct import half-hours. Names and types are unchanged. The 8 solar
households and fixture 01 gain complete days, and with them heating fits,
forecasts and explainable bills.

### 15.4 The heating fit is net of detected loads; forecast v3 adds them back

A hot-water timer that draws more in winter, or an EV that arrives mid-span,
would otherwise be fitted as heating, inflating the slope that the bill
explanation, the forecast and the backend all use. The monthly fit now uses
daily import minus `ev_kwh_low + timed_night_kwh_low`. The low bound is used
because subtracting the high bound would remove ordinary evening use too.

A net fit has a lower baseline, so `hdd-linear-v3` adds the recent detected load
back:

```
remaining kWh = remaining days × (baseline_daily_kwh + detected_load_kwh_day)
              + expected HDD × slope
```

`mart_forecast_input.detected_load_kwh_day` is the mean over the assessed days
in the 28 days up to `as_at_date`, and is NULL with fewer than 7 such days, in
which case `is_forecastable` is false.

⚠️ **§15.12 scores the forecast from outside, and it does not score this change.** The
backtest's champion is `hdd-linear-v2` refitted as at each date, because the export ships
v3 and the harness drops the comparison against a fit it cannot rebuild. So v3's argument
above — that adding detected load back improves the projection — remains untested against
anything outside itself. What §15.12 does establish is about the band, not the point
estimate, and it is not good news: see §8.6.

### 15.5 The detected-load bill driver

§8.5's attribution order becomes: period length, weather, base load,
**detected loads**, tariff, unexplained.

```
delta_from_detected_loads = (detected_load_kwh_day_this − detected_load_kwh_day_prior)
                            × days × rate
```

Without it, a new EV lands in `delta_unexplained`. Fixture 05 (`new_appliance`)
is expected to move its dominant driver from `unexplained` to `detected_loads`.

### 15.6 Solar and EV what-ifs

- **Solar** (accounts without an export register): the last 12 complete months
  of real import; 3, 5 and 7 kW systems; output from a regional yield table
  (low/high kWh per kW per day, by month, for the four weather regions) spread
  over daylight half-hours. Import and export are netted per real half-hour,
  then priced on the current plan with its buy-back rate. Payback is simple (no
  degradation, no price change) and runs from lowest cost ÷ highest saving to
  highest cost ÷ lowest saving; it is NULL when that saving is zero.
- **EV:** `add_ev` prices a year of assumed charging, starting in the evening
  (the baseline) or at night, on each Kea plan; `shift_detected` moves each
  detected `high_power` session to 23:00 and prices it on the current plan and
  on Time of Use. A flat plan honestly saves nothing. A third pattern,
  `solar_soak`, was added in V006 for households that export — see §15.11.
- Every row names its `assumptions_version` and its metered basis window.

### 15.7 The hardship early warning still has no score

The no-score rule (V001's hardship header, decision log 5 Sep) stands. The early warning is three
more facts on `common.hardship_signal_code` (`payment_drift`,
`bill_above_usual_payment`, `first_shortfall`) and, when drift or a shortfall
is present, a `kea.hardship_outlook` row: the first date the projected balance
meets the arrears definition (a bill still ≥ 5 % unpaid 30 days after its due
date), the projected overdue amount, and the arithmetic in words for staff.

How the job decides, as built (ruleset `hardship-identification-v2`):

- **Paid by the assessment date.** The warning signs and the outlook sum the
  payments linked to each bill and made by `assessed_on`. `payments_applied`
  is the biller's figure at extract time, so a replay at a past date would see
  payments that had not happened yet. The v1 rules still read it, as in
  Sprint 3.
- **Which bill is "above usual".** The latest issued bill, or a bill not yet
  paid in full and not yet in arrears, or failing both the newest forecast for
  an unbilled cycle. `kea.bill_forecast` is ex-GST, so the forecast is grossed
  up at the rate on the account's latest bill first. "Usual" is the median
  amount paid on the last 6 paid bills due before that bill (at least 3).
- **The projection.** Each open bill, then the forecast bill, is assumed paid
  at most the usual amount; a part payment already made is taken as final. The
  outlook is the first bill that ends up under 95 % paid 30 days after its due
  date. There is no outlook when the account is already in arrears (a v1 rule
  covers it) or when usual payments would cover every bill.
- **"Flagged" keeps its Sprint 3 meaning**: at least one v1 code.
  `signal_count` counts warning signs too, so dashboards and Story select on
  the codes, never on `signal_count > 0`.

A warning sign alone never creates an outreach. Alert rule v2 needs
`bill_above_usual_payment` together with `payment_drift` or `first_shortfall`,
or any v1 code as before. Early outreaches appear in dashboard 8's staff queue
only; nothing is sent, and customer-facing hardship copy is unchanged.

### 15.8 Schema rules V005 follows

- Every name is schema-qualified: a baselined database never ran V001's
  `search_path`.
- Every new foreign key is `DEFERRABLE INITIALLY IMMEDIATE`, because the
  publication replaces parents under `SET CONSTRAINTS ALL DEFERRED` and V003
  converted only the keys that existed when it ran.
- Row-level rules (low ≤ high, labels present exactly when something was
  detected, session length) are CHECK constraints; rules across rows or tables
  (no overlapping sessions, outlook after its assessment) are dbt or Soda checks.
- No grants: V001/V002's default privileges cover new `kea` tables.
- The job tables leave `computed_at` to its default, so a publication's rows all
  carry its transaction time, which `validate()` uses to find them.

### 15.9 What the backend reads

`design/serving_read_contract.json` lists every serving column the backend
reads or is asked to read: the four marts it maps today, `mart_daily_detected_load`,
`solar_estimate`, `ev_scenario`, and the new `mart_bill_explanation` columns.
The `ServingReadContract` test (run by `make ci ONLY=serving-db`) fails if a
migration renames, drops or retypes one, if `kea_app` cannot select it, or if
any served foreign key is not deferrable. The backend changes themselves are
the owner's; the brief in the Sprint 4 data plan specifies them.

### 15.10 What the rebuild measured

Everything above §15.9 is design. This section is what the pipeline actually did
when it was run, because a detection model that is only argued for is not
evidence of anything.

The method, and every threshold, is `analysis/sprint4_evaluation/README.md`,
written **before the first run** — changing a threshold now makes it a new
evaluation, not a better result. The harness is
`pipeline/scripts/ops/evaluate_sprint4.py` (`make evaluate-sprint4`); it reads
serving in a rolled-back READ ONLY transaction, `gen_audit` for the planted
answers, and two SELECTs on Snowflake silver, and writes only its own `results/`.
The report names households by their planted role — "wallbox, evening, 1" — and
never prints an ICP.

Run against release `demo-20260920T175812` (a full `make rebuild-demo`, now `make rebuild-all`; dbt **331
PASS, 0 WARN, 0 ERROR**): **63 figures, 15 of them with a threshold, 13 met.**
The table below is grouped by the claim each one tests, so a row can carry more
than one threshold — precision and recall are one row, and so are the three
wallbox step ratios. The two misses are the same two as the run before
detect-v2, and neither is caused by it.

| Claim in this section | Measured | Target | |
|---|---|---|---|
| §15.2 the classifier finds charging runs | precision 100%, recall 100% (349 of 349) | ≥ 90% / ≥ 85% | PASS |
| §15.2 a timer is detected as a timer | 2,410 of 2,411 planted nights, 0 read as EV | ≥ 80% | PASS |
| §15.2 the published band contains the load | **61.6%** of 3,614 planted days | ≥ 90% | **MISS** |
| §15.11 no class is claimed where the panels hid the run | **0** `low_power` claims on masked runs | 0 | PASS |
| §15.11 a solar household's night reads like anyone else's | recall **100%** on the 185 sessions the meter saw whole | — (no target) | — |
| §15.4 the net fit is the fit the household would have had | net − oracle slope ≤ 0.14 on all 11; published fit replicated on 49 of 49 ICPs | 0 mismatches | PASS |
| §15.5 a new EV becomes its own driver | step ratio 100.0–110.4% on all three non-solar wallboxes | ≥ 70% | PASS |
| §15.4 only v3 is served | 8,716 rows, all `hdd-linear-v3` | true | PASS |
| §15.7 the early warning arrives before arrears | 3 of 3 gradual accounts caught | ≥ 2 of 3 | PASS |
| §15.7 it does not fire on people who are fine | 0 early outreaches in 55.3 on-time account-years | ≤ 5 per 100 | PASS |
| §15.7 how far ahead it arrives | median **18 days** (leads 51, 18, 17) | ≥ 21 | **MISS** |

**The bounds miss is a statement about the band, not about the detection.**
The model finds the right half-hours — published minus planted half-hour count
has a median of 0 — and 89.1% of days land within 0.5 kWh of the band, 93.3%
within 10% of the day's planted kWh. What fails is the hard edge: the median
band is **0.47 kWh wide**, and of the days that fall outside it the median
miss is 0.17 kWh — 2.6% of the day — almost always *below* the low bound. Per
shape: `ev_wallbox_7kw` 55.5%, `ev_portable_2kw` 44.0%, `hot_water_3kw` 67.6%
— all three **unchanged to six decimal places** from the run before detect-v2,
which is the check that the release did not disturb a household without panels.

⚠️ **The headline fell from 65.2% to 61.6% and that is arithmetic, not a
regression — provably so.** The three solar+EV households added 384 planted days
at `ev_solar_wallbox_7kw` **30.7%**, the lowest of any shape. Take those 384 days
back out of the new figure and 2,225 − 118 = 2,107 of 3,614 − 384 = 3,230 is
**0.652322**, which is the pre-detect-v2 pooled figure to the last digit. Not
close to it: equal to it. The entire drop is the new shape entering the pool, and
not one day of the previous cohort moved. They score low for the reason §15.11
exists: where the array covered part of the draw, the band is struck on an import
register that never held the load.

⚠️ **The consequence is a wording rule, and it is load-bearing.** §15.2's low
bound is "the least the run can have used" *under its own subtraction* — it is
not a floor the true figure cannot fall below. Nothing may describe it as a
minimum, a guarantee, or "at least". The range is the answer; the two numbers
are two arithmetics, not an interval with a proof around it.

**The lead miss was measured and the rule was not reverted.** The pre-written
rule said a hardship miss returns the outreach rule to v1. It did not: the miss
is three days on the median of three numbers, each of those three arrived 28 to
63 days ahead of that account's own first v1 signal, and reverting would trade a
rule that caught every planted slide for one that fires after arrears have begun
— and drop the projected date from the staff queue with it. The threshold is
**not** retired, because three accounts cannot establish a median, and §15.7's
staff-queue-only rule stands until real arrears history can score it. Recorded in
`docs/decision-log.md`, 18 Sep 2026.

**Two findings with no threshold, both worth carrying into the product's
wording:**

- **A detection is a pattern, not an appliance.** Of the 39 households with
  nothing planted, **35 show high-power sessions** — LCL is real metering and
  real households run real 5 kW things. The classifier is doing what it was
  specified to do; "a load with the charging pattern of an EV" is therefore the
  literal truth and "your EV" is not. (39 rather than 42 because the three
  solar+EV hosts now have a planted answer and have left the unlabelled set.)
- **The solar model is in the right place, now against eight different arrays.**
  Each of the 8 generating households is checked against **its own** array —
  1.5 to 8 kWp, sized to load by `cohort.py` or to the car by
  `planted.SOLAR_PLAN` — rather than one flat 4 kWp for all, and the modelled
  export share of generation sits **1.3 to 2.6 points** below the metered one on
  a different sun shape and a different yield table. Close, as it should be, and
  not equal, which would have meant the check was circular.

The same evidence is browsable rather than only readable: the Sprint 4 dashboard
in Metabase plots the detector's output beside what the generator planted, and
is built from a committed script (stack doc §8).

### 15.11 The household with both panels and an EV (detect-v2)

Sprint 4 covered solar households and EV households; it never covered one that
is both, and that was enforced by construction rather than noticed — the source
generator refused to plant a load on a household with panels, and
`build_solar_estimates.py` skips any account that already exports. A real
customer with both therefore got a **silently degraded answer, not an error**,
and §15.10's own figures priced it: 49.1 high-power detections per
household-year on households without panels against **17.4** with them.
⚠️ That 17.4 is the figure that motivated the work and **cannot be
re-measured**: it was taken across eight generating households all carrying the
same flat 4 kWp array. Load-linked sizing and three of those households
acquiring a car have since changed both the arrays and the group, which now
reads **26.8** across the five that are still unlabelled. The rise is array
sizing, not detect-v2 — detect-v2 only ever *withholds* a class, so it cannot
raise a detection count.

**The physics, which no amount of modelling removes.** A meter records two
things, on two separate non-negative registers: `import = max(0, load − pv)`
and `export = max(0, pv − load)`. **Generation is never metered.** So on a
generating ICP there are two regimes, and they are different in kind:

| | what the import register holds | what may be said about load |
|---|---|---|
| the house is exporting | exactly 0 | **nothing.** Load is invisible, not small |
| daylight, not exporting | load − pv | it is **understated** by an amount nothing on the meter reveals |

A 7 kW wallbox under a generating array can present as 2 kW of import — below
the 5 kW `high_power` threshold — so detect-v1 published it as a portable
charger. That claim, not the missed detection, is the defect: a miss is silence,
and a misclassification is a confident wrong answer.

**What detect-v2 changes is what may be CLAIMED, never a threshold.**
`int_metering__import_half_hours` marks two half-hours, both **measured**, both
derived from the export register the household actually has:

- `is_masked` — the export register read above zero this half-hour.
- `is_daylight_shoulder` — not masked, but strictly between the day's first and
  last exporting half-hour. The panels demonstrably generated at both ends of
  it.

`int_metering__import_runs` counts them as `masked_half_hours`, and above zero:
`charger_class` becomes **NULL** rather than `'low_power'`, and `confidence` is
capped at `'low'`. `'high_power'` still stands where it is met — PV only ever
subtracts, so a run that cleared 5 kW anyway really did.

⚠️ **NULL `charger_class` is not "no EV found."** The session is published with
its kWh, its hours and its window; only the wallbox-against-portable
distinction is withheld. Any reader that branched on two classes needs a third
branch, and the customer-facing wording needs a sentence of its own — something
like *"we can only see what you bought from us; while your panels were
generating, we cannot tell what you used."*

**Four things are deliberately NOT done, and each would be easy:**

- **`is_assessed_day` is unchanged.** A solar household's night is as readable
  as anyone else's, and dropping its daylight would destroy the detection that
  works in order to protect the detection that does not.
- **`ev_kwh_low/high`, `ev_half_hours` and `detected_class` are unchanged.**
  Only class and confidence weaken, so `mart_forecast_input` and
  `hdd-linear-v3` see exactly what they saw under detect-v1. **There is no
  forecast change in this release**, by design.
- **No modelled PV enters a published model.** A reconstructed generation curve
  would be an estimate inside `silver`, which §15.2 rules out. The evaluation
  may reconstruct, because there the *generator's* output is known; a real
  customer's is not.
- **No self-consumption percentage.** It is not computable from two
  non-negative registers — the denominator, generation, is never measured. A
  measured *export headroom by time of day* is a sound future table and its own
  piece of work.

**The recommendation that makes the refusal safe to ship.** Nulling
`charger_class` on exactly the sessions that matter would *remove* EV advice
from the households with most to gain, because `build_ev_scenarios.py` filtered
on `charger_class = 'high_power'`. So the filter widens to include a masked
session with no class, and a third pattern is added: **`solar_soak`** places the
day's charging into that NZ day's half-hours ranked by **measured** export,
capped at each half-hour's own export, offered only when the basis window really
did export. ⚠️ **It may carry a negative saving and is published anyway.** On
Time of Use, midday import the panels did not cover sits in the most expensive
window, and the honest answer is sometimes "your night rate still wins";
suppressing the losing row would be the same silent wrongness in a new place.

**Array size is now load-linked.** All eight generating households previously
carried the same 4 kWp array regardless of use, so the smallest (2,401 kWh a
year) sold back 80% of what it made — a household nobody would install for.
`cohort.py` now derives `pv_kwp` per household from its NZ-scaled annual load,
bucketed to real product sizes and floored at 1.5 kWp, and `gen_metering.py`
reads it. `has_solar` itself is **deliberately unchanged**: the planting pool is
the complement of the solar set, so moving one household out of solar would
reshuffle which households carry Sprint 4's eight planted loads and cost every
figure in §15.10 its comparability. That coupling is recorded in
`docs/decision-log.md`, 20 Sep 2026.

**How it is scored.** Three solar households are now planted with an EV — one
charging at 23:00 (the control: panels change nothing in the dark), one from
17:30 (masked in summer, clear in winter, same household), one at 11:00 (the
worst case). The dbt singular test
`assert_masked_runs_have_no_charger_class.sql` and the serving CHECK
`charging_session_masking` both refuse a `low_power` claim on a masked run;
`make evaluate-sprint4` splits recall into sessions the panels hid and sessions
they did not. ⚠️ **The hidden-session recall has no target and never will**:
when the array covers the whole draw the import register reads 0, there is no
excess above the day's floor, and no run exists to find. That is the meter's
limit, not the ruleset's, and a target would be a promise about physics.

⚠️ **The 17:30 host turned out to be a second control, not the seasonal case,
and the reason is worth keeping.** It was planted to show the same appliance at
the same clock time masked in December and clear in June. It is never masked, in
any season — and not because the flag under-reaches. A 7.2 kW wallbox draws more
than any array in this cohort generates at 17:30 even at midsummer, so the
instant the charge begins the house stops exporting and the export register
reads 0 for the whole session. `is_masked` is therefore false throughout. So is
`is_daylight_shoulder`: it is defined as strictly between the day's **first and
last** exporting half-hour, and the charge itself has moved that last
half-hour back to before the session started. **A load big enough to hide behind
the panels is also big enough to stop the export that would have hidden it.**

That makes the evening host a control of a different kind from the 23:00 one.
The 23:00 host proves detect-v2 does not damage a night the panels were never
part of; the 17:30 host proves it does not reach into an evening charge the
panels were present for but could not cover. The seasonal split the plan wanted
lives on the **11:00 host** instead, where the array is at its peak, the session
sits inside the day's exporting span, and summer and winter genuinely differ.
Only partial masking — a draw small enough to leave export running, or the
shoulder either side of it — reaches the flags at all, which is precisely the
case where the import register is understated rather than blank.

---

### 15.12 The forecast measured against a published method (backtest)

§15.4 changed the forecast and argued for the change. Nothing had tested the forecast
against anything outside itself. `analysis/forecast_backtest/` is that test: it scores
the bill forecast against the kNN quantile method of **Botman et al. (2025)** on the
sealed export, and it is the comparison §8 of the stack document listed as the one open
question needing a comparison rather than a check. It answers part of that question, and
it answers it in the opposite direction from the one §8.6 assumes.

**Read what the champion here is before reading any number below.** The champion is
**`hdd-linear-v2` refitted as at each snapshot date** — `build_bill_forecasts.project_forecast()`,
imported rather than copied — and **not** §15.4's `hdd-linear-v3`. The export ships v3, and
it visibly is v3: rebuilding v2's `is_forecastable` from the export gives 9,666 rows against
the 8,716 the export publishes, differing on 1,632, and the exported heating fits differ from
v2's rule by up to 32.854. So the run uses `--shipped-fit skip`, drops the look-ahead
comparison against the shipped fit, and says so at the top of its report. **§15.4's change is
therefore unscored.** Scoring it means pointing the harness at v3, which waits for a go.
Held-out cycles start on or after 2026-02-22, the middle of the served span
(2025-08-30 to 2026-08-17); k = 100.

**The gate was fixed before the runs, and it has three parts.** Writing it afterwards would
have let the thresholds follow the numbers, which is the failure mode a backtest exists to
avoid:

| Clause | Threshold | Result | Verdict |
|---|---|---|---|
| Is kNN's band the right width? | P10–P90 coverage between **70% and 90%** | **81.6%** over 6,492 snapshots | pass |
| Is kNN usable where the champion declines? | median APE ≤ the champion's own **+ 5 points** = **10.2%** (over its 4,217) | **6.3%** over 2,408 snapshots, 22 accounts | pass |
| Is kNN competitive head-to-head? | mean CRPS ≤ **1.10×** the champion's | **0.88×** over 4,084 snapshots | pass |

**The finding that matters is not in the gate. It is the champion's coverage.** On the 4,084
snapshots both models forecast, kNN's P10–P90 holds the final kWh **82%** of the time and the
champion's ±15% of the kWh still to come holds it **51%** — 56% early in the cycle, falling to
**42%** at 20+ days elapsed, when it should be easiest. A band quoted as ±15% is being read as
a range, and it contains the answer about half the time. ⚠️ **That contradicts what §8.6
implies and it is the correction to take from this section.** kNN buys its calibration with
width — median band 142 kWh against the champion's 79, $39.47 against $22.47 — so the honest
statement is that the champion's band is not narrow because the weather term explains the
variance, it is narrow because ±15% was chosen and the variance was never checked against it.

⚠️ **The head-to-head win is real but not separated.** 0.88× is the point estimate; resampling
accounts gives a 95% interval of **0.72 to 1.06**, which crosses 1, and kNN has the lower mean
CRPS on only **56% of the 34 accounts**. Read it as *no worse, at better calibration*, not as
better. With 34 accounts it could not be read as more than that.

**Where kNN clearly earns its place is the population the champion refuses.** 2,408 snapshots
across 22 accounts have no champion forecast at all — no weather fit, or not enough history —
and kNN serves them at 6.3% median APE and 81% coverage. Those accounts see "forecast
unavailable" today.

**A seasonal-neighbour variant was tried and rejected, and the reason is the useful result.**
The idea was to draw neighbours only from comparable weeks of the year, on the reasoning that
a July bill should be predicted from July. It passes coverage (88.4% over 1,515) and it passes
APE (8.7% over 508). It fails on **sharpness**: mean CRPS **1.84×** the champion's over the
1,007 snapshots they share, and **2.17×** the plain kNN's over the 1,515 both settings served.
Restricting the pool left too few neighbours to bound the interval, so every prediction
widened. It also stopped answering: a snapshot is served only where 100 weeks survive the
±30-day window, so **11,963** were dropped as *too few weeks* and the served count fell to
**57%** of the baseline's. The wide intervals and the refusals are one shortage showing up
twice.

**What it is, mechanically.** Two make targets — `make backtest-reproduce` (Stage 1, against
the paper's own baselines) and `make backtest-forecast` (Stage 2, head-to-head) — each of
which runs `test_knn_quantile.py` inside `dataeng/dbt` **first** and refuses to score if it
fails. `knn_quantile.py` is pure numpy with no I/O. The harness is offline apart from one
read-only `SELECT` on `KEA.KEA_GOLD.FCT_METER_READ`, holds no DML, and writes only into its
own `results/`. Each report names the export it scored — sealed **2026-09-17 23:34 UTC** — and
the champion it compared against, not the day it was run, for the reason `sprint4_evaluation/`
carries no date either.

⚠️ **Nothing is wired up.** `knn_quantile.py` is imported by its own test and its own harness
and by nothing else; no dbt model or DAG reaches it; `hdd-linear-v3`, `kea.bill_forecast` and
`mart_forecast_input` are untouched. Every published number is exactly what it was before this
ran. Whether the fallback gets served is the open item added to §12 on 27 Sep 2026, and it is
a product decision about what an unexplained band costs, not a further measurement.

**CI holds the leakage rule.** The 43 tests include the ones checking the harness cannot see
the day it is predicting, and they need numpy, which the dbt image carries and the CI host
does not, so the host test step (`make ci ONLY=test`, stdlib-only by its own contract) skips
them. Since #222 (29 Sep 2026) the required check's dbt job runs all 43 inside the image as
`make ci ONLY=backtest`, and that step fails on any skip, so a green check on a PR touching
this code does say the leakage tests ran and passed. Both make targets also run them first.
