"""Conformance and quarantine routing. NEEDS THE LAKE -- run under spark-submit.

    make test-conformance

Kept apart from `make test-parse`, which is stdlib-only and runs on the host in
milliseconds. The checks here read Parquet that a real Spark job wrote, so they
need pyspark, pyarrow, and objects in MinIO. Splitting them keeps the fast
suite fast and honest about what it does not cover.

Two questions are answered:

  1. Does parse route the rejects that can only arrive in a FILE? The source
     table forbids all three by CHECK constraint, so they cannot be tested
     against real data at all -- which is why the fixture exists.
  2. Does the Parquet parse wrote actually conform to KEA-047? Checked against
     the footer, so it is the file's own schema being asserted, not the
     DataFrame that produced it.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The layout differs between the host (pipeline/{tests,lib,spark_jobs}) and the
# Spark container, where tests/ is mounted INSIDE spark_jobs/. Rather than
# encode either, take whichever candidates exist -- so the same file runs in
# both places and a mount change does not silently stop importing.
for candidate in (
    HERE.parents[1] / "lib", HERE.parents[1] / "spark_jobs",  # container: /opt/spark_jobs/{lib,..}
    HERE.parents[2] / "lib", HERE.parents[2] / "spark_jobs",  # host: pipeline/{lib,spark_jobs}
    HERE.parents[1],
):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import spark_session  # noqa: E402
from kea_pipeline import avro_schema, lake  # noqa: E402

LOAD_DATE = dt.date(2026, 8, 25)
BUCKET = os.environ.get("KEA_LAKE_BUCKET", lake.BUCKET)
FIXTURE_PREFIX = "fixtures/bronze/src_metering/interval_reads"

_spark = None


def spark():
    global _spark
    if _spark is None:
        _spark = spark_session.build("test_lake_conformance")
        _spark.sparkContext.setLogLevel("ERROR")
    return _spark


class TestQuarantineRouting(unittest.TestCase):
    """The fixture's six rows, through the real parse job."""

    @classmethod
    def setUpClass(cls):
        import parse_interval_reads
        from pyspark.sql.types import (
            DecimalType, IntegerType, LongType, StringType, StructField, StructType, TimestampType,
        )

        # Materialise the committed CSV into the bronze shape. The CSV is the
        # reviewable artefact; Parquet is what parse actually reads.
        schema = StructType([
            StructField("read_id", LongType()), StructField("icp_identifier", StringType()),
            StructField("register_code", StringType()), StructField("interval_start", TimestampType()),
            StructField("kwh", DecimalType(10, 4)), StructField("read_status", StringType()),
            StructField("version_no", IntegerType()), StructField("received_at", TimestampType()),
            StructField("source_file", StringType()), StructField("_extracted_at", TimestampType()),
            StructField("_source_system", StringType()), StructField("_source_file", StringType()),
        ])
        csv = spark().read.csv(
            f"file://{HERE.parent / 'fixtures' / 'bronze_interval_reads.csv'}", header=True, schema=schema
        )
        cls.fixture_at = lake.s3a(FIXTURE_PREFIX, BUCKET)
        csv.write.mode("overwrite").parquet(cls.fixture_at)

        # A load date of its own, so the fixture never collides with real data.
        cls.load_date = dt.date(2000, 1, 1)
        parse_interval_reads.main(
            [cls.load_date.isoformat(), "--force", "--source-prefix", cls.fixture_at]
        )
        # main() stops the session it built; reopen for the assertions.
        global _spark
        _spark = None

        cls.parsed = spark().read.parquet(
            lake.s3a(lake.parsed_prefix("src_metering", "interval_reads", cls.load_date), BUCKET)
        )
        cls.rejected = spark().read.parquet(
            lake.s3a(lake.quarantine_prefix("src_metering", "interval_reads", cls.load_date), BUCKET)
        )

    def _reasons(self):
        return {r["read_id"]: r["_reject_reason"] for r in self.rejected.collect()}

    def test_nothing_is_silently_dropped(self):
        """Parse reshapes, so every input row comes out somewhere. A row that is
        neither parsed nor quarantined has vanished, and that looks like success
        from every angle."""
        assert self.parsed.count() + self.rejected.count() == 6

    def test_the_three_good_rows_parse(self):
        assert self.parsed.count() == 3
        assert sorted(r["read_id"] for r in self.parsed.collect()) == [
            9000000001, 9000000002, 9000000003
        ]

    def test_all_three_read_statuses_reshape(self):
        got = {r["read_id"]: r["quality"] for r in self.parsed.collect()}
        assert got[9000000001] == "actual"
        assert got[9000000002] == "estimated"
        assert got[9000000003] == "final"

    def test_a_negative_read_is_quarantined_not_absolute_valued(self):
        """ck_kwh_non_negative means this cannot be a source row, so it can only
        arrive in a file. Quarantining it is what that CHECK exists to make
        possible."""
        assert self._reasons()[9000000004] == "parse.negative_read"

    def test_the_negative_read_is_not_in_parsed(self):
        assert 9000000004 not in [r["read_id"] for r in self.parsed.collect()]

    def test_an_unknown_read_status_is_quarantined(self):
        assert self._reasons()[9000000005] == "parse.enum_unknown"

    def test_a_null_in_a_required_column_is_quarantined(self):
        assert self._reasons()[9000000006] == "parse.null_in_required_field"

    def test_zero_kwh_is_not_a_reject(self):
        """A stuck meter reads zero all day and is a real defect -- but finding
        it needs a distribution over many rows, so it is silver's, not parse's."""
        assert 9000000002 in [r["read_id"] for r in self.parsed.collect()]

    def test_every_reason_used_is_in_the_vocabulary(self):
        from kea_pipeline import reshape
        assert set(self._reasons().values()) <= set(reshape.parse_reasons())


class TestParquetConformsToKea047(unittest.TestCase):
    """Read the FOOTER of what parse wrote, against the .avsc."""

    @classmethod
    def setUpClass(cls):
        import pyarrow.dataset as ds
        prefix = lake.parsed_prefix("src_metering", "interval_reads", LOAD_DATE)
        endpoint = os.environ["MINIO_ENDPOINT"]
        from pyarrow import fs
        cls.fs = fs.S3FileSystem(
            access_key=os.environ["MINIO_ROOT_USER"],
            secret_key=os.environ["MINIO_ROOT_PASSWORD"],
            endpoint_override=endpoint.replace("http://", ""),
            scheme="http",
        )
        cls.schema = ds.dataset(f"{BUCKET}/{prefix}", filesystem=cls.fs, format="parquet").schema
        cls.want = avro_schema.parsed_fields()

    def test_names_and_order_match_the_contract(self):
        """Order matters: a file with the right columns in the wrong order still
        reads, and breaks the first time anything positional touches it."""
        assert list(self.schema.names) == [f.name for f in self.want]

    def test_types_match_the_contract(self):
        expected = avro_schema.to_arrow(self.want)
        for want, got in zip(expected, self.schema):
            assert want.type == got.type, f"{want.name}: contract {want.type}, file {got.type}"

    def test_kwh_keeps_its_precision(self):
        """decimal(10,4), not a double. A float would lose cents on a bill that
        has to reproduce to $50.54."""
        import pyarrow as pa
        assert self.schema.field("kwh").type == pa.decimal128(10, 4)

    def test_the_data_column_and_the_metadata_column_both_survive(self):
        assert "source_file" in self.schema.names and "_source_file" in self.schema.names


class TestNullabilityIsEnforcedAsData(unittest.TestCase):
    """KEA-047's non-nullable fields hold no nulls.

    Parse does not tighten the Parquet nullability FLAG -- Spark will not narrow
    it, and coalesce() would flip it only by substituting a value for the null,
    which is silent corruption. So the contract is enforced where it can be
    enforced honestly: violating rows are quarantined, and this asserts the
    result. "Does not contain nulls" is a stronger claim than "should not".
    """

    def test_no_nulls_in_any_required_column(self):
        from pyspark.sql import functions as F
        df = spark().read.parquet(
            lake.s3a(lake.parsed_prefix("src_metering", "interval_reads", LOAD_DATE), BUCKET)
        )
        required = [f.name for f in avro_schema.parsed_fields() if not f.nullable]
        counts = df.select(
            *[F.sum(F.col(c).isNull().cast("int")).alias(c) for c in required]
        ).collect()[0].asDict()
        offenders = {c: n for c, n in counts.items() if n}
        assert not offenders, f"nulls in non-nullable columns: {offenders}"

    def test_source_file_is_the_only_nullable_data_column(self):
        nullable = [f.name for f in avro_schema.parsed_fields() if f.nullable]
        assert nullable == ["source_file"]


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    if _spark is not None:
        _spark.stop()
    raise SystemExit(0 if result.wasSuccessful() else 1)
