"""Summarise the Stage 3 lake into Postgres, so Metabase can read it.

    make lake-summary

WHY THIS EXISTS AT ALL. Metabase talks JDBC. Stage 3's output is Parquet on
MinIO, and there is no Snowflake yet — §4 is blocked on credentials — so there
is nothing for a dashboard to point at. This job reads the lake with Spark and
lands four small rollups in `lake_audit`, which Metabase already reaches
through the `Kea serving` connection.

`lake_audit` IS NOT THE PIPELINE, and the schema name is chosen to say so.
Stage 5 is what lands pipeline output in the serving store, with a per-table
strategy, FK-ordered dependencies and `synced_at` at commit time. These are
ANALYSIS rollups: derived, disposable, rebuilt from scratch on every run, and
never read by the agent. Putting them in `kea` would make them look like marts;
putting them in `metering` would make them look like the loader's work. The
same reasoning that keeps `gen_audit` out of `src_*`.

WHAT IS WORTH LOOKING AT, and why these four tables and not a copy of the
readings. The metering data itself is already visible in the Stage 2 dashboard
over `src_metering` — re-plotting kWh from the lake would add a second view of
the same numbers and a second thing to keep in agreement. What Stage 3 newly
makes visible is the pipeline's own behaviour:

  * `partitions`   what landed, when, how much, and whether the hash moved
                   -- all seventeen entities across four source systems, weather
                   included
  * `reads_daily`  what each load date actually contained
  * `arrival_lag`  how late corrections arrive — the distribution the whole
                   `received_at` watermark argument rests on, and the one
                   thing no source-side view can show
  * `weather_daily` the NIWA series as the LAKE holds it, not as the loader
                   wrote it. Nothing else plots temperature anywhere: the
                   Stage 2 dashboard predates the source, so a wrong-hemisphere
                   or day-shifted series would be invisible until it moved a
                   bill explanation

Rebuilt whole each run: the lake is the source of truth and these are a
projection of it, so incremental maintenance would buy nothing and add a
staleness question nobody wants to answer.

The compact demo intentionally writes one multi-day range partition. Older
daily partitions may remain in the lake for audit history; when a range
generation exists, this summary selects its load date across every entity so
the dashboard cannot add two cohort generations together.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys

sys.path.insert(0, "/opt/spark_jobs/lib")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

SPARK_JOBS = "/opt/spark_jobs"
if SPARK_JOBS not in sys.path:
    sys.path.insert(0, SPARK_JOBS)

import spark_session  # noqa: E402
from kea_pipeline import lake, spec  # noqa: E402

SCHEMA = "lake_audit"
BUCKET = os.environ.get("KEA_LAKE_BUCKET", lake.BUCKET)

#: Design §8.4 fixes this: GREATEST(16.0 - temp_c, 0), base 16, one definition
#: throughout. Not 18 -- services/api carried an 18 with an unsourced docstring
#: calling it "the NZ convention", corrected 2 Sep 2026.
HDD_BASE_C = 16.0

#: The cohort's NZ-local reading span, mirroring load_weather.py. src_weather
#: holds everything NIWA published -- Christchurch reaches back to 1954 -- and
#: rolling all of it up would put 69,569 points on a chart to say something
#: about two years. uk_to_nz.NZ_SPAN_END is the source of truth for the upper
#: bound; it cannot be imported here because this runs in the Spark container.
SPAN_LO, SPAN_HI = dt.date(2025, 8, 1), dt.date(2027, 8, 27)


def serving_jdbc() -> tuple[str, dict]:
    password = os.environ.get("KEA_SERVING_PASSWORD", "")
    if not password:
        raise SystemExit("KEA_SERVING_PASSWORD is not set in this container")
    host = os.environ.get("KEA_SRC_DB_HOST", "kea-serving-db")
    return (
        f"jdbc:postgresql://{host}:5432/{os.environ.get('KEA_SRC_DB_NAME', 'kea')}",
        {"user": "kea", "password": password, "driver": "org.postgresql.Driver"},
    )


def main() -> int:
    spark = spark_session.build("summarise_lake")
    spark.sparkContext.setLogLevel("ERROR")
    try:
        from pyspark.sql import functions as F

        url, properties = serving_jdbc()
        _ensure_schema(spark, url, properties)
        generation_date = _compact_generation_date(spark)
        if generation_date:
            print(f"  compact generation {generation_date}; older lake partitions retained, not summarised")

        # ---- 1. what landed, per partition ------------------------------
        rows = []
        for entity in spec.ENTITIES:
            for layer, prefix_of in (("raw", lake.raw_prefix), ("parsed", lake.parsed_prefix)):
                for load_date in _load_dates(spark, layer, entity):
                    if generation_date and load_date != generation_date:
                        continue
                    manifest = _manifest(spark, prefix_of(entity.source, entity.table, load_date))
                    if manifest is None:
                        continue
                    rows.append((
                        entity.source, entity.table, layer, load_date, entity.mode,
                        manifest.get("watermark_column"),
                        int(manifest.get("row_count") or 0),
                        int(manifest.get("source_row_count") or 0),
                        int(manifest.get("quarantined_rows") or 0),
                        manifest.get("content_hash"),
                        int(manifest.get("partitions") or 0),
                        _ts(manifest.get("extracted_at")),
                    ))
        partitions = spark.createDataFrame(
            rows,
            "source_system string, entity string, layer string, load_date date, mode string, "
            "watermark_column string, row_count long, source_row_count long, "
            "quarantined_rows long, content_hash string, spark_partitions int, "
            "extracted_at timestamp",
        )
        _write(partitions, "partitions", url, properties)
        print(f"  partitions   {partitions.count():>7,} rows")

        # ---- 2. the weather series ---------------------------------------
        # BEFORE the early return below, not after. The read rollups bail out
        # when nothing has parsed interval_reads yet, and weather has no
        # business being hostage to that -- the two sources are extracted by
        # different DAGs on different schedules, which is the whole point.
        weather = _weather(spark, F, generation_date)
        if weather is None:
            print("  no parsed weather yet; skipping weather_daily")
        else:
            _write(weather, "weather_daily", url, properties)
            print(f"  weather_daily{weather.count():>7,} rows")

        # ---- 3 & 4. the meter reads themselves ---------------------------
        read_dates = _load_dates(spark, "parsed", spec.get("src_metering.interval_reads"))
        if generation_date:
            read_dates = [d for d in read_dates if d == generation_date]
        if not read_dates:
            print("  no parsed interval_reads yet; skipping the read rollups")
            return 0

        reads = spark.read.parquet(*[
            lake.s3a(lake.parsed_prefix("src_metering", "interval_reads", d), BUCKET)
            for d in read_dates
        ]).withColumn(
            "load_date", F.to_date(F.from_utc_timestamp("received_at", "Pacific/Auckland"))
        ).withColumn(
            # ELAPSED days, not a calendar difference. datediff() truncates, and
            # the two disagree by 9 rows on a single load date -- 394 against
            # 385 for "more than 21 days late". Naming the measure is the only
            # way the number means anything.
            "lag_days",
            (F.col("received_at").cast("double") - F.col("interval_start").cast("double")) / 86400.0,
        ).cache()

        daily = reads.groupBy("load_date").agg(
            F.count("*").alias("rows"),
            F.countDistinct("icp_identifier").alias("icps"),
            F.round(F.sum("kwh"), 2).alias("kwh"),
            F.sum(F.when(F.col("quality") == "actual", 1).otherwise(0)).alias("actual"),
            F.sum(F.when(F.col("quality") == "estimated", 1).otherwise(0)).alias("estimated"),
            F.sum(F.when(F.col("quality") == "final", 1).otherwise(0)).alias("final"),
            F.sum(F.when(F.col("version_no") == 1, 1).otherwise(0)).alias("original"),
            F.sum(F.when(F.col("version_no") > 1, 1).otherwise(0)).alias("corrections"),
            F.sum(F.when(F.col("lag_days") > 21, 1).otherwise(0)).alias("late_over_21d"),
            F.round(F.max("lag_days"), 1).alias("max_lag_days"),
            F.round(F.avg("lag_days"), 2).alias("mean_lag_days"),
        )
        _write(daily, "reads_daily", url, properties)
        print(f"  reads_daily  {daily.count():>7,} rows")

        # A histogram, not a percentile: the shape is the point. Same-day
        # arrivals dominate so heavily that a mean says nothing, and the tail
        # out to 45 days is exactly what an interval_start watermark loses.
        lag = reads.withColumn(
            "lag_bucket",
            F.when(F.col("lag_days") < 1, "0 — same day")
             .when(F.col("lag_days") < 2, "1 day")
             .when(F.col("lag_days") < 3, "2 days")
             .when(F.col("lag_days") < 8, "3–7 days")
             .when(F.col("lag_days") < 22, "8–21 days")
             .when(F.col("lag_days") < 46, "22–45 days")
             .otherwise("46+ days"),
        ).groupBy("lag_bucket").agg(
            F.count("*").alias("rows"),
            F.sum(F.when(F.col("version_no") > 1, 1).otherwise(0)).alias("corrections"),
            F.round(F.min("lag_days"), 2).alias("min_lag_days"),
            F.round(F.max("lag_days"), 2).alias("max_lag_days"),
        )
        _write(lag, "arrival_lag", url, properties)
        print(f"  arrival_lag  {lag.count():>7,} rows")
        return 0
    finally:
        spark.stop()


def _weather(spark, F, generation_date=None):
    """The NIWA daily series over the cohort span, one row per region per day.

    READ FROM THE PARSED LAKE, not from src_weather. A panel over the source
    table would prove the loader wrote something; this proves the pipeline
    carried it, which is the only claim Stage 3 makes.

    The newest snapshot wins. Both weather entities are snapshots -- NIWA
    republishes the whole history monthly, so a corrected observation arrives
    as a changed row with nothing to say it moved -- and older partitions are
    superseded by construction rather than being extra days of history.
    """
    obs_dates = _load_dates(spark, "parsed", spec.get("src_weather.daily_observations"))
    station_dates = _load_dates(spark, "parsed", spec.get("src_weather.stations"))
    if generation_date:
        obs_dates = [d for d in obs_dates if d == generation_date]
        station_dates = [d for d in station_dates if d == generation_date]
    if not obs_dates or not station_dates:
        return None

    obs = spark.read.parquet(
        lake.s3a(lake.parsed_prefix("src_weather", "daily_observations", obs_dates[-1]), BUCKET))
    stations = spark.read.parquet(
        lake.s3a(lake.parsed_prefix("src_weather", "stations", station_dates[-1]), BUCKET))

    mean = F.col("temp_mean_c").cast("double")
    return (
        obs.join(stations.select("agent_no", "station_name", "climate_region"), "agent_no")
        .where(F.col("obs_date").between(F.lit(SPAN_LO), F.lit(SPAN_HI)))
        .select(
            "climate_region", "station_name", "obs_date",
            F.round(mean, 1).alias("temp_mean_c"),
            F.round(F.col("temp_max_c").cast("double"), 1).alias("temp_max_c"),
            F.round(F.col("temp_min_c").cast("double"), 1).alias("temp_min_c"),
            # NULL, NOT ZERO, when the temperature is missing. greatest() skips
            # nulls, so the obvious greatest(16 - temp, 0) quietly returns 0.0
            # for a day with no reading -- indistinguishable from a warm day,
            # and it would sum straight into a monthly total. The dictionary is
            # explicit that a day no station covers leaves HDD null and makes
            # the bill period unexplainable; this is where that starts.
            F.when(mean.isNull(), F.lit(None).cast("double"))
             .otherwise(F.round(F.greatest(F.lit(HDD_BASE_C) - mean, F.lit(0.0)), 2))
             .alias("hdd16"),
        )
    )


def _ensure_schema(spark, url, properties) -> None:
    """Spark's JDBC writer creates tables but not schemas."""
    import py4j
    jvm = spark._jvm
    conn = jvm.java.sql.DriverManager.getConnection(url, properties["user"], properties["password"])
    try:
        st = conn.createStatement()
        st.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        st.execute(f"COMMENT ON SCHEMA {SCHEMA} IS "
                   f"'Analysis rollups over the Stage 3 lake, rebuilt whole by "
                   f"analysis/metabase/summarise_lake.py. NOT pipeline output -- that is Stage 5.'")
        st.close()
    except py4j.protocol.Py4JJavaError as exc:
        raise SystemExit(f"could not create {SCHEMA}: {exc}") from exc
    finally:
        conn.close()


def _write(df, table: str, url, properties) -> None:
    df.write.mode("overwrite").option("truncate", "false") \
        .jdbc(url=url, table=f"{SCHEMA}.{table}", properties=properties)


def _load_dates(spark, layer: str, entity) -> list[dt.date]:
    prefix = (f"bronze/{entity.source}/{entity.table}" if layer == "raw"
              else f"bronze/parsed/{entity.source}/{entity.table}")
    jvm = spark._jvm
    root = jvm.org.apache.hadoop.fs.Path(lake.s3a(prefix, BUCKET))
    fs = root.getFileSystem(spark._jsc.hadoopConfiguration())
    if not fs.exists(root):
        return []
    out = []
    for status in fs.listStatus(root):
        m = re.search(r"load_date=(\d{4}-\d{2}-\d{2})", status.getPath().getName())
        if m:
            out.append(dt.date.fromisoformat(m.group(1)))
    return sorted(out)


def _compact_generation_date(spark) -> dt.date | None:
    """Newest interval partition whose manifest covers more than one day."""
    entity = spec.get("src_metering.interval_reads")
    ranges = []
    for load_date in _load_dates(spark, "raw", entity):
        manifest = _manifest(spark, lake.raw_prefix(entity.source, entity.table, load_date))
        if not manifest:
            continue
        lo = manifest.get("window_start_utc")
        hi = manifest.get("window_end_utc")
        if not lo or not hi:
            continue
        if dt.datetime.fromisoformat(hi) - dt.datetime.fromisoformat(lo) > dt.timedelta(days=2):
            ranges.append(load_date)
    return max(ranges) if ranges else None


def _manifest(spark, prefix: str):
    import json
    jvm = spark._jvm
    path = jvm.org.apache.hadoop.fs.Path(lake.s3a(lake.manifest_key(prefix), BUCKET))
    fs = path.getFileSystem(spark._jsc.hadoopConfiguration())
    if not fs.exists(path):
        return None
    stream = fs.open(path)
    try:
        return json.loads(bytes(stream.readAllBytes()).decode("utf-8"))
    finally:
        stream.close()


def _ts(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(value).replace(tzinfo=None)


if __name__ == "__main__":
    raise SystemExit(main())
