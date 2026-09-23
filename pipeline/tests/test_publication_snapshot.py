"""Optional full publication test using a disposable target and source snapshot.

Only transport is substituted: PostgreSQL text values stand in for exported
Arrow batches. Loader SQL, partitions and all Sprint 3 and 4 jobs run unchanged.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import migrate_serving
import publish_serving


@unittest.skipUnless(
    os.environ.get("KEA_TEST_SOURCE_DSN") and os.environ.get("KEA_TEST_DSN"),
    "requires disposable snapshot databases"
)
class SnapshotPublication(unittest.TestCase):
    def test_full_publication(self):
        import psycopg2
        import pyarrow as pa
        from psycopg2 import sql
        source = psycopg2.connect(os.environ['KEA_TEST_SOURCE_DSN'])
        target = psycopg2.connect(os.environ['KEA_TEST_DSN'])

        def batches(table):
            schema, name = table.name.split('.')
            with source.cursor() as cur:
                cur.execute('''SELECT column_name FROM information_schema.columns
                    WHERE table_schema=%s AND table_name=%s AND is_generated='NEVER'
                    ORDER BY ordinal_position''', (schema, name))
                columns = [r[0] for r in cur.fetchall()]
            with source.cursor(name='export_rows') as cur:
                cur.execute(sql.SQL('SELECT {} FROM {}.{}').format(
                    sql.SQL(',').join(sql.SQL('{}::text').format(sql.Identifier(c))
                                      for c in columns),
                    sql.Identifier(schema), sql.Identifier(name)))
                while rows := cur.fetchmany(50000):
                    yield pa.table({c: pa.array([r[i] for r in rows], type=pa.string())
                                    for i, c in enumerate(columns)})
        try:
            migrate_serving.migrate(target)
            with patch.object(publish_serving.loader, '_read_export', side_effect=batches):
                publish_serving.publish(target, 'snapshot-e2e')
            with target.cursor() as cur:
                cur.execute("SELECT row_counts FROM data_ops.serving_publication "
                            "WHERE release_id='snapshot-e2e'")
                counts = cur.fetchone()[0]
                self.assertGreater(counts['metering.meter_read_hh'], 1000000)
                self.assertGreater(counts['kea.plan_recommendation'], 0)
                self.assertGreater(counts['kea.solar_estimate'], 0)
                self.assertGreater(counts['kea.ev_scenario'], 0)
        finally:
            source.close()
            target.close()
