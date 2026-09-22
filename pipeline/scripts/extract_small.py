"""Extract and parse one small entity for one load date. No Spark.

    python3 extract_small.py src_catalogue.price_plans 2026-08-25 [--force]

Design §5.1 sizes the tool to the job: `interval_reads` is 2.45B rows a year
and needs a partitioned parallel read; the other sixteen are hundreds to
hundreds of thousands of rows and "Spark would spend longer starting up than
reading". This is that script. It runs inside the Airflow image, which already
has psycopg2, pyarrow and boto3, so a plain PythonOperator can call it -- no
sibling container, no cluster, no JVM.

EXTRACT AND PARSE IN ONE PROCESS, unlike the metering lane. There, the two are
separate jobs because a 19M-row parse is worth re-running without re-reading
the source. Here the whole entity is smaller than the startup cost of splitting
it, and one process means one place where the row count is conserved.

A SEALED PARTITION IS IMMUTABLE, and for snapshots that is what makes the exit
condition true at all. `interval_reads` can prove "re-running a load date
changes nothing" because its window is a pure function of the date. A snapshot
has no window: re-running 2026-08-25 tomorrow captures whatever the table holds
tomorrow. So once _SUCCESS is written the partition is not rewritten -- a
re-run is a no-op unless --force says otherwise, which is a decision someone
takes rather than a default. Without that rule the second exit condition would
hold for one of seventeen entities.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from kea_pipeline import avro_schema, lake, spec  # noqa: E402

def contract_for(entity: spec.Entity) -> Path:
    # In avro_schema because KEA-047 is the one entity whose contract is not
    # named after its table, and two callers must not each learn that.
    return avro_schema.contract_for(entity.source, entity.table)


def _s3():
    # Moved into lake.py when the Snowflake loader became a second caller: the
    # process that writes a partition and the one that reads it back must reach
    # the same MinIO the same way, or the drift shows up as an empty read.
    return lake.s3_filesystem()


def _connect(entity: spec.Entity):
    import psycopg2
    system = entity.source.removeprefix("src_")
    password = os.environ.get(f"KEA_EXTRACT_{system.upper()}_PASSWORD")
    if not password:
        raise SystemExit(
            f"KEA_EXTRACT_{system.upper()}_PASSWORD is not set in this process.\n"
            "Run `make source-roles`. The extract roles are created without a password "
            "on purpose, so this is a setup step and not a bug."
        )
    return psycopg2.connect(
        host=os.environ.get("KEA_SRC_DB_HOST", "kea-source-db"),
        dbname=os.environ.get("KEA_SRC_DB_NAME", "kea"),
        user=f"kea_extract_{system}", password=password,
    )


def _exists(s3, key: str) -> bool:
    from pyarrow import fs
    return s3.get_file_info(key).type != fs.FileType.NotFound


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("entity", help="e.g. src_catalogue.price_plans")
    ap.add_argument("load_date", type=dt.date.fromisoformat)
    ap.add_argument("--force", action="store_true",
                    help="rewrite a sealed partition. For a snapshot this DISCARDS the "
                         "record of what the source held that day")
    ap.add_argument(
        "--full-incremental",
        action="store_true",
        help="demo rebuild only: put the complete small incremental table in one "
             "partition instead of issuing hundreds of daily extracts",
    )
    args = ap.parse_args(argv)

    import pyarrow as pa
    import pyarrow.parquet as pq

    entity = spec.get(args.entity)
    if entity.qualified in spec.SPARK_ENTITIES:
        raise SystemExit(f"{entity.qualified} is Spark's (design §5.1); use extract_interval_reads.py")
    if args.full_incremental and entity.mode != spec.INCREMENTAL:
        raise SystemExit("--full-incremental applies only to an incremental entity")

    bucket = os.environ.get("KEA_LAKE_BUCKET", lake.BUCKET)
    raw_prefix = lake.raw_prefix(entity.source, entity.table, args.load_date)
    parsed_prefix = lake.parsed_prefix(entity.source, entity.table, args.load_date)
    s3 = _s3()

    print(f"{entity.qualified}  load_date={args.load_date}  mode={entity.mode}")

    if _exists(s3, f"{bucket}/{parsed_prefix}/_SUCCESS") and not args.force:
        print("  sealed already; nothing to do (--force to rewrite)")
        return 0

    predicate = None if args.full_incremental else entity.window_predicate(args.load_date)
    sql = f"SELECT * FROM {entity.qualified}" + (f" WHERE {predicate}" if predicate else "")
    print(f"  {sql}")

    extracted_at = dt.datetime.now(dt.timezone.utc)
    connection = _connect(entity)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            columns = [c.name for c in cursor.description]
            rows = cursor.fetchall()
    finally:
        connection.close()

    # --- raw bronze: as received, plus exactly the three provenance columns ---
    raw = {name: [r[i] for r in rows] for i, name in enumerate(columns)}
    raw["_extracted_at"] = [extracted_at] * len(rows)
    raw["_source_system"] = [entity.source] * len(rows)
    raw["_source_file"] = [f"{entity.qualified}@{args.load_date.isoformat()}"] * len(rows)
    raw_table = pa.table(raw)
    _write(s3, pq, bucket, raw_prefix, raw_table)

    # --- parsed: the contract's shape --------------------------------------
    want = avro_schema.parsed_fields(entity_schema=contract_for(entity))
    parsed_table = _conform(pa, raw, want, entity, len(rows))
    _write(s3, pq, bucket, parsed_prefix, parsed_table)

    digest = lake.content_hash(
        zip(*[parsed_table.column(f.name).to_pylist()
              for f in want if f.name != lake.VOLATILE_COLUMN])
    )
    manifest = lake.manifest(
        source=entity.source, entity=entity.table, load_date=args.load_date,
        window=(extracted_at, extracted_at) if predicate is None
        else __import__("kea_pipeline.window", fromlist=["window"]).nz_day_window(args.load_date),
        mode="demo_full" if args.full_incremental else entity.mode,
        watermark_column=entity.watermark,
        row_count=len(rows), content_hash=digest, source_row_count=len(rows),
        extracted_at=extracted_at, partitions=1,
    ) | {"contract": str(contract_for(entity).name)}
    _write_json(s3, f"{bucket}/{lake.manifest_key(raw_prefix)}", manifest)
    _write_json(s3, f"{bucket}/{lake.manifest_key(parsed_prefix)}", manifest)

    print(f"  rows {len(rows):,}   hash {digest}")
    return 0


def _conform(pa, raw: dict, want, entity: spec.Entity, n: int):
    """Apply design §5's reshapes and project onto the contract.

    The reshapes are driven by the CONTRACT: a column the .avsc renames or
    retypes is one parse has to rewrite. That is why the generated schemas
    carry the four overrides -- without them the contract would describe the
    source, and a parse job that did nothing at all would satisfy it.
    """
    from kea_pipeline import reshape

    source_names = set(raw)
    out = {}
    for field in want:
        if field.name in source_names:
            values = raw[field.name]
            if field.kind == "timestamp-micros":
                values = [reshape.to_instant(v) for v in values]
            out[field.name] = values
            continue
        # A contract column absent from the source is a reshape: find the one
        # source column it came from, or fail loudly rather than write nulls.
        if field.name == "is_medically_dependent":
            out[field.name] = [reshape.flag(v) for v in raw["med_dependent"]]
        elif field.name == "days_of_week":
            out[field.name] = [reshape.days_of_week(v) for v in raw["days_csv"]]
        elif field.name == "charge_group":
            out[field.name] = [reshape.charge_group(v) for v in raw["charge_scope"]]
        else:
            raise SystemExit(
                f"{entity.qualified}: the contract wants {field.name!r} and the source has no "
                "such column and no reshape produces it"
            )
    return pa.table(out, schema=avro_schema.to_arrow(want))


def _write(s3, pq, bucket: str, prefix: str, table) -> None:
    pq.write_table(table, f"{bucket}/{prefix}/part-00000.parquet", filesystem=s3)
    with s3.open_output_stream(f"{bucket}/{prefix}/_SUCCESS") as fh:
        fh.write(b"")


def _write_json(s3, key: str, payload: dict) -> None:
    with s3.open_output_stream(key) as fh:
        fh.write(json.dumps(payload, indent=2).encode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
