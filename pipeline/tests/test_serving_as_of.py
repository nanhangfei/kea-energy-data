"""serving_as_of.sql on PostgreSQL; set KEA_TEST_DSN to a disposable empty DB.

The script is a psql script and CI's image has no psql, so the test runs its
SQL without the psql lines: the \\if/\\set/\\echo meta-commands and the
transaction control around them. Each case runs in a transaction the test
rolls back.
"""
import datetime as dt
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "serving"))
import migrate_serving as migration

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "serving_as_of.sql"

# NZ day -> the NZ midnight that ends it, in UTC. 17 Aug ends in NZST (+12).
# Daylight saving starts on 27 Sep 2026, so that day ends in NZDT (+13), and
# ends on 4 Apr 2027, so that day ends back in NZST (+12). 31 Aug is a month's
# last day, so its month's hourly profile is complete and stays.
CUTOFFS = {
    dt.date(2026, 8, 17): dt.datetime(2026, 8, 17, 12, tzinfo=dt.timezone.utc),
    dt.date(2026, 8, 31): dt.datetime(2026, 8, 31, 12, tzinfo=dt.timezone.utc),
    dt.date(2026, 9, 27): dt.datetime(2026, 9, 27, 11, tzinfo=dt.timezone.utc),
    dt.date(2027, 4, 4): dt.datetime(2027, 4, 4, 12, tzinfo=dt.timezone.utc),
}
SESSIONS = ('UTC', 'Pacific/Auckland')


def script_sql(as_of):
    lines = [line for line in SCRIPT.read_text().splitlines()
             if not line.startswith('\\')
             and line.strip() not in ('BEGIN;', 'COMMIT;', 'ROLLBACK;')]
    return '\n'.join(lines).replace(":'as_of'", "'%s'" % as_of.isoformat())


@unittest.skipUnless(os.environ.get('KEA_TEST_DSN'), 'requires disposable PostgreSQL')
class ServingAsOfIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        cls.conn = psycopg2.connect(os.environ['KEA_TEST_DSN'])
        migration.migrate(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def run_as_of(self, as_of, session_tz):
        cutoff = CUTOFFS[as_of]
        half_hour = dt.timedelta(minutes=30)
        this_month = as_of.replace(day=1)
        months = {(this_month - dt.timedelta(days=1)).replace(day=1): 20,
                  this_month: 8,
                  (this_month + dt.timedelta(days=32)).replace(day=1): 5}
        with self.conn.cursor() as cur:
            cur.execute('SET LOCAL TIME ZONE %s', (session_tz,))
            for day in range(-1, 3):
                cur.execute(
                    "INSERT INTO kea.mart_daily_usage (icp_id, usage_date, interval_count,"
                    " expected_intervals, is_weekend) VALUES (1, %s, 48, 48, false)",
                    (as_of + dt.timedelta(days=day),))
            # Account 1 was read after the cutoff; account 2 has no reading time.
            for account, last_read_at in ((1, cutoff + dt.timedelta(days=2)), (2, None)):
                cur.execute(
                    "INSERT INTO kea.mart_account_context (account_id, account_number,"
                    " customer_id, customer_number, customer_name, icp_id, icp_number, plan_id,"
                    " plan_code, plan_name, user_type, payment_method, billing_cycle_day,"
                    " last_read_at) VALUES (%(a)s, 'A' || %(a)s, %(a)s, 'C' || %(a)s, 'Test',"
                    " %(a)s, 'ICP' || %(a)s, 1, 'P1', 'Plan', 'standard', 'direct_debit', 1,"
                    " %(read)s)", {'a': account, 'read': last_read_at})
            # The last half-hour of the NZ day, and the first of the next.
            for start in (cutoff - half_hour, cutoff):
                cur.execute(
                    "INSERT INTO kea.mart_charging_session (icp_id, session_start, session_end,"
                    " half_hours, kwh_low, kwh_high, charger_class, confidence, method_version,"
                    " masked_half_hours) VALUES (1, %s, %s, 1, 1, 2, 'low_power', 'high',"
                    " 'test', 0)", (start, start + half_hour))
            # The month before as_of, its own month and the month after.
            for month, sample_days in months.items():
                cur.execute(
                    "INSERT INTO kea.mart_hourly_profile (icp_id, profile_month, hour_of_day,"
                    " is_weekday, sample_days) VALUES (1, %s, 18, true, %s)",
                    (month, sample_days))
            cur.execute(script_sql(as_of))
            cur.execute('SELECT session_start FROM kea.mart_charging_session ORDER BY 1')
            sessions = [row[0] for row in cur.fetchall()]
            cur.execute('SELECT account_id, last_read_at FROM kea.mart_account_context')
            last_read = dict(cur.fetchall())
            cur.execute('SELECT max(usage_date) FROM kea.mart_daily_usage')
            newest_usage = cur.fetchone()[0]
            cur.execute('SELECT profile_month, sample_days FROM kea.mart_hourly_profile'
                        ' ORDER BY 1')
            profiles = cur.fetchall()
        self.conn.rollback()
        return sessions, last_read, newest_usage, profiles, months

    def test_cutoff_is_the_nz_midnight_whatever_the_session_timezone(self):
        for as_of, cutoff in CUTOFFS.items():
            for session_tz in SESSIONS:
                with self.subTest(as_of=as_of.isoformat(), session=session_tz):
                    sessions, last_read, newest_usage, _, _ = self.run_as_of(as_of, session_tz)
                    self.assertEqual(sessions, [cutoff - dt.timedelta(minutes=30)])
                    self.assertEqual(last_read[1], cutoff)
                    self.assertEqual(newest_usage, as_of)

    def test_an_account_with_no_reading_time_keeps_none(self):
        # least() ignores NULL, so it stamped the cutoff on account 2.
        for as_of in CUTOFFS:
            with self.subTest(as_of=as_of.isoformat()):
                _, last_read, _, _, _ = self.run_as_of(as_of, 'UTC')
                self.assertIsNone(last_read[2])

    def test_only_months_complete_by_as_of_keep_their_profile(self):
        # A mid-month as_of drops its own month, which averages later days; the
        # month before stays as built, sample_days and all. On a month's last
        # day that month is complete and stays too.
        for as_of in CUTOFFS:
            with self.subTest(as_of=as_of.isoformat()):
                _, _, _, profiles, months = self.run_as_of(as_of, 'UTC')
                ends = {month: (month + dt.timedelta(days=32)).replace(day=1)
                        - dt.timedelta(days=1) for month in months}
                expected = [(month, days) for month, days in sorted(months.items())
                            if ends[month] <= as_of]
                self.assertTrue(expected)
                self.assertEqual(profiles, expected)


if __name__ == '__main__':
    unittest.main()
