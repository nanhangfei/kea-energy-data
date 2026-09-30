"""Pull the serving tables out of Snowflake and land them in the lake.

    python3 export_warehouse_to_lake.py                 # every table
    python3 export_warehouse_to_lake.py metering.icp    # one

DESIGN §7 SAYS `COPY INTO s3://kea-lake/export/{table}/`, AND THAT CANNOT WORK.

Snowflake is SaaS in ap-southeast-2; MinIO runs on this machine with no public
endpoint, so Snowflake cannot COPY INTO it any more than it could read the lake
as an external stage on the way up. This is the same constraint that made
load_bronze_to_snowflake.py push rather than let Snowflake pull -- mirrored.
So the rows come DOWN through this process: SELECT, Arrow batches, Parquet into
MinIO. The design document has been corrected to match; it is recorded here too
because this file is where someone would come looking for the COPY INTO.

WHAT LANDS IS THE CURRENT PICTURE, NOT A DATED PARTITION. See
lake.export_prefix: bronze keeps every day's evidence, an export is replaced
whole each run. The prefix is cleared before it is written so a table that
shrinks cannot leave last run's extra part-files behind to be loaded again.

THE RESHAPING LIVES IN pipeline/export/<schema>.<table>.sql, NOT HERE. One file
per target table, named for it. That keeps the mapping reviewable as SQL --
where the enum translations, the plan-version resolution and the retention
window can be read and argued with -- and keeps this script a transport.

Runs inside dataeng/dbt:1.12.0, which carries the connector, pandas and pyarrow.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from kea_pipeline import lake, lineage, serving  # noqa: E402

EXPORT_SQL_DIR = Path(__file__).resolve().parents[2] / "export"

#: Rows per Parquet part. Large enough that 9.9M reads do not become thousands
#: of tiny files, small enough that a batch is a few tens of MB in memory --
#: fetch_arrow_all() on meter_read_hh would hold the whole table at once.
BATCH_ROWS = 1_000_000


def _connect():
    import snowflake.connector
    missing = [v for v in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_ROLE",
                           "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_DATABASE",
                           "SNOWFLAKE_PRIVATE_KEY_PATH") if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"not set in this container: {', '.join(missing)}")
    # Explicit role, warehouse and database for profiles.yml's reason: falling
    # back to the user's session defaults is a silent way to read from
    # somewhere nobody chose.
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        private_key_file=os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"],
        role=os.environ["SNOWFLAKE_ROLE"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        database=os.environ["SNOWFLAKE_DATABASE"].upper(),
    )


def export(table: serving.Table, conn) -> int:
    """Returns the number of rows written."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    query_file = EXPORT_SQL_DIR / table.sql_file
    if not query_file.exists():
        raise SystemExit(f"  no export query for {table.name}: expected {query_file}")

    bucket = os.environ.get("KEA_LAKE_BUCKET", lake.BUCKET)
    prefix = lake.export_prefix(table.name)
    s3 = lake.s3_filesystem()
    root = f"{bucket}/{prefix}"

    print(f"{table.name}  ({table.strategy})  -> {prefix}")

    # Clear first. A part-file from a previous, larger export is not history
    # here -- it is a duplicate the loader would happily read back in.
    try:
        s3.delete_dir_contents(root, missing_dir_ok=True)
    except TypeError:                     # older pyarrow has no missing_dir_ok
        try:
            s3.delete_dir_contents(root)
        except FileNotFoundError:
            pass

    # The warehouse table this query reads is not knowable from the SQL without
    # parsing it, so the lineage input is the SCHEMA the export layer draws
    # from. Coarser than Spark's plan-derived datasets and honest about it: it
    # records that this step consumed the marts, not which columns it touched.
    cur = conn.cursor()
    cur.execute(query_file.read_text())

    rows, part, schema = 0, 0, None
    for batch in cur.fetch_arrow_batches():
        if batch.num_rows == 0:
            continue
        # fetch_arrow_batches yields a Table in this connector build and a
        # RecordBatch in others. Accepting both costs one isinstance and
        # removes a dependency on which one today's wheel happens to give.
        tbl = batch if isinstance(batch, pa.Table) else pa.Table.from_batches([batch])
        schema = schema or tbl.schema
        # Every part must share one schema or the dataset is unreadable as a
        # whole. Snowflake can hand back a later batch that types an all-null
        # column differently, so later parts are cast to the first one's.
        if tbl.schema != schema:
            tbl = tbl.cast(schema)
        with s3.open_output_stream(f"{root}/part-{part:05d}.parquet") as sink:
            pq.write_table(tbl, sink, compression="snappy")
        rows += tbl.num_rows
        part += 1

    if schema is None:
        # An empty result is a legitimate outcome for a table nothing has
        # produced yet; it must still be sealed, or the loader cannot tell
        # "empty" from "never exported" and would leave stale rows in place.
        empty = pa.table({}, schema=pa.schema([]))
        with s3.open_output_stream(f"{root}/part-00000.parquet") as sink:
            pq.write_table(empty, sink)

    # _SUCCESS LAST, ALWAYS. It is the seal the loader checks, and writing it
    # before the data would make a half-written export look complete -- the
    # same idiom, and the same reason, as the parse job's marker.
    with s3.open_output_stream(f"{root}/_SUCCESS") as sink:
        sink.write(b"")

    print(f"  exported {rows:,} rows in {part or 1} part(s)")
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("table", nargs="?", help="e.g. metering.icp; default is all")
    args = ap.parse_args(argv)

    tables = [serving.get(args.table)] if args.table else list(serving.TABLES)

    conn = _connect()
    failures, total = [], 0
    try:
        for t in tables:
            started = time.monotonic()
            try:
                # The BARE table name, uppercased. t.name is the SERVING name
                # ("kea.mart_daily_usage"); passing it whole produced
                # KEA.KEA_MARTS.kea.mart_daily_usage -- a four-part name for a
                # three-part namespace, which Marquez stores happily and no
                # human recognises.
                # A mart exports 1:1 from the mart of the same name, so that
                # input is exact and is what carries the source-to-serving path.
                # The source-shaped tables are a different case: their queries
                # join gold, silver and staging in combinations only the SQL
                # knows, so naming one model would be a guess. NO INPUT IS
                # EMITTED for those -- an absent edge is honest, and a wrong one
                # would draw a lineage nobody could act on.
                inputs = ([lineage.snowflake("kea_marts", t.name.split(".", 1)[1])]
                          if t.schema == "kea" else [])
                with lineage.job(
                        f"export_warehouse.{t.name}",
                        inputs=inputs,
                        outputs=[lineage.lake(lake.export_prefix(t.name))]):
                    total += export(t, conn)
                print(f"  {time.monotonic() - started:.1f}s")
            except Exception as exc:      # noqa: BLE001
                # ONE FAILURE IS NOT A CASCADE -- load_all_bronze_to_snowflake's
                # rule. Every table is attempted, and what failed is reported
                # together at the end rather than as whatever scrolled past.
                print(f"  FAILED: {exc}")
                failures.append((t.name, exc))
    finally:
        conn.close()

    print(f"\n{len(tables) - len(failures)}/{len(tables)} tables, {total:,} rows")
    for name, exc in failures:
        print(f"  FAILED {name}: {str(exc)[:200]}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
