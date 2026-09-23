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
        import build_ev_scenarios as ev
        import build_hardship_assessments as hardship
        import build_plan_recommendations as plans
        import build_solar_estimates as solar
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
             patch.object(solar, 'main', return_value=0), \
             patch.object(ev, 'main', return_value=0), \
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


CONTRACT = Path(__file__).resolve().parents[2] / 'design' / 'serving_read_contract.json'
SERVED_SCHEMAS = ('metering', 'billing', 'catalogue', 'kea')
# Written by the publisher from Sprint 4 on; default privileges must reach them.
SPRINT4_TABLES = ('kea.mart_daily_detected_load', 'kea.mart_charging_session',
                  'kea.solar_estimate', 'kea.ev_scenario', 'kea.hardship_outlook')


@unittest.skipUnless(os.environ.get('KEA_TEST_DSN'), 'requires disposable PostgreSQL')
class ServingReadContract(unittest.TestCase):
    """The columns the backend reads, as the migrations actually leave them.

    A later migration that renames, drops or retypes one of them fails here,
    before the backend finds out in production.
    """

    @classmethod
    def setUpClass(cls):
        import json
        import psycopg2
        cls.contract = json.loads(CONTRACT.read_text())['tables']
        cls.conn = psycopg2.connect(os.environ['KEA_TEST_DSN'])
        migration.migrate(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def tearDown(self):
        # Every check only reads; a rollback also undoes SET ROLE.
        self.conn.rollback()

    def test_columns_keep_their_type_and_nullability(self):
        with self.conn.cursor() as cur:
            for table, columns in self.contract.items():
                schema, name = table.split('.')
                cur.execute(
                    """SELECT column_name, data_type, udt_schema, udt_name,
                              numeric_precision, numeric_scale, is_nullable
                       FROM information_schema.columns
                       WHERE table_schema = %s AND table_name = %s""", (schema, name))
                actual = {}
                for column, dtype, udt_schema, udt, precision, scale, nullable in cur.fetchall():
                    if dtype == 'numeric':
                        dtype = f'numeric({precision},{scale})'
                    elif dtype == 'USER-DEFINED':
                        dtype = f'{udt_schema}.{udt}'
                    actual[column] = dtype + ('' if nullable == 'YES' else ' not null')
                with self.subTest(table=table):
                    self.assertTrue(actual, f'{table} does not exist')
                    self.assertEqual({c: actual.get(c) for c in columns}, columns)

    def test_the_backend_role_can_read_every_column(self):
        from psycopg2 import sql
        with self.conn.cursor() as cur:
            cur.execute('SET ROLE kea_app')
            for table, columns in self.contract.items():
                # A savepoint per table, so one denial does not abort the rest.
                cur.execute('SAVEPOINT contract_read')
                with self.subTest(table=table):
                    try:
                        cur.execute(sql.SQL('SELECT {} FROM {} LIMIT 0').format(
                            sql.SQL(', ').join(map(sql.Identifier, columns)),
                            sql.Identifier(*table.split('.'))))
                    finally:
                        cur.execute('ROLLBACK TO SAVEPOINT contract_read')

    def test_the_publisher_can_write_the_sprint4_tables(self):
        with self.conn.cursor() as cur:
            for table in SPRINT4_TABLES:
                for privilege in ('SELECT', 'INSERT', 'DELETE'):
                    with self.subTest(table=table, privilege=privilege):
                        cur.execute('SELECT has_table_privilege(%s, %s, %s)',
                                    ('kea_loader', table, privilege))
                        self.assertTrue(cur.fetchone()[0])

    def test_every_served_foreign_key_is_deferrable(self):
        # A publication replaces parents under SET CONSTRAINTS ALL DEFERRED;
        # V003 converted only the keys that existed when it ran.
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT n.nspname || '.' || c.relname || '.' || con.conname
                   FROM pg_constraint con
                   JOIN pg_class c ON c.oid = con.conrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE con.contype = 'f' AND NOT con.condeferrable
                     AND n.nspname = ANY(%s)
                   ORDER BY 1""", (list(SERVED_SCHEMAS),))
            self.assertEqual([r[0] for r in cur.fetchall()], [])

    def test_masked_day_allows_a_clear_night_run_but_masked_run_refuses_low_power(self):
        # Day-level masking says nothing about a run at night. The session
        # constraint applies the class rule at the grain where it is known.
        import psycopg2

        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO kea.mart_daily_detected_load
                   (icp_id, usage_date, floor_kwh_per_hh, ev_half_hours,
                    ev_kwh_low, ev_kwh_high, ev_charger_class, ev_confidence,
                    masked_intervals, method_version)
                   VALUES (999001, '2026-01-01', 0.1, 2, 1, 2,
                           'low_power', 'low', 6, 'detect-v2')""")
            cur.execute(
                """INSERT INTO kea.mart_charging_session
                   (icp_id, session_start, session_end, half_hours, kwh_low,
                    kwh_high, charger_class, confidence, masked_half_hours,
                    method_version)
                   VALUES (999001, '2026-01-01 10:00+00',
                           '2026-01-01 11:00+00', 2, 1, 2,
                           'low_power', 'low', 0, 'detect-v2')""")
            cur.execute('SAVEPOINT masked_run')
            with self.assertRaises(psycopg2.errors.CheckViolation):
                cur.execute(
                    """INSERT INTO kea.mart_charging_session
                       (icp_id, session_start, session_end, half_hours, kwh_low,
                        kwh_high, charger_class, confidence, masked_half_hours,
                        method_version)
                       VALUES (999001, '2026-01-01 11:00+00',
                               '2026-01-01 12:00+00', 2, 1, 2,
                               'low_power', 'low', 1, 'detect-v2')""")
            cur.execute('ROLLBACK TO SAVEPOINT masked_run')


if __name__ == '__main__':
    unittest.main()
