"""Lake paths and the replay-comparison hash. Stdlib only.

The hash is what "re-running a load date changes nothing" MEANS, so its
properties are worth pinning here rather than discovering against 19M rows.
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from kea_pipeline import lake as m  # noqa: E402

D = dt.date(2026, 8, 25)


class TestPaths(unittest.TestCase):
    def test_raw_keeps_the_path_the_checkbox_quotes(self):
        """Stage 3's third checkbox says bronze/{source}/{entity}/load_date=...
        verbatim. Moving raw under bronze/raw/ for symmetry would be prettier
        and would make the box unpointable."""
        assert (
            m.raw_prefix("src_metering", "interval_reads", D)
            == "bronze/src_metering/interval_reads/load_date=2026-08-25"
        )

    def test_parsed_and_quarantine(self):
        assert m.parsed_prefix("src_metering", "interval_reads", D).startswith("bronze/parsed/")
        assert m.quarantine_prefix("src_metering", "interval_reads", D).startswith("quarantine/")

    def test_parsed_is_reserved_and_cannot_be_a_source(self):
        """The cost of keeping raw at the documented path: a fourth source
        called 'parsed' would collide with the parsed prefix, silently."""
        with self.assertRaisesRegex(m.LakeError, "reserved"):
            m.raw_prefix("parsed", "whatever", D)

    def test_a_source_name_cannot_break_the_prefix_structure(self):
        for bad in ("src/metering", "load_date=x"):
            with self.assertRaises(m.LakeError):
                m.raw_prefix(bad, "e", D)

    def test_spark_reads_s3a_not_s3(self):
        assert m.s3a("bronze/x").startswith("s3a://kea-lake/")

    def test_the_manifest_is_underscore_prefixed(self):
        """So Spark and Snowflake external tables both skip it, and the
        manifest never pollutes the dataset it describes."""
        assert m.manifest_key("bronze/x").endswith("/_MANIFEST.json")


class TestTheHash(unittest.TestCase):
    ROWS = [("a", 1, "x"), ("b", 2, None), ("b", 2, None), ("c", 3, "z")]

    def test_order_independent(self):
        """Stage 2's determinism test failed on this: it used an ordered
        string_agg whose ORDER BY was not unique, so two identical runs
        compared unequal. A sum of per-row digests cannot have that problem."""
        assert m.content_hash(self.ROWS) == m.content_hash(list(reversed(self.ROWS)))

    def test_a_duplicated_row_does_not_cancel(self):
        """This is what rules out XOR. 9,263 duplicate deliveries are
        legitimate source rows; under XOR a file with two copies would hash
        identically to one with none."""
        one = [("a", 1, "x")]
        two = [("a", 1, "x"), ("a", 1, "x")]
        assert m.content_hash(one) != m.content_hash(two)
        assert m.content_hash(two) != m.content_hash([])

    def test_a_null_is_not_the_empty_string(self):
        """concat_ws DROPS nulls, so ('a', None, 'b') and ('a', 'b', None)
        would collide without a sentinel."""
        assert m.content_hash([("a", None, "b")]) != m.content_hash([("a", "", "b")])
        assert m.content_hash([("a", None, "b")]) != m.content_hash([("a", "b", None)])

    def test_a_changed_value_changes_the_hash(self):
        assert m.content_hash([("a", 1, "x")]) != m.content_hash([("a", 2, "x")])

    def test_the_only_excluded_column_is_the_volatile_one(self):
        """_extracted_at legitimately differs between two runs of the same load
        date. Nothing else may be excluded, or the comparison stops meaning
        anything."""
        columns = ["read_id", "kwh", "_extracted_at", "_source_system"]
        assert m.hashed_columns(columns) == ["read_id", "kwh", "_source_system"]
        assert m.VOLATILE_COLUMN == "_extracted_at"

    def test_the_spark_expression_uses_real_control_characters(self):
        """Spark SQL has no \\x escape -- it reads '\\x1f' as four ordinary
        characters, which a value could contain. char(31)/char(0) are the
        actual bytes. Verified in a live session; pinned here."""
        sql = m.md5_chunk_sql(["a", "b"])[0]
        assert "char(31)" in sql and "char(0)" in sql
        assert "\\x1f" not in sql and "\\x00" not in sql

    def test_one_expression_per_chunk_and_they_differ(self):
        a, b = m.md5_chunk_sql(["a"])
        assert a != b  # different substrings of the same md5


class TestManifest(unittest.TestCase):
    def _one(self):
        lo = dt.datetime(2026, 8, 24, 12, tzinfo=dt.timezone.utc)
        return m.manifest(
            source="src_metering", entity="interval_reads", load_date=D,
            window=(lo, lo + dt.timedelta(hours=24)), mode="incremental",
            watermark_column="received_at", row_count=26925, content_hash="ab-cd",
            source_row_count=26925, extracted_at=lo, partitions=24,
        )

    def test_it_records_the_window_so_the_partition_claim_is_checkable(self):
        got = self._one()
        assert got["window_start_utc"].startswith("2026-08-24T12:00")
        assert got["window_end_utc"].startswith("2026-08-25T12:00")

    def test_it_records_what_the_hash_excludes(self):
        """So a later reader knows the comparison's terms rather than guessing."""
        assert self._one()["hash_excludes"] == ["_extracted_at"]

    def test_source_row_count_is_kept_for_re_querying(self):
        """Not redundant with row_count: the gate re-asks the source, and a
        disagreement means the source changed under a sealed load date --
        which really happens, because re-running `make init-sources` rewrites
        received_at for every revision."""
        assert "source_row_count" in self._one()


if __name__ == "__main__":
    unittest.main()
