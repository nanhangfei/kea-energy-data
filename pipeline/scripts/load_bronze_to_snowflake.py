"""Land one entity's parsed bronze for one load date in Snowflake KEA.RAW.

    python3 load_bronze_to_snowflake.py src_catalogue.price_plans 2026-08-25

Snowflake is SaaS and cannot reach the local MinIO, so the lake cannot be an
external stage and dbt cannot read Parquet where it lies. The rows have to be
pushed up. This uses the connector's PUT + COPY (`write_pandas`) against
Snowflake's own internal stage -- the same choice the sibling project made and
recorded, rather than a storage integration, which would need MinIO reachable
from AWS.

THE MODE SPLIT IS THE ONLY INTERESTING DECISION HERE.

A snapshot's bronze partition is a fresh FULL copy of the source table
(design §4.1). Yesterday's copy is not history -- it is a stale duplicate of
today's -- so RAW is TRUNCATE-and-reload for a snapshot and holds only the
current picture. The immutable day-by-day record stays where design §4.2 puts
it, in the lake, which is the thing bronze exists to be.

That split is also what keeps the staging layer honest. Design §6.1 says
staging is 1:1 with the source: rename and cast, no filtering. If RAW
accumulated every snapshot, every staging model would need a "latest
load_date" filter, which is business logic wearing a mechanical disguise --
and it would be wrong the first time two entities were loaded on different
days.

An incremental entity is the opposite: its partitions are disjoint by
construction, because window_predicate() never selects a row into two load
dates. So RAW accumulates them, and idempotency comes from deleting this load
date's own rows before appending. Re-running is then a net no-op and no other
load date is touched.

A MISSING PARTITION IS NOT A FAILURE. Weather is a monthly snapshot; most
calendar days have no bronze/parsed/src_weather/... at all. Checking _SUCCESS
and skipping cleanly is extract_small.py's "sealed already, nothing to do"
idiom, mirror-imaged for the unsealed case.

Runs inside dataeng/dbt:1.12.0, which carries the connector, pandas and
pyarrow -- see pipeline/docker/dbt/Dockerfile.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from kea_pipeline import avro_schema, lake, lineage, spec  # noqa: E402

#: One landing schema. Not one per source: the source is already in the table
#: name, and four schemas would mean four sets of grants to keep in step for a
#: boundary nothing enforces.
SCHEMA = "RAW"


def table_name(entity: spec.Entity) -> str:
    """`src_metering.interval_reads` -> `METERING__INTERVAL_READS`.

    The double underscore mirrors dbt's `stg_<source>__<entity>` (design §6.1)
    so the physical table and the model that reads it are recognisably the same
    thing. Uppercase because Snowflake folds unquoted identifiers up, and a
    lowercase name would have to be quoted forever after.
    """
    return f"{entity.source.removeprefix('src_')}__{entity.table}".upper()


def _connect():
    import snowflake.connector
    missing = [v for v in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_ROLE",
                           "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_DATABASE",
                           "SNOWFLAKE_PRIVATE_KEY_PATH") if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"not set in this container: {', '.join(missing)}")
    # Role, warehouse and database are passed explicitly for the reason
    # profiles.yml gives: falling back to the user's session defaults is a
    # silent way to write somewhere nobody chose.
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        private_key_file=os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"],
        role=os.environ["SNOWFLAKE_ROLE"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        database=os.environ["SNOWFLAKE_DATABASE"].upper(),
        schema=SCHEMA,
    )


def load(entity: spec.Entity, load_date: dt.date, conn=None) -> int:
    """Returns the number of rows landed; 0 when there is nothing to land.

    `conn` lets a caller staging several entities hand in one Snowflake session
    instead of a handshake per entity. Worth about 2 s of a 42 s step, measured
    -- and the measurement is the point, because the handshakes were the
    OBVIOUS suspect and were not the cost. write_pandas is: it creates a temp
    stage, PUTs the file, COPYs INTO and drops the stage, three round trips a
    table, and sixteen scripted tables is sixteen of those.

    Which is why the rebuild stages snapshots once rather than once per date --
    see rebuild_span.sh. Reusing the session is a real saving and a small one;
    not doing the work at all is the large one.

    When `conn` is given the caller owns it and this does not close it. When it
    is not, behaviour is exactly as before: connect, use, close.
    """
    import pyarrow.dataset as ds
    from pyarrow import fs

    bucket = os.environ.get("KEA_LAKE_BUCKET", lake.BUCKET)
    prefix = lake.parsed_prefix(entity.source, entity.table, load_date)
    s3 = lake.s3_filesystem()
    table = table_name(entity)

    print(f"{entity.qualified}  load_date={load_date}  {entity.mode}  -> {SCHEMA}.{table}")

    # _SUCCESS, not the directory: a partition can exist and be half-written.
    # The seal is what parse writes last, and it is the only thing that means
    # "this is complete".
    if s3.get_file_info(f"{bucket}/{prefix}/_SUCCESS").type == fs.FileType.NotFound:
        print("  no sealed parsed partition for this load date; nothing to load")
        return 0

    dataset = ds.dataset(f"{bucket}/{prefix}", filesystem=s3, format="parquet")

    # CONFORMANCE, RE-CHECKED AT READ TIME. Parse already validated its output
    # against this contract when it wrote it. Checking again here costs one
    # schema comparison and catches the case that check cannot: a contract
    # regenerated by `make avsc` after the partition was written, so the file
    # on disk and the file dbt is about to model have quietly diverged.
    contract = avro_schema.contract_for(entity.source, entity.table)
    fields = avro_schema.parsed_fields(entity_schema=contract)
    want = [f.name for f in fields]
    got = list(dataset.schema.names)
    if got != want:
        raise SystemExit(
            f"  {entity.qualified}: parsed columns do not match the contract\n"
            f"    file:     {got}\n    contract: {want}"
        )

    # types_mapper=ArrowDtype KEEPS THE PARQUET'S OWN TYPES. Plain to_pandas()
    # degrades anything pandas has no nullable equivalent for: a date32 column
    # becomes `object`, and an ALL-NULL date column then reaches Snowflake typed
    # as a number -- "expecting DATE but got NUMBER(38,0) for column REMOVED_ON".
    # The same degradation silently widened rel_humidity_pct from smallint to
    # float. Keeping Arrow types end to end removes the whole class rather than
    # casting columns back one at a time as each one is discovered.
    import pandas as pd
    df = dataset.to_table().to_pandas(types_mapper=pd.ArrowDtype)
    # Uppercased to match the identifiers in the CREATE TABLE above, so the
    # column names in RAW are predictable from the contract alone.
    df.columns = [c.upper() for c in df.columns]
    df["_LOAD_DATE"] = load_date
    print(f"  read {len(df):,} rows from bronze/parsed")

    from snowflake.connector.pandas_tools import write_pandas
    import snowflake.connector

    database = os.environ["SNOWFLAKE_DATABASE"].upper()
    owns_conn = conn is None
    if owns_conn:
        conn = _connect()
    # This hop crosses from the lake into the warehouse, and it is the join
    # between Spark's half of the lineage graph and everything above it: the
    # input below is the very prefix parse_interval_reads.py's listener emits as
    # its output. Same namespace, same name, so Marquez joins them into one path
    # rather than showing two.
    with lineage.job(f"snowflake_stage.{entity.qualified}",
                     inputs=[lineage.lake(prefix)],
                     # dbt's source name (`raw.interval_reads`), not the
                     # physical one (`RAW.METERING__INTERVAL_READS`). dbt-ol
                     # emits the former, and this is the hop where the two
                     # emitters have to agree or the warehouse floats free.
                     outputs=[lineage.snowflake("raw", entity.table)]):
      try:
          # THE TABLE IS CREATED FROM THE CONTRACT, NOT FROM THE DATA.
          # write_pandas(auto_create_table=True) infers column types from the
          # DataFrame it is handed, so the FIRST load date silently defines the
          # schema for every load after it. That is not hypothetical: invoices'
          # decimal(12,2) amounts became NUMBER(4,2) because 2026-08-25 happened
          # to hold only two-digit values, and 2026-08-26 failed with "Number out
          # of representable range ... value 135.95". A narrower SCALE would have
          # been worse -- silently truncated cents rather than an error.
          conn.cursor().execute(
              f'CREATE TABLE IF NOT EXISTS {database}.{SCHEMA}."{table}" (\n'
              f"    {avro_schema.to_snowflake_ddl(fields)},\n"
              f'    "_LOAD_DATE" DATE NOT NULL\n)'
          )

          if entity.mode == spec.SNAPSHOT:
              # overwrite=True truncates and reloads in one call, so there is no
              # separate DELETE to keep in step with it.
              ok, _, nrows, _ = write_pandas(
                  conn, df, table, database=database, schema=SCHEMA,
                  auto_create_table=False, overwrite=True,
                  quote_identifiers=True, use_logical_type=True)
          else:
              cur = conn.cursor()
              try:
                  cur.execute(
                      f'DELETE FROM {database}.{SCHEMA}."{table}" WHERE "_LOAD_DATE" = %s',
                      (load_date,))
                  print(f"  cleared {cur.rowcount:,} existing rows for this load date")
              except snowflake.connector.errors.ProgrammingError:
                  # The CREATE TABLE IF NOT EXISTS above ran first, so a missing
                  # table is no longer an expected case and must not be swallowed.
                  raise
              ok, _, nrows, _ = write_pandas(
                  conn, df, table, database=database, schema=SCHEMA,
                  auto_create_table=False, overwrite=False,
                  quote_identifiers=True, use_logical_type=True)
          if not ok:
              raise SystemExit(f"  write_pandas reported failure for {table}")
          print(f"  landed {nrows:,} rows")
          return nrows
      finally:
          if owns_conn:
              conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("entity", help="e.g. src_metering.interval_reads")
    ap.add_argument("load_date", type=dt.date.fromisoformat)
    args = ap.parse_args(argv)
    load(spec.get(args.entity), args.load_date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
