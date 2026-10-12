# Kea Energy — Data Platform

**A batch data platform that turns smart-meter readings into bill explanations, bill forecasts
and plan recommendations for New Zealand electricity customers.**

Customers want to know why a bill went up, what next month will cost and whether another
plan would be cheaper. Answering that needs half-hourly meter data, billing history, tariffs
and weather from separate systems, and the answers have to be correct. This pipeline
collects that data, models it and publishes it to the customer-facing app in tested
releases.

![Kea data pipeline](design/figures/pipeline.png)

## Highlights

- **End to end, from source to app.** 4 source systems and 17 entities flow through 6 Airflow
  DAGs, 53 dbt models and 8 Metabase dashboards. Each pipeline step is a single `make`
  command, and every step is safe to run again.
- **Correct to the cent.** The serving gate recalculates a real printed invoice from its
  half-hourly meter reads and must match it exactly: **$50.54**.
- **Forecasts are backtested before they ship.** On 200 households, a kNN quantile model
  ([Botman et al., 2025](analysis/forecast_backtest/README.md)) was tested as a fallback for
  the production bill forecast:
  - its P10–P90 range contained the actual month-end usage **81.1%** of the time (target 70–90%);
  - its CRPS (probabilistic forecast error, lower is better) was **0.85×** the production
    model's;
  - as a fallback, it raises the number of billing snapshots that get a forecast from
    **23,152 to 61,234**.
- **Failed releases never reach the app.** Each release publishes the served tables and the
  decisions computed from them in one transaction. If any dbt test fails, the app keeps
  serving the previous release.
- **Tested at every layer.** 488 Python tests, dbt data and unit tests, Soda checks, and a SQL
  gate at the end of each step.

## How it works

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
make rebuild-all          # the whole pipeline end to end (~23-37 min)
```

Or run it one step at a time. Each command calls the same scripts as the Airflow DAG:

| Step | Command | What it does |
|---|---|---|
| Stack | `make up` | Start the local platform |
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
analysis/            dashboards as code, forecast backtest, evaluations
```

## Limitations

- **Extracted from a monorepo.** `make publish-serving` expects the app's database and API
  in sibling directories (`../infra`, `../services/api`). Every step before it runs on its own.
- **Snowflake trial ended in October 2026.** All warehouse tables were archived to Parquet
  first ([`archive_warehouse.py`](pipeline/scripts/ops/archive_warehouse.py)). To rebuild,
  point `.env` at your own account.
- **Streaming is not built yet.** Kafka and Flink are defined in `docker-compose.yml`
  (`make up STREAMING=1`), but nothing produces to or consumes from them.
- **Dollar figures are illustrative.** Tariffs come from public sources; values that had to
  be assumed are marked as such in the data dictionary.

## Documentation

- [Platform design](design/kea-data-platform-design.md): what the pipeline does and the reasoning behind each design choice
- [Engineering stack](design/kea-data-engineering-stack.md): tools, versions and build order
- [Cohort selection](design/kea-cohort-selection.md): how the 200 households were chosen
- [Forecast backtest](analysis/forecast_backtest/README.md): method and full results
- [Serving migrations](migrations/serving/README.md): first run, baseline and recovery
