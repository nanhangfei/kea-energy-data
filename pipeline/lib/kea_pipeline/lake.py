"""Where things land in the lake, and what "unchanged on replay" means.

TWO PREFIX SHAPES, AND THE ASYMMETRY IS DELIBERATE.

    bronze/{source}/{entity}/load_date=D/            raw, as-received
    bronze/parsed/{source}/{entity}/load_date=D/     reshaped
    quarantine/{source}/{entity}/load_date=D/        rejected rows

`bronze/raw/{source}/...` would be prettier. Stage 3's third checkbox quotes
`bronze/{source}/{entity}/load_date=...` verbatim, and moving it makes the box
unpointable, so raw keeps the documented path. The price is that `parsed` is
now a RESERVED source name -- the day someone adds a fourth source called
parsed, the collision is silent. `check_source_name` refuses it rather than
leaving that to a reviewer.

WHAT "RE-RUNNING A LOAD DATE CHANGES NOTHING" ACTUALLY MEANS.

It cannot mean byte-identical files. `_extracted_at` records when the pull
happened, so it legitimately differs between two runs, and Parquet part-files
carry a fresh UUID each time. The claim has to be written down or it cannot be
checked:

    Two materialisations of a load date are identical when their ROW MULTISETS
    OVER EVERY COLUMN EXCEPT _extracted_at are equal.

Multiset, not set: 9,263 duplicate deliveries are legitimate source rows, and
a comparison that collapsed them would call a real change no change.

So the hash must be ORDER-INDEPENDENT and must NOT cancel duplicates. Stage 2
learned the first half the hard way -- its determinism test used an ordered
string_agg whose ORDER BY was not unique, so two identical runs compared
unequal. The second half rules out XOR, where a duplicated row cancels itself
out and a file with two copies hashes the same as a file with none. A SUM of
per-row digests has both properties.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
from typing import Iterable

BUCKET = "kea-lake"

#: Not a source. See the module docstring.
RESERVED_SOURCES = frozenset({"parsed"})

#: Excluded from the replay comparison, and the only column that may be.
VOLATILE_COLUMN = "_extracted_at"

#: 15 hex digits is 60 bits per chunk, two chunks is 120. Summed over a load
#: date's rows, comfortably inside a Spark decimal(38,0), so no wraparound to
#: reason about.
_CHUNK_HEX = 15
_CHUNKS = 2


class LakeError(ValueError):
    """A path that would collide with the layout's own conventions."""


def check_source_name(source: str) -> str:
    if source in RESERVED_SOURCES:
        raise LakeError(
            f"{source!r} is a reserved prefix in the bronze layout, not a source system; "
            "raw and parsed would collide"
        )
    if "/" in source or "=" in source:
        raise LakeError(f"source {source!r} would break the prefix structure")
    return source


def _partition(source: str, entity: str, load_date: dt.date) -> str:
    return f"{check_source_name(source)}/{entity}/load_date={load_date.isoformat()}"


def raw_prefix(source: str, entity: str, load_date: dt.date) -> str:
    """The path Stage 3's third checkbox quotes. Do not 'tidy' this."""
    return f"bronze/{_partition(source, entity, load_date)}"


def parsed_prefix(source: str, entity: str, load_date: dt.date) -> str:
    return f"bronze/parsed/{_partition(source, entity, load_date)}"


def quarantine_prefix(source: str, entity: str, load_date: dt.date) -> str:
    return f"quarantine/{_partition(source, entity, load_date)}"


def export_prefix(table: str) -> str:
    """Where a serving table's export lands, on its way back down.

    NOT partitioned by load_date, and that is the difference from bronze. A
    bronze partition is a day's evidence and every one of them is kept; an
    export is the CURRENT picture of a serving table, and yesterday's is a
    stale duplicate the moment the next one is written -- the same argument
    load_bronze_to_snowflake.py makes for truncating RAW on a snapshot. The
    history stays in bronze, which is what bronze is for.

    `table` is the fully qualified serving name, `metering.icp`, so the prefix
    says which table it feeds without a second mapping to keep in step.
    """
    if "/" in table or "=" in table:
        raise LakeError(f"table {table!r} would break the prefix structure")
    return f"export/{table}"


def s3a(prefix: str, bucket: str = BUCKET) -> str:
    """What Spark reads and writes. s3a://, not s3://: the Hadoop connector."""
    return f"s3a://{bucket}/{prefix}"



def s3_filesystem():
    """The pyarrow filesystem every non-Spark reader and writer of this lake uses.

    Spark reaches MinIO through the Hadoop s3a connector and its own config
    (see spark_session.py); everything else goes through pyarrow, and that
    "everything else" is now two callers -- the small-entity extract and the
    Snowflake loader. One function rather than two copies, so the endpoint and
    credentials cannot drift between the process that WRITES a partition and
    the one that READS it back to ship upstream.

    pyarrow is imported lazily for the same reason avro_schema.to_arrow() does
    it: this module is otherwise stdlib-only, and its tests run on a host with
    no pyarrow installed.
    """
    from pyarrow import fs
    endpoint = os.environ.get("MINIO_ENDPOINT", "http://minio:9000")
    return fs.S3FileSystem(
        access_key=os.environ["MINIO_ROOT_USER"],
        secret_key=os.environ["MINIO_ROOT_PASSWORD"],
        endpoint_override=endpoint.replace("http://", "").replace("https://", ""),
        scheme="http" if endpoint.startswith("http://") else "https",
    )

def manifest_key(prefix: str) -> str:
    """Underscore-prefixed, so Spark and Snowflake external tables both skip it
    and the manifest never pollutes the dataset it describes."""
    return f"{prefix}/_MANIFEST.json"


def manifest(
    *,
    source: str,
    entity: str,
    load_date: dt.date,
    window: tuple[dt.datetime, dt.datetime],
    mode: str,
    watermark_column: str | None,
    row_count: int,
    content_hash: str,
    source_row_count: int,
    extracted_at: dt.datetime,
    partitions: int,
) -> dict:
    """What a partition records about itself.

    `source_row_count` is not redundant with `row_count`. It is re-queried by
    the gate, and a disagreement means the source changed under a sealed load
    date -- which really happens here, because re-running `make init-data`
    rewrites received_at for every revision. Bronze immutability is a claim
    about the pipeline, not a guarantee about a generator someone may re-run,
    so the honest response is to detect it rather than assume it away.
    """
    lo, hi = window
    return {
        "load_date": load_date.isoformat(),
        "source_system": source,
        "entity": entity,
        "mode": mode,
        "watermark_column": watermark_column,
        "window_start_utc": lo.isoformat(),
        "window_end_utc": hi.isoformat(),
        "row_count": row_count,
        "source_row_count": source_row_count,
        "content_hash": content_hash,
        "hash_excludes": [VOLATILE_COLUMN],
        "partitions": partitions,
        "extracted_at": extracted_at.isoformat(),
        "extractor_version": "1",
    }


def hashed_columns(all_columns: Iterable[str]) -> list[str]:
    """Every column except the one that legitimately differs between runs."""
    return [c for c in all_columns if c != VOLATILE_COLUMN]


def combine_chunk_sums(sums: Iterable[int]) -> str:
    """Render the per-chunk sums as one comparable string.

    Kept apart from any engine so the extract (Spark) and the gate (pyarrow)
    can produce the same value from the same numbers.
    """
    return "-".join(f"{int(s):x}" for s in sums)


def row_digest(values: Iterable[object]) -> list[int]:
    """The reference implementation, in pure Python.

    Spark computes the same thing with md5(concat_ws(...)) for 19M rows; this
    exists so the definition is testable without an engine, and so the two can
    be shown to agree on a handful of rows.

    Nulls get their own sentinel rather than being skipped: concat_ws drops
    nulls, which would make ('a', None, 'b') and ('a', 'b', None) collide.
    """
    canonical = "\x1f".join("\x00" if v is None else str(v) for v in values)
    digest = hashlib.md5(canonical.encode("utf-8")).hexdigest()
    return [
        int(digest[i * _CHUNK_HEX : (i + 1) * _CHUNK_HEX], 16) for i in range(_CHUNKS)
    ]


def content_hash(rows: Iterable[Iterable[object]]) -> str:
    """Order-independent, duplicate-preserving digest of a whole partition."""
    totals = [0] * _CHUNKS
    for row in rows:
        for i, chunk in enumerate(row_digest(row)):
            totals[i] += chunk
    return combine_chunk_sums(totals)


def md5_chunk_sql(columns: list[str]) -> list[str]:
    """The same algorithm as a Spark SQL expression, one per chunk.

    char(31) and char(0), NOT '\\x1f' and '\\x00'. Spark SQL has no \\x escape:
    it reads '\\x1f' as the four characters backslash-x-1-f, so the separator
    would be ordinary text a value could contain, and the null sentinel would
    be as well. Verified in the session, not assumed.

    THE SAME ALGORITHM, NOT THE SAME NUMBER. This and `content_hash` above
    agree only where Spark's `cast(x as string)` and Python's `str(x)` agree --
    true for strings and integers, false for timestamps and decimals, which
    the two render differently. That is fine and is why nothing depends on it:
    a replay comparison is always one engine against its own earlier manifest.
    The Python version exists so the algorithm's PROPERTIES -- order
    independence, sensitivity to a duplicated row, a null distinguishable from
    the empty string -- are testable without an engine.
    """
    canonical = "concat_ws(char(31), " + ", ".join(
        f"coalesce(cast(`{c}` as string), char(0))" for c in columns
    ) + ")"
    return [
        f"sum(cast(conv(substr(md5({canonical}), {i * _CHUNK_HEX + 1}, {_CHUNK_HEX}), 16, 10) as decimal(38,0)))"
        for i in range(_CHUNKS)
    ]
