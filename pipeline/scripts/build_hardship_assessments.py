"""Assess every open account for explainable hardship signals.

    python3 build_hardship_assessments.py --dry-run
    python3 build_hardship_assessments.py
    python3 build_hardship_assessments.py --as-of 2027-09-21

Sprint 3 item E.  This is an identification job, not a score and not a
customer-contact path.  It creates one append-only ``kea.hardship_assessment``
parent for *every* open account, including accounts with no signals, and child
``kea.hardship_signal`` rows only for the five closed-vocabulary reason codes.

The rules are intentionally simple, visible and versioned:

* a failed direct debit in the last 180 days;
* two or more consecutive bills paid in full after their due date;
* two or more consecutive mature bills that were only partly paid;
* an established payer with no successful payment for 60 days while at least
  two bills in that gap remain unpaid; or
* a >=40% drop in the most recent complete seven days of import use against
  the preceding 21 complete days.

The first four rules use the payment and bill facts added in Sprint 3.  The
last one delegates its arithmetic to ``app.analytics.hardship.detect_usage_collapse``;
this script supplies real daily totals and does not duplicate the analytics
rule.  ``build_hardship`` is deliberately not called here: it produces the
customer-safe conversational handoff, whereas this job must retain internal,
auditable evidence for staff.

The default business date is the newest local date available from either the
payment feed or import readings.  The fixed source fixture can contain a late
payment after its final meter day, so consumption is assessed at its own latest
day and is skipped if that data is more than 31 days stale.  Its signal carries
its actual first/last dates; no old meter data is represented as current.

Rows are append-only.  Pass a stable ``--computed-at`` value when retrying a
partially interrupted orchestration run: the table's natural key makes that
retry a no-op rather than writing a duplicate assessment generation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Sequence

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))

# The usage-collapse detector belongs to the analytics layer.  Import it rather
# than recreating the 7-day / 40% arithmetic here, just as recommendation
# pricing imports tariff.py instead of maintaining a second rate engine.
_API_APP = Path(os.environ.get("KEA_API_DIR", _HERE.parents[3] / "services" / "api"))
if not (_API_APP / "app" / "analytics" / "engine.py").exists():
    raise SystemExit(
        f"the analytics engine is not at {_API_APP}/app/analytics/engine.py.\n"
        "Set KEA_API_DIR to the services/api directory."
    )
sys.path.insert(0, str(_API_APP))

from app.analytics.hardship import detect_usage_collapse  # noqa: E402
from load_export_to_serving import _connect as connect_serving  # noqa: E402


NZ = "Pacific/Auckland"
RULESET_VERSION = "hardship-identification-v1"

# A six-month payment view contains enough monthly bills to distinguish a
# repeated pattern from an isolated late payment.  The meter rule itself keeps
# the analytics module's 21-day baseline plus a seven-day tail.
OBSERVATION_DAYS = 180
USAGE_DAYS = 28
USAGE_TAIL_DAYS = 7
USAGE_DROP_THRESHOLD_PCT = 40.0
MAX_METER_STALENESS_DAYS = 31
MIN_LATE_PAYMENT_STREAK = 2
MIN_PART_PAYMENT_STREAK = 2
PAYMENTS_STOPPED_DAYS = 60
MIN_UNPAID_BILLS_FOR_STOPPED = 2
MIN_DAILY_READ_COVERAGE = 0.90
MONEY_EPSILON = Decimal("0.01")


@dataclass(frozen=True)
class Account:
    account_id: int
    account_number: str
    is_medically_dependent: bool
    is_vulnerable: bool


@dataclass(frozen=True)
class BillEvidence:
    bill_id: int
    due_on: dt.date
    amount_due: Decimal
    payments_applied: Decimal
    latest_successful_payment_on: dt.date | None


@dataclass(frozen=True)
class DateSummary:
    count: int
    first_on: dt.date
    last_on: dt.date


@dataclass(frozen=True)
class PaymentHistory:
    first_successful_on: dt.date | None
    latest_successful_on: dt.date | None


@dataclass(frozen=True)
class DailyUsage:
    usage_on: dt.date
    kwh: Decimal
    read_count: int


@dataclass(frozen=True)
class Signal:
    code: str
    value: str
    evidence_count: int
    first_seen_on: dt.date
    last_seen_on: dt.date
    explanation: str


@dataclass(frozen=True)
class Assessment:
    account: Account
    assessed_on: dt.date
    observed_from: dt.date
    observed_to: dt.date
    signals: tuple[Signal, ...]


# The source of the date must be data rather than the wall clock.  The source
# generator deliberately spans dates beyond today, and using current_date would
# silently shorten a reproducible assessment window.
MAX_DATE_SQL = f"""
select
    (
        -- The newest arrival partition normally contains only the first few
        -- intervals of its newest meter day. Anchoring on max(interval_start)
        -- makes that partial frontier the required seventh day and suppresses
        -- every usage signal. Pick the newest fleet day averaging at least the
        -- shortest valid NZ day (46 intervals); the per-account 90% check below
        -- still rejects any individual feed gap.
        select max(usage_on)
        from (
            select (h.interval_start at time zone '{NZ}')::date as usage_on
            from metering.meter_read_hh h
            join metering.meter_register r on r.register_id = h.register_id
            join billing.account a on a.icp_id = r.icp_id and a.end_date is null
            where r.direction = 'import'
            group by (h.interval_start at time zone '{NZ}')::date
            having count(*) >= 46 * count(distinct a.account_id)
        ) complete_fleet_days
    ) as latest_meter_on,
    (
        select max((p.paid_at at time zone '{NZ}')::date)
        from billing.payment p
        join billing.account a on a.account_id = p.account_id and a.end_date is null
    ) as latest_payment_on
"""

ACCOUNT_SQL = """
select a.account_id, a.account_number,
       c.is_medically_dependent, c.is_vulnerable
from billing.account a
join billing.customer c on c.customer_id = a.customer_id
join metering.icp i on i.icp_id = a.icp_id
where a.end_date is null
  and i.icp_number like '09%%'
order by a.account_id
"""

# ``payments_applied`` is the biller's amount applied to this invoice and is
# the direct source for the part-payment rule.  The individual payment join is
# separately needed to tell whether a completed payment happened after due date.
BILL_SQL = f"""
select b.account_id, b.bill_id, b.due_date, b.total_charges,
       b.payments_applied,
       max((p.paid_at at time zone '{NZ}')::date)
           filter (where not p.is_failed
                   and p.paid_at < ((%(as_of)s::date + 1)::timestamp
                                    at time zone '{NZ}'))
           as latest_successful_payment_on
from billing.bill b
left join billing.payment p on p.bill_id = b.bill_id
where b.due_date >= %(observed_from)s::date
  and b.due_date <= %(as_of)s::date
  and b.status <> 'cancelled'
  and b.total_charges > 0
group by b.account_id, b.bill_id, b.due_date, b.total_charges,
         b.payments_applied
order by b.account_id, b.due_date, b.bill_id
"""

FAILED_DIRECT_DEBIT_SQL = f"""
select p.account_id, count(*)::integer,
       min((p.paid_at at time zone '{NZ}')::date),
       max((p.paid_at at time zone '{NZ}')::date)
from billing.payment p
join billing.account a on a.account_id = p.account_id and a.end_date is null
where p.is_failed
  and p.method = 'direct_debit'
  and p.paid_at >= (%(observed_from)s::date::timestamp at time zone '{NZ}')
  and p.paid_at < ((%(as_of)s::date + 1)::timestamp at time zone '{NZ}')
group by p.account_id
"""

PAYMENT_HISTORY_SQL = f"""
select p.account_id,
       min((p.paid_at at time zone '{NZ}')::date),
       max((p.paid_at at time zone '{NZ}')::date)
from billing.payment p
join billing.account a on a.account_id = p.account_id and a.end_date is null
where not p.is_failed
  and p.paid_at < ((%(as_of)s::date + 1)::timestamp at time zone '{NZ}')
group by p.account_id
"""

# A missing meter interval can mimic a usage collapse.  Aggregate both kWh and
# row count, then require every day in the 28-day window and >=90% of the
# account's median daily coverage before calling the analytics detector.  This
# permits NZ's 46/50-interval DST days without accepting a partial export.
DAILY_USAGE_SQL = f"""
select a.account_id,
       (h.interval_start at time zone '{NZ}')::date as usage_on,
       sum(h.kwh) as kwh,
       count(*)::integer as read_count
from metering.meter_read_hh h
join metering.meter_register r on r.register_id = h.register_id
join billing.account a on a.icp_id = r.icp_id and a.end_date is null
where r.direction = 'import'
  and h.quality <> 'missing'
  and h.interval_start >= (%(usage_from)s::date::timestamp at time zone '{NZ}')
  and h.interval_start < ((%(usage_to)s::date + 1)::timestamp at time zone '{NZ}')
group by a.account_id, (h.interval_start at time zone '{NZ}')::date
order by a.account_id, usage_on
"""


def _as_decimal(value) -> Decimal:
    return Decimal(str(value))


def _longest_streak(
    bills: Sequence[BillEvidence], predicate: Callable[[BillEvidence], bool]
) -> tuple[BillEvidence, ...]:
    """Return the longest contiguous sequence of bills satisfying ``predicate``.

    The incoming view contains only positive, mature bills in the observation
    window, ordered by due date.  A bill paid on time (or a fully unpaid bill)
    resets a late-payment streak; an invoice paid in full resets a part-payment
    streak.  That is what makes both rules about repetition rather than a
    single accidental late payment.
    """
    best: tuple[BillEvidence, ...] = ()
    current: list[BillEvidence] = []
    for bill in sorted(bills, key=lambda b: (b.due_on, b.bill_id)):
        if predicate(bill):
            current.append(bill)
            if len(current) > len(best):
                best = tuple(current)
        else:
            current = []
    return best


def _paid_in_full(bill: BillEvidence) -> bool:
    return bill.payments_applied + MONEY_EPSILON >= bill.amount_due


def _part_paid(bill: BillEvidence) -> bool:
    return (bill.payments_applied > MONEY_EPSILON
            and bill.payments_applied + MONEY_EPSILON < bill.amount_due)


def usage_collapse_signal(
    daily: Sequence[DailyUsage], *, usage_to: dt.date
) -> Signal | None:
    """Run the shared detector only when the data can rule out a feed gap."""
    expected_days = [usage_to - dt.timedelta(days=USAGE_DAYS - 1 - offset)
                     for offset in range(USAGE_DAYS)]
    by_day = {row.usage_on: row for row in daily}
    if any(day not in by_day for day in expected_days):
        return None

    rows = [by_day[day] for day in expected_days]
    median_count = statistics.median(row.read_count for row in rows)
    if median_count <= 0:
        return None
    if any(row.read_count < median_count * MIN_DAILY_READ_COVERAGE for row in rows):
        return None

    collapse = detect_usage_collapse(
        [float(row.kwh) for row in rows],
        tail_days=USAGE_TAIL_DAYS,
        threshold_pct=USAGE_DROP_THRESHOLD_PCT,
    )
    if not collapse.detected:
        return None

    recent_from = expected_days[-USAGE_TAIL_DAYS]
    return Signal(
        code="usage_collapse",
        value=(f"Recent {USAGE_TAIL_DAYS}-day average {collapse.drop_pct:.0f}% below "
               f"the preceding {USAGE_DAYS - USAGE_TAIL_DAYS}-day average "
               f"({collapse.recent_mean:.1f} vs {collapse.earlier_mean:.1f} kWh/day)"),
        evidence_count=USAGE_TAIL_DAYS,
        first_seen_on=recent_from,
        last_seen_on=usage_to,
        explanation=("Complete import meter data shows a sustained recent consumption "
                     "drop. This is a care signal that needs human context, not a "
                     "finding about the customer's behaviour."),
    )


def evaluate_signals(
    *,
    as_of: dt.date,
    bills: Sequence[BillEvidence],
    failed_direct_debits: DateSummary | None,
    payment_history: PaymentHistory | None,
    daily_usage: Sequence[DailyUsage],
    usage_to: dt.date | None,
) -> tuple[Signal, ...]:
    """Apply the versioned, explainable rules for one account."""
    signals: list[Signal] = []

    if failed_direct_debits is not None:
        signals.append(Signal(
            code="direct_debit_failed",
            value=f"{failed_direct_debits.count} failed direct debit(s)",
            evidence_count=failed_direct_debits.count,
            first_seen_on=failed_direct_debits.first_on,
            last_seen_on=failed_direct_debits.last_on,
            explanation=("A direct-debit attempt failed in the observation window. "
                         "Failure is a payment event, not an assumption drawn from "
                         "missing payments."),
        ))

    late = _longest_streak(
        bills,
        lambda bill: (_paid_in_full(bill)
                      and bill.latest_successful_payment_on is not None
                      and bill.latest_successful_payment_on > bill.due_on),
    )
    if len(late) >= MIN_LATE_PAYMENT_STREAK:
        last_paid = max(bill.latest_successful_payment_on for bill in late)
        delays = [(bill.latest_successful_payment_on - bill.due_on).days
                  for bill in late if bill.latest_successful_payment_on is not None]
        signals.append(Signal(
            code="late_payment_streak",
            value=(f"{len(late)} consecutive bills paid {min(delays)}–{max(delays)} "
                   "days after due date"),
            evidence_count=len(late),
            first_seen_on=late[0].due_on,
            last_seen_on=last_paid,
            explanation=("Consecutive mature invoices were settled after their due dates. "
                         "The rule excludes one-off late payments and unpaid new bills."),
        ))

    partial = _longest_streak(bills, _part_paid)
    if len(partial) >= MIN_PART_PAYMENT_STREAK:
        outstanding = sum((bill.amount_due - bill.payments_applied for bill in partial),
                          Decimal("0"))
        signals.append(Signal(
            code="part_payment_arrears",
            value=(f"{len(partial)} consecutive bills partly paid; "
                   f"${outstanding:,.2f} remains across the streak"),
            evidence_count=len(partial),
            first_seen_on=partial[0].due_on,
            last_seen_on=partial[-1].due_on,
            explanation=("The biller recorded a positive but incomplete payment on each "
                         "consecutive invoice. This identifies compounding arrears, not "
                         "a one-off underpayment."),
        ))

    gap_start = as_of - dt.timedelta(days=PAYMENTS_STOPPED_DAYS)
    unpaid_recent = [
        bill for bill in bills
        if gap_start < bill.due_on <= as_of and not _paid_in_full(bill)
    ]
    if (payment_history is not None
            and payment_history.latest_successful_on is not None
            and payment_history.latest_successful_on < gap_start
            and len(unpaid_recent) >= MIN_UNPAID_BILLS_FOR_STOPPED):
        no_payment_days = (as_of - payment_history.latest_successful_on).days
        signals.append(Signal(
            code="payments_stopped",
            value=(f"No successful payment for {no_payment_days} days after an "
                   f"established payment history; {len(unpaid_recent)} due bill(s) "
                   "remain unpaid"),
            evidence_count=len(unpaid_recent),
            first_seen_on=min(bill.due_on for bill in unpaid_recent),
            last_seen_on=max(bill.due_on for bill in unpaid_recent),
            explanation=("The account paid successfully before the current gap, then made "
                         "no successful payment while multiple due invoices remained open. "
                         "This deliberately excludes a new account with no history."),
        ))

    if usage_to is not None:
        usage_signal = usage_collapse_signal(daily_usage, usage_to=usage_to)
        if usage_signal is not None:
            signals.append(usage_signal)

    # The schema enum is closed.  Fail before writing rather than letting a
    # spelling error become a partial transaction at the database boundary.
    codes = [signal.code for signal in signals]
    if len(codes) != len(set(codes)):
        raise AssertionError(f"duplicate hardship signal generated: {codes}")
    return tuple(signals)


def _load_anchor_dates(cur) -> tuple[dt.date | None, dt.date | None]:
    cur.execute(MAX_DATE_SQL)
    meter_on, payment_on = cur.fetchone()
    return meter_on, payment_on


def _load_accounts(cur, account_ids: set[int] | None) -> list[Account]:
    cur.execute(ACCOUNT_SQL)
    accounts = [Account(
        account_id=int(account_id),
        account_number=account_number,
        is_medically_dependent=bool(is_medically_dependent),
        is_vulnerable=bool(is_vulnerable),
    ) for account_id, account_number, is_medically_dependent, is_vulnerable
      in cur.fetchall()]
    if account_ids is not None:
        accounts = [account for account in accounts if account.account_id in account_ids]
    return accounts


def _load_bills(
    cur, *, observed_from: dt.date, as_of: dt.date
) -> dict[int, list[BillEvidence]]:
    cur.execute(BILL_SQL, {"observed_from": observed_from, "as_of": as_of})
    out: dict[int, list[BillEvidence]] = {}
    for account_id, bill_id, due_on, amount_due, payments_applied, latest_paid_on in cur.fetchall():
        out.setdefault(int(account_id), []).append(BillEvidence(
            bill_id=int(bill_id),
            due_on=due_on,
            amount_due=_as_decimal(amount_due),
            payments_applied=_as_decimal(payments_applied),
            latest_successful_payment_on=latest_paid_on,
        ))
    return out


def _load_failed_direct_debits(
    cur, *, observed_from: dt.date, as_of: dt.date
) -> dict[int, DateSummary]:
    cur.execute(FAILED_DIRECT_DEBIT_SQL, {"observed_from": observed_from, "as_of": as_of})
    return {
        int(account_id): DateSummary(int(count), first_on, last_on)
        for account_id, count, first_on, last_on in cur.fetchall()
    }


def _load_payment_history(cur, *, as_of: dt.date) -> dict[int, PaymentHistory]:
    cur.execute(PAYMENT_HISTORY_SQL, {"as_of": as_of})
    return {
        int(account_id): PaymentHistory(first_on, last_on)
        for account_id, first_on, last_on in cur.fetchall()
    }


def _load_daily_usage(
    cur, *, usage_from: dt.date, usage_to: dt.date
) -> dict[int, list[DailyUsage]]:
    cur.execute(DAILY_USAGE_SQL, {"usage_from": usage_from, "usage_to": usage_to})
    out: dict[int, list[DailyUsage]] = {}
    for account_id, usage_on, kwh, read_count in cur.fetchall():
        out.setdefault(int(account_id), []).append(DailyUsage(
            usage_on=usage_on,
            kwh=_as_decimal(kwh),
            read_count=int(read_count),
        ))
    return out


def build_assessments(
    *,
    accounts: Iterable[Account],
    as_of: dt.date,
    observed_from: dt.date,
    bills_by_account: dict[int, list[BillEvidence]],
    failed_dd_by_account: dict[int, DateSummary],
    history_by_account: dict[int, PaymentHistory],
    daily_usage_by_account: dict[int, list[DailyUsage]],
    usage_to: dt.date | None,
) -> list[Assessment]:
    """Build one parent object per supplied account, even when it is clean."""
    assessments = []
    for account in accounts:
        signals = evaluate_signals(
            as_of=as_of,
            bills=bills_by_account.get(account.account_id, ()),
            failed_direct_debits=failed_dd_by_account.get(account.account_id),
            payment_history=history_by_account.get(account.account_id),
            daily_usage=daily_usage_by_account.get(account.account_id, ()),
            usage_to=usage_to,
        )
        assessments.append(Assessment(
            account=account,
            assessed_on=as_of,
            observed_from=observed_from,
            observed_to=as_of,
            signals=signals,
        ))
    return assessments


PARENT_INSERT_SQL = """
insert into kea.hardship_assessment
    (account_id, assessed_on, ruleset_version, signal_count,
     is_medically_dependent, is_vulnerable, observed_from, observed_to,
     computed_at)
values %s
on conflict (account_id, assessed_on, computed_at) do nothing
returning assessment_id, account_id
"""

SIGNAL_INSERT_SQL = """
insert into kea.hardship_signal
    (assessment_id, signal_code, signal_value, evidence_count,
     first_seen_on, last_seen_on, explanation)
values %s
"""


def _batches(rows: Sequence[tuple], size: int = 500) -> Iterable[Sequence[tuple]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def insert_assessments(
    cur, assessments: Sequence[Assessment], *, computed_at: dt.datetime
) -> dict[int, int]:
    """Insert one complete append-only assessment generation in this transaction."""
    from psycopg2.extras import execute_values

    parent_rows = [(
        assessment.account.account_id,
        assessment.assessed_on,
        RULESET_VERSION,
        len(assessment.signals),
        assessment.account.is_medically_dependent,
        assessment.account.is_vulnerable,
        assessment.observed_from,
        assessment.observed_to,
        computed_at,
    ) for assessment in assessments]

    assessment_ids: dict[int, int] = {}
    for batch in _batches(parent_rows):
        execute_values(cur, PARENT_INSERT_SQL, batch, page_size=len(batch))
        assessment_ids.update({int(account_id): int(assessment_id)
                               for assessment_id, account_id in cur.fetchall()})

    # A conflict means this exact --computed-at generation has already committed
    # as a whole.  Do not append children to a pre-existing parent: the parent
    # and children share the surrounding transaction, so a committed parent
    # already has its corresponding immutable signal set.
    signals_by_account = {assessment.account.account_id: assessment.signals
                          for assessment in assessments}
    signal_rows = [
        (assessment_ids[account_id], signal.code, signal.value, signal.evidence_count,
         signal.first_seen_on, signal.last_seen_on, signal.explanation)
        for account_id, signals in signals_by_account.items()
        if account_id in assessment_ids
        for signal in signals
    ]
    for batch in _batches(signal_rows):
        execute_values(cur, SIGNAL_INSERT_SQL, batch, page_size=len(batch))

    if assessment_ids:
        cur.execute("""
            select a.account_id, a.signal_count, count(s.signal_id)::smallint
            from kea.hardship_assessment a
            left join kea.hardship_signal s on s.assessment_id = a.assessment_id
            where a.assessment_id = any(%s)
            group by a.account_id, a.signal_count
            having a.signal_count <> count(s.signal_id)::smallint
        """, (list(assessment_ids.values()),))
        mismatches = cur.fetchall()
        if mismatches:
            raise AssertionError(
                f"hardship assessment signal_count does not match its children: {mismatches}"
            )
    return assessment_ids


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _parse_timestamp(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--computed-at must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=_parse_date,
                        help="business date to assess (default: newest feed date)")
    parser.add_argument("--computed-at", type=_parse_timestamp,
                        help="stable UTC run timestamp for an idempotent retry")
    parser.add_argument("--account", type=int, action="append",
                        help="restrict to account_id (repeatable; useful for investigation)")
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate and print, but roll back before writing")
    args = parser.parse_args(argv)

    conn = connect_serving()
    conn.autocommit = False
    try:
        cur = conn.cursor()
        latest_meter_on, latest_payment_on = _load_anchor_dates(cur)
        available_dates = [day for day in (latest_meter_on, latest_payment_on) if day is not None]
        if not available_dates and args.as_of is None:
            raise SystemExit("no meter reads or payments exist; pass --as-of only after feeds land")
        as_of = args.as_of or max(available_dates)
        observed_from = as_of - dt.timedelta(days=OBSERVATION_DAYS - 1)
        account_ids = set(args.account) if args.account else None
        accounts = _load_accounts(cur, account_ids)

        usage_to: dt.date | None = latest_meter_on
        if usage_to is not None and usage_to > as_of:
            usage_to = as_of
        if usage_to is not None and (as_of - usage_to).days > MAX_METER_STALENESS_DAYS:
            print(f"usage collapse skipped: latest meter day {usage_to} is more than "
                  f"{MAX_METER_STALENESS_DAYS} days behind assessment date {as_of}")
            usage_to = None

        bills = _load_bills(cur, observed_from=observed_from, as_of=as_of)
        failed_dd = _load_failed_direct_debits(cur, observed_from=observed_from, as_of=as_of)
        history = _load_payment_history(cur, as_of=as_of)
        daily_usage: dict[int, list[DailyUsage]] = {}
        if usage_to is not None:
            usage_from = usage_to - dt.timedelta(days=USAGE_DAYS - 1)
            daily_usage = _load_daily_usage(cur, usage_from=usage_from, usage_to=usage_to)

        assessments = build_assessments(
            accounts=accounts,
            as_of=as_of,
            observed_from=observed_from,
            bills_by_account=bills,
            failed_dd_by_account=failed_dd,
            history_by_account=history,
            daily_usage_by_account=daily_usage,
            usage_to=usage_to,
        )
        counts = Counter(signal.code for assessment in assessments for signal in assessment.signals)
        flagged = sum(bool(assessment.signals) for assessment in assessments)
        print(f"hardship ruleset {RULESET_VERSION}: {len(assessments):,} open accounts, "
              f"{flagged:,} flagged, observed {observed_from} .. {as_of}")
        print("signals: " + (", ".join(f"{code}={count}" for code, count in sorted(counts.items()))
                            or "none"))

        if args.dry_run:
            conn.rollback()
            return 0

        computed_at = args.computed_at or dt.datetime.now(dt.timezone.utc)
        inserted = insert_assessments(cur, assessments, computed_at=computed_at)
        conn.commit()
        print(f"inserted {len(inserted):,} append-only assessments at "
              f"{computed_at.isoformat()}")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
