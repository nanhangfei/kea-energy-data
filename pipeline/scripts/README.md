# `pipeline/scripts/`

Flat on purpose. The verb prefix already groups these — `assert_*`, `gen_*`,
`set_*`, `extract_*` cluster when sorted — and a `verify/assert_isolation.sh`
would say "assert" twice while adding nothing a reader did not already have.

What the layout cannot say is **who runs each one**, which is the distinction
that decides whether a change here can break a nightly run, whether it needs a
credential, and whether it is allowed to be slow. So it is said here.

| | Runs | |
|---|---|---|
| `extract_small.py` | **Airflow** | The only file here a DAG calls |
| `load_bronze_to_snowflake.py` | a person, or Stage 6 | bronze/parsed → `KEA.RAW`, one entity one load date |
| `load_all_bronze_to_snowflake.py` | a person, or Stage 6 | the same, looped over all seventeen |
| `gen_source/` | a person | Stage 2's generator, plus the NIWA weather loader |
| `set_extract_roles.sh` | a person, once | Nothing works until it has |
| `gen_avsc.py` · `gen_avsc.sh` | a person | Regenerates the derived contracts |
| `gen_fixture_weather.sh` | a person | Regenerates the fixtures' weather seed from `sample/*.sql`, on a throwaway Postgres |
| `build_bill_forecasts.py` | Airflow or a person | Idempotently project warehouse-approved snapshots into bill amounts and bands |
| `build_plan_recommendations.py` | a person or scheduler | Append one GST-inclusive recommendation generation |
| `verify_bill_reproduction.py` | CI or a person | Re-rate one historical bill per open account; fails outside $1 |
| `build_hardship_assessments.py` | a person or scheduler | Append one explainable assessment for every open account |
| `create_hardship_alerts.py` | a person or scheduler | Create pending in-app records only; never sends them |
| `rebuild_demo.sh` | a person | Compact end-to-end Sprint 2/3 rebuild, including all four decision jobs |
| `assert_isolation.sh` | a person, or CI | Exit code is the answer |
| `assert_idempotent.sh` | a person, or CI | |
| `demo_independence.sh` | a person | Breaks a credential on purpose |

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

`make load-snowflake LOAD_DATE=…`.

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

`make source-roles`.

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

`make avsc`.

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

`build_hardship_assessments.py` always writes a parent assessment, including a zero-signal result. Its
five signal codes cover failed direct debit, consecutive late payment, consecutive part
payment, stopped payment and an adequately-covered usage collapse. Medical dependency and
vulnerability are copied as care flags, never treated as financial evidence.

`create_hardship_alerts.py` considers only the latest assessment for each account. It writes
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
  dataeng/dbt:1.12.0 python3 build_plan_recommendations.py --dry-run
```

## `rebuild_demo.sh` — compact, complete demo data

`make rebuild-demo` is the default end-to-end rebuild for demonstrations. It generates 50
households and sends their complete two-year interval span through one Spark range extraction.
That overlap is needed to fit heating sensitivity against the available weather observations;
the one-year recommendation window is still enforced downstream. Small snapshots and billing
events are extracted once. All nine hand-written Sprint 2 fixtures are retained, including the
397-day year-on-year example.

The script recreates RAW from the current contracts, runs the complete dbt graph, reloads the 20
serving tables, and then builds bill forecasts, recommendations, hardship assessments and
pending in-app alerts, as well as verifying historical bill reproduction. It is the profile
with the explicit under-30-minute target. `rebuild_span.sh` remains available when the purpose
is to exercise every daily partition rather than prepare a Sprint demonstration.
The complete 50-household run measured about **7 minutes on 8 Sep 2026**.
Use `make demo-refresh` for the same rebuild followed by all five code-managed
Metabase dashboards and their verification gate.

## The three `assert_*` / `demo_*` scripts

Not tests of code — proofs about a **running system**, which is why they are
here and not in `pipeline/tests/`: they need the database, the lake, or a
deliberately broken credential.

| | Proves |
|---|---|
| `assert_isolation.sh` | 20 reads across four credentials: each role reads its own system, none reads the other three, none reads `gen_audit`. Four are positive controls, because a test where everything fails is also passed by a typo in the hostname |
| `assert_idempotent.sh` | Re-extracting a load date produces an identical content hash — and **fails if `_extracted_at` did not move**, because a hash that matches by virtue of nothing re-running proves nothing |
| `demo_independence.sh` | Breaks the billing credential and shows metering completing anyway. Stage 3's first criterion is not provable by inspection: three `dag_id`s in three files show what the author intended, not what happens |

⚠️ `demo_independence.sh` restores the credential on `EXIT`, `INT` and `TERM`,
and again at startup. If it is killed in a way that skips all three,
`make source-roles` puts it back.

## What is deliberately not here

**`assert_bronze.py` is in `spark_jobs/`** — it reads Parquet *and* queries the
source, and only `spark_jobs/` is mounted into the Spark containers. It is the
Stage 3 gate; what lives here needs no Spark.

**`metabase_setup.py` moved to `analysis/metabase/`.** It provisions the BI
tool, and stack doc §8 is explicit that Metabase is the analysis surface rather
than part of the pipeline. Sitting here implied a dependency that does not
exist — nothing in a DAG has ever called it.

**`load_weather.py` is in `gen_source/`.** It loads files a person downloaded
by hand into `src_weather`, which makes it a source-data step like the rest of
Stage 2. DataHub requires a licence to be accepted per dataset, and that is a
person's decision, not a scheduler's.
