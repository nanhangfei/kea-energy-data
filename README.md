# Kea Energy — Data Platform

**A batch data platform that turns smart-meter readings into bill explanations, bill forecasts
and plan recommendations for New Zealand electricity customers.**

Customers want to know why a bill went up, what next month will cost and whether another
plan would be cheaper. Answering that needs half-hourly meter data, billing history, tariffs
and weather from separate systems, and the answers have to be correct. This pipeline
collects that data, models it and publishes it to the customer-facing app in tested
releases.

## Architecture

![Kea data pipeline](design/figures/pipeline.png)

1. **Sources.** Four Postgres systems (billing, metering, catalogue, weather) are generated
   from public data and include deliberate defects and late meter corrections.
2. **Extract.** Each source system has its own Airflow DAG and least-privilege database role.
   Spark handles the half-hourly meter reads and plain Python handles the 16 smaller entities.
   Extracts are incremental on arrival time, so late corrections are picked up.
3. **Lake.** Data lands as Parquet on S3-compatible storage, sealed per New Zealand day and
   checked against Avro contracts.
4. **Warehouse.** Data is loaded into Snowflake, then modelled by dbt through staging →
   silver → gold → marts, with tests at each layer.
5. **Serve.** Marts are exported as a release and published to the app's Postgres. In the
   same transaction, decision jobs compute bill forecasts, plan recommendations, hardship
   alerts and solar, battery and EV estimates.
6. **Analyse.** Metabase dashboards read the published data. Every dashboard is built from
   code and each card is checked on every build (`make dashboards`).

## Analysis

The published data answers the questions above. These charts come from the demo walkthrough
dashboard, which follows one household from the test data through every feature. The data is
synthetic and the dashboard shows no identifiers.

### Why did my bill go up?

The bill for 2 Jun – 1 Jul 2026 was **29% higher** than the one before ($219 → $284). From
20 June, the half-hourly data shows long evening blocks shaped like an EV charger:

![Daily use, with the EV-shaped part highlighted](analysis/figures/dashboard-ev-detection.png)

The bill explanation prices each cause in dollars. The EV-shaped load accounts for **$54** of
the rise and colder weather for $26, while a shorter billing period saves $7. The $11 that no
measured cause explains is shown rather than hidden.

![Why the bill changed, in dollars](analysis/figures/dashboard-bill-explanation.png)

### What will next month cost?

During the cycle that started on 2 Aug 2026, the daily forecast sat up to **63% above** the
same period last year from day one, and its range never came down to last year's bill. The
customer gets a "heading unusually high" warning weeks before the bill arrives.

![The forecast during the cycle, against last year](analysis/figures/dashboard-forecast.png)

### Would another plan be cheaper?

Re-pricing this home's last twelve months of half-hourly use on each plan, **Time of Use**
costs about **$400 a year less** than its current Free Weekends plan. A switch is
recommended only when it saves money.

![A year of this home's use, priced on each plan](analysis/figures/dashboard-plan-comparison.png)

## Tech stack

| Layer | Tool | Why |
|---|---|---|
| Orchestration | Airflow 3.3 | Data-aware scheduling: the daily pipeline starts when its source data has landed, not at a fixed time |
| Processing | Spark 3.5, Python | Spark only for the millions of meter reads; small tables don't justify a cluster |
| Lake | MinIO, Parquet, Avro | Runs locally with the same S3 API as AWS; Avro contracts catch schema drift before ingest |
| Warehouse | Snowflake, dbt 1.12 | Layered, version-controlled SQL models with built-in tests and docs |
| Serving | Postgres 17 | The app's own database; schema changes go through immutable, versioned migrations |
| Data quality | dbt tests, Soda Core | Tests the logic during the build and checks the data after each load |
| Observability | OpenLineage + Marquez, Prometheus + Grafana | Table-level lineage plus pipeline health and alerting |
| Analytics | Metabase | Dashboards built from code, with every card checked automatically |

## Getting started

**Prerequisites:** Docker, GNU Make, Python 3.11, a Snowflake account (set up once with
[`snowflake/bootstrap.sql`](snowflake/bootstrap.sql)), and a
[Kaggle API token](https://www.kaggle.com/settings) to download the meter data.

```bash
git clone https://github.com/nanhangfei/kea-energy-data.git && cd kea-energy-data
cp .env.example .env      # fill in passwords and Snowflake key-pair auth;
                          # add KAGGLE_USERNAME and KAGGLE_KEY
make ci                   # offline checks; needs no .env or credentials
make serving-db           # the app's Postgres, which releases publish into
make rebuild-all          # the whole pipeline end to end (~23-37 min)
```

Or run it one step at a time. Each command calls the same scripts as the Airflow DAG:

| Step | Command | What it does |
|---|---|---|
| Stack | `make up`, `make serving-db` | Start the local platform and the serving database |
| Sources | `make init-sources` | Generate and load the four source systems |
| Lake | `make ingest-date LOAD_DATE=2026-08-25` | Extract one NZ day from every source |
| Warehouse | `make build-warehouse` | Load Snowflake and run `dbt build` |
| Serving | `make publish-serving RELEASE=r1` | Publish one release with its decisions |
| Analysis | `make dashboards` | Rebuild the Metabase dashboards |

Local UIs: Airflow [:8080](http://localhost:8080), Metabase [:3002](http://localhost:3002),
Grafana [:3001](http://localhost:3001), Marquez [:3000](http://localhost:3000).
`make help` lists every command and option.

## Testing and data quality

| Command | What it checks |
|---|---|
| `make ci` | Lint, unit tests, migrations, dbt parse and DAG imports, offline |
| `make ingest-date VERIFY=1` | The lake: an idempotent replay, contract conformance, and that a billing outage leaves metering unaffected |
| `make run-pipeline VERIFY=1` | Failure paths end to end, for example that a failing dbt test leaves the served data unchanged |

Every step ends with a gate that fails the run: source integrity, lake isolation, `dbt test`
plus Soda on the warehouse, and `assert_serving` plus Soda on the published release.

## Data

The data is generated from real public datasets, so it behaves like real data but contains
no customer information.

| Source | Used for |
|---|---|
| [Low Carbon London](https://www.kaggle.com/datasets/jeanmidev/smart-meters-in-london) smart-meter data | A deterministic 200-household cohort, shifted to NZ time and seasons ([details](dictionary/lcl-source-data.md)) |
| [NIWA](dictionary/niwa-weather-source.md) daily temperatures | Heating-degree days for weather-adjusted forecasts (non-commercial licence, not redistributed) |
| [Retail tariffs](dictionary/ecotricity-tariff-source.md) | Three plan families used to price every bill |
| [`sample/`](sample/) | Nine hand-built households, each a "why did my bill go up" scenario that checks its own totals |

## Project structure

```
pipeline/
  airflow/dags/      one extract DAG per source, the daily pipeline, forecast refresh
  spark_jobs/        meter-read extract and parse
  contracts/avro/    data contracts for all 17 source entities
  dbt_project/       staging → silver → gold → marts, with tests
  export/            one SQL file per served table
  scripts/           loaders, publisher, decision jobs, gates, data generation
  soda/              warehouse and serving checks
migrations/serving/  versioned schema migrations for the serving database
design/              architecture docs, DDL and the pipeline diagram
dictionary/          source datasets: meaning, units, quirks, provenance
analysis/            dashboards as code, dashboard screenshots, evaluations
```

## Roadmap

| Stage | What it delivers | Status |
|---|---|---|
| **1. Platform foundation** | Local stack; four source systems generated from real data, with planted defects; per-source extracts into an S3 lake with Avro contracts; Snowflake + dbt warehouse; atomic publishing into the app's Postgres | Done |
| **2. Quality and operations** | dbt tests, Soda checks and a serving gate that reproduces a real printed bill to the cent; lineage in Marquez; pipeline health and alerting in Grafana; asset-driven Airflow orchestration with backfill | Done |
| **3. Core customer features** | Bill explanation, mid-cycle bill forecast and warning, plan comparison, hardship signals with staff alerts | Done |
| **4. Advanced analytics** | EV and water-heater load detection; solar, battery and EV what-ifs; hardship early warning; each scored against planted answers | Done |
| **5. Analysis and scale** | 200-household cohort; Metabase dashboards built from code; pinned versions and offline CI checks (`make ci`) | Done |
| **6. Delivery** | CI on every pull request, including `dbt build` in an isolated schema; first run of the guarded `make deploy` | In progress |
| **7. Trustworthy answers** | Calibrated forecast ranges; quarantined accounts visible to the app instead of silently missing; tariff changes explained by re-rating in SQL | Planned |
| **8. Production readiness** | Immutable export releases with a manifest; deployment to AWS (MWAA, EMR Serverless, RDS) with infrastructure as code | Planned |
| **9. Streaming** | Meter reads replayed through Kafka and Flink, proven to produce the same marts as batch | Planned |

## Limitations

- **Dollar figures are illustrative.** Tariffs come from public sources; values that had to
  be assumed are marked as such in the data dictionary.

## About this repository

This repository is extracted from **Kea**, a team project developed in
`CS778-S2-2026-AWS-Challenge/HAKAS-kea-energy` (private). I was the team's data engineer.
This repository contains only the part I was responsible for: the data pipeline from the
source systems to the published serving database, and the analysis built on it. The app's
API and its web and mobile front ends, which read the served data, remain in the original
repository.

## Documentation

- [Platform design](design/kea-data-platform-design.md): what the pipeline does and the reasoning behind each design choice
- [Engineering stack](design/kea-data-engineering-stack.md): tools, versions and build order
- [Cohort selection](design/kea-cohort-selection.md): how the 200 households were chosen
- [Serving migrations](migrations/serving/README.md): first run, baseline and recovery
