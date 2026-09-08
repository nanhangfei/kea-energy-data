"""Read an .avsc and say what columns a conforming Parquet file must have.

Stage 3's fourth checkbox is "Parse writes Parquet conforming to the Avro
schema". That is only meaningful if something checks it, and the check has to
catch the failure that actually costs money: a column Snowflake reads as the
wrong type. A schema drifting from its data is invisible until a query returns
a number nobody expected.

TWO LAYERS, AND ONLY THE FIRST ONE LIVES HERE.

  * Structural -- names, order, types, nullability. Decidable from the .avsc
    and the Parquet FOOTER, with no rows read at all.
  * Value-level -- a `quality` outside its symbol list, say. That needs
    fastavro and actual rows, and it belongs with the parse job.

This module deliberately produces a NEUTRAL field list rather than a
pyarrow.Schema, and `to_arrow()` is a thin layer on top that imports pyarrow
lazily. The reason is practical: the host has no pyarrow, so a pyarrow-typed
core would make the whole of step 0 un-runnable outside a container, and the
point of step 0 is that it needs no infrastructure. The mapping is still one
function, in one place, tested on its own.

WHY THE METADATA IS A SECOND SCHEMA. Every parsed dataset is "its entity's
schema, then exactly the _pipeline_metadata block". Keeping the block separate
means KEA-047 stays honest on a Kafka topic, where a load date and an S3 key
are meaningless -- and it makes the composition rule something the conformance
check enforces rather than something a reviewer has to remember.

Stdlib only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple

#: Resolved relative to this file, so lib/ and contracts/ must stay siblings
#: wherever they are mounted. KEA_CONTRACTS_DIR overrides it, so a layout
#: change is a config fix rather than a code change -- and so a wrong mount
#: fails loudly at import instead of silently reading a stale schema.
CONTRACTS = Path(
    os.environ.get("KEA_CONTRACTS_DIR", Path(__file__).resolve().parents[2] / "contracts")
) / "avro"

KEA_047 = CONTRACTS / "kea-047-meter-read.avsc"
PIPELINE_METADATA = CONTRACTS / "_pipeline_metadata.avsc"


def contract_for(source: str, table: str) -> Path:
    """The .avsc describing one source entity.

    Sixteen entities are derived by `make avsc` and land at
    `<source-without-src_>/<table>.avsc`. The seventeenth does not: KEA-047 is
    hand-written under its own specification number, because it is the contract
    design §5.3 argues over and the one Sprint 5 re-serialises onto Kafka.

    That exception lives here rather than in each caller. It cost a load
    failure once already -- the Snowflake loader copied extract_small.py's
    naive `source/table.avsc` mapping, which is correct for every entity that
    script handles precisely because interval_reads is Spark's and never
    reaches it.
    """
    if (source, table) == ("src_metering", "interval_reads"):
        return KEA_047
    return CONTRACTS / source.removeprefix("src_") / f"{table}.avsc"


class SchemaError(ValueError):
    """The .avsc says something this pipeline cannot express in Parquet."""


class Field(NamedTuple):
    """One column, in the only terms the conformance check needs."""

    name: str
    kind: str  # long | int | string | boolean | timestamp-micros | date | time-micros | decimal(p,s)
    nullable: bool
    symbols: tuple[str, ...] = ()  # enums only, for the value-level check


def load(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _kind(node) -> tuple[str, tuple[str, ...]]:
    """Normalise one Avro type node to a kind string and any enum symbols."""
    if isinstance(node, str):
        if node not in ("long", "int", "string", "boolean", "double", "float"):
            raise SchemaError(f"unsupported Avro primitive {node!r}")
        return node, ()

    if not isinstance(node, dict):
        raise SchemaError(f"cannot read Avro type node {node!r}")

    logical = node.get("logicalType")
    if logical in ("timestamp-micros", "date", "time-micros"):
        # `date` and `time-micros` are here because the catalogue really uses
        # them: a plan version is valid from a DATE (no midnight to argue
        # over), and a TOU window runs from a TIME OF DAY that recurs every
        # day and is not an instant at all. Flattening either into a timestamp
        # would invent a zone the source does not have.
        return logical, ()
    if logical == "decimal":
        try:
            return f"decimal({node['precision']},{node['scale']})", ()
        except KeyError as exc:
            raise SchemaError(f"decimal without {exc.args[0]}") from exc
    if node.get("type") == "array":
        # days_of_week is smallint[] in the serving schema and an array here.
        # Only arrays of a primitive are supported: nothing in this platform
        # needs a nested record, and guessing at one would be worse than
        # refusing.
        item, _ = _kind(node["items"])
        return f"array<{item}>", ()
    if node.get("type") == "enum":
        # Parquet has no enum type; Spark and pyarrow both land it as a string.
        # The symbol list survives here so the value-level check can use it.
        return "string", tuple(node["symbols"])
    if logical is not None:
        raise SchemaError(f"unsupported Avro logicalType {logical!r}")
    return _kind(node["type"])


def fields(schema: dict) -> list[Field]:
    """The record's columns, in declaration order.

    Order matters and is asserted downstream: a Parquet file whose columns are
    the right set in the wrong order still reads, and still produces a schema
    mismatch the first time anything positional touches it.
    """
    if schema.get("type") != "record":
        raise SchemaError(f"expected a record, got {schema.get('type')!r}")

    out: list[Field] = []
    for field in schema["fields"]:
        node = field["type"]
        nullable = False
        if isinstance(node, list):
            # A union. Only ['null', X] is supported: a genuine multi-type
            # union has no single Parquet column type, and nothing in this
            # platform needs one.
            non_null = [n for n in node if n != "null"]
            if len(node) != 2 or "null" not in node or len(non_null) != 1:
                raise SchemaError(
                    f"{field['name']}: only ['null', X] unions are supported, got {node!r}"
                )
            nullable, node = True, non_null[0]
        kind, symbols = _kind(node)
        out.append(Field(field["name"], kind, nullable, symbols))
    return out


def parsed_fields(
    entity_schema: Path = KEA_047, metadata_schema: Path = PIPELINE_METADATA
) -> list[Field]:
    """What a parsed Parquet file must contain: the entity, then the metadata.

    Composition rather than a merged schema, so the rule in design 4.2 -- every
    bronze file carries _extracted_at / _source_system / _source_file -- is
    applied identically to all seventeen entities and cannot be half-applied to
    one of them.
    """
    entity = fields(load(entity_schema))
    metadata = fields(load(metadata_schema))

    clash = {f.name for f in entity} & {f.name for f in metadata}
    if clash:
        raise SchemaError(f"metadata column would shadow a data column: {sorted(clash)}")
    return entity + metadata


def to_snowflake_ddl(schema_fields: list[Field]) -> str:
    """Column definitions for a Snowflake CREATE TABLE, from the contract.

    WHY THIS EXISTS RATHER THAN write_pandas(auto_create_table=True). That flag
    infers column types from the DataFrame it is handed, which means the FIRST
    load date silently defines the schema for every load after it. A
    decimal(12,2) column whose first day happened to hold only two-digit
    amounts became NUMBER(4,2), and the second day failed with "Number out of
    representable range ... value 135.95". The rounding case is worse: a
    narrower scale would have truncated cents rather than erroring.

    The contract already states the precision the source declares. Building the
    table from it means the schema is a property of the agreement, not of
    whichever rows arrived first.

    TIMESTAMP_TZ, not _LTZ or _NTZ. Every timestamp in these contracts is
    timestamp-micros, which is an INSTANT; _TZ keeps the offset with the value
    so nothing depends on the session's TIMEZONE parameter to read it back the
    way it was written.
    """
    def one(f: Field) -> str:
        if f.kind.startswith("decimal("):
            precision, scale = (int(n) for n in f.kind[8:-1].split(","))
            sql = f"NUMBER({precision},{scale})"
        elif f.kind.startswith("array<"):
            sql = "ARRAY"
        else:
            sql = {
                "long": "NUMBER(38,0)",
                "int": "NUMBER(38,0)",
                "string": "VARCHAR",
                "boolean": "BOOLEAN",
                "double": "FLOAT",
                "float": "FLOAT",
                "timestamp-micros": "TIMESTAMP_TZ",
                "date": "DATE",
                "time-micros": "TIME",
            }[f.kind]
        null = "" if f.nullable else " NOT NULL"
        return f'"{f.name.upper()}" {sql}{null}'

    return ",\n    ".join(one(f) for f in schema_fields)


def to_arrow(schema_fields: list[Field]):
    """Map to a pyarrow.Schema. Imported lazily -- see the module docstring."""
    import pyarrow as pa

    def one(f: Field):
        if f.kind.startswith("decimal("):
            precision, scale = (int(n) for n in f.kind[8:-1].split(","))
            return pa.decimal128(precision, scale)
        return {
            "long": pa.int64(),
            "int": pa.int32(),
            "string": pa.string(),
            "boolean": pa.bool_(),
            "double": pa.float64(),
            "float": pa.float32(),
            "timestamp-micros": pa.timestamp("us", tz="UTC"),
            "date": pa.date32(),
            "time-micros": pa.time64("us"),
        }[f.kind] if not f.kind.startswith("array<") else pa.list_(
            {"int": pa.int32(), "long": pa.int64(), "string": pa.string()}[f.kind[6:-1]]
        )

    return pa.schema([pa.field(f.name, one(f), nullable=f.nullable) for f in schema_fields])
