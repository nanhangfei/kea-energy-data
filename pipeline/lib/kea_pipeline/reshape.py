"""The reshapes of design doc 5, as pure functions, and the reject vocabulary.

Parse does exactly this and nothing else: "reshape what the extract dropped
into the conformed schema... No business logic -- validation, quarantine
routing and key resolution all happen in dbt."

The line is worth stating precisely, because every one of these is a judgement
call away from being business logic:

  RESHAPING is a total, row-local rewriting of a representation. 'A' and
  'actual' are the same fact written by two teams. Nothing is decided.

  BUSINESS LOGIC needs something the row does not carry -- another row,
  another table, a rule about what should happen. Choosing which of two
  versions of a reading wins is business logic, and parse must not do it,
  because that choice is precisely the evidence Stage 3's exit criterion
  tests for.

So: no dedup, no key resolution, no cross-row anything. A reject here is
decidable from one row of one file.

Stdlib only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

NZ = ZoneInfo("Pacific/Auckland")

CONTRACTS = Path(os.environ.get("KEA_CONTRACTS_DIR", Path(__file__).resolve().parents[2] / "contracts"))
REJECT_REASONS = CONTRACTS / "reject_reasons.json"


class RejectRow(Exception):
    """This row cannot be reshaped. Carries the vocabulary code for quarantine."""

    def __init__(self, reason: str, detail: str = ""):
        if reason not in parse_reasons():
            raise KeyError(f"{reason!r} is not in the parse vocabulary; add it to {REJECT_REASONS.name}")
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _vocabulary() -> dict:
    with open(REJECT_REASONS, encoding="utf-8") as fh:
        return json.load(fh)


def parse_reasons() -> dict[str, str]:
    """The codes this stage may emit. Loaded, not hardcoded, so the file is
    the contract rather than a description of one."""
    return _vocabulary()["parse"]


# --- the five reshapes of design 5 -----------------------------------------

#: read_status -> quality. 'F' is here because the source really produces it
#: (1,010 rows), even though design 5's table lists only A and E and the
#: serving enum common.read_quality has no 'final'. Mapping it onto 'actual'
#: would lose a distinction the source draws, which is a decision, not a
#: rewriting -- so KEA-047 carries the third symbol and Stage 5's loader owns
#: the clash. See the open item in the Stage 3 plan.
READ_STATUS = {"A": "actual", "E": "estimated", "F": "final"}

#: The Y/N flags billing stores as characters.
YES_NO = {"Y": True, "N": False}

#: CHG_GROUP -> charge_group. The three the source CHECK permits.
CHARGE_GROUP = {"NET": "network", "ENE": "energy", "OTH": "other"}


def quality(read_status: str | None) -> str:
    """'A' -> 'actual'. The reshape the meter-read parse exists for."""
    if read_status is None:
        raise RejectRow("parse.null_in_required_field", "read_status")
    try:
        return READ_STATUS[read_status.strip().upper()]
    except KeyError:
        raise RejectRow("parse.enum_unknown", f"read_status={read_status!r}") from None


def flag(value: str | None) -> bool | None:
    """'Y'/'N' -> boolean. Null stays null: an unanswered question is not a No,
    and MED_DEPENDENT in particular decides whether the agent may speak at all."""
    if value is None or value == "":
        return None
    try:
        return YES_NO[value.strip().upper()]
    except KeyError:
        raise RejectRow("parse.enum_unknown", f"flag={value!r}") from None


def charge_group(chg_group: str | None) -> str:
    """'NET' -> 'network'."""
    if chg_group is None:
        raise RejectRow("parse.null_in_required_field", "chg_group")
    try:
        return CHARGE_GROUP[chg_group.strip().upper()]
    except KeyError:
        raise RejectRow("parse.enum_unknown", f"chg_group={chg_group!r}") from None


def days_of_week(days_csv: str | None) -> list[int]:
    """'1,2,3,4,5' -> [1,2,3,4,5], ISO day numbers.

    Split on the separator, never searched as a substring. `position('1' in
    '1,2,3,4,5')` and its relatives match the '1' inside '11', which is the
    documented TOU banding trap -- a plan that should apply on weekdays
    quietly applying on a Sunday, and the bill still balancing.
    """
    if days_csv is None or days_csv.strip() == "":
        raise RejectRow("parse.null_in_required_field", "days_csv")
    out = []
    for part in days_csv.split(","):
        part = part.strip()
        if not part.isdigit() or not 1 <= int(part) <= 7:
            raise RejectRow("parse.enum_unknown", f"days_csv={days_csv!r}")
        out.append(int(part))
    if len(set(out)) != len(out):
        raise RejectRow("parse.enum_unknown", f"days_csv repeats a day: {days_csv!r}")
    return sorted(out)


def to_instant(value: dt.datetime | None) -> dt.datetime | None:
    """Billing's naive `timestamp` -> a UTC instant. Metering's is already one.

    The source systems disagree: `src_metering` uses `timestamptz`
    throughout, `src_billing` uses naive `timestamp` on ISSUE_DT, LAST_MOD_TS
    and the rest. The conformed schema is timestamptz in UTC, so the naive
    ones need a zone attached -- and choosing it is a decision that has to be
    written down rather than defaulted.

    THEY ARE READ AS PACIFIC/AUCKLAND. The billing system is a New Zealand
    retailer's, its clock is the local one, and the dates it stores are the
    dates printed on the bill. Reading them as UTC instead would move every
    issue date up to 13 hours, which is enough to shift a bill's period end
    across a plan-version boundary and rate it on the wrong tariff -- the
    silent failure of design 2.1.2.

    An aware value passes through converted, never re-localised, so running
    this twice is harmless.
    """
    if value is None:
        return None
    if not isinstance(value, dt.datetime):
        raise RejectRow("parse.timestamp_unparseable", f"{value!r}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=NZ)
    return value.astimezone(dt.timezone.utc)


def kwh(value) -> object:
    """Non-negative by contract. A negative value cannot be a source row --
    ck_kwh_non_negative forbids it -- so meeting one means reading a file, and
    the only honest response is to quarantine it rather than take its
    magnitude and guess at a direction."""
    if value is None:
        raise RejectRow("parse.null_in_required_field", "kwh")
    try:
        negative = value < 0
    except TypeError:
        raise RejectRow("parse.type_unparseable", f"kwh={value!r}") from None
    if negative:
        raise RejectRow("parse.negative_read", f"kwh={value}")
    return value
