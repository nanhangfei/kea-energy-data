"""Emitting OpenLineage from the steps that are not Spark.

WHY THIS FILE EXISTS AT ALL. Airflow's OpenLineage provider emits a job and a
run event per task and nothing else: measured on this stack, 379 events, every
one with `inputs: []` and `outputs: []`. DockerOperator cannot know what ran
inside the container, and even an Airflow Asset outlet did not become a dataset.
So Marquez held 22 job nodes, 0 datasets and 0 edges -- a list, not a graph.

Spark's listener solves its own half by reading the logical plan. These three
steps -- bronze -> Snowflake, Snowflake -> lake, lake -> Postgres -- are plain
Python, so they have to say what they read and wrote themselves.

⚠️ THE DATASET NAMES MUST MATCH WHAT SPARK EMITS, or the result is two graphs
that merely look like one. OpenLineage's convention is a namespace identifying
the STORE and a name identifying the object within it, and the Spark listener
normalises `s3a://` to `s3://`. So a lake dataset is namespace `s3://kea-lake`,
name `bronze/parsed/src_metering/interval_reads` -- matching byte for byte what
the parse job's listener produces for the same prefix. Get this wrong and
nothing errors; the graph just quietly has two components.

⚠️ NOTHING HERE MAY FAIL A JOB. Lineage is a description of a run, and losing
the description must never destroy the thing described. Every call is wrapped:
a missing library, an unreachable Marquez or a malformed event degrades to a
printed line and the pipeline carries on.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

#: Matches AIRFLOW__OPENLINEAGE__NAMESPACE and spark.openlineage.namespace. All
#: three must agree or the halves land in separate namespaces.
NAMESPACE = os.environ.get("OPENLINEAGE_NAMESPACE", "kea_energy")
URL = os.environ.get("OPENLINEAGE_URL", "http://marquez:5000")
BUCKET = os.environ.get("KEA_LAKE_BUCKET", "kea-lake")


def lake(prefix: str) -> tuple[str, str]:
    """A lake object, named at the LOGICAL level -- the partition is stripped.

    ⚠️ THIS DROPS `load_date=...`, AND THE FIRST VERSION DID NOT. Naming the
    dataset after the exact partition written looked more precise and broke the
    graph: parse wrote `...interval_reads/load_date=2026-09-02` while the
    Snowflake stage read `...load_date=2026-09-03`, so Marquez held two
    unrelated nodes and no edge between them. Every night would add a fresh
    disconnected chain rather than another run of one pipeline.

    A dataset is the TABLE; a partition is a run of it. That is also how the
    Snowflake and Postgres names here work, and mixing the two conventions is
    what made one hop of "source to serving" quietly absent.
    """
    logical = "/".join(part for part in prefix.strip("/").split("/")
                       if not part.startswith("load_date="))
    return f"s3://{BUCKET}", logical


def snowflake(schema: str, table: str) -> tuple[str, str]:
    """A warehouse table, named the way dbt-ol names it.

    ⚠️ LOWERCASED, AND THE FIRST VERSION WAS NOT. Snowflake folds unquoted
    identifiers up, so uppercase felt right -- and it split the graph in half.
    dbt-ol emits `KEA.kea_marts.mart_daily_usage`; this emitted
    `KEA.KEA_MARTS.MART_DAILY_USAGE`; Marquez treated them as two unrelated
    datasets and the export appeared to read something dbt had never written.
    Measured: three duplicate pairs, 26 orphan nodes, and no path from the
    source to any serving mart.

    Matching dbt is not a preference here. dbt is the only emitter for the
    warehouse's interior, so its convention is the one the join has to use.
    """
    account = os.environ.get("SNOWFLAKE_ACCOUNT", "unknown")
    database = os.environ.get("SNOWFLAKE_DATABASE", "KEA")
    return f"snowflake://{account}", f"{database}.{schema.lower()}.{table.lower()}"


def serving(schema: str, table: str) -> tuple[str, str]:
    host = os.environ.get("KEA_SERVING_HOST", "kea-serving-db")
    return f"postgres://{host}:5432", f"kea.{schema}.{table}"


def source_table(schema: str, table: str) -> tuple[str, str]:
    """A src_* table, where the pipeline's lineage graph begins.

    Same Postgres instance as the serving store -- kea-serving-db hosts both the
    src_* schemas and the served ones -- so this is deliberately the same shape
    as serving(). The two ends of the graph being in one database is a property
    of this local stack, not of the design; in production they are far apart and
    only the namespace string changes.
    """
    host = os.environ.get("KEA_SRC_DB_HOST", "kea-serving-db")
    return f"postgres://{host}:5432", f"kea.{schema}.{table}"


def _client():
    from openlineage.client import OpenLineageClient
    from openlineage.client.transport.http import HttpConfig, HttpTransport
    return OpenLineageClient(transport=HttpTransport(HttpConfig(url=URL)))


def _datasets(pairs):
    from openlineage.client.event_v2 import Dataset
    return [Dataset(namespace=ns, name=name) for ns, name in pairs or []]


@contextmanager
def job(name: str, inputs=None, outputs=None):
    """Wrap a step so it emits START then COMPLETE, or START then FAIL.

        with lineage.job("snowflake_stage",
                         inputs=[lineage.lake(prefix)],
                         outputs=[lineage.snowflake("RAW", table)]):
            ...

    The FAIL event matters as much as the COMPLETE one: a graph that only ever
    records successes cannot answer "when did this dataset last actually
    update", which is the question lineage is for.
    """
    from datetime import datetime, timezone

    client = run_id = RunEvent = RunState = Run = Job = None
    try:
        from openlineage.client.event_v2 import Job, Run, RunEvent, RunState
        client, run_id = _client(), str(uuid.uuid4())
    except Exception as exc:  # noqa: BLE001
        print(f"  lineage disabled ({exc}); the step still runs")

    def emit(state) -> None:
        if client is None:
            return
        try:
            client.emit(RunEvent(
                eventType=state,
                eventTime=datetime.now(timezone.utc).isoformat(),
                run=Run(runId=run_id),
                job=Job(namespace=NAMESPACE, name=name),
                inputs=_datasets(inputs),
                outputs=_datasets(outputs),
                producer="https://github.com/nanhangfei/kea-energy-data",
            ))
        except Exception as exc:  # noqa: BLE001
            print(f"  lineage {state} not emitted for {name}: {exc}")

    emit(RunState.START if RunState else None)
    try:
        yield
    except Exception:
        emit(RunState.FAIL if RunState else None)
        raise
    emit(RunState.COMPLETE if RunState else None)
