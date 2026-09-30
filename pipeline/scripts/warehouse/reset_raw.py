"""Drop every KEA.RAW table, so the next load recreates them from the contracts.

    python3 warehouse/reset_raw.py

`make build-warehouse RESET_RAW=1` runs it before staging, and so does
rebuild_span.sh. DESTRUCTIVE, and meant to be: RAW then holds only what the
next load stages.

WHY DROP AND NOT TRUNCATE. The loader creates each table from its Avro contract
with CREATE TABLE IF NOT EXISTS, so a table that survives keeps its OLD
definition -- accounts before TERM_MONTHS, for one -- and a current contract
can no longer be loaded into it. A rebuild is the schema-evolution boundary,
and only a DROP crosses it.

WHY A REBUILD NEEDS IT AT ALL. Staging replaces only the load dates it is
given. After the span or the generation moves, the old load dates would
survive beside the new ones, invisible to every per-date check and wrong in
every aggregate.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from kea_pipeline import spec  # noqa: E402

import load_bronze_to_snowflake as loader  # noqa: E402


def main() -> int:
    conn = loader._connect()
    database = os.environ["SNOWFLAKE_DATABASE"].upper()
    try:
        for entity in spec.ENTITIES:
            table = loader.table_name(entity)
            conn.cursor().execute(f'DROP TABLE IF EXISTS {database}.{loader.SCHEMA}."{table}"')
            print(f"  {table}: dropped")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
