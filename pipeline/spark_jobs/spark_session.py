"""One Spark session builder, and the JDBC URL for the one source Spark reads.

Shared by the extract and the parse so the S3A configuration cannot drift
between them -- a mismatch there fails at write time, hours after the job that
introduced it looked fine.
"""

from __future__ import annotations

import os
import sys

# lib/ is mounted inside spark_jobs/ so that it and contracts/ stay siblings;
# see docker-compose.yml.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))


def event_log_uri() -> str:
    """Persistent event-log location shared with the Spark History Server."""
    bucket = os.environ.get("KEA_LAKE_BUCKET", "kea-lake")
    return f"s3a://{bucket}/_spark/events"


def build(app_name: str):
    """A session wired to MinIO over s3a://.

    Every one of these settings is required for MinIO specifically rather than
    real S3, and the failure mode when one is missing is an opaque 400 or a
    hang, not a clear message.
    """
    from pyspark.sql import SparkSession

    endpoint = os.environ["MINIO_ENDPOINT"]
    return (
        SparkSession.builder.appName(app_name)
        # The master UI only lists applications; its short-lived driver UI is
        # where Jobs, Stages and SQL DAGs live. Persist the driver's event log
        # so spark-history-server can reconstruct those pages after the job's
        # container has exited. This must be configured before getOrCreate():
        # the event logger starts with SparkContext, not on the first action.
        .config(
            "spark.eventLog.enabled",
            os.environ.get("SPARK_EVENT_LOG_ENABLED", "true").lower(),
        )
        .config("spark.eventLog.dir", event_log_uri())
        .config("spark.eventLog.compress", "true")
        .config("spark.hadoop.fs.s3a.endpoint", endpoint)
        .config("spark.hadoop.fs.s3a.access.key", os.environ["MINIO_ROOT_USER"])
        .config("spark.hadoop.fs.s3a.secret.key", os.environ["MINIO_ROOT_PASSWORD"])
        # MinIO serves buckets as a path, not as a DNS subdomain. Without this
        # the connector asks for kea-lake.minio:9000 and gets nowhere.
        .config("spark.hadoop.fs.s3a.path.style.access", "true")
        .config("spark.hadoop.fs.s3a.connection.ssl.enabled", str(not endpoint.startswith("http://")).lower())
        .config(
            "spark.hadoop.fs.s3a.aws.credentials.provider",
            "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider",
        )
        # v1 writes to a temp path and renames on commit; on object storage a
        # rename is a copy, so a load date's worth of files is written twice.
        .config("spark.hadoop.mapreduce.fileoutputcommitter.algorithm.version", "2")
        # The decimal in KEA-047 is numeric(10,4). Parquet's own decimal is
        # exact; letting Spark fall back to a double would lose cents on a
        # bill that has to reproduce to $50.54.
        .config("spark.sql.parquet.writeLegacyFormat", "false")
        # Spark 3.5 still DEFAULTS to INT96 for timestamps -- the Impala-era
        # encoding, which carries no logical type and no isAdjustedToUTC flag,
        # so a reader gets timestamp[ns] with no zone and has to guess. KEA-047
        # says timestamp-micros, and Snowflake external tables read INT96
        # poorly. The conformance test caught this: it read the footer and
        # found timestamp[ns] where the contract said timestamp[us, tz=UTC].
        .config("spark.sql.parquet.outputTimestampType", "TIMESTAMP_MICROS")
        # Explicit rather than inherited from the container's TZ, so a load
        # date means the same thing whoever runs the job.
        .config("spark.sql.session.timeZone", "UTC")
        # ------------------------------------------------------- lineage
        # OPENLINEAGE IS CONFIGURED HERE, WITH THE S3A SETTINGS, FOR THE SAME
        # REASON THEY ARE: this is the one builder both Spark jobs use, and a
        # listener attached in one job and not the other would produce a lineage
        # graph with a hole in it that nothing reports.
        #
        # The listener reads Spark's own logical plan, so the JDBC source and
        # the s3a:// outputs arrive as real datasets. That is the difference
        # from Airflow's provider, which emits job events with no inputs or
        # outputs at all because DockerOperator cannot know what ran inside.
        #
        # Namespace `kea_energy` matches AIRFLOW__OPENLINEAGE__NAMESPACE, or the
        # two halves would land in separate namespaces and the graph would be
        # two disconnected pictures rather than one.
        #
        # It degrades quietly: with the jar absent the config keys are ignored,
        # and with Marquez unreachable the listener logs and the job continues.
        # Lineage is a description of the run, and losing it must never fail the
        # run that produced the data.
        .config("spark.extraListeners",
                "io.openlineage.spark.agent.OpenLineageSparkListener")
        .config("spark.openlineage.transport.type", "http")
        .config("spark.openlineage.transport.url",
                os.environ.get("OPENLINEAGE_URL", "http://marquez:5000"))
        .config("spark.openlineage.namespace",
                os.environ.get("OPENLINEAGE_NAMESPACE", "kea_energy"))
        .getOrCreate()
    )


def metering_jdbc() -> tuple[str, dict]:
    """The URL and properties for src_metering, and nothing else.

    The password comes from KEA_EXTRACT_METERING_PASSWORD, which is the only
    source credential this container is given. If someone later wants Spark to
    read billing, the honest change is another credential and another job --
    not widening this one, which is what the four roles exist to prevent.
    """
    password = os.environ.get("KEA_EXTRACT_METERING_PASSWORD", "")
    if not password:
        raise SystemExit(
            "KEA_EXTRACT_METERING_PASSWORD is not set in this container.\n"
            "Run `make source-roles` and `docker compose up -d spark-master spark-worker`."
        )
    host = os.environ.get("KEA_SRC_DB_HOST", "kea-source-db")
    database = os.environ.get("KEA_SRC_DB_NAME", "kea")
    return (
        f"jdbc:postgresql://{host}:5432/{database}",
        {
            "user": "kea_extract_metering",
            "password": password,
            "driver": "org.postgresql.Driver",
            # One round trip per 100k rows rather than per row.
            "fetchsize": "100000",
            "pushDownPredicate": "true",
        },
    )
