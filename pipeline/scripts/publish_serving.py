"""Publish the current sealed lake export and Sprint 3 results in one transaction.

Requires an explicit KEA_PUBLISH_DSN and a unique --release-id. Warehouse
export must be complete and must not be replaced while this command runs.
"""
import argparse
import datetime as dt
import json
import os

import load_export_to_serving as loader
from migrate_serving import LOCK_ID, check_current

#: What the Sprint 3 jobs write inside the publication. Named for lineage only:
#: the served tables themselves come from loader.serving.TABLES.
DECISIONS = ('kea.bill_forecast', 'kea.plan_recommendation', 'kea.plan_bill_example',
             'kea.hardship_assessment', 'kea.hardship_signal', 'kea.alert')


def validate(conn, counts):
    for table in ('billing.customer', 'billing.account', 'billing.bill',
                  'catalogue.plan', 'metering.meter_read_hh', 'kea.mart_account_context'):
        if counts.get(table, 0) <= 0:
            raise RuntimeError(f'Publication requires non-empty {table}')
    with conn.cursor() as cur:
        for table in ('kea.bill_forecast', 'kea.plan_recommendation',
                      'kea.plan_bill_example', 'kea.hardship_assessment'):
            cur.execute(f'SELECT count(*) FROM {table}')
            counts[table] = cur.fetchone()[0]
            if not counts[table]:
                raise RuntimeError(f'Missing Sprint 3 output: {table}')
        cur.execute('''SELECT count(*) FROM kea.mart_account_context m
                       JOIN billing.customer c USING (customer_id)
                       WHERE m.email IS DISTINCT FROM c.email''')
        if cur.fetchone()[0]:
            raise RuntimeError('Customer emails differ between mart and replica')
        cur.execute('SET CONSTRAINTS ALL IMMEDIATE')


def publish(conn, release_id):
    """Publish one release. Returns its row counts, or None if it was already published."""
    import build_bill_forecasts as forecasts
    import build_hardship_assessments as hardship
    import build_plan_recommendations as plans
    import create_hardship_alerts as alerts
    import verify_bill_reproduction as bill_verifier
    with conn:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
            cur.execute("SET LOCAL lock_timeout = '30s'")
        check_current(conn)
        with conn.cursor() as cur:
            cur.execute('SELECT 1 FROM data_ops.serving_publication WHERE release_id=%s',
                        (release_id,))
            if cur.fetchone():
                print(f'{release_id} already published; no changes')
                return None
        tables = list(loader.serving.TABLES)
        counts = loader.load_fk_set(conn, [t for t in tables
            if t.strategy != loader.serving.PARTITION_SWAP], commit=False)
        for table in tables:
            if table.strategy == loader.serving.PARTITION_SWAP:
                counts[table.name] = loader.load_partition_swap(conn, table, commit=False)
        with conn.cursor() as cur:
            loader.sync_serving_sequences(cur)
        forecasts.run(connection=conn)
        for job in (plans, bill_verifier, hardship, alerts):
            if job.main([], connection=conn) != 0:
                raise RuntimeError(f'{job.__name__} failed')
        validate(conn, counts)
        with conn.cursor() as cur:
            cur.execute('INSERT INTO data_ops.serving_publication(release_id,row_counts) '
                        'VALUES (%s,%s::jsonb)', (release_id, json.dumps(counts)))
    print(f'published {release_id}: {json.dumps(counts, sort_keys=True)}')
    return counts


def _log_marts(conn, counts, started, run_id, error):
    # Never raises: a log row that cannot be written must not replace the
    # publication's own outcome, least of all its exception.
    try:
        loader.log_marts(conn, counts, started, dt.datetime.now(dt.UTC), run_id, error)
    except Exception as exc:  # noqa: BLE001
        print(f'WARNING: kea.mart_load_log not written: {exc}')


def main():
    import psycopg2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-id', required=True)
    parser.add_argument('--run-id', default=os.environ.get('DBT_INVOCATION_ID'),
                        help='the warehouse run this came from, for kea.mart_load_log '
                             '(default: the release id)')
    args = parser.parse_args()
    dsn = os.environ.get('KEA_PUBLISH_DSN')
    if not dsn:
        raise SystemExit('Set KEA_PUBLISH_DSN to the explicit target database')
    names = [t.name for t in loader.serving.TABLES]
    conn = psycopg2.connect(dsn)
    started, counts, error = dt.datetime.now(dt.UTC), None, None
    try:
        # ONE LINEAGE JOB, because it is one transaction: the served tables and
        # the decisions computed from them commit together or not at all.
        with loader.lineage.job(
                'publish_serving',
                inputs=[loader.lineage.lake(loader.lake.export_prefix(n)) for n in names],
                outputs=[loader.lineage.serving(*n.split('.', 1))
                         for n in (*names, *DECISIONS)]):
            counts = publish(conn, args.release_id)
    except Exception as exc:
        error = str(exc)[:500]
        raise
    finally:
        # kea.mart_load_log is what the Stage 5 dashboard and assert_serving
        # read, so a failed publication records itself as well as a good one.
        # It is written after the publication's transaction has ended, because
        # a row written inside it would roll back with the failure it describes.
        # A release that was already published changed nothing and logs nothing.
        if counts is not None or error:
            _log_marts(conn, counts or {}, started, args.run_id or args.release_id, error)
        conn.close()


if __name__ == '__main__':
    main()
