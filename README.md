# Kea Energy — Data Platform

The data platform behind **Kea**, an AI assistant for New Zealand electricity customers.
It explains why a bill went up, forecasts the next one, recommends a cheaper plan, prices
solar, battery and EV options, and flags households heading into payment difficulty.

This repository is the batch pipeline that feeds the assistant. It takes data from four
source systems, writes it to an S3 data lake, models it in Snowflake with dbt, and
publishes tested releases into the application's Postgres. Every number the assistant
quotes can be traced back to a source row.

![Kea data pipeline](design/figures/pipeline.png)

## Tech stack

| Concern | Tools |
|---|---|
| Orchestration | Airflow 3.3 (asset-driven DAGs) |
| Processing | Spark 3.5 for half-hourly meter reads, plain Python for the smaller entities |
| Storage | MinIO (S3 API), Parquet, Avro schemas as data contracts |
| Warehouse | Snowflake, dbt 1.12 (staging → silver → gold → marts) |
| Serving | Postgres 17, versioned immutable migrations |
| Data quality | dbt tests and unit tests, Soda Core, SQL serving gate |
| Observability | OpenLineage + Marquez (lineage), Prometheus + Grafana (pipeline health), Metabase (analysis) |
| Streaming (optional) | Kafka, Schema Registry, Flink |
| Infrastructure | Docker Compose, Make, Vault (dev) |

## Data

The source systems are generated from real public datasets, so the data has real
behaviour but contains no customer information.

| Source | Used for |
|---|---|
| [Low Carbon London](https://www.kaggle.com/datasets/jeanmidev/smart-meters-in-london) smart-meter data | A deterministic 200-household cohort, re-timed to New Zealand local time ([how](dictionary/lcl-source-data.md)) |
| [NIWA](dictionary/niwa-weather-source.md) daily temperature | Heating-degree days for weather-adjusted forecasts (non-commercial licence, not redistributed) |
| [Retail tariffs](dictionary/ecotricity-tariff-source.md) | Three plan families used to price bills; values that had to be assumed are marked |
| [`sample/`](sample/) | Nine hand-built households, each a bill-shock scenario that checks its own totals |

Billing history, data defects and late revisions are added on top, so the quality gates
have real problems to catch. Dollar figures are illustrative.

## Getting started

**Prerequisites:** Docker, GNU Make, Python 3.11, a Snowflake account (set up once with
[`snowflake/bootstrap.sql`](snowflake/bootstrap.sql)), and a Kaggle API token.

```bash
cp .env.example .env      # fill in passwords, Snowflake key-pair auth,
                          # and add KAGGLE_USERNAME / KAGGLE_KEY
make help                 # list every step in run order
make ci                   # lint, unit tests, dbt parse, DAG import; needs no .env
make rebuild-all          # the whole pipeline end to end (~23-37 min)
```

Each step is one idempotent command, and each calls the same scripts the Airflow DAG runs:

| Step | Command | What it does |
|---|---|---|
| Stack | `make up` | Start the local platform (`STREAMING=1` adds Kafka and Flink) |
| Sources | `make init-sources` | Generate and load the four source systems, then check them |
| Lake | `make ingest-date LOAD_DATE=…` | Extract one NZ day from every source into bronze, then check it |
| Warehouse | `make build-warehouse` | Load Snowflake RAW, run `dbt build`, then run Soda checks |
| Serving | `make publish-serving RELEASE=…` | Export, publish one release with its decisions, then check it |
| Analysis | `make dashboards` | Rebuild Metabase dashboards from code and check every card |
| Orchestration | `make run-pipeline` | Trigger the daily DAG; `VERIFY=1` runs the quality proofs |

Local UIs: Airflow `:8080`, Metabase `:3002`, Grafana `:3001`, Marquez `:3000`, Spark history `:18080`.

## Repository layout

```
pipeline/
  airflow/dags/      extract DAGs (one per source), the daily pipeline, forecast refresh
  spark_jobs/        meter-read extract and parse, bronze checks
  contracts/avro/    data contracts for all 17 source entities
  dbt_project/       staging → silver → gold → marts, with tests and unit tests
  export/            one SQL file per served table: the warehouse-to-app mapping
  scripts/           loaders, the publisher, decision jobs, gates, data generation
  soda/              warehouse and serving checks
  monitoring/        Prometheus, Grafana and alerting configuration
migrations/serving/  immutable, ordered schema migrations for the serving database
design/              platform design, stack and build order, DDL
dictionary/          source datasets: meaning, units, quirks, provenance
analysis/            Metabase dashboards as code, forecast backtest, evaluations
sample/              nine bill-shock households used as test fixtures
```

## Documentation

- [Platform design](design/kea-data-platform-design.md): what the pipeline does and why
- [Engineering stack](design/kea-data-engineering-stack.md): tools, versions and build order
- [Serving migrations](migrations/serving/README.md): first run, baseline and recovery
- [Synthetic sources](pipeline/scripts/gen_source/README.md): what is generated and what is assumed
- [Forecast backtest](analysis/forecast_backtest/README.md): kNN quantile forecasts (Botman et al., 2025) compared against the production model

## Status

- **Extracted from a monorepo.** The serving database and the assistant API lived in
  sibling directories (`../infra`, `../services/api`). `make publish-serving` expects
  them there. Every step before it runs on its own.
- **The original Snowflake trial ended in October 2026.** Every warehouse table was
  archived to Parquet first ([`archive_warehouse.py`](pipeline/scripts/ops/archive_warehouse.py)).
  To run the warehouse step again, point `.env` at your own account.
