# Kea Energy — Data Platform Design

**Scope:** four source systems → S3 data lake → Snowflake + dbt → PostgreSQL serving store → the
customer-facing application.
**Companion:** [`kea-data-engineering-stack.md`](kea-data-engineering-stack.md) covers the tools,
versions and build order. This document covers what the pipeline does and why each non-obvious
choice was made.

Section numbers are stable: code comments, tests and migrations cite them (`design §6.3`).

| DDL file | Schemas | Written by | Rebuildable? |
|---|---|---|---|
| [`kea_source_schema.sql`](kea_source_schema.sql) | `src_billing`, `src_metering`, `src_catalogue`, `src_weather` | the source systems | n/a, read only |
| [`kea_serving_schema.sql`](kea_serving_schema.sql) | `common`, `metering`, `billing`, `catalogue`, `kea` | the pipeline (`kea_loader`) | **yes**, from bronze |
| [`kea_app_schema.sql`](kea_app_schema.sql) | `app` | the application (`kea_app`) | **no** |
| [`kea_deferred_schema.sql`](kea_deferred_schema.sql) | proposed tables, never applied | — | — |

The serving schema is applied by versioned migrations ([`migrations/serving/`](../migrations/serving/README.md));
`kea_serving_schema.sql` is the readable reference. Column-level detail lives in the DDL comments
and is not repeated here.

---

## 1. What this platform is for

The retailer's billing system, half-hourly meter feed and plan catalogue are separate systems with
nothing connecting them. This platform is that connecting layer: it extracts from each source,
reconciles them into one model, and publishes a serving copy the application can query in under
500 ms.

**Non-goals:** writing back to any source system, or becoming the system of record for billing,
metering or tariffs.

### 1.1 Why the files are split by writer

DDL is split by **who writes the data**, not by subject. That makes "can we rebuild this?" answerable
per table: anything the pipeline writes can be rebuilt from bronze; anything the application writes
cannot. Conversation history is the clearest case, so `kea_loader` has no grant on `app`, not even
`USAGE`, and a pipeline bug cannot reach it even by a fully-qualified name.

**The application computes nothing.** It reads pre-computed marts. Bill attribution, plan re-rating,
forecasting and aggregation all run in the warehouse, where they are version-controlled, tested and
identical for every customer.

### 1.2 What this project builds, and what it does not

This project builds the integration layer, not a billing system. Most of the serving store is a
**read model**: a projection of data mastered elsewhere.

| Namespace | System of record | `kea_app` | `kea_loader` |
|---|---|---|---|
| `common` | shared enum types | — | — |
| `metering` | the meter-data feed | SELECT | write |
| `billing` | the billing system | SELECT | write |
| `catalogue` | the plan catalogue | SELECT | write |
| `kea` | derived marts, forecasts and decisions | SELECT | write |
| `app` | conversation history | **write** | **no access** |

All six live in one PostgreSQL database; the namespace records where data is mastered.
`billing.account` is the join across three source systems (customer, connection, plan), and
`kea.mart_account_context` makes it a single-row lookup.

**Why a rating engine exists.** Billing rates only the plan a customer is on, after the fact. Two
things it never does: **counterfactual rating** (what would plan X have cost?) for plan comparison,
and **forward rating** (where will this cycle land?) for bill-shock warnings. The reconciliation
targets (§10.3) prove our engine agrees with the retailer's on the one case both can compute, before
it is trusted on the cases only ours can.

**Deliberately not in the serving store:**
- **Raw weather.** NIWA data is an input to the marts; the figures the application needs are already
  denormalised into `mart_daily_usage` and `mart_bill_explanation`.
- **Materialized views.** Postgres holds a 13-month subset of meter reads, so anything computed there
  would see only that subset. Daily and hourly aggregates are computed in the warehouse over the full
  history and loaded as tables.

---

## 2. Source systems

Four independent systems, each a separate extract with its own connection, credentials, schedule
and failure mode. They are modelled as four schemas in one database for development, but the
pipeline treats them as separate: four connections, no cross-schema joins at extract time.

| Source | Contains | Production volume (140k customers) | Change pattern |
|---|---|---|---|
| **Billing** | customers, accounts, invoices, invoice lines, payments, plan history | ~1.7M invoices/yr, ~12M lines/yr | daily, append-heavy |
| **Meter feed** | networks, connection points, meters, registers, interval reads | **2.45B reads/yr** | continuous; corrections re-issued with a higher `version_no` |
| **Plan catalogue** | price plans, TOU windows, rate components, term credits | hundreds of rows | rare, versioned |
| **Weather (NIWA)** | stations, daily observations | thousands of rows | monthly, third-party files |

Source names differ from serving names on purpose (`invoices` → `billing.bill`, `rate_components`
→ `catalogue.tariff_component`). Three systems built by different teams is the realistic case, and
reconciling the naming is staging's job (§8.3).

### 2.1 Cross-system references

The sources already record their associations: billing stores the **ICP number** and **plan code**
when an account is created. The warehouse conforms keys rather than discovering relationships.

| Relationship | Joined on |
|---|---|
| account → connection | `ICP_NUMBER` |
| account → plan | `PLAN_CODE` **and** the bill's period end |
| meter read → register | `icp_identifier` + `register_code` |

Sources store business keys, never denormalised copies, so each fact has one source of truth.
Surrogate keys are still worth minting: a register has no globally unique business key, and the
natural key `(icp_number, register_code, interval_start)` would put a text column in the primary key
of a 2.45B-row table.

Two problems remain, both inherent to separate systems.

### 2.1.1 Orphan references

No foreign key spans databases, so a stored `ICP_NUMBER` may not exist in metering (a typo, a
deletion, or extracts taken an hour apart). Orphans **fail loudly**: resolution returns nothing and
the account is quarantined (§6.3).

### 2.1.2 Temporal resolution — the dangerous one

A plan code has many versions (`UNIQUE (code, valid_from)`), because a tariff change inserts a row.
Resolving a bill to its plan is a lookup **as at the bill's period end**:

```sql
plan_code = invoice.PLAN_CODE
AND valid_from <= invoice.PERIOD_END
AND (valid_to IS NULL OR valid_to > invoice.PERIOD_END)
```

Get it wrong and a historical bill is re-rated against today's tariff. The arithmetic still balances
and the number looks plausible; it is simply wrong. On the full-scale run, a code-only lookup
mis-rated **2,878 of 11,534 bills (25%)**.

**Mitigations:** `dim_plan` is type-2 versioned (§6.4); `billing.bill.plan_id` points at the version
actually rated; a dbt test asserts every resolved version brackets its bill's period end; and the
reconciliation target (§10.3) only reproduces with the correct versions.

### 2.2 Assumptions, flagged as such

| Assumption | Rationale | If wrong |
|---|---|---|
| Extracts read a replica or snapshot, not CDC | simplest thing that works | CDC replaces §4's snapshot logic with change-stream consumption |
| The meter feed arrives in scheduled batches | NZ's EIEP file-exchange convention (stack doc §1.2) | streaming becomes the production path, not a demonstration |
| Cross-system referential integrity is not guaranteed | separate databases cannot enforce it | §6.3's orphan handling becomes dead code; harmless |
| Meter reads are revised and re-issued | common in retail metering | if append-only, reprocessing simplifies |

Design for the third regardless: separate databases cannot enforce integrity between them.

---

## 3. Architecture

![Kea data pipeline](figures/pipeline.png)

1. **Extract** — one Airflow DAG per source. Spark reads the meter feed; plain Python reads the rest
   (§5.1). Output lands in `s3://kea-lake/bronze/`, immutable and partitioned by NZ load date.
2. **Parse** — reshape to the conformed schema and write Parquet that conforms to the Avro contract
   (`bronze/parsed/`).
3. **Warehouse** — parsed Parquet is pushed into Snowflake `RAW`; dbt builds staging → silver →
   gold → marts, then dbt tests and Soda gate the run.
4. **Export** — marts and replicas are pulled from Snowflake into `s3://kea-lake/export/`.
5. **Publish** — one PostgreSQL transaction loads the export, computes the decisions (forecasts,
   recommendations, hardship, solar/EV estimates) and records the release.

**Governing rule** (stack doc §1.1): SQL in Snowflake under dbt by default. Spark is used for one
job, the meter-read extract and parse.

---

## 4. Extraction

Four independent jobs. Independence is the point: a billing failure must not block metering.

### 4.1 Per-source strategy

| Source table | Method | Watermark | Frequency |
|---|---|---|---|
| catalogue, customers, accounts, metering reference tables | full snapshot | — | daily |
| `invoices`, `payments` | incremental | `ISSUE_DT` / `paid_ts` (NZ wall clock) | daily |
| `invoice_lines` | full snapshot | — (no timestamp of its own) | daily |
| `interval_reads` | incremental | **`received_at`**, not `interval_start` | daily |
| weather | full snapshot | — | **monthly** |

**Small tables get full snapshots.** Under ~1M rows a snapshot is simpler, self-correcting and removes
missed-delete bugs. (`invoice_lines` crosses that threshold in production and will then need a
timestamp of its own.)

**A sealed snapshot partition is immutable.** A snapshot captures the table as it is when it runs,
so re-running an old load date would legitimately differ. Once `_SUCCESS` is written, a re-run is a
no-op unless `--force` is passed. That makes "re-running a load date changes nothing" true for all
seventeen entities.

**The meter-read watermark is `received_at`.** A correction to a three-week-old reading arrives today
as a new row with a higher `version_no`. A watermark on `interval_start` never sees it, so the bill
is explained from superseded data, silently. A watermark on arrival time catches every correction
with **no lookback window**. Silver then keeps the highest `version_no` per
`(icp, register, interval_start)`.

### 4.2 Landing in bronze

```
s3://kea-lake/bronze/{source}/{entity}/load_date=YYYY-MM-DD/part-*.parquet
s3://kea-lake/bronze/parsed/{source}/{entity}/load_date=YYYY-MM-DD/...
```

Bronze is **as received**: no cleaning, coercion or renaming, so downstream logic can always be
re-run against exactly what arrived without touching a production source. Every file carries
`_extracted_at`, `_source_system` and `_source_file`; an underscore prefix means pipeline metadata.

**`load_date` is a New Zealand calendar day.** The window is `[midnight D, midnight D+1)` in
`Pacific/Auckland`, half-open, and derived from the load date alone, never from the clock. The
failure it prevents is silent:

| Window for `load_date=2026-08-25` | Rows | Planted late correction |
|---|---:|---:|
| UTC day | 26,623 | **0 of 48** |
| NZ day | 26,925 | **48 of 48** |

There is no `received_at <= now()` bound and no stored watermark: either would make a replay depend
on when or how often it ran, and break backfill. An NZ day is 23, 24 or 25 hours, and the extract's
hourly partitions follow it.

**Parquet timestamps are written as `TIMESTAMP_MICROS`.** Spark 3.5 defaults to `INT96`, which carries
no timezone semantics; the contract says `timestamp-micros`.

---

## 5. Parsing: bronze raw → bronze parsed

One job: reshape into the conformed schema. No business logic; validation, quarantine and key
resolution happen in dbt.

| Source form | Conformed form |
|---|---|
| `read_status 'A' / 'E' / 'F'` | `quality 'actual' / 'estimated' / 'final'` |
| `MED_DEPENDENT 'Y' / 'N'` | boolean |
| `days_csv '1,2,3,4,5'` | `days_of_week smallint[]` |
| `CHG_GROUP 'NET' / 'ENE' / 'OTH' / 'CRD'` | `charge_group 'network' / 'energy' / 'other' / 'credit'` |
| naive billing `timestamp` | `timestamptz`, read as `Pacific/Auckland` |

Naive billing timestamps are NZ wall-clock dates printed on the bill. Reading them as UTC shifts
dates by up to 13 hours, enough to cross a plan-version boundary (§2.1.2). `'F'` keeps its own
symbol in the contract; mapping it onto `'actual'` would be business logic. The export maps it to
`validated` (§8.3).

### 5.1 Which extracts actually need Spark

| Extract | Volume | Tool |
|---|---|---|
| catalogue, weather | hundreds to thousands of rows | Python → Parquet |
| billing | ~12M lines/yr | Python → Parquet |
| metering reference tables | thousands | Python → Parquet |
| `interval_reads` | **2.45B rows/yr** | **Spark** (EMR Serverless in production) |

**Spark runs for one source, not four.** A distributed engine for every extract would pay cluster
start-up and operational surface on jobs that finish in seconds. The meter lane uses
`spark.read.jdbc(predicates=[...])` with one literal `received_at` range per predicate: aligned with
the watermark, evenly sized as history grows, and each partition independently re-runnable. Numeric
`partitionColumn` bounds cannot express a date range.

### 5.2 Where Spark's other workloads live

The governing rule names one other Spark exception, the production rating engine. It is domain logic
run outside this pipeline; only its output (bills) arrives here.

### 5.3 Schema contract vs file format — two separate choices

| | Choice | Why |
|---|---|---|
| Schema contract | **Avro schema** (`KEA-047` for meter reads) | names, types, nullability: what a reading *is* |
| File format on S3 | **Parquet** | columnar: prunes columns and pushes predicates down for analytical scans |
| Wire format on Kafka | **Avro binary** (planned streaming path) | compact per-message encoding with Schema Registry |

One schema, two serialisations, so downstream cannot tell which path a row arrived by.

**One contract is hand-written; sixteen are derived.** `KEA-047` encodes decisions (its `quality`
enum), so it is written by hand. The other sixteen are generated from `information_schema`
(`gen_avsc.py`), and every ingest runs `--check` to fail on drift. The generator refuses unmapped
types rather than guessing.

**Derived contracts carry the reshapes as overrides.** A contract taken straight from the source
would be satisfied by a parse job that did nothing.

**Conformance is checked against the Parquet footer**, so it costs no row reads and runs on every
partition. It caught Spark's `INT96` default. The same contract also generates the Spark schema, the
Snowflake `CREATE TABLE` and the Arrow types. Letting the warehouse infer types instead meant the
first load date defined the schema: `decimal(12,2)` amounts became `NUMBER(4,2)`.

---

## 6. Warehouse (Snowflake + dbt)

### 6.1 Layer conventions

| Layer | Naming | Materialisation | Rule |
|---|---|---|---|
| staging | `stg_<source>__<entity>` | view | 1:1 with source; rename and cast only |
| silver | `int_<domain>__<purpose>` | table | validation, quarantine routing, key resolution |
| gold | `dim_<entity>`, `fct_<event>` | table | conformed star schema, source-agnostic |
| marts | `mart_<consumer>` | table | denormalised for one consumer |

The double underscore keeps source and entity readable through the whole lineage graph.

### 6.2 Key resolution

**Rule 1: surrogate keys are stable across runs.** One append-only key map per entity
(`key_map_icp`, `key_map_customer`, `key_map_plan`, `key_map_register`); new business keys get the
next id and existing keys always resolve to the same one. Regenerating ids would break every
foreign key in the serving store.

**Rule 2: plan resolution is temporal.** `key_map_plan` is keyed on `(plan_code, effective_from)`,
not on `plan_code` (§2.1.2).

Gold and marts join only on surrogates. Business keys stay as attributes because customers quote
them.

### 6.3 Cross-system integrity

| Class | Example | Handling |
|---|---|---|
| **Orphan reference** | an account's ICP is missing from metering | quarantine; the application must not answer for that account |
| **Timing skew** | account created between the billing and metering extracts | retry; quarantine after 3 consecutive load dates miss it |
| **Unknown plan code** | an account's plan is not in the catalogue | quarantine; cannot rate without the tariff |
| **No plan version covers the period** | the code exists but no version brackets the period end | quarantine the bill; **never fall back to the nearest version** |

All rejections (parse failures, validation failures, integrity failures) land in **one quarantine
table with one `_reject_reason` vocabulary** ([`reject_reasons.json`](../pipeline/contracts/reject_reasons.json)).
Rows are routed, never dropped.

**Known gap: quarantine does not yet reach the serving store.** The warehouse holds all planted
defects with their reasons, but a quarantined account has no valid ICP or plan, and
`billing.account` requires both. The export therefore drops these accounts rather than inventing a
placeholder reference. Closing the gap is a schema decision (a nullable foreign key, or a separate
serving quarantine table), tracked in §12, not something to fix by loosening a constraint.

### 6.4 Gold model

```
dim_customer   dim_icp   dim_register   dim_plan (type 2)   dim_date
      └──────────┴───────────┬──────────┴──────────────────┘
               fct_meter_read             fct_bill_line
          (icp × register × interval)     (bill × line)
```

`dim_register` carries only **unambiguous** registers. A read has no `meter_id`, so resolving a
register goes ICP → meters → registers. Filter to meters live at the reading's instant; if more than
one survives, quarantine rather than pick. Picking fans out every reading at that ICP (measured:
+750 rows and +447 kWh on one household, with no error).

Heating degree days are regional, so they live in `fct_weather_daily` at `(date, region)` grain
rather than on `dim_date`; the marts denormalise them.

### 6.5 Marts

| Mart | Grain | Purpose |
|---|---|---|
| `mart_account_context` | account | one-row account lookup |
| `mart_daily_usage` | ICP × NZ day | usage, weather, base load, completeness |
| `mart_hourly_profile` | ICP × month × hour × weekday | the bill's "average day" chart |
| `mart_bill_explanation` | bill | bill-change drivers, pre-decomposed (§8.5) |
| `mart_forecast_input` | account × cycle × as-at day | dated forecast inputs, replayable (§8.6) |
| `mart_daily_detected_load` | ICP × NZ day | detected EV and timer loads (§15.2) |
| `mart_charging_session` | session | EV-pattern runs (§15.2) |
| `mart_load_log` | mart × load | freshness and lineage of every load |

`mart_load_log` makes "how fresh is this, and did it succeed?" answerable without inspecting the data.

**Deferred: `mart_scale_test`.** The 2.45B-row re-rate is the retailer's production projection; the
demo warehouse holds about 10M reads. Running it would mean synthesising ~250× the data to measure
Snowflake rather than this pipeline. It is never exported, so nothing waits on it.

---

## 7. Export to PostgreSQL

Snowflake cannot write to Postgres directly, and as SaaS it cannot reach a local MinIO either. So
the export **pulls**: a local process runs one query per target table
([`pipeline/export/`](../pipeline/export/)), receives Arrow batches and writes Parquet to
`s3://kea-lake/export/`. A loader then publishes it.

### 7.1 Shape decision

| Namespace | Shape | Source |
|---|---|---|
| `metering`, `billing`, `catalogue` | faithful replica, source-shaped | gold |
| `kea` | denormalised, query-shaped | marts and decision jobs |
| `app` | runtime writes | never loaded |

Replicas stay source-shaped because re-rating needs the exact tariff structure (plan → TOU band →
tariff component). Only `kea` denormalises.

### 7.2 Per-table strategy

| Target | Strategy |
|---|---|
| catalogue, metering reference tables, customers, accounts | replace inside the publication transaction |
| bills, bill lines | replace by period |
| `metering.meter_read_hh` | **monthly partition swap**; 13 months for the cohort, full span for the nine fixture households |
| `kea.mart_*` | replace |
| `kea.bill_forecast`, decision tables | append a new generation, stamped with one `computed_at` |
| `app.*` | **never** |

Production would keep daily aggregates in Postgres and fetch half-hourly detail from the warehouse
on demand. Thirteen months rather than twelve keeps year-on-year comparison possible at every point
in the year.

### 7.3 Load mechanics

**The unit of atomicity is the whole foreign-key-bound set, not one table.** Replacing a parent table
while a child still references it violates its foreign key, and no delete order avoids that in
general. So the publisher stages every table, deletes children first, inserts parents first and
commits **once** with constraints deferred. The store can never be observed half-applied, and the
decisions computed from the new data commit in the same transaction.

Meter reads are never deleted broadly. Each month is built beside the table with a matching `CHECK`
constraint and attached, so `ATTACH PARTITION` is metadata-only.

### 7.4 Two details that are easy to get wrong

**Load order follows the FK graph**, held in one ordered list (`kea_pipeline.serving.TABLES`) that both
the exporter and the publisher read. Three dependencies are easy to miss:
- `catalogue.plan` → `metering.network`: plans are network-specific, so the two are not siblings.
- `billing.bill_line` → `catalogue.tariff_component`: the whole catalogue must be current first.
- `billing.customer` has no dependencies and can load first.

**`synced_at` is set per table at commit time**, not per row with `DEFAULT now()`, so "how fresh is this
replica?" has one answer.

---

## 8. The domain model

### 8.1 Where the design came from

Three real NZ electricity bills, cross-checked against the product brief. The bills supplied the
entities and the arithmetic: charge groups, rate bases, TOU bands, line losses, GST and the ICP and
plan-code structure. The brief supplied what a bill cannot show: half-hourly data, weather and
forecasting.

Two rules were found only by **reconciling the totals**, not by reading the labels (§8.2.1, §8.2.2).
Stopping at the labels would have shipped both bugs.

### 8.2 The decisions worth knowing

### 8.2.1 TOU bands are scoped per charge group

On the same plan and period, network and energy charges split the same consumption differently:

| Group | Bands | Split |
|---|---|---|
| Network | 2 | 38.22 peak / 142.22 off-peak = 180.44 kWh |
| Energy | 3 | 34.08 peak / 74.96 off-peak / 71.39 shoulder = 180.43 kWh |

Keying bands to the plan alone would mis-band every TOU network line. Re-rating a candidate plan is
therefore one banding pass **per charge group**.

### 8.2.2 Customers are billed on metered volume plus losses

The bill prints "34.08 kWh, 1.74 kWh losses @ 0.26" and charges $9.31:

```
34.08 × 0.26          = 8.86   does not match
(34.08 + 1.74) × 0.26 = 9.31   matches
```

`billed_quantity` is a generated column so no consumer can forget the losses, which would undercharge
~5% on every import line.

### 8.2.3 Solar export is modelled by direction, not by sign

Registers and tariff components carry `direction`; `kwh` and `rate` stay non-negative. Relaxing a
`CHECK` on the meter-read table later would mean revalidating every partition of a 2.45B-row table.

> **The rule:** decisions that touch the big table are made up front. Standalone tables are added
> when the feature is built.

### 8.2.4 Three tables were cut from the first release

`meter`, `payment` and `account_plan_history` were cut because no first-release feature read them.
Each is a standalone addition with no `ALTER` to an existing table. `payment` and
`account_plan_history` have since shipped; `meter` is still deferred, and registers reference the ICP
directly.

### 8.2.5 The forecast is two tables, split by writer

| Table | Key | Writer | Answers |
|---|---|---|---|
| `kea.mart_forecast_input` | `(account, period_start, as_at_date)` | dbt | what do we know about this period so far? |
| `kea.bill_forecast` | `(account, period_start, forecast_date)` | the forecasting job | what will the bill be? |

Inputs are dated snapshots, never overwritten, so any past forecast can be replayed with a new model
version. Bounds are stored and checked (`projected_low <= projected_amount <= projected_high`), so an
incoherent interval fails the insert instead of reaching a customer.

There is deliberately **no foreign key** from forecast to input: the input is a rebuildable mart, and
a foreign key would let a rebuild cascade into forecast history.

> **The rule:** a mart may be rebuilt at any time, so nothing durable may depend on one by reference.

### 8.3 Source-to-serving mapping

Most of the mapping is renaming and type coercion in staging. The rows marked **resolved** are the
integration.

| Source | Serving | Transformation |
|---|---|---|
| `src_billing.customers` | `billing.customer` | `MED_DEPENDENT` → boolean |
| `src_billing.accounts` | `billing.account` | `ICP_NUMBER` → **`icp_id` resolved**; `PLAN_CODE` → **`plan_id` resolved by period end** (§2.1.2) |
| `src_billing.invoices` | `billing.bill` | `period_days` generated |
| `src_billing.invoice_lines` | `billing.bill_line` | quantity + losses → generated `billed_quantity`; all four charge groups mapped, including `CRD` → `credit` |
| `src_metering.connection_points` | `metering.icp` + `metering.property` | one table split in two |
| `src_metering.registers` | `metering.meter_register` | reparented to the ICP |
| `src_metering.interval_reads` | `metering.meter_read_hh` | → **`register_id` resolved**; latest `version_no` wins; `final` → `validated` |
| `src_catalogue.price_plans` | `catalogue.plan` | `network_code` → **`network_id` resolved** |
| `src_catalogue.tou_windows` | `catalogue.tou_band` | `days_csv` → `smallint[]`; scope → `charge_group` |
| `src_catalogue.rate_components` | `catalogue.tariff_component` | `window_id` → **`band_id` resolved**; the table the rating engine reads |

### 8.4 The four columns that model a household, and are easily confused

Both explanation and forecasting model a household the same way:

```
daily kWh  ≈  baseline_daily_kwh  +  heating_sensitivity_kwh_per_hdd × HDD
```

| Column | Table | Used by |
|---|---|---|
| `heating_degree_days` | `mart_daily_usage` | both, summed per period |
| `heating_sensitivity_kwh_per_hdd` | both marts | §8.5 **and** §8.6: one number, stored in both |
| `baseline_daily_kwh` | `mart_forecast_input` | §8.6 only |
| `base_load_kwh` | `mart_daily_usage` | §8.5 and fault detection (§8.7) |

**Heating degree days** = `GREATEST(16.0 − mean_temp_c, 0)`. Usage is roughly linear in degree days,
not in temperature: the floor collapses the no-heating range to zero. Cooling is not modelled, so
summer air conditioning lands in `delta_unexplained` rather than being misattributed.

**`baseline_daily_kwh`** is the weather-stripped intercept: each past day's kWh minus its HDD ×
sensitivity, averaged. A plain historical average already contains past heating and would count
weather twice in a forecast.

**`heating_sensitivity_kwh_per_hdd`** is the slope, fitted per household **at monthly grain**. A
daily fit was noise, because the cohort is London metering re-timed to NZ seasons: the seasonal
envelope matches Auckland, the day-to-day weather does not.

| Fit | Households | Median slope | Median R² |
|---|---:|---:|---:|
| daily | 499 | 0.45 | 0.069 |
| **monthly** | 446 | **0.74** | **0.467** |

> **The rule:** fit an estimator at the grain it is spent at. Bills spend it per period.

The fit's R² is published and gated (≥ 0.30): a slope with no explanatory power must not price a
weather driver. The same stored slope serves explanation and forecasting, so the two cannot
contradict each other about one household.

**`base_load_kwh`** is a different thing entirely: the day's quietest half-hour × 48, the household's
always-on draw. A stuck water-heater element lifts it and keeps it lifted; a new evening appliance
leaves it untouched. That is what separates a fault from a new appliance.

| | `baseline_daily_kwh` | `base_load_kwh` |
|---|---|---|
| Computed | weather-stripped average over many days | quietest half-hour of one day × 48 |
| Answers | what does a mild day cost? | what draws continuously? |
| Used for | forecasting | explanation, fault detection |

**On a generating ICP the minimum is taken overnight (22:00–05:00 NZ).** Behind a solar meter, import is
`max(0, load − pv)`, so the all-day minimum was 0 on 97% of complete days and measured the sun, not the
house. No NZ roof generates in that window on any day of the year. Non-generating ICPs still use all
48 half-hours; changing them is an open decision (§12).

Both fitted coefficients are nullable: a new connection has no history, and NULL tells the consumer to
fall back and widen its range rather than pretend to know.

### 8.5 How a bill increase is attributed

`mart_bill_explanation` decomposes the change between two closed bills into named drivers plus a
residual, **in a fixed order**, each measured after the previous ones are removed:

```
rate = (prior_amount − daily_rate × prior_days) / prior_kwh        prior period's variable rate

delta_from_period_length = (days − prior_days) × (prior_kwh / prior_days × rate + daily_rate)
delta_from_weather       = sens × (hdd − prior_hdd) × rate
delta_from_base_load     = (base − prior_base) × days × rate
delta_from_detected_loads  (§15.5)
delta_from_tariff        = this period's kWh re-rated under both plan versions
delta_unexplained        = amount_delta − (all of the above)
```

**Period length goes first.** A 43-day bill looks like a 40% consumption rise and is not one. Pricing
at the prior period's own variable rate (fixed charges removed first) makes each driver checkable
against the bill the customer already has.

Worked on the cold-snap fixture ($97.78 → $163.19): period −$3.15, weather **+$61.92**, base load
+$15.98, tariff $0.00, unexplained −$9.34. A test asserts the drivers sum to `amount_delta` exactly.

**`delta_unexplained` is a feature.** "I can account for $34 of the $61 increase" is honest; inventing a
driver to close the gap is not. **`is_explainable`** gates the claim: measured drivers must cover at
least 60% of the movement, the heating fit must pass, the period must have no estimated reads, and
both periods must have complete weather. Missing weather means the bill is not explained; gaps are
never filled silently.

**Limitations.** The decomposition is sequential, so where two drivers interact (a longer *and* colder
period) the overlap goes to whichever comes first; the residual keeps the total exact. And where the
plan version changed, `delta_from_tariff` is **NULL, not 0**, until the rating engine is expressed in
SQL (§12): a zero would push a real tariff change into `unexplained` and make the decomposition look
complete.

### 8.6 A forecast cycle, worked through

Fixture 09 seeds one cycle: account period 1–30 June, as at 10 June.

```
fixed so far        = 18.00 × 10/30                 = 6.00
rate                = (62.19 − 6.00) / 179.90 kWh    = 0.3123 $/kWh
remaining baseline  = 20 days × 9.2002              = 184.00 kWh
remaining weather   = 187.90 expected HDD × 0.93    = 174.75 kWh
projected_amount    = 18.00 + (179.90 + 358.75) × 0.3123 = $186.24
band                = ±15% of the remaining variable cost  = $169.43 – $203.05
```

Three design points:
- **Split, not extrapolated.** Separating baseline from weather lets a forecast warm spell pull the
  projection down on its own.
- **Only the unknown is uncertain.** The metered kWh and the fixed charges are known, so the band
  applies to the remainder only. Applying ±15% to the whole bill would make it 66% wider for no reason.
- **Fact displaces forecast.** `hdd_to_date + hdd_expected_remaining` is constant across a cycle; a
  test asserts it. Expected HDD comes from 30-year climate normals per region and day of year.

**The ±15% band is not a calibrated interval.** Scored against a published method (§15.12), it
contains the final usage only **51%** of the time, and **41%** in the last ten days of a cycle. The
narrowing is real arithmetic; the width was a judgement that the data does not support.

If the quality gate blocks a run, no new input row is written and the model forecasts from yesterday's
inputs: correct but stale.

### 8.7 The hourly profile, and what shape buys over volume

`mart_hourly_profile` is the bill's printed "average weekday" chart, made queryable. Its grain is
`(icp, month, hour, is_weekday)`:
- **per month**, because shape moves with the season;
- **hourly**, matching the 24-bar chart; half-hour detail lives in `mart_daily_usage`;
- **weekday vs weekend**, because averaging them creates a shape neither day has.

`sample_days` (NOT NULL) records how much evidence each average rests on. Import and export are
separate non-negative columns (§8.2.3).

**Shape separates what volume cannot.** A faulty element lifts every hour of the day; a new dryer
lifts only 21:00–23:00. The pipeline can describe the shape; it never names an appliance, because
half-hourly data cannot identify one.

---

## 9. Orchestration

Airflow 3.3 locally; MWAA is the AWS target.

| DAG | Schedule | Does |
|---|---|---|
| `kea_extract_billing`, `_metering`, `_catalogue` | daily | extract and parse one source; emit a bronze asset |
| `kea_extract_weather` | **monthly** | NIWA republishes the full record monthly |
| `kea_daily_pipeline` | **asset-driven**: all three daily sources landed | `build-warehouse` → `publish-serving` → `dashboards` task groups |
| `kea_forecast_refresh` | manual | rebuild the forecast mart and publish a release |

**Separate extract DAGs make independence a fact, not a convention.** One DagRun has one failure state,
so a combined DAG would let a billing failure block metering however the tasks were wired.

**Asset scheduling beats a fixed hour.** A 02:00 schedule is a guess about how long extracts take; a
wrong guess reads yesterday's partition and reports success.

**The quality gate is an edge.** `dbt test` and the Soda warehouse scan sit between build and publish,
so a failure leaves publishing `upstream_failed` and Postgres keeps serving the previous release:
correct but stale beats fresh but wrong.

**The publish is one task.** Splitting the FK-ordered load across tasks would give each table its own
commit and break the atomicity in §7.3.

**Load dates in Airflow 3.** `@daily` fires with a zero-length data interval, so a DAG that takes the
fire time's date extracts a day that has barely begun. Every task derives its load date from `run_after`
through one function (`kea_pipeline.window.load_date_for_fire_time`), stepping back one **calendar**
day, never 24 hours, which would skip DST days.

**Backfill uses the same DAG.** Asset-scheduled DAGs cannot use `airflow backfill`, so `load_date` is a
DAG param and a range becomes one triggered run per date (`make run-pipeline FROM=… TO=…`). There is
no second code path.

Spark, dbt and Soda run as sibling containers via `DockerOperator`, keeping their dependencies out of
the Airflow image. Each task gets only its own source's credentials.

---

## 10. Data quality

| Gate | Where | On failure |
|---|---|---|
| Contract conformance | after parse | the ingest fails |
| Isolation and bronze checks | end of the Lake step | the ingest fails |
| dbt tests | after `dbt run` | nothing is published |
| Soda warehouse scan | after dbt tests | nothing is published |
| `assert_serving` | after the publish, as the loader | the run fails |
| Soda serving scan | after `assert_serving` | reported |

### 10.1 Tests that matter most

**Reconciliation.** Recompute a real printed bill end to end from metered half-hours: **$50.54** (TOU,
solar export, 24 days). This proves the tariff model and rating logic better than any synthetic
assertion (§10.3).

**Plan version brackets the bill period.** The §2.1.2 failure produces a plausible wrong number rather
than an error, so it gets its own test.

**TOU band charge group matches the component's.** Postgres cannot check this across tables, and
getting it wrong mis-bands every TOU network line. A singular dbt test covers it.

**Volume and freshness, adapted to the source.** The source is a fixed historical extract, so the
textbook checks (freshness of readings, ±10% volume per load date) would be red every night.
Freshness is measured on the **load**; the ±10% volume band on consumption days where the cohort is
fully present. A gate that is always red gets switched off.

**Orphan rate** is banded above the planted baseline, so it fires on a change of regime (skewed
extracts), not on the five defects planted on purpose.

### 10.2 An honesty note

Low Carbon London is a curated academic release with few organic defects. The quality rules are
built against a realistic NZ production defect profile (gaps, spikes, stuck meters, estimated and
final reads, duplicates, orphan references, DST days) and proven by **injecting those defects
synthetically**. The answer key lives in `gen_audit`, which no extract role can read.

The same applies to the advanced analytics (§15): London households in 2011–14 had no EVs, so EV
sessions, water-heater timers and payment slides are planted with known answers and scored. That
proves the pipeline finds what was planted, not that real households behave that way. Planting
candidates are screened against their own load first, so a detector is never scored against its own
noise.

### 10.3 Reconciliation targets

**Bill B — TOU with solar, 24 days, plan `NZSZ_ZERO_STD_FXTOU_240901_T1`**

| Group | Amount (ex GST) |
|---|---|
| Network | $29.22 |
| Energy | $21.10 = import $27.70 − export $6.60 |
| Other | $0.22 |
| **Total** | **$50.54** |

It exercises a partial period, three TOU bands, per-charge-group band scoping, line losses, export
credits and a levy on import only.

**The criterion allows one cent.** Ten of eleven lines reproduce to the cent. The levy line contradicts
itself: its printed rate (0.0013) and printed amount ($0.22) cannot both hold for 180.43 kWh. The
catalogue stores the **printed rate**, not a back-solved one, because tuning an input until the output
matches would defeat the test.

**Bill A** ($248.07, flat, 31 days) is kept as documented evidence only: no source data exists behind
it, so reconciling it would only test an invention.

---

## 11. Failure and recovery

| Failure | Behaviour |
|---|---|
| One extract fails | the others proceed; publishing waits; the app serves the previous release |
| dbt test or Soda gate fails | nothing is published; alert |
| Publish fails at any statement | the transaction rolls back; every table is unchanged |
| Bad data reached the serving store | re-run from bronze; never re-extract from a source |

**Reprocessing is idempotent by construction.** Two materialisations of a load date are identical when
their **row multisets over every column except `_extracted_at`** are equal. The digest is a *sum* of
per-row hashes: order-independent, and unlike XOR, a duplicated row does not cancel itself out. The
check also fails if `_extracted_at` did not move, because a match where nothing re-ran proves nothing.

**Backfill** reuses the same DAG and scripts. A second code path is a second set of bugs, and always
the under-tested one.

**A span move is not a backfill.** When the demo's date span moves, new rows land on *different*
dates and the old rows survive every per-date step:

```
drop + recreate RAW   total          <- the only total operation in the chain
extract               per partition  <- sealed partitions are skipped unless forced
stage                 per load date
partition swap        per month
dbt delete+insert     per key
materialized views    never refresh on their own
```

The last span move surfaced five bugs of this shape, and four exited 0. Hence two rules: reset RAW by
**DROP**, not TRUNCATE, and force re-extraction of sealed partitions. The procedure is stack doc §7.

---

## 12. Open items

| Item | Blocks |
|---|---|
| Confirm the production extract interface (replica, API, file, CDC) and meter-feed cadence | §4 strategy; whether streaming matters |
| Quarantined accounts cannot reach the serving store | §6.3's promise that the app declines rather than answers. Needs a schema decision |
| `delta_from_tariff` is NULL where the plan version changed | a complete decomposition (§8.5). Needs the rating engine in SQL |
| Whether `base_load_kwh` uses the overnight window for every ICP | §8.4. Changes every published base load; needs a rebuild and re-measurement |
| A second import register would split `base_load_kwh` from the detection floor | §15.2. Today every ICP has one import register |
| Detected-load band containment: 52.2% against a 90% target | §15.10. Needs real EV metering to choose between a narrower band and a tolerance |
| Whether the kNN fallback serves accounts the forecast declines | §15.12. A product decision; the evidence exists |
| Score `hdd-linear-v3` in the backtest | §15.12 scored v2 |
| NZ public-holiday calendar | `dim_date.is_public_holiday` is NULL (unknown), not false |
| Nearest-station weather fill | specified in [`niwa-weather-source.md`](../dictionary/niwa-weather-source.md); gaps are currently NULL |
| Immutable export release prefixes and manifest | concurrent export and publish (stack doc §6.4) |
| Verify BRIN `pages_per_range` propagates to partitions | query performance |

---

## 13. Deferred

### 13.1 Mixed-ownership tables

`hardship_case` (created on escalation, then updated by a person) belongs in `app` when built.

`kea.alert` was the other candidate and is settled: it is **append-only**. The pipeline inserts and
never rewrites, so the application's updates to `sent_at` / `delivered_at` / `opened_at` / `acted_at`
are safe. It must never be added to `serving.TABLES`, which replaces its tables on every publish.

### 13.2 Deferred tables

[`kea_deferred_schema.sql`](kea_deferred_schema.sql) holds proposed tables that are never applied.
Adopting one means writing the next serving migration and updating `kea_serving_schema.sql`.
`metering.solar_installation` (source data) and `kea.load_decomposition` (our inference) remain
deferred; §15 delivered its features from metered evidence instead.

---

## 14. Summary of decisions

1. **Four independent extracts** for four independent systems; one failure never blocks another.
2. **Keys are conformed, not discovered.** Append-only key maps keep surrogates stable.
3. **Plan resolution is temporal.** A bill is rated against the version in force at its period end.
4. **Cross-system integrity is handled explicitly.** Orphans quarantine; the app declines rather than
   guesses.
5. **One quarantine table, one reason vocabulary**, for every rejection path.
6. **Bronze is immutable**, so reprocessing never touches a production source.
7. **Spark for one job.** Only the meter-read extract needs distributed compute.
8. **Replicas stay source-shaped**; only `kea` denormalises.
9. **Postgres holds a 13-month subset** of meter reads; the warehouse holds the history.
10. **Quality gates block publishing.** Correct but stale beats fresh but wrong.
11. **The loader cannot reach `app`.** Conversation history is the one thing no re-run recovers.
12. **Forecast inputs and outputs are separate tables**, with no foreign key between them.
13. **Detected loads are daily evidence with a low and high bound**, not an appliance breakdown.
14. **New models are accepted against planted answers on thresholds written first**, and misses are
    recorded as misses.
15. **Models are also measured against a published method**, and the measurement may contradict the
    design (§15.12).

---

## 15. Advanced analytics: detected loads, solar and EV, hardship early warning

Three stretch features: an appliance-level breakdown, solar and EV what-ifs, and a hardship early
warning. Schema: migrations V005–V008. §15.1–§15.9 are the design, §15.10 the measured result,
§15.11 a supplement for households with both panels and an EV, and §15.12 an external backtest of
the bill forecast.

### 15.1 What is measured and what is estimated

| Table | Kind | Writer | Load |
|---|---|---|---|
| `kea.mart_daily_detected_load` | **measured** from half-hourly import | dbt → export | replace |
| `kea.mart_charging_session` | **measured** EV-pattern runs | dbt → export | replace |
| `kea.solar_estimate` | **estimated** what-if, priced on real import | job in the publication | append |
| `kea.ev_scenario` | **estimated** what-if, priced on real import | job in the publication | append |
| `kea.hardship_outlook` | **projected** arrears date and amount | hardship job | append |

Every figure is computed in the pipeline and published in the one publication transaction. Planted
test answers (`gen_audit`) never reach serving.

### 15.2 Detected loads are evidence, not a breakdown

- **Per NZ day**, so any billing period is a plain sum. One row per assessed day, zeros included;
  no row means "not assessed".
- **Always a range.** Per half-hour of a run: `low` = import − the mean import just before and after
  the run; `high` = import − the day's floor (`base_load_kwh / 48`). The floor belongs to base load,
  so nothing is counted twice. `low` is not a guaranteed minimum (§15.10).
- **Classification**, first match wins: a cold-month-only night block stays with heating; ≥ 2
  half-hours at ≥ 5 kW is `ev / high_power`; a block starting in the same slot on ≥ 60% of nights is
  `timed_night`; a long steady 1.8–4 kW run is `ev / low_power` at medium confidence at most.
- **Wording:** "a load with the charging pattern of an EV", never "your EV".

Today every ICP has one import register, so the floor and the detection series come from the same
register. A controlled second register would split them (§12).

### 15.3 Daily completeness counts each import half-hour once

`interval_count` counts distinct import half-hours, not rows across registers. Previously a solar
household read 96 on a 48-half-hour day and a day with gaps could pass as complete.

### 15.4 The heating fit is net of detected loads; forecast v3 adds them back

A winter-heavier water heater or a newly arrived EV would otherwise inflate the heating slope. The fit
uses daily import minus the *low* detected EV and timer kWh (the high bound would remove ordinary
evening use). `hdd-linear-v3` adds the recent detected load back:

```
remaining kWh = remaining days × (baseline_daily_kwh + detected_load_kwh_day) + expected HDD × slope
```

`detected_load_kwh_day` is the mean over assessed days in the last 28; with fewer than 7, the account
is not forecastable.

### 15.5 The detected-load bill driver

Attribution order becomes: period length, weather, base load, **detected loads**, tariff, unexplained.

```
delta_from_detected_loads = (detected_kwh_day_this − detected_kwh_day_prior) × days × rate
```

Without it, a new EV lands in `delta_unexplained`.

### 15.6 Solar and EV what-ifs

- **Solar** (households without panels): the last 12 complete months of real import; 3, 5 and 7 kW
  systems; regional low/high yield by month, spread over daylight half-hours; netted per real half-hour
  and priced on the current plan with its buy-back rate. Payback is simple and given as a range.
- **Battery** (13.5 kWh, 5 kW, 90% round trip): walked through the real half-hours in order; surplus
  charges it before any export. On plans with more than one import rate, a `grid_charge` variant also
  fills from the grid in the cheapest half-hours, leaving room for the previous day's surplus. A
  battery row that saves less than panels alone is published, not suppressed.
- **EV:** `add_ev` prices a year of assumed charging, evening or overnight, on each plan;
  `shift_detected` moves detected sessions to 23:00; `solar_soak` places charging in the day's
  highest-export half-hours (§15.11).
- Every row names its `assumptions_version` and metered basis window; sources are cited in
  `estimate_assumptions.py`.

### 15.7 The hardship early warning still has no score

No hardship score is produced. The warning is three facts (`payment_drift`,
`bill_above_usual_payment`, `first_shortfall`) and, when drift or a shortfall is present, a
`kea.hardship_outlook` row: the first projected date the balance meets the arrears definition (a
bill ≥ 5% unpaid 30 days after its due date), the projected amount, and the arithmetic in words.

- Payments are summed **as at the assessment date**, so a replay never sees future payments.
- "Usual" is the median amount paid on the last six paid bills (at least three).
- An outreach needs `bill_above_usual_payment` together with drift or a shortfall. Early outreach
  appears in the staff queue only; nothing is sent to the customer.

### 15.8 Schema rules V005 follows

- Every name is schema-qualified.
- New foreign keys are `DEFERRABLE INITIALLY IMMEDIATE`, because the publication replaces parents with
  constraints deferred.
- Row rules are `CHECK` constraints; cross-row rules (no overlapping sessions) are dbt or Soda checks.
- Job tables default `computed_at`, so one publication's rows share its transaction time.

### 15.9 What the backend reads

[`serving_read_contract.json`](serving_read_contract.json) lists every serving column the application
reads. The `ServingReadContract` test (`make ci ONLY=serving-db`) fails if a migration renames, drops or
retypes one, if `kea_app` cannot select it, or if a served foreign key is not deferrable.

### 15.10 What the rebuild measured

Method and every threshold were written **before the first run**
([`analysis/sprint4_evaluation/`](../analysis/sprint4_evaluation/README.md)); changing a threshold
afterwards would make it a new evaluation, not a better result. Latest run: the 200-household cohort,
2 Oct 2026, 19 planted households.

| Claim | Measured | Target | |
|---|---|---|---|
| The classifier finds charging runs (§15.2) | precision 99.7%, recall 99.8% | ≥ 90% / ≥ 85% | pass |
| A timer is detected as a timer | 100% of planted nights | ≥ 80% | pass |
| The published band contains the load | **52.2%** of planted days | ≥ 90% | **miss** |
| No charger class is claimed where panels hid the run (§15.11) | 0 claims | 0 | pass |
| The net heating fit replicates (§15.4) | 0 mismatches | 0 | pass |
| A new EV becomes its own bill driver (§15.5) | step ratio 99.6–106.3% on all six wallboxes | ≥ 70% | pass |
| Only `hdd-linear-v3` is served | true | true | pass |
| The early warning arrives before arrears (§15.7) | 6 of 6 gradual accounts | ≥ 4 of 6 | pass |
| It does not fire on people who are fine | 0 per 100 on-time account-years (282) | ≤ 5 | pass |
| How far ahead it arrives | median **22 days** | ≥ 21 | pass |

**The band miss is about the band's hard edge, not detection.** The model finds the right half-hours;
what fails is containment, with misses almost always just below the low bound. Hence a wording rule:
`low` is "the least the run used under this subtraction", never a minimum or a guarantee. The range
is the answer.

**A detection is a pattern, not an appliance.** Real London households run real 5 kW loads: 82 of the
200 show EV-shaped runs on more than 2% of their days. That is the population EV precision is honestly
measured against.

### 15.11 The household with both panels and an EV (detect-v2)

A meter records `import = max(0, load − pv)` and `export = max(0, pv − load)` on two non-negative
registers. **Generation is never metered.** So:

| | Import register holds | What may be said about load |
|---|---|---|
| the house is exporting | exactly 0 | **nothing**: load is invisible |
| daylight, not exporting | load − pv | it is **understated** by an unknown amount |

A 7 kW wallbox under a generating array can look like 2 kW of import, which detect-v1 published as a
portable charger: a confident wrong answer. **detect-v2 changes what may be claimed, not a threshold.**
Half-hours with measured export, or between the day's first and last exporting half-hour, are
flagged; a run containing them gets `charger_class = NULL` and confidence capped at low.
`high_power` still stands, because PV only ever subtracts.

Deliberately not done: dropping daylight from assessed days, changing published kWh, modelling PV
generation inside a published model, or publishing a self-consumption percentage (its denominator is
never measured).

To keep advice for exactly these households, EV what-ifs include masked sessions and add a
**`solar_soak`** pattern that charges in the day's highest measured-export half-hours. It may show a
negative saving and is published anyway. Recall on fully hidden sessions has no target: when the array
covers the whole draw, the meter shows nothing to find.

### 15.12 The forecast measured against a published method (backtest)

[`analysis/forecast_backtest/`](../analysis/forecast_backtest/README.md) scores the bill forecast
against the kNN quantile method of **Botman et al. (2025)** on the sealed export, with a three-part gate
fixed before the runs. The champion is **`hdd-linear-v2` refitted as at each date**; v3 (§15.4) is not
scored yet (§12). Latest run: the 200-household cohort, export of 2 Oct 2026.

| Gate clause | Threshold | Result | |
|---|---|---|---|
| kNN's P10–P90 coverage | 70–90% | **81.1%** over 29,302 snapshots | pass |
| Accuracy where the champion declines | median APE ≤ champion's + 5 points (10.9%) | **5.6%** over 10,863 snapshots | pass |
| Head to head | mean CRPS ≤ 1.10× champion's | **0.85×** over 18,439 snapshots (95% CI 0.78–0.93) | pass |

**The finding that matters is the champion's calibration.** On snapshots both models forecast, the
±15% band contains the final usage **51%** of the time, falling to **41%** at 20+ days elapsed; kNN's
P10–P90 contains it **79%**, at roughly twice the width (median 140 kWh against 77). The band is
narrow because ±15% was chosen, not because the weather term explains the variance (§8.6).

**As a fallback, kNN extends coverage**: snapshots with a forecast rise from **23,152 to 61,234** (148 →
194 accounts). Those accounts see "forecast unavailable" today.

**A seasonal-neighbour variant was rejected.** Restricting neighbours to the same season left too few
of them: CRPS 1.96× the plain kNN's, and only 57% of snapshots served.

**Nothing is wired up.** `knn_quantile.py` is imported only by its tests and harness; every published
number is unchanged. Whether to serve the fallback is a product decision (§12). The 43 tests, including
the leakage checks, run in CI and fail on any skip.
