"""The Stage 3 gate. The analogue of gen_source/99_assert.sql, over the lake.

    make assert-bronze          check every bronze partition that exists
    make assert-idempotent      re-extract a load date and compare

Stage 3 is done when "a three-week-old correction is picked up by an ordinary
nightly run, and re-running a load date changes nothing". Both halves are
claims about behaviour, and the house standard is that a box is ticked only
when there is something to point at. This is that something.

IT RUNS EVERY CHECK AND REPORTS THEM ALL, rather than raising on the first
failure the way 99_assert.sql does. That is a deliberate difference: SQL
assertions run inside one transaction against one database, so stopping early
costs nothing, but each check here is a Spark job over object storage, and
finding out about four problems takes four times as long if the script stops
at the first. The exit code is still the gate.

A NOTE ON MEASURES. "More than 21 days late" is 394 rows by elapsed time and
385 by calendar-day difference on the same partition -- datediff() truncates.
Neither is wrong; quoting one and computing the other is. Every check below
names its measure.

WHAT IS DELIBERATELY NOT CHECKED. Credential isolation is `make
assert-isolation`: it is about what the DATABASE refuses, needs no Spark, and
must connect over TCP as three different roles, which this job cannot do
holding one credential. Keeping them apart is the point.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import spark_session  # noqa: E402
from kea_pipeline import avro_schema, lake, window  # noqa: E402

SOURCE = "src_metering"
ENTITY = "interval_reads"
TABLE = "src_metering.interval_reads"
BUCKET = os.environ.get("KEA_LAKE_BUCKET", lake.BUCKET)

#: The planted 36-day-late revision (06_revisions.sql). Stage 3's first exit
#: condition is that an ordinary nightly run picks this up.
LATE_ICP = "0900075386VE52"
LATE_REGISTER = "UN"
LATE_READING_NZ_DATE = dt.date(2026, 7, 20)
LATE_ROWS = 48
LATE_LOAD_DATE = dt.date(2026, 8, 25)

#: The source columns, in order. Bronze must be these and exactly these, plus
#: the three provenance columns -- "as received" is checkable or it is a slogan.
SOURCE_COLUMNS = [
    "read_id", "icp_identifier", "register_code", "interval_start", "kwh",
    "read_status", "version_no", "received_at", "source_file",
]
METADATA_COLUMNS = ["_extracted_at", "_source_system", "_source_file"]


class Gate:
    """Numbered checks, all of them run, first failure sets the exit code."""

    def __init__(self):
        self.failures: list[str] = []
        self.n = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.n += 1
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {self.n:>2}. {name}" + (f"  -- {detail}" if detail else ""))
        if not ok:
            self.failures.append(f"{self.n}. {name}: {detail}")
        return ok

    def note(self, text: str) -> None:
        print(f"       {text}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--expect-contiguous", action="store_true",
                    help="require no gaps in the checked range (true only after a backfill; "
                         "bronze is deliberately sparse before one)")
    ap.add_argument("--from", dest="from_date", type=dt.date.fromisoformat,
                    help="scope the contiguity check to this range. Partitions outside it are "
                         "still gated on everything else -- the range narrows one claim, not the run")
    ap.add_argument("--to", dest="to_date", type=dt.date.fromisoformat)
    args = ap.parse_args(argv)

    spark = spark_session.build("assert_bronze")
    spark.sparkContext.setLogLevel("ERROR")
    g = Gate()
    try:
        from pyspark.sql import functions as F

        load_dates = _bronze_load_dates(spark)
        if not load_dates:
            print("no bronze partitions; nothing to gate")
            return 1
        print(f"gating {len(load_dates)} bronze partition(s): "
              f"{', '.join(d.isoformat() for d in load_dates)}\n")

        url, properties = spark_session.metering_jdbc()

        # --- 1. every partition is complete ----------------------------------
        missing = []
        for d in load_dates:
            for prefix in (lake.raw_prefix(SOURCE, ENTITY, d), lake.parsed_prefix(SOURCE, ENTITY, d)):
                for marker in ("_SUCCESS", "_MANIFEST.json"):
                    if not _exists(spark, f"{lake.s3a(prefix, BUCKET)}/{marker}"):
                        missing.append(f"{prefix}/{marker}")
        g.check("every partition carries _SUCCESS and a manifest", not missing,
                f"missing: {missing[:3]}" if missing else "")

        # --- 2. bronze is as-received ----------------------------------------
        bronze = spark.read.parquet(*[lake.s3a(lake.raw_prefix(SOURCE, ENTITY, d), BUCKET)
                                      for d in load_dates]).cache()
        g.check("bronze columns are the source's, plus exactly the three metadata columns",
                bronze.columns == SOURCE_COLUMNS + METADATA_COLUMNS,
                f"got {bronze.columns}" if bronze.columns != SOURCE_COLUMNS + METADATA_COLUMNS else "")

        # --- 3. the windows partition the source -----------------------------
        # The strongest single assertion here. If the windows overlapped, rows
        # would be double-counted; if they gapped, rows would vanish -- and both
        # look like ordinary variation in a daily row count.
        total = 0
        per_date_ok = True
        for d in load_dates:
            lo, hi = window.nz_day_window(d)
            in_lake = spark.read.parquet(lake.s3a(lake.raw_prefix(SOURCE, ENTITY, d), BUCKET)).count()
            at_source = _source_count(spark, url, properties, window.window_sql("received_at", d))
            total += in_lake
            if in_lake != at_source:
                per_date_ok = False
                g.note(f"{d}: lake {in_lake:,} vs source {at_source:,}")
        g.check("each partition holds exactly what its window selects at source", per_date_ok)

        distinct_ids = bronze.select("read_id").distinct().count()
        g.check("no read_id appears in two partitions", distinct_ids == total,
                f"{total:,} rows but {distinct_ids:,} distinct read_ids" if distinct_ids != total else
                f"{total:,} rows, all distinct")

        # --- 4. the late revision, and its negative control -------------------
        if LATE_LOAD_DATE in load_dates:
            parsed_late = spark.read.parquet(
                lake.s3a(lake.parsed_prefix(SOURCE, ENTITY, LATE_LOAD_DATE), BUCKET))
            late = parsed_late.filter(
                (F.col("icp_identifier") == LATE_ICP)
                & (F.col("register_code") == LATE_REGISTER)
                & (F.col("version_no") == 2)
                & (F.to_date(F.from_utc_timestamp("interval_start", "Pacific/Auckland"))
                   == F.lit(LATE_READING_NZ_DATE))
            )
            found = late.count()
            g.check(f"the {LATE_ROWS}-row, 36-day-late revision survives to parsed",
                    found == LATE_ROWS, f"found {found}")

            # THE NEGATIVE CONTROL. Watermarking on reading time instead of
            # arrival time is the mistake design §4.1 exists to prevent, and it
            # is invisible without this: the extract still returns ~26,900
            # perfectly reasonable rows either way.
            lo, hi = window.nz_day_window(LATE_LOAD_DATE)
            by_reading_time = parsed_late.filter(
                (F.col("interval_start") >= F.lit(lo.replace(tzinfo=None)))
                & (F.col("interval_start") < F.lit(hi.replace(tzinfo=None)))
                & (F.col("icp_identifier") == LATE_ICP) & (F.col("version_no") == 2)
            ).count()
            g.check("...and a window on interval_start would find NONE of it",
                    by_reading_time == 0, f"found {by_reading_time}")
        else:
            g.check(f"the planted late revision is checkable (needs load_date={LATE_LOAD_DATE})",
                    False, "that partition is not in the lake")

        # --- 5. nothing outside its window, and no clock in the SQL ----------
        stray = 0
        for d in load_dates:
            lo, hi = window.nz_day_window(d)
            stray += spark.read.parquet(lake.s3a(lake.raw_prefix(SOURCE, ENTITY, d), BUCKET)).filter(
                (F.col("received_at") < F.lit(lo.replace(tzinfo=None)))
                | (F.col("received_at") >= F.lit(hi.replace(tzinfo=None)))
            ).count()
        g.check("no row sits outside the window of the partition holding it", stray == 0,
                f"{stray} strays" if stray else "")

        volatile_free = True
        for d in load_dates[:1]:
            try:
                window.assert_no_volatile_sql(window.window_sql("received_at", d))
                for pred in window.hourly_predicates(d):
                    window.assert_no_volatile_sql(pred)
            except window.WindowError as exc:
                volatile_free = False
                g.note(str(exc))
        g.check("the generated SQL contains no clock-dependent function", volatile_free)

        # --- 6. the source has not changed under a sealed partition ----------
        # Bronze immutability is a claim about the PIPELINE, not a guarantee
        # about a generator someone may re-run: 06_revisions.sql assigns
        # received_at explicitly, so `make init-data` really does rewrite the
        # contents of past windows. Detect it rather than assume it away.
        drifted = []
        for d in load_dates:
            manifest = _read_manifest(spark, lake.raw_prefix(SOURCE, ENTITY, d))
            now_at_source = _source_count(spark, url, properties, window.window_sql("received_at", d))
            if manifest.get("source_row_count") != now_at_source:
                drifted.append(f"{d}: sealed {manifest.get('source_row_count'):,} "
                               f"now {now_at_source:,}")
        g.check("the source still agrees with every sealed manifest", not drifted,
                "; ".join(drifted) if drifted else "")

        # --- 7. contiguity ----------------------------------------------------
        # Scoped on purpose. A deliberate one-off partition outside the
        # backfilled range -- 2026-09-27 is here as a 23-hour-day probe -- is
        # not a gap in the backfill, and deleting real data to make a check
        # pass would be the wrong repair. The range narrows this one claim;
        # every other check still covers every partition.
        lo = args.from_date or min(load_dates)
        hi = args.to_date or max(load_dates)
        in_range = [d for d in load_dates if lo <= d <= hi]
        span = (hi - lo).days + 1
        gaps = span - len(in_range)
        if args.expect_contiguous:
            g.check(f"load dates are contiguous over {lo}..{hi}", gaps == 0,
                    f"{gaps} missing day(s) of {span}" if gaps else f"{span} days, no gaps")
        else:
            g.note(f"(contiguity not required: {len(in_range)} of {span} days present. "
                   "Statelessness cannot detect a DagRun that never happened -- "
                   "pass --expect-contiguous after a backfill.)")

        # --- 8. parsed conforms, and 9. parse loses nothing -------------------
        want = [f.name for f in avro_schema.parsed_fields()]
        conform_ok, balance_ok = True, True
        for d in load_dates:
            parsed = spark.read.parquet(lake.s3a(lake.parsed_prefix(SOURCE, ENTITY, d), BUCKET))
            if parsed.columns != want:
                conform_ok = False
                g.note(f"{d}: parsed columns {parsed.columns}")
            b = spark.read.parquet(lake.s3a(lake.raw_prefix(SOURCE, ENTITY, d), BUCKET)).count()
            q_prefix = lake.s3a(lake.quarantine_prefix(SOURCE, ENTITY, d), BUCKET)
            q = spark.read.parquet(q_prefix).count() if _exists(spark, f"{q_prefix}/_SUCCESS") else 0
            if parsed.count() + q != b:
                balance_ok = False
                g.note(f"{d}: bronze {b:,} != parsed {parsed.count():,} + quarantine {q:,}")
        g.check("parsed carries KEA-047's columns, in order", conform_ok)
        g.check("parse loses nothing: parsed + quarantine == bronze", balance_ok)

        # --- 10. the contract's nullability holds in the data ------------------
        required = [f.name for f in avro_schema.parsed_fields() if not f.nullable]
        parsed_all = spark.read.parquet(*[lake.s3a(lake.parsed_prefix(SOURCE, ENTITY, d), BUCKET)
                                          for d in load_dates])
        counts = parsed_all.select(
            *[F.sum(F.col(c).isNull().cast("int")).alias(c) for c in required]).collect()[0].asDict()
        offenders = {c: n for c, n in counts.items() if n}
        g.check("no nulls in any column KEA-047 declares non-nullable", not offenders, str(offenders))

    finally:
        spark.stop()

    print()
    if g.failures:
        print(f"STAGE 3 GATE FAILED: {len(g.failures)} of {g.n} checks")
        for f in g.failures:
            print(f"  - {f}")
        return 1
    print(f"STAGE 3 GATE PASSED: {g.n} checks over {len(load_dates)} load date(s)")
    return 0


def _bronze_load_dates(spark) -> list[dt.date]:
    """Whatever bronze actually holds.

    Discovered rather than declared, so the gate cannot quietly pass by
    checking a list that has drifted from the lake. Non-bronze prefixes are
    invisible here by construction -- the conformance fixture writes parsed and
    quarantine at load_date=2000-01-01 and no bronze, so it is never gated as
    if it were real data.
    """
    jvm = spark._jvm
    root = jvm.org.apache.hadoop.fs.Path(lake.s3a(f"bronze/{SOURCE}/{ENTITY}", BUCKET))
    fs = root.getFileSystem(spark._jsc.hadoopConfiguration())
    if not fs.exists(root):
        return []
    found = []
    for status in fs.listStatus(root):
        match = re.search(r"load_date=(\d{4}-\d{2}-\d{2})", status.getPath().getName())
        if match:
            found.append(dt.date.fromisoformat(match.group(1)))
    return sorted(found)


def _source_count(spark, url, properties, predicate: str) -> int:
    query = f"(SELECT count(*) AS n FROM {TABLE} WHERE {predicate}) t"
    return spark.read.jdbc(url=url, table=query, properties=properties).collect()[0]["n"]


def _exists(spark, uri: str) -> bool:
    path = spark._jvm.org.apache.hadoop.fs.Path(uri)
    return path.getFileSystem(spark._jsc.hadoopConfiguration()).exists(path)


def _read_manifest(spark, prefix: str) -> dict:
    path = spark._jvm.org.apache.hadoop.fs.Path(lake.s3a(lake.manifest_key(prefix), BUCKET))
    fs = path.getFileSystem(spark._jsc.hadoopConfiguration())
    stream = fs.open(path)
    try:
        return json.loads(bytes(stream.readAllBytes()).decode("utf-8"))
    finally:
        stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
