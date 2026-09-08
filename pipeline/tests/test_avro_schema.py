"""KEA-047 and the .avsc -> column-list mapping. Stdlib only, no pyarrow.

This is the structural half of the conformance check. It needs no data and no
lake, so the contract can be settled before parse writes its first file --
which matters, because a schema that drifts from its data is invisible until a
query returns a number nobody expected.

The pyarrow half is exercised in test_avro_conformance.py, which needs a
container.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from kea_pipeline import avro_schema as m  # noqa: E402


class TestKea047(unittest.TestCase):
    """The contract design 5.3 names in six places and no file defined."""

    def setUp(self):
        self.fields = m.fields(m.load(m.KEA_047))
        self.by_name = {f.name: f for f in self.fields}

    def test_it_matches_the_source_table_column_for_column(self):
        """src_metering.interval_reads, in order. Parse RESHAPES; it does not
        add, drop or reorder, so the contract is the source's shape with one
        column renamed."""
        assert [f.name for f in self.fields] == [
            "read_id",
            "icp_identifier",
            "register_code",
            "interval_start",
            "kwh",
            "quality",  # <- read_status, reshaped
            "version_no",
            "received_at",
            "source_file",
        ]

    def test_only_source_file_is_nullable(self):
        """Matching the DDL exactly: every other column is NOT NULL, so a null
        arriving in one is a file-boundary fault and belongs in quarantine."""
        assert [f.name for f in self.fields if f.nullable] == ["source_file"]

    def test_the_two_instants_are_timestamps_not_dates(self):
        """Every NZ-local date in the platform is DERIVED through
        Pacific/Auckland. Storing a date beside the instant would let the two
        disagree, and the load date is exactly that derivation."""
        assert self.by_name["interval_start"].kind == "timestamp-micros"
        assert self.by_name["received_at"].kind == "timestamp-micros"

    def test_kwh_keeps_the_sources_precision(self):
        """numeric(10,4). A float would lose cents on a bill that has to
        reproduce to $50.54."""
        assert self.by_name["kwh"].kind == "decimal(10,4)"

    def test_quality_carries_final(self):
        """The source produces 1,010 'F' rows. common.read_quality in the
        serving schema is ('actual','estimated','validated','missing') and has
        no 'final' -- an open item for Stage 5's loader, deliberately not
        resolved by making parse throw the distinction away."""
        assert self.by_name["quality"].symbols == ("actual", "estimated", "final")

    def test_version_no_survives_parse(self):
        """'Latest wins' is silver's dedup (design 4.1). If parse resolved it,
        the superseded rows would be gone and Stage 3's exit criterion -- that
        a three-week-old correction is picked up -- would have no evidence."""
        assert "version_no" in self.by_name

    def test_the_business_keys_are_not_resolved(self):
        """Key resolution is silver's. Bronze carries what arrived."""
        assert "icp_identifier" in self.by_name and "register_code" in self.by_name
        assert "register_id" not in self.by_name and "icp_id" not in self.by_name


class TestPipelineMetadata(unittest.TestCase):
    def test_the_three_columns_design_4_2_requires(self):
        names = [f.name for f in m.fields(m.load(m.PIPELINE_METADATA))]
        assert names == ["_extracted_at", "_source_system", "_source_file"]

    def test_every_metadata_name_is_underscore_prefixed(self):
        """The rule that keeps _source_file (pipeline provenance) apart from
        source_file (the retailer's delivery filename). Both land in the same
        Parquet file, and the underscore is the only thing telling them
        apart."""
        for field in m.fields(m.load(m.PIPELINE_METADATA)):
            assert field.name.startswith("_"), field.name


class TestComposition(unittest.TestCase):
    def test_parsed_is_the_entity_then_exactly_the_metadata(self):
        entity = m.fields(m.load(m.KEA_047))
        parsed = m.parsed_fields()
        assert parsed[: len(entity)] == entity
        assert [f.name for f in parsed[len(entity) :]] == [
            "_extracted_at",
            "_source_system",
            "_source_file",
        ]

    def test_the_data_column_and_the_metadata_column_coexist(self):
        names = [f.name for f in m.parsed_fields()]
        assert "source_file" in names and "_source_file" in names

    def test_a_metadata_column_shadowing_a_data_column_is_refused(self):
        """If the underscore convention were ever dropped, this is what would
        catch it -- rather than one column silently overwriting the other."""
        clashing = Path(__file__).parent / "_clash.avsc"
        clashing.write_text(
            json.dumps(
                {
                    "type": "record",
                    "name": "Clash",
                    "fields": [{"name": "source_file", "type": "string"}],
                }
            )
        )
        try:
            with self.assertRaisesRegex(m.SchemaError, "shadow"):
                m.parsed_fields(metadata_schema=clashing)
        finally:
            clashing.unlink()


class TestTypeMapping(unittest.TestCase):
    def _one(self, type_node):
        return m.fields(
            {"type": "record", "name": "T", "fields": [{"name": "f", "type": type_node}]}
        )[0]

    def test_a_null_union_is_nullable_and_keeps_its_type(self):
        got = self._one(["null", "string"])
        assert got.nullable and got.kind == "string"

    def test_an_enum_becomes_a_string_and_keeps_its_symbols(self):
        """Parquet has no enum type. The symbols survive so the value-level
        check can still refuse a quality outside the list."""
        got = self._one({"type": "enum", "name": "E", "symbols": ["a", "b"]})
        assert got.kind == "string" and got.symbols == ("a", "b")

    def test_a_multi_type_union_is_refused_rather_than_guessed(self):
        with self.assertRaisesRegex(m.SchemaError, "only \\['null', X\\]"):
            self._one(["null", "string", "long"])

    def test_a_decimal_without_a_scale_is_refused(self):
        with self.assertRaisesRegex(m.SchemaError, "scale"):
            self._one({"type": "bytes", "logicalType": "decimal", "precision": 10})

    def test_an_unsupported_logical_type_is_refused_not_ignored(self):
        """Silently falling back to the underlying primitive is how a
        timestamp becomes a bare integer in Snowflake."""
        with self.assertRaisesRegex(m.SchemaError, "logicalType"):
            self._one({"type": "string", "logicalType": "uuid"})

    def test_the_three_supported_logical_types(self):
        """timestamp-micros, date and time-micros -- each because the source
        really uses it. A plan version is valid from a DATE, with no midnight
        to argue over, and a TOU window runs from a TIME OF DAY that recurs
        and is not an instant at all. Flattening either into a timestamp would
        invent a zone the source does not have."""
        assert self._one({"type": "long", "logicalType": "timestamp-micros"}).kind == "timestamp-micros"
        assert self._one({"type": "int", "logicalType": "date"}).kind == "date"
        assert self._one({"type": "long", "logicalType": "time-micros"}).kind == "time-micros"

    def test_an_array_of_a_primitive(self):
        """days_csv '1,2,3,4,5' becomes days_of_week [1,2,3,4,5] -- design 5's
        reshape, and the reason the catalogue contract cannot simply mirror the
        source's column types."""
        assert self._one({"type": "array", "items": "int"}).kind == "array<int>"


if __name__ == "__main__":
    unittest.main()
