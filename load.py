"""Load the data-platform schema and the sample fixtures into PostgreSQL.

    python data/load.py                 # source + serving schema, then all 9 fixtures
    python data/load.py --schema-only   # just the DDL
    python data/load.py 03 09           # the DDL, then only those fixtures
    python data/load.py --drop          # start from nothing first

## Why this is a script and not an Alembic migration

`kea.*`, `metering.*`, `billing.*` and `catalogue.*` belong to the **loader**, not to
this service. The namespace table in the platform design gives `kea_app` SELECT on them
and `kea_loader` write; Alembic is the application's migration tool, so having it create
the loader's tables would mean two authorities for one set of tables, drifting apart the
first time a column is added on either side.

So the split is:

    alembic upgrade head     app.*        what the agent writes
    python data/load.py      kea.* etc.   what the agent reads

Both point at `KEA_DATABASE_URL`, and they do not overlap.

## Why psycopg is not imported

`infra/pg-local.sh` builds a Postgres with no `psql` in the bundle, so `psql -f` is not
available to everyone on the team. The API already depends on asyncpg, so the fixtures
are executed through that — one less thing to install, and it works identically on the
machines that do have Docker.

Statements are sent as one script per file inside a transaction, which matters: the
fixtures contain `DO $$ ... $$` blocks and CTE-generated reads that a naive split on
";" would tear in half.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESIGN = HERE / "design"
SAMPLES = HERE / "sample"

#: Order matters. Source shapes first, then the serving replicas and marts that the
#: fixtures' INSERTs target. The deferred schema is Sprint 3 and is not loaded — it
#: references tables (`kea.plan_recommendation`) nothing here populates.
SCHEMA_FILES = ("kea_source_schema.sql", "kea_serving_schema.sql")

#: Everything the platform owns. `app` is Alembic's and is deliberately absent.
LOADER_SCHEMAS = (
    "src_billing",
    "src_metering",
    "src_catalogue",
    "src_weather",
    "metering",
    "billing",
    "catalogue",
    "kea",
    "common",
)


def database_url() -> str:
    """asyncpg wants a bare postgresql:// URL — SQLAlchemy's +asyncpg suffix is a
    dialect marker it does not understand."""
    url = os.getenv("KEA_DATABASE_URL", "postgresql://kea:kea@localhost:5432/kea")
    return url.replace("postgresql+asyncpg://", "postgresql://")


def fixture_files(wanted: list[str]) -> list[Path]:
    every = sorted(SAMPLES.glob("*.sql"))
    if not wanted:
        return every
    chosen = [p for p in every if re.match(r"(\d+)", p.name).group(1) in {w.zfill(2) for w in wanted}]
    missing = {w.zfill(2) for w in wanted} - {re.match(r"(\d+)", p.name).group(1) for p in chosen}
    if missing:
        sys.exit(f"no fixture numbered {', '.join(sorted(missing))} in {SAMPLES}")
    return chosen


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", nargs="*", help="fixture numbers, e.g. 03 09. Default: all")
    parser.add_argument("--schema-only", action="store_true", help="DDL, no fixtures")
    parser.add_argument(
        "--drop",
        action="store_true",
        help="drop the loader's schemas first. Never touches `app`, which holds the "
        "conversation transcripts and is Alembic's.",
    )
    args = parser.parse_args()

    try:
        import asyncpg
    except ImportError:
        sys.exit("asyncpg is not installed — run this from services/api/.venv")

    conn = await asyncpg.connect(database_url())
    try:
        if args.drop:
            # `app` is excluded by construction, not by a filter someone could edit:
            # LOADER_SCHEMAS does not contain it. Dropping the transcripts because
            # somebody wanted fresh sample data would be a bad afternoon.
            for schema in LOADER_SCHEMAS:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            print(f"dropped {len(LOADER_SCHEMAS)} loader schemas (app untouched)")

        for name in SCHEMA_FILES:
            path = DESIGN / name
            if not path.exists():
                sys.exit(f"missing {path}")
            await conn.execute(path.read_text())
            print(f"  schema  {name}")

        if not args.schema_only:
            for path in fixture_files(args.fixtures):
                # Each fixture in its own transaction. The files carry CHECK-style
                # assertions that deliberately fail the transaction rather than load
                # wrong data, and one bad fixture should not roll back the good ones.
                async with conn.transaction():
                    await conn.execute(path.read_text())
                print(f"  fixture {path.name}")

        rows = await conn.fetch(
            """
            SELECT table_schema, count(*) AS tables
            FROM information_schema.tables
            WHERE table_schema = ANY($1::text[])
            GROUP BY table_schema ORDER BY table_schema
            """,
            list(LOADER_SCHEMAS),
        )
        print("\nloaded:")
        for row in rows:
            print(f"  {row['table_schema']:14s} {row['tables']} tables")

        marts = await conn.fetch(
            """
            SELECT 'mart_account_context' AS mart, count(*) FROM kea.mart_account_context
            UNION ALL SELECT 'mart_daily_usage', count(*) FROM kea.mart_daily_usage
            UNION ALL SELECT 'mart_hourly_profile', count(*) FROM kea.mart_hourly_profile
            UNION ALL SELECT 'mart_bill_explanation', count(*) FROM kea.mart_bill_explanation
            UNION ALL SELECT 'mart_forecast_input', count(*) FROM kea.mart_forecast_input
            UNION ALL SELECT 'bill_forecast', count(*) FROM kea.bill_forecast
            ORDER BY 1
            """
        )
        print("\nkea.* rows:")
        for row in marts:
            print(f"  {row['mart']:24s} {row['count']}")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
