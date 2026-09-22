"""Create safe, pending in-app outreach from the latest hardship assessments.

    python3 create_hardship_alerts.py --dry-run
    python3 create_hardship_alerts.py
    python3 create_hardship_alerts.py --account 100001

Sprint 3 item F. This job creates records only: it does not call a provider,
send a message, or mark an alert sent. Runtime owns ``sent_at``, delivery and
engagement fields. Every row is ``pending``, ``in_app`` and unscheduled until a
separate, explicitly authorised delivery workflow acts on it.

The customer-facing copy deliberately contains no debt amount, reason code or
diagnosis. The linked assessment retains staff-only evidence; the message is a
gentle offer of a confidential conversation.

Two database indexes make retries and repeated assessments safe: one alert per
assessment, and at most one unacted hardship outreach per account. A daily
assessment therefore cannot produce daily messages while an earlier outreach
is still open.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))

from load_export_to_serving import _connect as connect_serving  # noqa: E402

SUBJECT = "Support is available"
BODY = (
    "If paying for electricity is difficult, support and payment options are "
    "available. You can ask us for a private conversation when you are ready."
)


# Latest per account, rather than one global max(computed_at). An authorised
# single-account reassessment must not hide every other account's most recent
# full-run result. Clean latest assessments are excluded: they document that
# the account was checked, but they do not trigger contact.
ASSESSMENTS_SQL = """
with latest as (
    select distinct on (a.account_id)
           a.assessment_id, a.account_id, a.assessed_on, a.computed_at,
           a.signal_count
    from kea.hardship_assessment a
    order by a.account_id, a.assessed_on desc, a.computed_at desc,
             a.assessment_id desc
)
select a.assessment_id, a.account_id, a.assessed_on, a.signal_count,
       exists (
           select 1 from kea.alert x
           where x.assessment_id = a.assessment_id
             and x.kind = 'hardship_outreach'
       ) as already_alerted,
       exists (
           select 1 from kea.alert x
           where x.account_id = a.account_id
             and x.kind = 'hardship_outreach'
             and x.acted_at is null
             and x.status in ('pending', 'sent', 'delivered', 'suppressed')
       ) as has_open_outreach
from latest a
where a.signal_count > 0
order by a.account_id
"""

INSERT_SQL = """
insert into kea.alert
    (account_id, kind, channel, status, subject, body, assessment_id,
     scheduled_for)
values %s
on conflict do nothing
returning alert_id, assessment_id
"""


@dataclass(frozen=True)
class Candidate:
    assessment_id: int
    account_id: int
    assessed_on: dt.date
    signal_count: int
    already_alerted: bool
    has_open_outreach: bool

    @property
    def eligible(self) -> bool:
        return (self.signal_count > 0
                and not self.already_alerted
                and not self.has_open_outreach)


def _load_candidates(cur, account_ids: set[int] | None) -> list[Candidate]:
    cur.execute(ASSESSMENTS_SQL)
    rows = [Candidate(
        assessment_id=int(assessment_id), account_id=int(account_id),
        assessed_on=assessed_on, signal_count=int(signal_count),
        already_alerted=bool(already_alerted),
        has_open_outreach=bool(has_open_outreach),
    ) for (assessment_id, account_id, assessed_on, signal_count,
           already_alerted, has_open_outreach) in cur.fetchall()]
    if account_ids is not None:
        rows = [row for row in rows if row.account_id in account_ids]
    return rows


def insert_alerts(cur, candidates: list[Candidate]) -> dict[int, int]:
    """Insert eligible alerts and return assessment_id -> alert_id."""
    from psycopg2.extras import execute_values

    eligible = [candidate for candidate in candidates if candidate.eligible]
    if not eligible:
        return {}
    rows = [(
        candidate.account_id,
        "hardship_outreach",
        "in_app",
        "pending",
        SUBJECT,
        BODY,
        candidate.assessment_id,
        None,  # explicitly unscheduled; this job is not a delivery authority
    ) for candidate in eligible]
    execute_values(cur, INSERT_SQL, rows, page_size=500)
    return {int(assessment_id): int(alert_id)
            for alert_id, assessment_id in cur.fetchall()}


def main(argv: list[str] | None = None, *, connection=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", type=int, action="append",
                        help="restrict to account_id (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report eligible assessments without inserting alerts")
    args = parser.parse_args(argv)

    if connection is not None and args.dry_run:
        raise ValueError('dry_run cannot share a publication transaction')
    conn = connection if connection is not None else connect_serving()
    if connection is None:
        conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("select count(*) from kea.hardship_assessment")
        if cur.fetchone()[0] == 0:
            raise SystemExit("no hardship assessments exist; "
                             "run build_hardship_assessments.py first")
        candidates = _load_candidates(cur, set(args.account) if args.account else None)
        states = Counter(
            "eligible" if row.eligible else
            "already_alerted" if row.already_alerted else
            "open_outreach"
            for row in candidates
        )
        print(f"hardship outreach: {len(candidates):,} latest flagged assessments; "
              f"eligible={states['eligible']:,}, already alerted={states['already_alerted']:,}, "
              f"open outreach={states['open_outreach']:,}")

        if args.dry_run:
            conn.rollback()
            return 0

        inserted = insert_alerts(cur, candidates)
        if connection is None:
            conn.commit()
        print(f"created {len(inserted):,} pending, unscheduled in-app alert(s); sent 0")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        if connection is None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
