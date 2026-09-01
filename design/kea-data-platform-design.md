# Kea Energy — Data Platform Design

**Owner:** Harvey Nan (Data Engineer)
**Scope:** three source databases → S3 → Snowflake/dbt → RDS PostgreSQL serving store → the agent

**DDL files, in run order:**

| # | File | Schemas | Written by | Regenerable? |
|---|---|---|---|---|
| 1 | `kea_source_schema.sql` | `src_billing`, `src_metering`, `src_catalogue` | Kea's existing systems | n/a — read only |
| 2 | `kea_serving_schema.sql` | `common`, `metering`, `billing`, `catalogue`, `kea` | the pipeline (`kea_loader`) | **yes** — re-run from bronze |
| — | `kea_deferred_schema.sql` | Sprint 3–4 additions | mixed | — |

**`app` is not here.** The `app` schema — conversation history, written by the agent (`kea_app`) at runtime — is owned by the application and defined in the application's repo, not this one. It is out of scope for the pipeline. It still appears throughout this document wherever the pipeline's boundary depends on it.

Column-level detail lives in those files as comments and is not repeated here. This document covers what SQL comments structurally cannot: how the pieces relate, and why the non-obvious choices were made.

**Companion:** `kea-data-engineering-stack.md` (platform and tooling decisions).

---

## 1. What this platform is for

From the brief:

> "Kea's billing system, half-hourly meter-data feed, and plan catalogue all exist as separate systems with no intelligent layer connecting them."

This pipeline **is** that connecting layer. It does not replace any source system. Its job is to extract from three independent databases, reconcile them into one coherent model, and land a serving copy the agent can query in under 500 ms.

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
- The reconciliation targets (§10.1) catch it: reproducing $248.07 and $50.54 is only possible with the correct tariff versions

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

**Governing rule, carried from the stack doc:** SQL in Snowflake under dbt by default. Spark handles only three named exceptions — raw parsing/reshaping, REFIT down-sampling, and the rating engine.

---

## 4. Extraction

Three independent jobs. Independence is the point: a billing extract failure must not block metering.

### 4.1 Per-source strategy

| Source table | Method | Watermark | Frequency |
|---|---|---|---|
| `src_catalogue.*` | Full snapshot | none | Daily |
| `src_billing.customers`, `.accounts` | Full snapshot | none | Daily |
| `src_billing.invoices`, `.invoice_lines` | Incremental | `ISSUE_DT` | Daily |
| `src_metering.networks`, `.connection_points`, `.meters`, `.registers` | Full snapshot | none | Daily |
| `src_metering.interval_reads` | Incremental | **`received_at`** — not `interval_start` | Daily |

**Small tables get full snapshots.** Under ~1M rows, a snapshot is simpler, self-correcting, and removes an entire class of missed-delete bugs. Plans, ICPs and registers all qualify.

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

---

## 5. Parsing: bronze raw → bronze parsed

One step, one job: reshape what the extract dropped into the conformed schema, and write Parquet that Snowflake can read as typed columns. No business logic — validation, quarantine and key resolution all happen in dbt.

Reshaping covers the mechanical differences between three systems built by different teams:

| From the source | To the conformed schema |
|---|---|
| `read_status 'A'` / `'E'` | `quality 'actual'` / `'estimated'` |
| `MED_DEPENDENT 'Y'` / `'N'` | `is_medically_dependent` boolean |
| `days_csv '1,2,3,4,5'` | `days_of_week smallint[]` |
| `CHG_GROUP 'NET'` | `charge_group 'network'` |
| `timestamp` (billing) vs `timestamptz` (metering) | `timestamptz`, UTC |

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

### 5.2 Where Spark's other workloads live

The stack doc names three exceptions to the SQL-in-Snowflake rule. Only the first appears in this pipeline:

| Exception | In this pipeline? |
|---|---|
| Raw parsing / reshaping | **Yes** — the meter-read extract above |
| REFIT 8s → 30-min down-sampling | No. A one-off Sprint 4 job feeding the E6 feasibility gate |
| The rating engine | No. Domain logic, run separately; its *output* lands in bills |

### 5.3 Schema contract vs file format — two separate choices

These are independent, and conflating them is easy:

| | Choice | Why |
|---|---|---|
| **Schema contract** | **Avro schema** (`KEA-047`) | Field names, types, nullability. Defines what a meter reading *is* |
| **File format on S3** | **Parquet** | How those rows are stored for analytical reading |
| **Wire format on Kafka** | **Avro binary** (Sprint 5) | How a single row is encoded in a message |

**One schema, two serialisations.** The batch path writes Parquet files that conform to the Avro schema. The Sprint 5 replay producer encodes the same schema as Avro binary for Kafka, because Schema Registry and per-message compactness require it. Downstream, neither can tell which path a row arrived by — which is the property that makes the ingestion paths interchangeable.

**Why Parquet rather than Avro files on S3.** Everything that reads bronze reads it analytically — Snowflake external tables scanning a subset of columns across many rows. Parquet is columnar, so it prunes unread columns and pushes predicates down; Avro is row-oriented and would force a full read of every row to get one field. At 2.45B rows that difference is not marginal. Avro's advantages — compact single-row encoding, embedded schema per message — matter on a Kafka topic, not in an analytical scan.

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
| **Timing skew** | Billing extracted at 02:00, metering at 03:00; an account created between them | Retry next run; only quarantine after N consecutive failures (default 3) |
| **Unknown plan code** | `accounts.PLAN_CODE` not in the catalogue extract | Quarantine. Cannot rate without the tariff, and a bill explanation without rating is worthless |
| **No plan version covers the period** | Code exists, but no row's `valid_from`/`valid_to` brackets the bill's period end | Quarantine the bill. **Do not fall back to the nearest version** — that is the silent-failure mode in §2.1.2 |

All four land in **one quarantine table with one `_reject_reason` vocabulary** — the same table that receives Spark parse failures and dbt validation rejections. Three rejection paths, one audit trail. Fragmenting it across systems is exactly what makes a data issue undiagnosable at 9am on demo day.

**Quarantine rates surface to the product.** If a household has 4% estimated reads, or its account is quarantined, the agent hedges or declines rather than answering confidently. This is the link most pipelines never make.

### 6.4 Gold model

```
dim_customer     dim_icp        dim_plan       dim_date
     │              │              │              │
     └──────────────┴──────┬───────┴──────────────┘
                           │
              fct_meter_read      fct_bill_line
              (grain: icp ×       (grain: bill ×
               register ×          line)
               interval)
```


`dim_date` carries the NZ holiday calendar and heating degree days joined from NIWA, so the explainer's weather comparison needs no join at query time.

### 6.5 Marts

| Mart | Grain | Consumer |
|---|---|---|
| `mart_account_context` | account | Postgres `kea.mart_account_context` |
| `mart_daily_usage` | icp × day | Postgres `kea.mart_daily_usage` |
| `mart_hourly_profile` | icp × month × hour × weekday | Postgres `kea.mart_hourly_profile` |
| `mart_bill_explanation` | bill | Postgres `kea.mart_bill_explanation` — drivers pre-decomposed in attribution order |
| `mart_forecast_input` | account × cycle × day | Postgres `kea.mart_forecast_input` — one snapshot per day, so a past forecast can be replayed |
| `mart_scale_test` | icp × interval | The 2.45B-row re-rate. **Never exported** — Snowflake-only |

Plus one table the pipeline writes about itself: **`kea.mart_load_log`**, one row per mart per load, carrying `started_at` / `completed_at` / `row_count` / `warehouse_run_id` / `status`. It makes "how fresh is this, and did it succeed" answerable without inspecting the data, and traces a serving row back through dbt to the bronze extract that produced it. The agent can check staleness before answering; ops can diagnose a partial load.

---

## 7. Export to PostgreSQL

Snowflake cannot write to Postgres directly. Two hops:

```
Snowflake  --COPY INTO-->  s3://kea-lake/export/{table}/  --loader-->  Postgres
```

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

All three are standalone additions — a `CREATE TABLE` with no migration and no downtime — and all three now live in `kea_deferred_schema.sql`.

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

**What the input carries.** Four groups, and the last is the one worth arguing
about:

- **Cycle position** — `days_elapsed` / `days_total`, `kwh_to_date`, `cost_to_date`.
- **Anchors to extrapolate from** — `baseline_daily_kwh`, and `same_period_last_year_kwh` / `_amount`. Two honest baselines: this household's own recent daily rate, and this household a year ago.
- **Weather** — `hdd_to_date` (fact), `hdd_expected_remaining` (forecast), and `heating_sensitivity_kwh_per_hdd`, the per-household conversion between them. This is what makes the projection better than `kwh_to_date / days_elapsed × days_total`. The sensitivity column is the same one `mart_bill_explanation` carries, so explaining a bill and forecasting one share a single notion of how weather-driven a house is.
- **Charges already certain** — `fixed_charges_period`. Daily charges are `daily_rate × days_total` on day one of the cycle; only the variable part needs forecasting. Pre-computing them keeps the model from attaching uncertainty to a number that has none, which is why the band on the output stays narrow.

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
| `src_billing.invoice_lines` | `billing.bill_line` | `QTY` + `LOSS_QTY` → `quantity` + `loss_kwh`, plus **generated** `billed_quantity`<br>`CHG_GROUP 'NET'` → `charge_group 'network'` |
| `src_metering.connection_points` | `metering.icp` + `metering.property` | One source table split into two; `address_full` and `climate_region` move to `property` |
| `src_metering.registers` | `metering.meter_register` | Reparented to `icp_id` (the `meter` table is deferred) |
| `src_metering.interval_reads` | `metering.meter_read_hh` | `icp_identifier` + `register_code` → **`register_id` — resolved**<br>`read_status 'A'/'E'` → `quality`<br>`version_no` → latest wins, then dropped |
| `src_catalogue.price_plans` | `catalogue.plan` | `network_code` text → **`network_id` — resolved**<br>`user_category 'LOW'` → `user_type 'low_user'` |
| `src_catalogue.tou_windows` | `catalogue.tou_band` | `days_csv '1,2,3,4,5'` → `days_of_week smallint[]`<br>`charge_scope 'NET'` → `charge_group 'network'` |
| `src_catalogue.rate_components` | `catalogue.tariff_component` | `window_id` → **`band_id` — resolved**<br>`rate_uom 'KWH'` → `basis 'per_kwh'`<br>`component_desc` → `component_name`. **This is the table the rating engine reads** |
| `src_metering.meters` | *(not exported)* | Used only to resolve `registers.meter_id` → `icp_id`, because the `meter` table is deferred from serving (§8.2.4) |
| `src_billing.payments` | *(deferred)* | Commented out at source. Sprint 2 needs only `invoices.PAYMENTS_APPL`; transaction detail arrives with hardship triage in Sprint 4 |

---

### 8.4 The weather and baseline columns

Both marts model a household the same way, and the model is worth stating once
because five columns across two tables are its coefficients:

```
daily kwh  ≈  baseline_daily_kwh  +  heating_sensitivity_kwh_per_hdd × HDD
              └── intercept ───┘      └────────── slope ──────────┘
```

Fitting it is the warehouse's job; nothing in the DDL enforces it. What the DDL
does is keep the two coefficients together, in both places they are used, so that
explaining a bill and forecasting one cannot drift apart.

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

#### `baseline_daily_kwh`

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

#### `heating_sensitivity_kwh_per_hdd`

The slope — kWh added per degree day. Every sample fixture carries `0.93`
(`generate_scenarios.py:51`) so that weather is never what distinguishes one
scenario from another, except in the season fixture where the degree-day swing is
the whole story.

The magnitude is itself a finding. Roughly: `~0.2` is a house that barely responds
to cold (wood or gas heat, off-meter — or an empty house); `~0.9` is a typical NZ
home with electric heating; `~2.5` is a poorly insulated house on resistive
heaters. The last case is a heat-pump or insulation conversation, reached from
meter data without a site visit.

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

#### `base_load_kwh` is a different thing entirely

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

#### Nullability is deliberate

`baseline_daily_kwh` and `heating_sensitivity_kwh_per_hdd` are both nullable. A new
connection has no history to fit; a household whose occupancy just changed has
history that no longer applies. NULL is the correct answer in both cases, and the
consumer is expected to fall back to a cruder projection and widen
`projected_low`/`projected_high` accordingly. The stored-bounds decision (§8.2.5)
is what makes "I know less about this customer" expressible rather than hidden.

The linearity is an approximation, flagged here in the same spirit as §2.2:
heat-pump efficiency falls in extreme cold, so sensitivity is not truly constant
across the range. It is good enough over the range NZ bills live in.

### 8.5 A forecast cycle, worked through

The columns above are easier to justify against real numbers, and there is now
a fixture that produces them: **`data/sample/09-bill_mid_cycle_forecast.sql`**.
Every figure below is what that file actually seeds, not an illustration.
Account 900801, period 2024-06-01 to 06-30, as at **10 June** — day 10 of 30,
with two completed bills behind it: the same period last year, and April as the
most recent mild month.

**Who writes what, and when.** Two DAGs (§9), which is the practical reason the
tables are split at all: `kea_daily_pipeline` writes the input at 02:00 NZT
after the quality gate, while `kea_forecast_refresh` writes the output and may
run more often, since it only reads the mart and does arithmetic. Input first,
always — the model never touches source tables.

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

### 8.6 The hourly profile, and what shape buys over volume

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

**The quality gate is a real gate.** If `dbt_test` or `soda_scan` fails, the export tasks do not run. Postgres keeps serving yesterday's data, which is correct-but-stale rather than fresh-but-wrong. For a product whose whole premise is trustworthy explanations, that trade is the right way round.

**Separate DAGs** for anything with a different cadence: `kea_weather_load` (NIWA, daily), `kea_forecast_refresh` (mid-cycle, could be more frequent than the main pipeline), `kea_scale_test` (manual trigger, Snowflake-only).

**Weather is warehouse-only.** NIWA data lands in the warehouse and is consumed by the marts, but is never exported to Postgres. The figures the agent needs — `temp_mean_c`, `heating_degree_days` in `mart_daily_usage`, and the period comparisons in `mart_bill_explanation` — are already denormalised in. Exporting the raw table would add one nobody queries.

---

## 10. Data quality

Three gates at different points:

| Gate | Where | Blocks |
|---|---|---|
| Schema validation | Spark parse | Malformed rows → quarantine |
| dbt tests | after `dbt_run` | Build fails → no export |
| Soda scans | after `dbt_test` | Freshness, volume, distribution drift → no export |

### 10.1 Tests that matter most

**Reconciliation.** Recompute two real bills end to end and assert the totals: $248.07 (flat, 31 days) and $50.54 (TOU + solar, 24 days). If the pipeline reproduces both from source data, the tariff model and rating logic are correct. This is worth more than any synthetic assertion.

**Plan version brackets the bill period.** For every bill, assert that the resolved plan version's `valid_from`/`valid_to` contains `period_end`. This is the §2.1.2 failure, and it is the one that produces a plausible wrong number rather than an error.

**Not expressible as a SQL constraint, so it must be a dbt test:** `tariff_component.band_id` must reference a `tou_band` whose `charge_group` matches the component's. Postgres cannot check across tables; nothing else will catch it; and getting it wrong silently mis-bands every TOU network line.

**Volume anomaly.** Meter reads per day should be within ±10% of trailing average. A collapse usually means a partial extract, which is worse than no extract because it looks like a real drop in consumption.

**Orphan rate.** Cross-system unresolved references should be near zero. A spike means the extracts are skewed in time, not that customers vanished.

### 10.2 An honesty note

LCL and REFIT are curated academic releases — already cleaned. They contain very few organic defects. So quality rules here are **built against a realistic NZ production defect profile** (interval gaps, negative reads, implausible spikes, estimated flags, DST transitions) and **validated by injecting those defects synthetically**.

That is the honest thing to say in review: the source data is clean; here is how the pipeline is proven against the defects production data would actually have.

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

**Backfill** uses the same DAG with `--start-date` / `--end-date`. No separate backfill code path — a second code path is a second set of bugs, and it is always the one that is under-tested.

---

## 12. Open items

| Item | Owner | Blocks |
|---|---|---|
| Write dbt staging models + schema.yml for all 12 source tables | Harvey | Everything downstream |
| Generate LCL/REFIT data into `src_*` shapes | Harvey | Extract cannot be tested without it |
| Confirm extract interface for all three sources (replica / API / file / CDC) | Adi + Harvey | §4 strategy |
| Confirm meter-feed delivery cadence | Adi (`KEA-501`) | Streaming relevance |
| Confirm revision mechanics — that a revision arrives as a new row with a higher `version_no` | Harvey | §4.1 `received_at` watermark, and the silver dedup that keeps the highest `version_no` |
| Agree quarantine `_reject_reason` vocabulary | Harvey + Stephen | §6.3, and it must be settled before three writers exist |
| Verify BRIN `pages_per_range` propagates to partitions | Harvey | Query performance |
| Decide Postgres retention for `meter_read_hh` | Harvey + Adi | Currently assumed 13 months |
| Execute against a live **PG17** instance | Data | All three files apply cleanly on 16.15, and Bill B reconciles to $50.54 there. 17 itself is still unverified, and the dev database currently carries files 1 and 2 — the deferred schema is not applied |
| Set `app.conversation.retain_until` policy | Adi + Product — **owner not settled** | Consumer Care decision, not a technical default |
| Split `alert` ownership before Sprint 3 builds it | Data | Mixed writer; see §13.1 |
| dbt test: `tariff_component.band_id` charge_group must match | Data | Not expressible as a SQL CHECK |
| Generate the full partition set | Data | Only three example partitions are declared |

---

## 13. Deferred

### 13.1 Mixed-ownership tables

Two tables in `kea_deferred_schema.sql` have mixed ownership and need splitting when built:

| Table | Problem | Options |
|---|---|---|
| `alert` | Created by the pipeline, but `sent_at` / `delivered_at` / `opened_at` / `acted_at` are written at runtime | Split into `alert` (loaded) + `app.alert_delivery`, or grant the app UPDATE on those columns specifically |
| `hardship_case` | Created by the agent on escalation, then updated by a human | Belongs in `app` |

Worth deciding when those features land, not now.

---

### 13.2 Deferred tables

`kea_deferred_schema.sql` holds 13 tables for Sprint 3 and 4, following the same namespace split. Note it cuts across features — `metering.solar_installation` is source data, while `kea.load_decomposition`, our inference from it, is ours.

**Two have mixed ownership and need splitting when built:** `alert` is created by the pipeline but its `sent_at` / `delivered_at` / `opened_at` are written at runtime; `hardship_case` is created by the agent on escalation and belongs in `app`. Worth deciding when those features land.

Run `kea_serving_schema.sql` first.

---

## 14. Summary of decisions

1. **Three independent extracts**, because three independent databases. A failure in one must not block the others.
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
