"""Reshape one load date of raw bronze into parsed Parquet conforming to KEA-047.

    spark-submit parse_interval_reads.py 2026-08-25 [--force]

Design 5: "One step, one job: reshape what the extract dropped into the
conformed schema... No business logic -- validation, quarantine routing and
key resolution all happen in dbt."

WHAT THIS JOB IS ALLOWED TO DO, stated as a rule rather than a list, because
every addition to it will look reasonable at the time:

    A parse decision is decidable from ONE ROW OF ONE FILE.

read_status 'A' and quality 'actual' are the same fact written by two teams;
rewriting one as the other decides nothing. By contrast:

  * DEDUP is forbidden. "Latest version wins" needs a second row, and it is
    silver's (design 4.1). It would also destroy the thing Stage 3 exists to
    demonstrate: the 48-row, 36-day-late revision is a version_no=2 row whose
    version_no=1 sibling sits in an older partition. Collapsing them here
    would leave the exit criterion with nothing to find.
  * KEY RESOLUTION is forbidden. (icp_identifier, register_code) -> register_id
    needs the registers table.
  * A DUPLICATE DELIVERY is not a reject. 9,263 of them are legitimate source
    rows, and spotting one needs a second row anyway.

THE ONE REJECT THAT CANNOT COME FROM THE SOURCE. src_metering.interval_reads
carries CHECK (kwh >= 0), which is the enforcement of design 8.2.3 -- export
is modelled by direction, not by sign. So a negative kwh is not a small
number: it is a row that cannot have come from the source table at all, and
meeting one means reading a file. Quarantining it is precisely what that CHECK
exists to make possible. Taking its magnitude would invent a direction the
data does not carry.

NULLABILITY IS ENFORCED AS DATA, NOT AS A FLAG. Everything arriving from a
JDBC read is nullable=True -- the driver does not carry NOT NULL through --
while KEA-047 declares all but source_file non-nullable. Parse does NOT tighten
the Parquet flag, and the reason is worth recording so nobody "fixes" it:

  * DataFrame.to() widens nullability and refuses to narrow it, because Spark
    cannot prove a column holds no nulls.
  * coalesce(col, lit(...)) does flip the flag -- by SUBSTITUTING a value for
    the null. That is silent corruption the moment the reject logic misses a
    case, which is a bad trade for a boolean in a file footer.

So the contract is enforced where it can be enforced honestly: every row with
a null in a required column is quarantined as parse.null_in_required_field,
and the conformance test asserts that the written data contains no nulls in
those columns. "Does not" is a stronger claim than "should not", and it is the
one that survives contact with a bad file. Nothing downstream loses by it --
Snowflake external tables read every Parquet column as nullable regardless.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import spark_session  # noqa: E402
from kea_pipeline import avro_schema, lake, reshape  # noqa: E402

SOURCE = "src_metering"
ENTITY = "interval_reads"

#: The order reject checks are applied in. Fixed and documented so a row that
#: fails two of them always reports the same reason -- otherwise the manifest
#: counts would shift between runs for no reason anyone could see. Nulls come
#: first because the other two cannot be evaluated on a null.
REJECT_ORDER = ("parse.null_in_required_field", "parse.enum_unknown", "parse.negative_read")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("load_date", type=dt.date.fromisoformat, help="an NZ calendar day")
    ap.add_argument(
        "--from-date",
        type=dt.date.fromisoformat,
        help="demo rebuild only: record the multi-day source window in the manifest",
    )
    ap.add_argument("--force", action="store_true", help="rewrite an already-parsed partition")
    ap.add_argument(
        "--source-prefix",
        default=None,
        help="read bronze from here instead (the negative-read fixture uses this)",
    )
    args = ap.parse_args(argv)
    if args.from_date is not None and args.from_date > args.load_date:
        ap.error("--from-date must not be after load_date")

    bucket = os.environ.get("KEA_LAKE_BUCKET", lake.BUCKET)
    raw = args.source_prefix or lake.s3a(lake.raw_prefix(SOURCE, ENTITY, args.load_date), bucket)
    parsed_at = lake.s3a(lake.parsed_prefix(SOURCE, ENTITY, args.load_date), bucket)
    rejects_at = lake.s3a(lake.quarantine_prefix(SOURCE, ENTITY, args.load_date), bucket)

    print(f"load_date   {args.load_date}")
    print(f"read        {raw}")
    print(f"parsed      {parsed_at}")
    print(f"quarantine  {rejects_at}")

    spark = spark_session.build(f"parse_interval_reads_{args.load_date}")
    spark.sparkContext.setLogLevel("WARN")
    try:
        from pyspark.sql import functions as F

        if _exists(spark, f"{parsed_at}/_SUCCESS") and not args.force:
            print("\n_SUCCESS present and --force not given; nothing to do")
            return 0

        bronze = spark.read.parquet(raw)
        bronze_rows = bronze.count()

        tagged = bronze.withColumn("_reject_reason", _reject_reason(F)).cache()

        good = tagged.filter(F.col("_reject_reason").isNull())
        bad = tagged.filter(F.col("_reject_reason").isNotNull())

        parsed = _conform(spark, good.withColumn("quality", _quality(F)))
        rejected = bad.drop("quality") if "quality" in bad.columns else bad

        parsed_rows = parsed.count()
        reject_rows = rejected.count()

        # Design 5's claim, as arithmetic: parse RESHAPES, so every input row
        # comes out somewhere. A row that is neither parsed nor quarantined has
        # been silently dropped, which is the one outcome that looks like
        # success from every angle.
        if parsed_rows + reject_rows != bronze_rows:
            raise SystemExit(
                f"parse lost rows: {bronze_rows} in, {parsed_rows} parsed + "
                f"{reject_rows} quarantined = {parsed_rows + reject_rows}"
            )

        digest = _hash(parsed)
        parsed.write.mode("overwrite").parquet(parsed_at)
        _write_json(
            spark,
            f"{parsed_at}/_MANIFEST.json",
            lake.manifest(
                source=SOURCE, entity=ENTITY, load_date=args.load_date,
                window=_window_range(args.from_date or args.load_date, args.load_date),
                mode="parsed", watermark_column=None,
                row_count=parsed_rows, content_hash=digest, source_row_count=bronze_rows,
                extracted_at=dt.datetime.now(dt.timezone.utc),
                partitions=parsed.rdd.getNumPartitions(),
            )
            | {"quarantined_rows": reject_rows, "contract": "KEA-047"},
        )

        if reject_rows:
            rejected.write.mode("overwrite").parquet(rejects_at)
            print("\nquarantined by reason:")
            for row in rejected.groupBy("_reject_reason").count().collect():
                print(f"  {row['_reject_reason']:32} {row['count']:>8,}")
        else:
            print("\nquarantine   (empty -- nothing rejected)")

        print(f"\nbronze      {bronze_rows:,}")
        print(f"parsed      {parsed_rows:,}")
        print(f"quarantined {reject_rows:,}")
        print(f"hash        {digest}")
        return 0
    finally:
        spark.stop()


def _quality(F):
    """read_status -> quality, built FROM reshape.READ_STATUS.

    Native SQL rather than a Python UDF, but driven by the same dict the pure
    functions use, so there is still one definition of the mapping. A UDF would
    be one definition too, and unusably slow on the 19M-row backfill.
    """
    column = F.upper(F.trim(F.col("read_status")))
    mapping = None
    for source_value, conformed in reshape.READ_STATUS.items():
        branch = F.when(column == source_value, F.lit(conformed))
        mapping = branch if mapping is None else mapping.when(column == source_value, F.lit(conformed))
    return mapping


def _reject_reason(F):
    """The first failing check, in REJECT_ORDER. Null when the row is fine."""
    required = [f.name for f in avro_schema.fields(avro_schema.load(avro_schema.KEA_047))
                if not f.nullable and f.name != "quality"]
    any_required_null = None
    for name in required:
        test = F.col(name).isNull()
        any_required_null = test if any_required_null is None else any_required_null | test

    unknown_status = ~F.upper(F.trim(F.col("read_status"))).isin(list(reshape.READ_STATUS))

    return (
        F.when(any_required_null | F.col("read_status").isNull(), F.lit(REJECT_ORDER[0]))
        .when(unknown_status, F.lit(REJECT_ORDER[1]))
        .when(F.col("kwh") < 0, F.lit(REJECT_ORDER[2]))
    )


def _conform(spark, df):
    """Project onto KEA-047 + the metadata block: right columns, right order,
    right types, right nullability.

    DataFrame.to() reconciles names, order and types in one step. It does NOT
    tighten nullability -- see the module docstring for why that is correct
    rather than a limitation worked around.

    The schema is built straight from the .avsc. Deliberately NOT via
    avro_schema.to_arrow(): the Spark image has no pyarrow, and the job has no
    business depending on it. Arrow is the conformance TEST's concern, which is
    the right place for it -- the test reads the Parquet footer this job wrote
    and checks it against the same contract, independently.
    """
    from pyspark.sql.types import (
        DecimalType, IntegerType, LongType, StringType, StructField, StructType, TimestampType,
    )

    kind_to_spark = {
        "long": LongType(), "int": IntegerType(), "string": StringType(),
        "timestamp-micros": TimestampType(),
    }

    def spark_type(field):
        if field.kind.startswith("decimal("):
            precision, scale = (int(n) for n in field.kind[8:-1].split(","))
            return DecimalType(precision, scale)
        return kind_to_spark[field.kind]

    # nullable=True throughout: this schema states names, order and types.
    # The contract's nullability is a data property here, checked by the
    # conformance test, not a flag asserted on a column Spark cannot vouch for.
    schema = StructType([
        StructField(f.name, spark_type(f), True) for f in avro_schema.parsed_fields()
    ])
    return df.select(*[f.name for f in schema.fields]).to(schema)


def _hash(df) -> str:
    columns = lake.hashed_columns(df.columns)
    sums = df.selectExpr(*lake.md5_chunk_sql(columns)).collect()[0]
    return lake.combine_chunk_sums(0 if s is None else int(s) for s in sums)


def _window(load_date: dt.date):
    from kea_pipeline import window
    return window.nz_day_window(load_date)


def _window_range(first_date: dt.date, last_date: dt.date):
    from kea_pipeline import window
    lo = window.nz_midnight(first_date)
    _, hi = window.nz_day_window(last_date)
    return lo, hi


def _exists(spark, uri: str) -> bool:
    path = spark._jvm.org.apache.hadoop.fs.Path(uri)
    return path.getFileSystem(spark._jsc.hadoopConfiguration()).exists(path)


def _write_json(spark, uri: str, payload: dict) -> None:
    path = spark._jvm.org.apache.hadoop.fs.Path(uri)
    stream = path.getFileSystem(spark._jsc.hadoopConfiguration()).create(path, True)
    try:
        stream.write(bytearray(json.dumps(payload, indent=2).encode("utf-8")))
    finally:
        stream.close()


def _with_lineage(argv=None) -> int:
    """main(), wrapped so this job says what it read and wrote.

    THE NAMES MUST MATCH THE OTHER STEPS EXACTLY. load_bronze_to_snowflake.py
    emits this job's OUTPUT as its input, and that shared string is the only
    thing joining Spark's half of the graph to the warehouse's. They both build
    it from kea_pipeline.lake, so the two cannot drift.
    """
    import argparse as _ap
    import datetime as _dt
    from kea_pipeline import lineage

    known, _ = _ap.ArgumentParser(add_help=False).parse_known_args(argv)  # noqa: F841
    load_date = _dt.date.fromisoformat((argv or __import__("sys").argv[1:])[0])
    with lineage.job(
            f"parse_interval_reads.{SOURCE}.{ENTITY}",
            inputs=[lineage.lake(lake.raw_prefix(SOURCE, ENTITY, load_date))],
            outputs=[lineage.lake(lake.parsed_prefix(SOURCE, ENTITY, load_date)),
                     lineage.lake(lake.quarantine_prefix(SOURCE, ENTITY, load_date))]):
        return main(argv)


if __name__ == "__main__":
    raise SystemExit(_with_lineage())
