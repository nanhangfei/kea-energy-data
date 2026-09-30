"""Archive every table in the Snowflake warehouse as Parquet, before access ends.

    python3 ops/archive_warehouse.py                 # into MinIO: archive/snowflake/<date>/
    python3 ops/archive_warehouse.py --out /archive  # and a copy in a local directory

WHY IT EXISTS. The Snowflake trial converts on 3 Oct 2026. After that nothing
here can read the warehouse: not `make build-warehouse` or `make
publish-serving`, and not the two analyses that query it directly --
`make evaluate-sprint4` (silver) and the forecast backtest's Stage 2
(`KEA_GOLD.FCT_METER_READ`). The serving store keeps its last release, and the
lake's export keeps the serving marts, but only 13 months of half-hourly reads
and nothing from RAW, staging or silver. This copies the rest down while it can
still be read.

WHAT IT COPIES. Every BASE TABLE in the database, every schema but
INFORMATION_SCHEMA, as it stands when this runs. Views are not copied: dbt
rebuilds them from the code over the archived tables, and their names are
listed in the manifest. Each table lands as Parquet part files and a
`_SUCCESS` seal, the lake's own idiom, under <root>/<SCHEMA>/<TABLE>/.
`_MANIFEST.json` at the root records Snowflake's own row count beside the rows
written; if any table disagrees, the run exits non-zero.

WHERE. MinIO first (`kea-lake/archive/snowflake/<date>/`), beside the rest of
the lake. A MinIO volume does not survive `make clean`, so `--out` writes the
same files to a directory as well; `data/raw/` is gitignored and the pre-push
guard refuses data directories, so a copy there cannot be committed by accident.

Read-only on Snowflake. Nothing reads the archive automatically. Re-running for
the same date replaces that date's archive whole. Runs inside dataeng/dbt, like
the exporter whose connection and batching it reuses.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "lib"))
sys.path.insert(0, str(HERE.parents[1] / "warehouse"))

from export_warehouse_to_lake import _connect  # noqa: E402
from kea_pipeline import lake  # noqa: E402

PREFIX = "archive/snowflake"


def inventory(conn) -> tuple[list[tuple[str, str, int]], list[str]]:
    """(base tables with Snowflake's row count, views), schema by schema."""
    cur = conn.cursor()
    cur.execute("""
        SELECT table_schema, table_name, table_type, row_count
        FROM   information_schema.tables
        WHERE  table_schema <> 'INFORMATION_SCHEMA'
        ORDER  BY table_schema, table_name""")
    tables, views = [], []
    for schema, name, kind, count in cur.fetchall():
        if kind == "BASE TABLE":
            tables.append((schema, name, int(count or 0)))
        else:
            views.append(f"{schema}.{name}")
    return tables, views


class Sinks:
    """Every write goes to MinIO and, with --out, to the local directory too."""

    def __init__(self, day: str, out: Path | None):
        self.s3 = lake.s3_filesystem()
        self.root = f"{os.environ.get('KEA_LAKE_BUCKET', lake.BUCKET)}/{PREFIX}/{day}"
        self.out = out / day if out else None

    def reset(self) -> None:
        # A re-run for the same day replaces it whole: a table that shrank must
        # not leave last run's extra part files behind.
        try:
            self.s3.delete_dir_contents(self.root, missing_dir_ok=True)
        except TypeError:                                  # older pyarrow
            try:
                self.s3.delete_dir_contents(self.root)
            except FileNotFoundError:
                pass
        if self.out and self.out.exists():
            if not (self.out / "_MANIFEST.json").exists() and any(self.out.iterdir()):
                raise SystemExit(f"{self.out} exists and is not an archive; refusing to replace it")
            shutil.rmtree(self.out)

    def parquet(self, path: str, table) -> None:
        import pyarrow.parquet as pq
        with self.s3.open_output_stream(f"{self.root}/{path}") as sink:
            pq.write_table(table, sink, compression="snappy")
        if self.out:
            target = self.out / path
            target.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, target, compression="snappy")

    def text(self, path: str, body: str) -> None:
        with self.s3.open_output_stream(f"{self.root}/{path}") as sink:
            sink.write(body.encode())
        if self.out:
            target = self.out / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")


def archive(conn, schema: str, name: str, sinks: Sinks) -> tuple[int, int]:
    """Copies one table; returns (rows, part files)."""
    import pyarrow as pa

    cur = conn.cursor()
    cur.execute(f'SELECT * FROM "{schema}"."{name}"')
    rows, part, first = 0, 0, None
    for batch in cur.fetch_arrow_batches():
        if batch.num_rows == 0:
            continue
        # As in the exporter: a Table in this connector build, a RecordBatch in
        # others; and later parts cast to the first one's schema, because
        # Snowflake can type an all-null column differently batch to batch.
        tbl = batch if isinstance(batch, pa.Table) else pa.Table.from_batches([batch])
        first = first or tbl.schema
        if tbl.schema != first:
            tbl = tbl.cast(first)
        sinks.parquet(f"{schema}/{name}/part-{part:05d}.parquet", tbl)
        rows += tbl.num_rows
        part += 1
    if first is None:
        # An empty table is still archived, so its absence cannot be mistaken
        # for a table that was never copied.
        sinks.parquet(f"{schema}/{name}/part-00000.parquet", pa.table({}))
        part = 1
    sinks.text(f"{schema}/{name}/_SUCCESS", "")
    return rows, part


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, help="also write the archive under this directory")
    args = ap.parse_args(argv)

    day = dt.date.today().isoformat()
    conn = _connect()
    tables, views = inventory(conn)
    if not tables:
        raise SystemExit("no base tables found: wrong database or role?")

    sinks = Sinks(day, args.out)
    sinks.reset()
    print(f"archiving {len(tables)} tables from {os.environ['SNOWFLAKE_DATABASE'].upper()} "
          f"-> {sinks.root}" + (f" and {sinks.out}" if sinks.out else ""))

    manifest, mismatched = [], []
    for schema, name, expected in tables:
        rows, parts = archive(conn, schema, name, sinks)
        manifest.append({"schema": schema, "table": name, "rows": rows,
                         "rows_in_snowflake": expected, "parts": parts})
        flag = "" if rows == expected else f"  MISMATCH: Snowflake says {expected:,}"
        if flag:
            mismatched.append(f"{schema}.{name}")
        print(f"  {schema}.{name:<45} {rows:>12,} rows  {parts} part(s){flag}")

    # The manifest is written last, the same way _SUCCESS is: a half-finished
    # archive has none.
    sinks.text("_MANIFEST.json", json.dumps({
        "archived_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "database": os.environ["SNOWFLAKE_DATABASE"].upper(),
        "tables": manifest,
        "views_not_archived": views,
        "total_rows": sum(t["rows"] for t in manifest),
    }, indent=2) + "\n")

    total = sum(t["rows"] for t in manifest)
    print(f"archived {len(manifest)} tables, {total:,} rows; {len(views)} views listed, not copied")
    if mismatched:
        print(f"{len(mismatched)} table(s) disagree with Snowflake's own count: {', '.join(mismatched)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
