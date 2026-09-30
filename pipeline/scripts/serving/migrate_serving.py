"""Apply immutable serving SQL migrations using KEA_MIGRATION_DSN.

Existing databases require --baseline after their schema has been reviewed
against V001. The baseline records history; it does not recreate objects.
"""
import argparse
import hashlib
import os
import re
from pathlib import Path

LOCK_ID = 72419031
MIGRATIONS = Path(__file__).resolve().parents[3] / 'migrations' / 'serving'


def migrations():
    result = []
    for path in sorted(MIGRATIONS.glob('V[0-9]*__*.sql')):
        version = int(path.name.split('__')[0][1:])
        body = path.read_text()
        result.append((version, path.name, hashlib.sha256(body.encode()).hexdigest(), body))
    if not result or len({r[0] for r in result}) != len(result):
        raise RuntimeError('Missing or duplicate serving migrations')
    return sorted(result)


def check_current(conn):
    with conn.cursor() as cur:
        cur.execute('SELECT version, checksum FROM data_ops.schema_migration ORDER BY version')
        actual = dict(cur.fetchall())
    expected = {v: digest for v, _, digest, _ in migrations()}
    if actual != expected:
        raise RuntimeError('Serving schema is not current; run make publish-serving MIGRATE=1')


def migrate(conn, baseline=False):
    with conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_lock(%s)', (LOCK_ID,))
    try:
        with conn.cursor() as cur:
            cur.execute('CREATE SCHEMA IF NOT EXISTS data_ops')
            cur.execute('''CREATE TABLE IF NOT EXISTS data_ops.schema_migration (
                version integer PRIMARY KEY, filename text NOT NULL,
                checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())''')
        conn.commit()
        entries = migrations()
        with conn.cursor() as cur:
            cur.execute('SELECT version, checksum FROM data_ops.schema_migration')
            applied = dict(cur.fetchall())
        if set(applied) - {r[0] for r in entries}:
            raise RuntimeError('Database has migrations unknown to this checkout')
        for version, name, digest, body in entries:
            if version in applied:
                if applied[version] != digest:
                    raise RuntimeError(f'Applied migration changed: {name}')
                continue
            with conn.cursor() as cur:
                if version == 1:
                    cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname IN "
                                "('billing','metering','catalogue','kea')")
                    existing = cur.fetchone()[0] > 0
                    if existing and not baseline:
                        raise RuntimeError(
                            "Existing serving tables: review V001, then use --baseline"
                        )
                    if existing:
                        # Reject an incomplete legacy installation even with baseline requested.
                        # Match concrete DDL only. V001 also contains a PL/pgSQL
                        # format string such as meter_read_hh_{suffix}; treating
                        # that prefix as a literal table makes every valid
                        # existing database impossible to baseline.
                        concrete = re.findall(
                            r'CREATE TABLE\s+([a-z_]+\.[a-z_]+)'
                            r'(?=\s*(?:\(|PARTITION\s+OF))', body)
                        for table in concrete:
                            cur.execute('SELECT to_regclass(%s)', (table,))
                            if cur.fetchone()[0] is None:
                                raise RuntimeError(f'Cannot baseline: missing {table}')
                    else:
                        cur.execute(body)
                else:
                    cur.execute(body)
                cur.execute('INSERT INTO data_ops.schema_migration(version, filename, checksum) '
                            'VALUES (%s,%s,%s)', (version, name, digest))
            conn.commit()
            print(f'applied {name}')
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_unlock(%s)', (LOCK_ID,))
        conn.commit()


def main():
    import psycopg2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    dsn = os.environ.get('KEA_MIGRATION_DSN')
    if not dsn:
        raise SystemExit('Set KEA_MIGRATION_DSN to the explicit target database')
    conn = psycopg2.connect(dsn)
    try:
        migrate(conn, args.baseline)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
