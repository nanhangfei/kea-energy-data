"""Generate the .avsc for the sixteen entities that are not KEA-047.

    python3 pipeline/scripts/gen_avsc.py            # write them
    python3 pipeline/scripts/gen_avsc.py --check    # fail if any is stale

KEA-047 is hand-written, and stays hand-written: it is the contract design §5.3
argues over, the one Sprint 5 re-serialises onto a Kafka topic, and the one
whose `quality` enum encodes a decision (that 'F' means `final` and not
`actual`). The other sixteen carry no such argument -- they are the source
tables' shapes, typed. Writing sixteen files by hand would produce sixteen
opportunities to mistype a column and no additional insight.

So they are DERIVED, and this script is the reviewable artefact: the Postgres
-> Avro type mapping is stated once, here, instead of sixteen times by hand.
The generated files are COMMITTED, so a reader sees the contract without
running anything, and `--check` in CI catches a schema that has drifted from
the source it claims to describe.

Runs against information_schema through the same four extract credentials the
pipeline uses -- so a table this script can describe is a table the pipeline
can read, and one it cannot is caught here rather than at 3am.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from kea_pipeline import spec  # noqa: E402

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "avro"

#: Postgres type -> Avro type. Every choice here is one someone would otherwise
#: make eleven times, differently.
#:
#: `numeric` becomes decimal with the column's own precision and scale, never a
#: double: these are money and kWh, and a bill has to reproduce to the cent.
#: `timestamptz` becomes timestamp-micros, an instant. `timestamp` (naive)
#: becomes timestamp-micros TOO, because parse attaches Pacific/Auckland on the
#: way through -- the conformed schema is UTC instants throughout, and a naive
#: local time is not a thing downstream should ever have to reason about.
TYPE_MAP = {
    "bigint": "long",
    "integer": "int",
    "smallint": "int",
    "text": "string",
    "character varying": "string",
    "character": "string",
    "boolean": "boolean",
    "double precision": "double",
    "real": "float",
    "date": "int-date",
    "timestamp with time zone": "timestamp",
    "timestamp without time zone": "timestamp",
    # A TOU window runs from a time of DAY that recurs -- 07:00 to 11:00 every
    # weekday -- not from an instant. Postgres stores microseconds, so does the
    # contract; flattening it to a timestamp would invent a date and a zone.
    "time without time zone": "time",
}


#: The reshapes design §5 names, expressed as overrides on the derived schema.
#:
#: WITHOUT THESE THE CONTRACT WOULD DESCRIBE THE SOURCE, NOT THE CONFORMED
#: SHAPE. §5's whole point is that parse rewrites these four, so a generated
#: .avsc taken straight from information_schema would declare `days_csv` a
#: string and then be satisfied by a parse job that did nothing -- the
#: conformance check would pass while the reshape silently stopped happening.
#:
#: Keyed by (schema, table, column) -> (conformed name, Avro type, nullable
#: unchanged). `charge_scope` appears on two tables and is reshaped on both.
RESHAPES = {
    ("src_billing", "customers", "med_dependent"):
        ("is_medically_dependent", "boolean"),
    ("src_catalogue", "tou_windows", "days_csv"):
        ("days_of_week", {"type": "array", "items": "int"}),
    ("src_catalogue", "tou_windows", "charge_scope"):
        ("charge_group", "string"),
    ("src_catalogue", "rate_components", "charge_scope"):
        ("charge_group", "string"),
}


def avro_type(data_type: str, precision, scale) -> dict | str:
    if data_type == "numeric":
        if precision is None:
            raise SystemExit("numeric without a precision cannot be a decimal contract")
        return {"type": "bytes", "logicalType": "decimal",
                "precision": int(precision), "scale": int(scale or 0)}
    mapped = TYPE_MAP.get(data_type)
    if mapped is None:
        raise SystemExit(f"no Avro mapping for Postgres type {data_type!r}; add one deliberately")
    if mapped == "timestamp":
        return {"type": "long", "logicalType": "timestamp-micros"}
    if mapped == "time":
        return {"type": "long", "logicalType": "time-micros"}
    if mapped == "int-date":
        # A date is a date, not an instant: no zone, no midnight to argue over.
        return {"type": "int", "logicalType": "date"}
    return mapped


def columns(cursor, entity: spec.Entity) -> list[dict]:
    cursor.execute(
        """
        SELECT column_name, data_type, is_nullable,
               numeric_precision, numeric_scale
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (entity.source, entity.table),
    )
    rows = cursor.fetchall()
    if not rows:
        raise SystemExit(f"{entity.qualified}: no columns -- wrong credential, or the table is gone")
    out = []
    for name, data_type, nullable, precision, scale in rows:
        override = RESHAPES.get((entity.source, entity.table, name))
        if override:
            name, typed = override
        else:
            typed = avro_type(data_type, precision, scale)
        field = {"name": name, "type": ["null", typed] if nullable == "YES" else typed}
        if nullable == "YES":
            field["default"] = None
        out.append(field)
    return out


def schema_for(cursor, entity: spec.Entity) -> dict:
    record = "".join(part.title() for part in entity.table.split("_"))
    return {
        "type": "record",
        "name": record.rstrip("s") if record.endswith("s") else record,
        "namespace": f"nz.co.kea.{entity.source.removeprefix('src_')}",
        "doc": (
            f"{entity.qualified}, as the pipeline conforms it. GENERATED by "
            f"pipeline/scripts/gen_avsc.py from information_schema -- edit the source DDL or "
            f"the generator, not this file. Extracted as a {entity.mode}"
            + (f" on {entity.watermark}." if entity.watermark else
               ", because it is far under the ~1M rows at which design §4.1 prefers an "
               "incremental read, and a snapshot cannot miss a DELETE.")
            + " Business keys are NOT resolved here and duplicates are NOT collapsed: "
            "both are silver's work."
        ),
        "fields": columns(cursor, entity),
    }


def path_for(entity: spec.Entity) -> Path:
    return CONTRACTS / entity.source.removeprefix("src_") / f"{entity.table}.avsc"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="do not write; exit 1 if any committed schema is stale")
    args = ap.parse_args(argv)

    import psycopg2

    stale, written = [], []
    for source in ("src_metering", "src_billing", "src_catalogue", "src_weather"):
        system = source.removeprefix("src_")
        password = os.environ.get(f"KEA_EXTRACT_{system.upper()}_PASSWORD")
        if not password:
            raise SystemExit(f"KEA_EXTRACT_{system.upper()}_PASSWORD is not set; run `make source-roles`")
        conn = psycopg2.connect(
            host=os.environ.get("KEA_SRC_DB_HOST", "kea-serving-db"),
            dbname=os.environ.get("KEA_SRC_DB_NAME", "kea"),
            user=f"kea_extract_{system}", password=password,
        )
        try:
            with conn.cursor() as cursor:
                for entity in spec.script_entities(source):
                    target = path_for(entity)
                    body = json.dumps(schema_for(cursor, entity), indent=2) + "\n"
                    if args.check:
                        if not target.exists() or target.read_text(encoding="utf-8") != body:
                            stale.append(str(target.relative_to(CONTRACTS.parents[1])))
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(body, encoding="utf-8")
                        written.append(target.name)
        finally:
            conn.close()

    if args.check:
        if stale:
            print("stale Avro contracts (re-run `make avsc`):")
            for s in stale:
                print(f"  {s}")
            return 1
        print("all generated Avro contracts match the source schema")
        return 0
    print(f"wrote {len(written)} contracts: {', '.join(sorted(written))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
