"""PostgreSQL integration checks; set KEA_TEST_DSN to a disposable empty DB."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import migrate_serving as migration
import publish_serving as publication


@unittest.skipUnless(os.environ.get('KEA_TEST_DSN'), 'requires disposable PostgreSQL')
class PublicationIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        cls.conn = psycopg2.connect(os.environ['KEA_TEST_DSN'])
        migration.migrate(cls.conn)
        with cls.conn, cls.conn.cursor() as cur:
            cur.execute('CREATE TABLE data_ops.test_probe(value integer)')
            cur.execute('INSERT INTO data_ops.test_probe VALUES (1)')

    @classmethod
    def tearDownClass(cls):
        # Leave the database as migrate() left it, so the class can run again.
        with cls.conn, cls.conn.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS data_ops.test_probe')
            cur.execute("DELETE FROM data_ops.serving_publication "
                        "WHERE release_id = 'integration-release'")
        cls.conn.close()

    def test_migration_retry_and_checksum(self):
        migration.migrate(self.conn)
        migration.check_current(self.conn)
        entries = migration.migrations()
        modified = [(v, n, 'changed' if v == 1 else h, b) for v, n, h, b in entries]
        with (
            patch.object(migration, "migrations", return_value=modified),
            self.assertRaisesRegex(RuntimeError, "Applied migration changed"),
        ):
            migration.migrate(self.conn)

    def test_publication_rolls_back_and_retry_is_idempotent(self):
        import build_bill_forecasts as forecasts
        import build_hardship_assessments as hardship
        import build_plan_recommendations as plans
        import create_hardship_alerts as alerts
        import verify_bill_reproduction as bill_verifier

        def load(conn, tables, *, commit):
            self.assertFalse(commit)
            with conn.cursor() as cur:
                cur.execute('UPDATE data_ops.test_probe SET value=value+1')
            return {'probe': 1}

        def value():
            with self.conn.cursor() as cur:
                cur.execute('SELECT value FROM data_ops.test_probe')
                return cur.fetchone()[0]

        with patch.object(publication.loader, 'load_fk_set', side_effect=load), \
             patch.object(publication.loader, 'load_partition_swap', return_value=1), \
             patch.object(publication.loader, 'sync_serving_sequences'), \
             patch.object(forecasts, 'run'), \
             patch.object(plans, 'main', return_value=0), \
             patch.object(bill_verifier, 'main', return_value=0), \
             patch.object(hardship, 'main', return_value=0), \
             patch.object(alerts, 'main', return_value=0), \
             patch.object(publication, 'validate') as validate:
            validate.side_effect = RuntimeError('injected validation failure')
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                publication.publish(self.conn, 'integration-release')
            self.assertEqual(value(), 1)
            validate.side_effect = None
            publication.publish(self.conn, 'integration-release')
            self.assertEqual(value(), 2)
            publication.publish(self.conn, 'integration-release')
            self.assertEqual(value(), 2)


if __name__ == '__main__':
    unittest.main()
