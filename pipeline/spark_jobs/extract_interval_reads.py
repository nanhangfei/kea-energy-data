"""Extract one load date of src_metering.interval_reads into raw bronze.

    spark-submit extract_interval_reads.py 2026-08-25 [--force]

WHY SPARK READS THIS ONE TABLE AND NOTHING ELSE. Design 5.1 sizes the tool to
the job: catalogue is hundreds of rows and billing is comfortably single-node,
so both are plain scripts. interval_reads is 2.45B rows/yr in production,
where "a single-threaded JDBC read would not finish in a batch window and a
partitioned parallel read is the only practical option". Locally it is 19.1M
rows and a median 26,943 per night -- small. The partitioning is still built
and exercised here, because Stage 3's second checkbox asks for it and the
house standard is that a box is ticked only when something in the repo can be
pointed at.

THE PARTITIONED READ USES `predicates`, NOT `partitionColumn`.

The bounded form (partitionColumn/lowerBound/upperBound/numPartitions) splits
a numeric range into equal slices. It cannot express "by received_at date
range" -- design 5.1's actual words -- and it would need a min/max probe that
changes as history grows. `predicates` takes one literal WHERE clause per
partition, so:

    one predicate  =  one Spark partition
                   =  one JDBC connection
                   =  one index range scan on ix_reads_received
                   =  one independently re-runnable unit

which is the sentence, executed. The predicates come from the shared window
module, so they tile [lo, hi) exactly and there are 23, 24 or 25 of them
depending on whether the NZ day carries a DST transition.

BRONZE IS AS-RECEIVED. No cleaning, no coercion, no renaming: read_status is
still read_status here, and becomes `quality` only in parse. The point is that
a bug in silver never requires going back to a production source system.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import spark_session  # noqa: E402
from kea_pipeline import lake, window  # noqa: E402

SOURCE = "src_metering"
ENTITY = "interval_reads"
TABLE = "src_metering.interval_reads"


def range_predicates(
    first_date: dt.date,
    last_date: dt.date,
    *,
    partitions: int = 12,
    column: str = "received_at",
) -> list[str]:
    """Split a multi-day demo window into a bounded number of JDBC scans."""
    if last_date < first_date:
        raise ValueError("last_date is before first_date")
    total_days = (last_date - first_date).days + 1
    stride = max(1, math.ceil(total_days / partitions))
    out = []
    cursor = first_date
    while cursor <= last_date:
        stop = min(last_date + dt.timedelta(days=1), cursor + dt.timedelta(days=stride))
        lo = window.nz_midnight(cursor).isoformat()
        hi = window.nz_midnight(stop).isoformat()
        sql = (f"{column} >= TIMESTAMPTZ '{lo}' AND "
               f"{column} < TIMESTAMPTZ '{hi}'")
        window.assert_no_volatile_sql(sql)
        out.append(sql)
        cursor = stop
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("load_date", type=dt.date.fromisoformat, help="an NZ calendar day")
    ap.add_argument(
        "--from-date",
        type=dt.date.fromisoformat,
        help="demo rebuild only: extract this date through load_date into one partition",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="rewrite a partition that already has _SUCCESS (replay proof uses this)",
    )
    args = ap.parse_args(argv)

    first_date = args.from_date or args.load_date
    if first_date > args.load_date:
        ap.error("--from-date must not be after load_date")
    lo = window.nz_midnight(first_date)
    _, hi = window.nz_day_window(args.load_date)
    predicates = (
        window.hourly_predicates(args.load_date, "received_at")
        if first_date == args.load_date
        else range_predicates(first_date, args.load_date)
    )
    prefix = lake.raw_prefix(SOURCE, ENTITY, args.load_date)
    target = lake.s3a(prefix, os.environ.get("KEA_LAKE_BUCKET", lake.BUCKET))

    print(f"load_date   {args.load_date}  "
          f"({(args.load_date - first_date).days + 1} NZ dates)")
    print(f"window      {lo.isoformat()} .. {hi.isoformat()}")
    print(f"partitions  {len(predicates)} received_at ranges")
    print(f"target      {target}")

    spark = spark_session.build(f"extract_interval_reads_{first_date}_{args.load_date}")
    spark.sparkContext.setLogLevel("WARN")
    try:
        from pyspark.sql import functions as F

        if _already_done(spark, target) and not args.force:
            print("\n_SUCCESS present and --force not given; nothing to do")
            return 0

        url, properties = spark_session.metering_jdbc()
        extracted_at = dt.datetime.now(dt.timezone.utc)

        raw = spark.read.jdbc(url=url, table=TABLE, predicates=predicates, properties=properties)

        # The three columns design 4.2 requires. Underscore-prefixed, because
        # interval_reads already has a data column called source_file and the
        # prefix is the only thing telling the two apart.
        landed = (
            raw.withColumn("_extracted_at", F.lit(extracted_at).cast("timestamp"))
            .withColumn("_source_system", F.lit(SOURCE))
            .withColumn(
                "_source_file",
                F.lit(f"{TABLE}@{first_date.isoformat()}..{args.load_date.isoformat()}"),
            )
        )
        landed = landed.repartition(len(predicates))
        landed.cache()

        row_count = landed.count()
        digest = _content_hash(landed)

        landed.write.mode("overwrite").parquet(target)

        _write_manifest(
            spark,
            target,
            lake.manifest(
                source=SOURCE,
                entity=ENTITY,
                load_date=args.load_date,
                window=(lo, hi),
                mode="incremental",
                watermark_column="received_at",
                row_count=row_count,
                content_hash=digest,
                source_row_count=row_count,
                extracted_at=extracted_at,
                partitions=len(predicates),
            ),
        )

        print(f"\nrows        {row_count:,}")
        print(f"hash        {digest}")
        print(f"manifest    {lake.manifest_key(prefix)}")
        return 0
    finally:
        spark.stop()


def _content_hash(df) -> str:
    """Order-independent, duplicate-preserving. See lake.py for why both.

    The expression is built in lake.py beside the pure-Python reference
    implementation, so the two can be shown to agree rather than assumed to.
    """
    columns = lake.hashed_columns(df.columns)
    sums = df.selectExpr(*lake.md5_chunk_sql(columns)).collect()[0]
    return lake.combine_chunk_sums(0 if s is None else int(s) for s in sums)


def _already_done(spark, target: str) -> bool:
    """Has this partition been written? Asked of the lake, not of a state table.

    Consistent with the window: nothing about a load date is remembered
    anywhere, so a re-run needs no cleanup and a backfill needs no ordering.
    """
    jvm = spark._jvm
    path = jvm.org.apache.hadoop.fs.Path(f"{target}/_SUCCESS")
    fs = path.getFileSystem(spark._jsc.hadoopConfiguration())
    return fs.exists(path)


def _write_manifest(spark, target: str, payload: dict) -> None:
    jvm = spark._jvm
    path = jvm.org.apache.hadoop.fs.Path(f"{target}/_MANIFEST.json")
    fs = path.getFileSystem(spark._jsc.hadoopConfiguration())
    stream = fs.create(path, True)
    try:
        stream.write(bytearray(json.dumps(payload, indent=2).encode("utf-8")))
    finally:
        stream.close()


def _with_lineage(argv=None) -> int:
    """main(), wrapped so the graph has a beginning.

    This is the only step that reads the SOURCE, so its input dataset is where
    "source to serving" starts. Its output is what parse_interval_reads.py
    declares as its input -- both built from kea_pipeline.lake, so the join
    cannot drift.
    """
    import datetime as _dt
    import sys as _sys
    from kea_pipeline import lineage

    load_date = _dt.date.fromisoformat((argv or _sys.argv[1:])[0])
    with lineage.job(
            f"extract_interval_reads.{SOURCE}.{ENTITY}",
            inputs=[lineage.source_table(SOURCE, ENTITY)],
            outputs=[lineage.lake(lake.raw_prefix(SOURCE, ENTITY, load_date))]):
        return main(argv)


if __name__ == "__main__":
    raise SystemExit(_with_lineage())
