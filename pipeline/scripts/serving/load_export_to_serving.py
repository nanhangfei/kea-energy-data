"""Load the exported warehouse tables into the backend's database.

    python3 load_export_to_serving.py                  # every table, in FK order
    python3 load_export_to_serving.py metering.icp     # one

The second half of Stage 5. export_warehouse_to_lake.py put Parquet under
export/<schema>.<table>/ in MinIO; this reads it back and applies design §7.2's
per-table strategy. The two share kea_pipeline.serving.TABLES, so neither can
invent a table or an order the other does not know about.

WHY COPY AND A TEMP TABLE, RATHER THAN INSERT.

    CREATE TEMP TABLE _stg_x (LIKE target INCLUDING DEFAULTS);
    COPY _stg_x FROM STDIN (FORMAT csv, NULL '');
    DELETE FROM target ...;  INSERT INTO target SELECT * FROM _stg_x;

Three things fall out of `LIKE target`, and the third is the one that would
otherwise cost an afternoon:

  * the target is empty for the shortest possible window, inside one
    transaction, so a mid-load failure leaves it exactly as it was -- Stage 5's
    second exit condition;
  * COPY is an order of magnitude faster than row-by-row inserts, which matters
    at 9.9M reads;
  * the staging table inherits the target's COLUMN TYPES, so Postgres parses
    each value on the way in. catalogue.tou_band.days_of_week is smallint[],
    and the export hands it the literal '{1,2,3,4,5,6,7}' -- COPY turns that
    into an array with no adaptation code here at all. The same holds for every
    enum: 'direct_debit' becomes common.payment_method because the column says
    so, not because this file knows the type exists.

NULL '' WITH QUOTED EMPTY STRINGS. pyarrow writes a null as an unquoted empty
field and an empty string as a quoted one, and Postgres CSV reads exactly that
distinction. Without it every empty text field would arrive as NULL and every
NOT NULL text column would start rejecting rows that were fine in the warehouse.

IDENTITY COLUMNS AND OVERRIDING SYSTEM VALUE. Every serving primary key is
GENERATED ALWAYS AS IDENTITY, so an INSERT that supplies one is refused unless
it says OVERRIDING SYSTEM VALUE. The ids here come from the warehouse's key
maps and must be preserved -- they are what makes a serving row traceable back
through dbt to the bronze extract. See sync_serving_sequences() for the other
half of that bargain.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from kea_pipeline import lake, lineage, serving  # noqa: E402


def _connect():
    import psycopg2
    # kea_loader, not the owner. Design §1.1 makes the loader's lack of reach
    # into app.* a property of the credential, and connecting as the owner
    # would quietly make that untrue -- the same mistake set_extract_roles.sh
    # exists to prevent on the way in.
    #
    # NO DEFAULT HOST. The served tables live in the backend's database. A
    # loader that fell back to a local Postgres would report success while the
    # application went on reading something else.
    dsn = os.environ.get("KEA_PUBLISH_DSN")
    if not dsn:
        raise SystemExit(
            "KEA_PUBLISH_DSN is not set in this container. Point it at the\n"
            "backend database as kea_loader; see .env.example. First time on a\n"
            "database: make publish-serving RELEASE=<id> MIGRATE=1 ROLES=1")
    return psycopg2.connect(dsn)


def _read_export(table: serving.Table):
    """Yield pyarrow Tables, one per exported part-file.

    Part by part rather than as one dataset: meter_read_hh is 9.9M rows and
    holding it whole in this process buys nothing -- COPY streams either way.
    """
    import pyarrow.dataset as ds
    from pyarrow import fs

    bucket = os.environ.get("KEA_LAKE_BUCKET", lake.BUCKET)
    prefix = lake.export_prefix(table.name)
    s3 = lake.s3_filesystem()
    root = f"{bucket}/{prefix}"

    # The seal, not the directory. An export can exist and be half-written.
    if s3.get_file_info(f"{root}/_SUCCESS").type == fs.FileType.NotFound:
        raise SystemExit(f"  {table.name}: no sealed export at {prefix} -- run the exporter first")

    dataset = ds.dataset(root, filesystem=s3, format="parquet",
                         exclude_invalid_files=True)
    for frag in dataset.get_fragments():
        table_part = frag.to_table()
        # SNOWFLAKE FOLDS UNQUOTED IDENTIFIERS UP, POSTGRES FOLDS THEM DOWN.
        # So every exported column arrives as ICP_ID and every serving column
        # is icp_id, and COPY -- which needs the names quoted, because some of
        # them are reserved words -- would look for a column that does not
        # exist. Renaming here rather than quoting differently keeps the
        # mismatch in one place instead of in every export query.
        table_part = table_part.rename_columns(
            [c.lower() for c in table_part.column_names])
        yield table_part


def _copy(cur, target: str, arrow_table) -> int:
    """COPY one Arrow table into an existing Postgres table."""
    import pyarrow.csv as pacsv

    if arrow_table.num_rows == 0:
        return 0
    buf = io.BytesIO()
    pacsv.write_csv(
        arrow_table, buf,
        pacsv.WriteOptions(include_header=False, quoting_style="needed"))
    buf.seek(0)
    cols = ", ".join(f'"{c}"' for c in arrow_table.column_names)
    cur.copy_expert(
        f"COPY {target} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')", buf)
    return arrow_table.num_rows


def _stage(cur, table: serving.Table) -> tuple[str, int, list[str]]:
    """Load the export into a temp table shaped like the target."""
    stg = f"_stg_{table.name.replace('.', '_')}"
    cur.execute(f"CREATE TEMP TABLE {stg} (LIKE {table.name} INCLUDING DEFAULTS) "
                f"ON COMMIT DROP")
    rows, columns = 0, []
    for part in _read_export(table):
        columns = columns or part.column_names
        rows += _copy(cur, stg, part)
    return stg, rows, columns


def _delete_for(cur, table: serving.Table, stg: str, full: bool) -> None:
    """Clear what this table's load is about to replace.

    `full` IS THE DIFFERENCE BETWEEN A REPLICA LOAD AND A NIGHTLY ONE, and it
    changes what DELETE_BY_PERIOD is allowed to mean.

    Design §7.2 gives bills a period-scoped delete so a reissued invoice
    replaces its own period without touching its neighbours. That is right when
    billing.bill is loaded on its own. It is wrong in a full run, because the
    parents are being replaced too: a bill whose period is not in today's
    export still references an account that is about to be deleted, and the
    delete fails on the foreign key -- which is exactly how the fixtures' own
    bills blocked the first full load.

    So a table loaded ALONGSIDE its parents is cleared completely, and the
    period scope applies only when it is loaded alone.
    """
    if table.strategy == serving.TRUNCATE or full:
        cur.execute(f"DELETE FROM {table.name}")
    elif table.period_column:
        cur.execute(f"DELETE FROM {table.name} WHERE {table.period_column} IN "
                    f"(SELECT DISTINCT {table.period_column} FROM {stg})")
    else:
        # bill_line has no period of its own and is scoped through its parent,
        # which is also why it must load after the bill.
        cur.execute(f"DELETE FROM {table.name} WHERE bill_id IN "
                    f"(SELECT DISTINCT bill_id FROM {stg})")


def _insert(cur, table: serving.Table, stg: str, columns: list[str]) -> None:
    # AN EMPTY EXPORT IS A NO-OP, NOT A SYNTAX ERROR.
    #
    # _stage reads the export as record BATCHES and takes the column names from
    # the first one. A Parquet file holding zero rows still has a schema but
    # yields no batches, so `columns` comes back empty and this used to build
    # `INSERT INTO billing.bill () OVERRIDING SYSTEM VALUE SELECT ...`, which
    # Postgres rejects and which rolls back the whole twenty-table
    # transaction. One legitimately empty table took every other one down.
    #
    # Found on a three-date rebuild, where no complete billing period exists so
    # billing.bill exports nothing. The full span never hits it, which is
    # exactly why it would have sat here.
    #
    # The DELETE above still runs: an empty export for a TRUNCATE table means
    # the table should END empty, and skipping the delete as well would leave
    # yesterday's rows behind claiming to be today's.
    if not columns:
        print("  (export is empty -- deleted, nothing to insert)")
        return
    cols = ", ".join(f'"{c}"' for c in columns)
    cur.execute(f"INSERT INTO {table.name} ({cols}) OVERRIDING SYSTEM VALUE "
                f"SELECT {cols} FROM {stg}")


def _set_synced_at(cur, table: serving.Table) -> None:
    """One value for the whole table, at commit time -- not DEFAULT now() per
    row. A slow load would otherwise spread timestamps across a single table
    and "how fresh is this replica" would stop having an answer."""
    cur.execute("""select 1 from information_schema.columns
                   where table_schema=%s and table_name=%s
                     and column_name='synced_at'""",
                (table.schema, table.name.split(".", 1)[1]))
    if cur.fetchone():
        cur.execute(f"UPDATE {table.name} SET synced_at = now()")


def _clear_orphaned_forecasts(cur, staged) -> None:
    """Drop forecast history for accounts this load is about to remove.

    kea.bill_forecast IS NOT STAGE 5'S TO LOAD. It holds the output of a
    forecast, which is Stage 6's; the warehouse produces mart_forecast_input,
    the INPUTS, and nothing here can fill the other table honestly.

    But it has a foreign key to billing.account, and a full load replaces the
    account universe. A forecast whose account no longer exists is not history
    worth keeping -- it is a dangling reference to a customer this database has
    stopped describing -- and leaving it in place makes the account delete
    fail, which is how fixture 09's seeded forecasts blocked the load.

    SCOPED TO THE ACCOUNTS THAT ARE DISAPPEARING, deliberately. Clearing the
    whole table would be simpler and would silently destroy real forecast
    history the first time this runs against a database that has some.
    """
    stg_account = next((stg for t, stg, _, _ in staged
                        if t.name == "billing.account"), None)
    if stg_account is None:
        return
    cur.execute(f"""DELETE FROM kea.bill_forecast
                    WHERE account_id NOT IN (SELECT account_id FROM {stg_account})""")
    if cur.rowcount:
        print(f"  cleared {cur.rowcount:,} forecast rows for accounts being removed")


def load_fk_set(conn, tables: list[serving.Table], *, commit=True) -> dict[str, int]:
    """Load every FK-bound table as ONE transaction.

    DESIGN §7.2 DESCRIBES THIS PER TABLE -- "truncate + reload, one
    transaction" -- AND PER TABLE IT CANNOT WORK. Emptying metering.property
    while metering.icp still references it is a foreign key violation, and so
    is every other parent in the graph; the order that avoids it does not exist
    while each table commits on its own, because some table is always a parent
    of one that has not been reloaded yet.

    So the unit of atomicity is the whole set: stage everything, DELETE in
    reverse dependency order so children go before parents, INSERT in forward
    order so parents arrive before children, and commit once.

    This is STRONGER than what §7.2 promises, not weaker. The design's exit
    condition is that a mid-table failure leaves that table unchanged; this
    leaves the entire serving store unchanged, so a load can never be observed
    half-applied across two tables that reference each other.

    metering.meter_read_hh is not here. It carries no foreign keys by design
    (§7.4), which is exactly what lets it swap partitions in its own
    transaction while this one runs.
    """
    cur = conn.cursor()
    # CHECKED ONCE, AT COMMIT, AGAINST THE FINISHED STATE. A replica load
    # deletes a row and re-inserts it with the same id inside one transaction,
    # and an immediately-checked foreign key fails on the delete even though
    # the row is back before commit. No delete order avoids that in general.
    # The constraints are DEFERRABLE INITIALLY IMMEDIATE, so this affects this
    # transaction only -- every other writer still gets statement-level checks.
    cur.execute("SET CONSTRAINTS ALL DEFERRED")
    staged = []
    for t in tables:
        stg, rows, columns = _stage(cur, t)
        staged.append((t, stg, rows, columns))

    full = len(tables) > 1
    if full:
        _clear_orphaned_forecasts(cur, staged)
    for t, stg, _, _ in reversed(staged):          # children first
        _delete_for(cur, t, stg, full)
    for t, stg, _, columns in staged:              # parents first
        _insert(cur, t, stg, columns)
        _set_synced_at(cur, t)

    if commit:
        conn.commit()
    return {t.name: rows for t, _, rows, _ in staged}


def load_partition_swap(conn, table: serving.Table, *, commit=True) -> int:
    """Design §7.3: build each month beside the table, then swap it in.

    THE ORDER HERE IS THE WHOLE TRICK. Postgres refuses to attach a partition
    that overlaps rows already in DEFAULT, and DEFAULT holds the fixture reads.
    So DEFAULT is emptied first; only then can the months covering those same
    reads be attached.

    Each new month carries a CHECK constraint matching its bounds, which is
    what makes ATTACH a metadata operation instead of a full validation scan.
    The constraint is dropped afterwards: it has done its job and would
    otherwise cost a second test per row on every future insert.
    """
    parent = table.name
    cur = conn.cursor()
    stg, rows, columns = _stage(cur, table)
    cols = ", ".join(f'"{c}"' for c in columns)

    cur.execute(f"""
        select distinct date_trunc('month', interval_start
                                   at time zone 'Pacific/Auckland')::date
        from {stg} order by 1""")
    # THE BOUNDS MUST BE NZ INSTANTS, NOT DATES.
    #
    # A date handed to a timestamptz comparison is resolved in the SESSION
    # timezone, and this container runs UTC. NZ midnight on the 1st is 12:00
    # UTC on the LAST DAY OF THE PREVIOUS MONTH, so a bound of '2023-06-01'
    # sits twelve hours after the NZ month actually begins -- and the readings
    # in between belong to no month at all. They were not rejected; the
    # per-month INSERT simply never selected them, and 48 rows vanished
    # silently between a 9,732,560-row export and a 9,732,512-row table.
    #
    # Building the boundary in Pacific/Auckland and letting psycopg2 send an
    # aware datetime makes the partition edges land where the local month
    # does, which is where design §4 says an NZ day starts.
    from zoneinfo import ZoneInfo
    NZ = ZoneInfo("Pacific/Auckland")

    def month_start(d):
        return dt.datetime(d.year, d.month, 1, tzinfo=NZ)

    months = [r[0] for r in cur.fetchall()]

    cur.execute(f"TRUNCATE {parent}_default")      # before any ATTACH

    for m in months:
        nxt = (m.replace(year=m.year + 1, month=1, day=1) if m.month == 12
               else m.replace(month=m.month + 1, day=1))
        lo, hi = month_start(m), month_start(nxt)
        suffix = m.strftime("%Y_%m")
        part, new = f"{parent}_{suffix}", f"{parent}_{suffix}_new"
        bare = part.split(".")[-1]

        cur.execute(f"DROP TABLE IF EXISTS {new}")
        cur.execute(f"CREATE TABLE {new} (LIKE {parent} INCLUDING ALL)")
        cur.execute(f"""ALTER TABLE {new} ADD CONSTRAINT chk_range
                        CHECK (interval_start >= %s AND interval_start < %s)""", (lo, hi))
        cur.execute(f"""INSERT INTO {new} ({cols}) SELECT {cols} FROM {stg}
                        WHERE interval_start >= %s AND interval_start < %s""", (lo, hi))

        cur.execute("select to_regclass(%s)", (part,))
        if cur.fetchone()[0] is not None:
            cur.execute(f"ALTER TABLE {parent} DETACH PARTITION {part}")
            cur.execute(f"DROP TABLE {part}")
        cur.execute(f"ALTER TABLE {new} RENAME TO {bare}")
        cur.execute(f"ALTER TABLE {parent} ATTACH PARTITION {part} "
                    f"FOR VALUES FROM (%s) TO (%s)", (lo, hi))
        cur.execute(f"ALTER TABLE {part} DROP CONSTRAINT chk_range")

    # ⚠️ A MONTH THE EXPORT NO LONGER CARRIES MUST BE DROPPED, NOT LEFT.
    #
    # The loop above only visits months PRESENT IN THE STAGED DATA, so it can
    # replace a partition and can never remove one. But the export applies a
    # 13-month retention window anchored on the data's own max
    # (metering.meter_read_hh.sql), so months fall out of it as the data moves
    # forward -- and each one left behind is rows serving holds that the
    # warehouse no longer has.
    #
    # Normally the window advances a day at a time and this leaks one partition
    # a month, slowly enough to look like nothing. Moving the span forward by
    # 364 days dropped twelve months out of the window at once: 8,062,810 rows
    # for 2025-07 .. 2026-06 survived in serving carrying the OLD shift, while
    # every month the export did carry was correctly replaced. Mixed shifts in
    # one table, and no per-month check would see it.
    #
    # Safe for the fixtures, and deliberately so: the export carries their
    # entire span rather than the 13-month window, so their months are in
    # `months` and are kept by the same rule that discards the rest.
    schema, bare_parent = parent.split(".", 1)
    keep = {m.strftime("%Y_%m") for m in months}
    cur.execute("""select c.relname
                     from pg_inherits i
                     join pg_class c on c.oid = i.inhrelid
                     join pg_class p on p.oid = i.inhparent
                     join pg_namespace n on n.oid = p.relnamespace
                    where n.nspname = %s and p.relname = %s""",
                (schema, bare_parent))
    for (relname,) in cur.fetchall():
        suffix = relname[len(bare_parent) + 1:]
        # The DEFAULT partition is emptied above and must survive: ATTACH needs
        # it present, and dropping it would change the table's shape.
        if suffix == "default" or suffix in keep:
            continue
        cur.execute(f"ALTER TABLE {parent} DETACH PARTITION {schema}.{relname}")
        cur.execute(f"DROP TABLE {schema}.{relname}")
        print(f"  dropped {relname}: outside the export's retention window")

    if commit:
        conn.commit()
    return rows


def sync_serving_sequences(cur) -> None:
    """Put every identity sequence above the ids that were just inserted.

    THE SAME TIME BOMB gen_source/01_schemas.sql DEFUSES ON THE WAY IN, and its
    guard does not reach here: gen_audit.sync_sequences() scans `src_%` only.
    OVERRIDING SYSTEM VALUE does not advance a sequence, so after this load
    every serving sequence still sits near 1 while rows sit far above it. It
    would not fail today. It fails the first time anything inserts a row
    WITHOUT supplying an id -- the application, months from now, on a row that
    has nothing to do with this pipeline.

    The work is done by a SECURITY DEFINER function in the DDL rather than
    inline: setval needs UPDATE on the sequence, and kea_loader holds sequence
    privileges in `kea` only. Granting it UPDATE on every sequence in four
    schemas would buy one maintenance call with a permanent widening of what a
    pipeline bug can reach; one EXECUTE grant on one function does not.
    """
    cur.execute("SELECT common.sync_serving_sequences()")


def log_marts(conn, counts: dict[str, int], started, finished,
              run_id: str | None, error: str | None) -> None:
    """One row per mart per load, in its own transaction.

    Separate because the load itself is one long transaction whose temp tables
    are ON COMMIT DROP -- committing a log row in the middle of it would take
    the staged data with it.
    """
    cur = conn.cursor()
    for t in serving.MARTS:
        if t.name == "kea.mart_load_log":
            continue
        cur.execute("""INSERT INTO kea.mart_load_log
                       (mart_name, started_at, completed_at, row_count,
                        warehouse_run_id, status, error_message)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (t.name.split(".", 1)[1], started, finished,
                     counts.get(t.name), run_id,
                     "failed" if error else "succeeded", error))
    conn.commit()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("table", nargs="?", help="e.g. metering.icp; default is all")
    ap.add_argument("--run-id", default=os.environ.get("DBT_INVOCATION_ID"),
                    help="the warehouse run this came from, for mart_load_log")
    args = ap.parse_args(argv)

    tables = [serving.get(args.table)] if args.table else list(serving.TABLES)
    reads = [t for t in tables if t.strategy == serving.PARTITION_SWAP]
    fk_set = [t for t in tables if t.strategy != serving.PARTITION_SWAP]

    conn = _connect()
    started, counts, error = dt.datetime.now(dt.UTC), {}, None
    try:
        if fk_set:
            print(f"loading {len(fk_set)} FK-bound tables as one transaction")
            for t in fk_set:
                print(f"  {t.name}  ({t.strategy})")
            # ONE LINEAGE JOB FOR THE WHOLE SET, because it is one transaction.
            # Emitting a job per table would draw a graph whose nodes can
            # succeed independently, and the entire point of this step is that
            # they cannot -- they commit together or not at all.
            with lineage.job(
                    "load_serving",
                    inputs=[lineage.lake(lake.export_prefix(t.name)) for t in fk_set],
                    outputs=[lineage.serving(*t.name.split(".", 1)) for t in fk_set]):
                counts.update(load_fk_set(conn, fk_set))
            print("  committed")
        for t in reads:
            print(f"{t.name}  ({t.strategy})")
            with lineage.job(
                    f"load_serving.{t.name}",
                    inputs=[lineage.lake(lake.export_prefix(t.name))],
                    outputs=[lineage.serving(*t.name.split(".", 1))]):
                counts[t.name] = load_partition_swap(conn, t)
            print(f"  {counts[t.name]:,} rows")

        with conn.cursor() as cur:
            sync_serving_sequences(cur)
        conn.commit()
    except Exception as exc:                       # noqa: BLE001
        conn.rollback()
        error = str(exc)[:500]
        print(f"\nFAILED: {error}")
    finally:
        try:
            log_marts(conn, counts, started,
                      dt.datetime.now(dt.UTC), args.run_id, error)
        finally:
            conn.close()

    for name in (t.name for t in tables):
        print(f"  {name:34s} {counts.get(name, 0):>12,}")
    print(f"\n{len(counts)}/{len(tables)} tables, {sum(counts.values()):,} rows")
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
