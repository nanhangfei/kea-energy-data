# `pipeline/scripts/`

**Grouped by the layer each script acts on, not by build stage.** The build stages
were the obvious axis and the wrong one: `load_bronze_to_snowflake.py` is the
Warehouse step's job run by Orchestration's DAG, `publish_serving.py` is Serving *and*
Sprint 3, `verify_bill_reproduction.py` is Delivery's CI checking Serving's output, and the
eight Sprint 4 decision jobs sit outside the roadmap entirely. A layer does not
renumber when the roadmap finishes -- and the layers are the Makefile's step
names: `lake/` is `make ingest-date`'s, `warehouse/` is `build-warehouse`'s and
`serving/` is `publish-serving`'s.

| Directory | What lives there |
|---|---|
| `gen_source/` | the source generator (`make init-sources`): the LCL cohort, the defects, the NIWA loader |
| `lake/` | source → bronze → parsed, and the Avro contracts that shape it |
| `warehouse/` | bronze → `KEA.RAW`, and the export back out to the lake |
| `serving/` | migrations, the publication, and the loader the backend's database sees |
| `decisions/` | the Sprint 3/4 jobs that write an opinion: forecasts, recommendations, estimates, hardship |
| `gates/` | anything whose exit code is the answer, plus the CI checks |
| `ops/` | rebuilds, backfills, seeding, credentials, the evaluation harness |
| `reference/` | committed reference data and the script that regenerates it |
| *(top level)* | `knn_quantile.py` and `backtest_bill_forecast.py`: the offline forecast backtest (`make backtest-reproduce`, `make backtest-forecast`). Research rather than a layer — nothing in the pipeline reads what it writes; see `analysis/forecast_backtest/` |

⚠️ **`lake/extract_small.py` is the only file here a DAG imports by name**
(`_kea_common.py` inserts `/opt/airflow/scripts/lake` and does a bare
`import extract_small`), so moving or renaming it changes a DAG. The same is
true of `serving/load_export_to_serving.py`, whose `_connect` six scripts in
three other directories import.
Flat on purpose. The verb prefix already groups these — `assert_*`, `gen_*`,
`set_*`, `extract_*` cluster when sorted — and a `verify/assert_isolation.sh`
would say "assert" twice while adding nothing a reader did not already have.

What the layout cannot say is **who runs each one**, which is the distinction
that decides whether a change here can break a nightly run, whether it needs a
credential, and whether it is allowed to be slow. So it is said here.

| | Runs | |
|---|---|---|
| `lake/extract_small.py` | **Airflow** | The only file here a DAG calls |
| `lake/extract_all_small.py` | a person | The same, looped over the sixteen that are not Spark's — inside `make ingest-date` |
| `warehouse/load_bronze_to_snowflake.py` | a person, or Airflow (`snowflake_stage`) | bronze/parsed → `KEA.RAW`, one entity one load date |
| `warehouse/load_all_bronze_to_snowflake.py` | `make build-warehouse`, or Airflow (`snowflake_stage`) | the same, looped over all seventeen |
| `gen_source/` | a person | the source generator, plus the NIWA weather loader |
| `ops/set_extract_roles.sh` | a person, once | Nothing works until it has |
| `lake/gen_avsc.py` · `gen_avsc.sh` | a person | Regenerates the derived contracts |
| `ops/gen_fixture_weather.sh` | `make build-warehouse` (`--check`), or a person with `REGEN=1` | Regenerates the fixtures' weather seed from `sample/*.sql`, on a throwaway Postgres; `--check` fails on a stale one and writes nothing |
| `decisions/build_bill_forecasts.py` | Airflow or a person | Idempotently project warehouse-approved snapshots into bill amounts and bands |
| `decisions/build_plan_recommendations.py` | a person or scheduler | Append one GST-inclusive recommendation generation |
| `decisions/build_solar_estimates.py` | the publication, or a person | Append one rooftop-solar estimate generation (3, 5 and 7 kW, each with and without a home battery, as ranges) |
| `decisions/build_ev_scenarios.py` | the publication, or a person | Append one EV charging what-if generation |
| `decisions/estimate_common.py` · `estimate_assumptions.py` | imported | The two estimate jobs' half-hours, profile and pricing, and their tagged, versioned assumptions |
| `reference/fetch_solar_yield.py` | a person | Regenerates `reference/solar_yield_nz.csv` from PVGIS |
| `gates/verify_bill_reproduction.py` | CI or a person | Re-rate one historical bill per open account; fails outside $1 |
| `decisions/build_hardship_assessments.py` | a person or scheduler | Append one explainable assessment for every open account |
| `decisions/create_hardship_alerts.py` | a person or scheduler | Create pending in-app records only; never sends them |
| `ops/evaluate_sprint4.py` | a person, via `make evaluate-sprint4` | Read-only: scores Sprint 4 against the answers the generator planted; aggregates only |
| `knn_quantile.py` | imported | The Botman et al. kNN-quantile model, its month-end extension and its CRPS scores; pure numpy, no I/O |
| `backtest_bill_forecast.py` | a person | `make backtest-reproduce` and `make backtest-forecast`: the offline forecast backtest on the sealed lake export, plus one read-only warehouse SELECT for older reads (Stage 2); writes only `analysis/forecast_backtest/results/` |
| `ops/rebuild_span.sh` | a person, after a span move | Every load date through the lake step and incremental staging, then `make build-warehouse` and `make publish-serving` once |
| `ops/archive_warehouse.py` | a person, once, before Snowflake access ends | Read-only: copies every base table in the warehouse to Parquet in MinIO (`archive/snowflake/<date>/`) and, with `--out`, a local directory, with a manifest that must match Snowflake's own row counts; run in the dbt image the way `make publish-serving` runs the exporter |
| `ops/run_pipeline.sh` | `make run-pipeline` | Triggers `kea_daily_pipeline` per load date and waits; refuses a paused DAG |
| `ops/set_lake_audit_role.sh` | `make dashboards` | Ensures `kea_lake_audit`: owns `lake_audit`, reads no source system; the lake summary's login in `make` and in Airflow |
| `ops/assert_local_serving.sh` | `make rebuild-all`, `make publish-serving DEMO_RESET=1` | Refuses unless `KEA_MIGRATION_DSN` is this machine's kea-db |
| `warehouse/reset_raw.py` | `make build-warehouse RESET_RAW=1`, `rebuild_span.sh` | Drops every `KEA.RAW` table so the next load recreates it from the contracts |
| `serving/reset_demo_decisions.sql` | `make publish-serving DEMO_RESET=1` | Clears the old decision history just before a demo publish recreates it |
| `gates/deploy_gate.sh` | `make deploy` | Read-only gates: `.env`, a clean tree, `HEAD` at `origin/main`, kea-db up, no pipeline run in flight |
| `gates/check_migrations_immutable.sh` | CI, or `make ci ONLY=migrations` | Fails when a serving migration already on the base branch was changed, renamed or deleted |
| `gates/assert_isolation.sh` | a person, or CI | Exit code is the answer |
| `gates/assert_idempotent.sh` | a person, or CI | |
| `ops/demo_independence.sh` | a person | Breaks a credential on purpose |

## `load_*_to_snowflake.py` — the boundary the lake cannot cross

Snowflake is SaaS and cannot reach the local MinIO, so the lake cannot be an external
stage and dbt cannot read Parquet where it lies. These push it up with the connector's
PUT + COPY against Snowflake's own internal stage.

Two things in them are easy to get wrong and produce no error:

⚠️ **The table is created from the Avro contract, not from the data.**
`write_pandas(auto_create_table=True)` infers column types from whatever DataFrame it is
handed, so the FIRST load date silently defines the schema for every load after it.
Invoices' `decimal(12,2)` amounts became `NUMBER(4,2)` because 2026-08-25 held only
two-digit values, and the next day failed on `135.95`. The error is the lucky case — a
narrower *scale* would have truncated cents on every row instead.

⚠️ **Parquet is read with `types_mapper=pd.ArrowDtype`.** Plain `to_pandas()` degrades any
type pandas cannot hold nulls in: an all-null `date` column arrives at Snowflake typed as a
number, and `rel_humidity_pct` silently widened from smallint to float. Keeping Arrow types
end to end removes the class rather than the instances.

A missing partition is a clean skip, not a failure — weather is a monthly snapshot, so on
most load dates two of the seventeen legitimately have nothing to load.

`make build-warehouse LOAD_DATE=…`.

⚠️ **Sweeping many load dates in a shell loop needs `</dev/null` on each `make`.** Otherwise
`make` consumes the loop's own stdin and the loop processes exactly one date — and reports
success, because that one date succeeded.

## `extract_small.py` — the runtime one

`_kea_common.py` reaches it by `sys.path.insert(0, "/opt/airflow/scripts")` and
a bare `import extract_small`, so **moving or renaming it changes a DAG**.

It extracts sixteen of the seventeen source entities — everything except
`interval_reads`, which is Spark's (design §5.1) — and runs inside the Airflow
image, where psycopg2 and pyarrow already are. Standing up a distributed engine
for a 73-row table costs more in startup than the read costs at all.

## `set_extract_roles.sh` — not optional, and easy to miss

`kea_source_schema.sql` creates the four `kea_extract_*` roles with `LOGIN` and
**no password**, deliberately — "set it out of band from the secret store". So
until this runs they cannot connect at all, and the only working credential is
the superuser. Using that would make the extract's independence a fiction:
`kea` can read all four schemas *and* `gen_audit`.

`make ingest-date`, which runs `set_extract_roles.sh` first.

## `gen_avsc.py` · `gen_avsc.sh` — the contracts, derived

Regenerates sixteen `.avsc` from `information_schema`, so the Postgres→Avro
mapping is stated once in a reviewable place instead of sixteen times by hand.
`--check` fails when a committed contract has drifted from the table it claims
to describe.

KEA-047 is hand-written and is never touched here: it is the one carrying an
argument, and its `quality` enum encodes a decision.

⚠️ The generated schemas apply design §5's reshapes as **overrides**. Without
them the contract would describe the *source*, and a parse job that did nothing
whatsoever would satisfy its own conformance check.

`make ingest-date REGEN=1`.

## The Sprint 3 decision jobs

These four scripts read the serving store directly and are deliberately outside
`serving.TABLES`. Recommendations, assessments and alerts are append-only decision records;
the regular warehouse loader must not erase their history or reset runtime-owned alert state.
They have no Makefile targets of their own: `publish_serving.py` runs them, after the
forecasts, inside its one transaction, so nothing they write reaches the backend without a
release.

`build_plan_recommendations.py` compares every eligible network tariff and flexible / 6 / 12-month
offer. Stored customer-facing amounts are GST-inclusive. `usage_saving` is the unconditional
tariff difference; `term_credit` is kept separate because it is conditional on
completing the term. Early-exit charges are not included in annual savings. A
switch is recommended only when `usage_saving > 0`.

`verify_bill_reproduction.py` is the acceptance gate. It takes one latest completed bill per
non-quarantined open account, resolves the historical plan version and term, and re-rates that bill's actual local
half-hourly import/export usage with the same tariff assembler used by recommendations. Any
modelled ex-GST total more than $1 from `charges_excl_gst` fails the job, and with it the publish. Declared
integrity quarantines are listed as skips and receive no recommendation; pricing unresolvable
reads would turn a known defect into an apparently precise saving.

`build_hardship_assessments.py` (ruleset `hardship-identification-v2`) always writes a parent
assessment, including a zero-signal result. Its five v1 signal codes cover failed direct debit,
consecutive late payment, consecutive part payment, stopped payment and an adequately-covered
usage collapse. Sprint 4 adds three warning signs that can appear before arrears:
`payment_drift` (each of the last 3 bills paid at least 3 days later than the one before, the
latest at most 7 days late), `bill_above_usual_payment` (the latest bill, an unpaid open bill
or the newest forecast at least 130% of the median paid per bill) and `first_shortfall` (the
newest past-due bill paid under 95% after 3 paid in full). The warning signs read what had been
paid by the assessment date. When drift or a shortfall is present, the job also writes a
`kea.hardship_outlook` row: the date the account is projected to fall into arrears if it keeps
paying at most its usual amount, the amount then overdue, and the arithmetic in words for
staff. Medical dependency and vulnerability are copied as care flags, never treated as
financial evidence.

`create_hardship_alerts.py` (rule v2) considers only the latest assessment for each account and
reads its codes. Any v1 code makes it eligible, as in Sprint 3. A large bill together with drift
or a shortfall is an early outreach; any other warning sign on its own creates none. It writes
an unscheduled `pending` / `in_app` record with neutral copy, and it performs no provider or
delivery call. Database uniqueness guards make retries a no-op and prevent repeated outreach
while an earlier alert is still open.

To inspect a decision job without inserting rows, run it on its own with `--dry-run` (the
recommender also takes `--check`, which builds all 41 tariffs and stops). From `data/`:

```bash
docker run --rm --network kea_default --env-file .env \
  --add-host=host.docker.internal:host-gateway \
  -v "$PWD/pipeline:/opt/pipeline:ro" -v "$PWD/../services/api:/opt/api:ro" \
  -e KEA_API_DIR=/opt/api -w /opt/pipeline/scripts \
  dataeng/dbt:1.12.0 python3 decisions/build_plan_recommendations.py --dry-run
```

## The Sprint 4 estimate jobs

`build_solar_estimates.py` and `build_ev_scenarios.py` follow the same rules: append-only,
outside `serving.TABLES`, generated cohort only, and run by `publish_serving.py` straight after
the recommender, whose window, tariffs and GST rule they import. No Makefile target either,
for the same reason; run them with `--dry-run` as above. Every row names
`estimate_assumptions.ASSUMPTIONS_VERSION`, and every dollar figure is an estimate.

⚠️ **Netting happens on each real half-hour, before anything is summed.** Panels or a
charger change a half-hour's import, and what is left over is exported; summing a year into
weekday × half-hour cells first would let one week's export cancel another week's import.
`estimate_common.py` says why the cells are exact once netted.

`build_solar_estimates.py` covers accounts with no export register. It spreads a day's
output over sunrise to sunset (NOAA equations at the region's station) along a half-sine,
using the low and high yields of the account's weather region, and prices the netted year on
the current plan. Payback is simple payback, from the best case to the worst case, and is
null when the saving does not pay the system back.

Each size is estimated twice, and `battery_kwh` says which: `0` is panels alone, `13.5` adds
a home battery whose cost is inside `install_cost_nzd_*`. The battery is walked through the
real half-hours in time order: surplus charges it (5 kW, 90% round trip, the loss taken on
the way in), import is met from it, and a gap in the readings empties it. On a plan with more
than one import rate, each size also gets a `grid_charge` battery (`battery_dispatch`) that
tops up from the grid in the plan's cheapest half-hours, leaving room for as much surplus as
the panels made the day before, and gives the energy back where the rate, after the loss,
still beats them; grid energy is never counted as solar self-use. ⚠️ A
battery row is not always the better row: where the buy-back is close to the import rate it
can save less than its `battery_kwh = 0` sibling, and it is published all the same. The battery's size, power and
round trip are read off one product's datasheet and its cost brackets a survey of NZ installed
prices; `estimate_assumptions.py` names both sources beside the values.

`build_ev_scenarios.py` writes two scenarios. `add_ev` adds a year's charging as one
session a day, from 18:00 or from 23:00, on every eligible Kea plan. `shift_detected` moves
the detected low bound of each `high_power` session in `kea.mart_charging_session` to
23:00 the same night, priced on the current plan and on Time of Use; on a flat plan the
saving is $0.

`reference/solar_yield_nz.csv` is committed. `reference/fetch_solar_yield.py` rebuilds it
from the public PVGIS API (no key), and the file records its source and retrieval date on
every row. Changing it means changing `ASSUMPTIONS_VERSION`.

## `evaluate_sprint4.py` — the planted answers

`make evaluate-sprint4` runs it after `make rebuild-all`. It reads serving in a READ ONLY
transaction, the source database's `gen_audit` answers and two Snowflake silver tables, and
writes only `analysis/sprint4_evaluation/results/`. It is the one script here that reads
`gen_audit`: the planted answers never reach serving, so nothing in the pipeline can use them.
The report names households by their planted role and prints no ICP or account number. The
method and thresholds are in `analysis/sprint4_evaluation/README.md`.

## `make rebuild-all` — compact, complete demo data

The default end-to-end rebuild for demonstrations. It is no longer a script: it is the stage
commands in sequence (`init-sources`, `ingest-date`, `build-warehouse`, `publish-serving`), so
the demo passes every gate a single load date does. It generates 50 households and sends their
complete two-year interval span through one Spark range extraction (`ingest-date
FROM_DATE=...`). That overlap is needed to fit heating sensitivity against the available
weather observations; the one-year recommendation window is still enforced downstream. Small
snapshots and billing events are extracted once. All nine hand-written Sprint 2 fixtures are
retained, including the 397-day year-on-year example.

It recreates RAW from the current contracts (`RESET_RAW=1`), runs the complete dbt graph and
the Soda gate, reloads the serving tables, and builds bill forecasts, recommendations,
hardship assessments and pending in-app alerts inside the publish, as well as verifying
historical bill reproduction. It is the profile with the explicit under-30-minute target.
`rebuild_span.sh` remains available when the purpose is to exercise every daily partition
rather than prepare a Sprint demonstration. The script it replaced measured about **7
minutes on 8 Sep 2026**, without the gates it now runs. It ends with the code-managed
Metabase dashboards and their verification gate (`make dashboards`).

## The three `assert_*` / `demo_*` scripts

Not tests of code — proofs about a **running system**, which is why they are
here and not in `pipeline/tests/`: they need the database, the lake, or a
deliberately broken credential.

| | Proves |
|---|---|
| `gates/assert_isolation.sh` | 20 reads across four credentials: each role reads its own system, none reads the other three, none reads `gen_audit`. Four are positive controls, because a test where everything fails is also passed by a typo in the hostname |
| `gates/assert_idempotent.sh` | Re-extracting a load date produces an identical content hash — and **fails if `_extracted_at` did not move**, because a hash that matches by virtue of nothing re-running proves nothing |
| `ops/demo_independence.sh` | Breaks the billing credential and shows metering completing anyway. The Lake step's first criterion is not provable by inspection: three `dag_id`s in three files show what the author intended, not what happens |

⚠️ `demo_independence.sh` restores the credential on `EXIT`, `INT` and `TERM`,
and again at startup. If it is killed in a way that skips all three,
`make ingest-date` puts it back.

## What is deliberately not here

**`assert_bronze.py` is in `spark_jobs/`** — it reads Parquet *and* queries the
source, and only `spark_jobs/` is mounted into the Spark containers. It is the
Lake gate; what lives here needs no Spark.

**`metabase_setup.py` moved to `analysis/metabase/`.** It provisions the BI
tool, and stack doc §8 is explicit that Metabase is the analysis surface rather
than part of the pipeline. Sitting here implied a dependency that does not
exist — nothing in a DAG has ever called it.

**`load_niwa_weather.py` is in `gen_source/`.** It loads files a person downloaded
by hand into `src_weather`, which makes it a source-data step like the rest of
Sources. DataHub requires a licence to be accepted per dataset, and that is a
person's decision, not a scheduler's.
