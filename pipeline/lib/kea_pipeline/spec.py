"""The seventeen source entities, and how each is extracted.

Design §4.1's per-source table, made executable. One row here is one extract
task, so a disagreement between the doc and the pipeline is a diff rather than
an argument.

WHY MOST OF THEM ARE SNAPSHOTS. §4.1: "Under ~1M rows, a snapshot is simpler,
self-correcting, and removes an entire class of missed-delete bugs." An
incremental extract cannot see a DELETE -- the row simply stops being returned
and the lake keeps it forever.

WHAT A SNAPSHOT DOES TO "RE-RUNNING A LOAD DATE CHANGES NOTHING".

This is the part the design does not address and it needs stating. For
`interval_reads` the exit condition is provable, because the window is a pure
function of the load date: the same predicate always selects the same rows. A
snapshot has no window. Re-running load_date=2026-08-25 tomorrow captures
whatever the table holds TOMORROW, so a replay legitimately differs and the
partition stops being a record of what the source held on the 25th.

The rule adopted here: **a sealed snapshot partition is immutable.** Once
_SUCCESS is written, a re-run is a no-op unless --force is passed explicitly.
Re-running a load date then changes nothing for all seventeen entities rather than
for the one that was hardest, and the exit condition means the same thing
everywhere. --force remains for the case where the earlier run is known to be
wrong, which is a decision someone takes rather than a default.

THE TWO DEVIATIONS FROM §4.1, BOTH DELIBERATE.

`invoices` stays incremental on ISSUE_DT, as the design says. That column is a
naive `timestamp`, not a timestamptz, so the window is expressed in NZ WALL
CLOCK rather than as an instant -- see `window_predicate`. It is worth keeping
incremental precisely because it exercises a second watermark shape.

`invoice_lines` becomes a SNAPSHOT, against §4.1. The table has no timestamp
column at all, so "incremental on ISSUE_DT" can only be reached by joining to
`invoices` -- at which point the extract is no longer a single-table read, and
a bug in the join silently drops billing lines. At 101,284 rows the design's
own rule applies. Production is a different matter: ~12M lines/yr is over the
threshold, and there the join (or a real timestamp on the table) has to be
faced. Recorded rather than quietly decided.
"""

from __future__ import annotations

import datetime as dt
from typing import NamedTuple

from . import window

SNAPSHOT = "snapshot"
INCREMENTAL = "incremental"


class Entity(NamedTuple):
    source: str        # src_billing | src_metering | src_catalogue | src_weather
    table: str
    mode: str
    watermark: str | None
    #: True when the watermark is a naive `timestamp` read as NZ wall clock,
    #: rather than a timestamptz compared as an instant.
    naive_watermark: bool = False

    @property
    def qualified(self) -> str:
        return f"{self.source}.{self.table}"

    @property
    def connection(self) -> str:
        """The Airflow connection this entity may be read with, and only this
        one. Four systems, four credentials -- a task that could reach two of
        them makes the isolation a convention."""
        return f"kea_src_{self.source.removeprefix('src_')}"

    def window_predicate(self, load_date: dt.date) -> str | None:
        """The WHERE clause for one load date, or None for a snapshot."""
        if self.mode == SNAPSHOT:
            return None
        if self.naive_watermark:
            # A naive timestamp carries no zone, so it cannot be compared to an
            # instant. Billing is a New Zealand retailer's system and the dates
            # it stores are the ones printed on the bill, so the bounds are NZ
            # WALL CLOCK -- the same calendar day the instant-based windows
            # cover, expressed in the only terms this column has.
            lo = load_date.isoformat()
            hi = (load_date + dt.timedelta(days=1)).isoformat()
            sql = (f"{self.watermark} >= TIMESTAMP '{lo} 00:00:00' "
                   f"AND {self.watermark} < TIMESTAMP '{hi} 00:00:00'")
            window.assert_no_volatile_sql(sql)
            return sql
        return window.window_sql(self.watermark, load_date)


#: The seventeen. `interval_reads` is here for completeness and is extracted by
#: Spark (design §5.1); everything else is a plain script.
ENTITIES: tuple[Entity, ...] = (
    # --- metering ---------------------------------------------------------
    Entity("src_metering", "networks", SNAPSHOT, None),
    Entity("src_metering", "connection_points", SNAPSHOT, None),
    Entity("src_metering", "meters", SNAPSHOT, None),
    Entity("src_metering", "registers", SNAPSHOT, None),
    Entity("src_metering", "interval_reads", INCREMENTAL, "received_at"),
    # --- billing ----------------------------------------------------------
    Entity("src_billing", "customers", SNAPSHOT, None),
    Entity("src_billing", "accounts", SNAPSHOT, None),
    Entity("src_billing", "invoices", INCREMENTAL, "issue_dt", naive_watermark=True),
    Entity("src_billing", "invoice_lines", SNAPSHOT, None),
    # Payment time is stored as a naive NZ billing-system timestamp, just like
    # ISSUE_DT. Treat its daily window as NZ wall clock, not as a UTC instant.
    Entity("src_billing", "payments", INCREMENTAL, "paid_ts", naive_watermark=True),
    Entity("src_billing", "account_plan_history", SNAPSHOT, None),
    # --- catalogue --------------------------------------------------------
    Entity("src_catalogue", "price_plans", SNAPSHOT, None),
    Entity("src_catalogue", "tou_windows", SNAPSHOT, None),
    Entity("src_catalogue", "rate_components", SNAPSHOT, None),
    Entity("src_catalogue", "term_credits", SNAPSHOT, None),
    # --- weather ----------------------------------------------------------
    # A THIRD PARTY's system, not Kea's, and the only one here retrieved as
    # files rather than read from a database Kea runs. Snapshot for a reason
    # beyond size: NIWA republishes the whole history monthly, and a corrected
    # historical observation arrives as a CHANGED ROW in the next file. An
    # incremental read keyed on anything would keep the superseded value
    # forever, because nothing about the row says it moved.
    Entity("src_weather", "stations", SNAPSHOT, None),
    Entity("src_weather", "daily_observations", SNAPSHOT, None),
)

#: Spark's one job (design §5.1). Everything else is a script, because
#: standing up a distributed engine for a 73-row table costs more in startup
#: than the read costs at all.
SPARK_ENTITIES = frozenset({"src_metering.interval_reads"})


def by_source(source: str) -> list[Entity]:
    return [e for e in ENTITIES if e.source == source]


def script_entities(source: str) -> list[Entity]:
    return [e for e in by_source(source) if e.qualified not in SPARK_ENTITIES]


def get(qualified: str) -> Entity:
    for entity in ENTITIES:
        if entity.qualified == qualified:
            return entity
    raise KeyError(f"{qualified} is not one of the seventeen source entities")
