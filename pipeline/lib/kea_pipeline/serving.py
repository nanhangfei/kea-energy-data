"""The serving tables Stage 5 loads, in the order the foreign keys demand.

ONE LIST, TWO CONSUMERS -- the exporter and the loader -- for the same reason
spec.ENTITIES is one list: two copies of "which tables exist" drift, and the
drift shows up as a foreign key violation halfway through a load rather than as
a disagreement anyone can see.

THE ORDER IS THE FK GRAPH (design §7.4), NOT ALPHABETICAL AND NOT A GUESS.
Three of these dependencies are easy to miss and the design names all three:

    catalogue.plan -> metering.network     they read like siblings; they are not
    billing.bill_line -> catalogue.tariff_component    not just billing.bill
    billing.customer -> nothing at all     it can go first, in parallel

metering.meter_read_hh deliberately carries no foreign keys, which is what
makes its partition swap safe to run alongside everything else.

app.* IS ABSENT AND MUST STAY ABSENT. Design §1.1: kea_loader has no grant on
it, "not even USAGE". The mechanism is stronger than a withheld grant -- the
schema is defined in the application's repo and does not exist in the DDL this
loader runs against, so a bug cannot reach it even by fully-qualified name.
"""

from __future__ import annotations

from typing import NamedTuple

#: Replace the table wholesale, inside one transaction. For anything small
#: enough that a diff would cost more than a reload (design §7.2).
TRUNCATE = "truncate"

#: DELETE the periods this load covers, then insert them. Bills are corrected
#: and reissued, so a period must be replaceable without touching its
#: neighbours.
DELETE_BY_PERIOD = "delete_by_period"

#: Build the month beside the table, then swap it in as a partition. The only
#: strategy that never leaves the served table mid-write.
PARTITION_SWAP = "partition_swap"


class Table(NamedTuple):
    name: str           # fully qualified: "metering.icp"
    strategy: str
    period_column: str | None = None   # DELETE_BY_PERIOD only

    @property
    def schema(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def sql_file(self) -> str:
        """The export query that produces it. Named for its target, so the
        mapping needs no lookup table of its own."""
        return f"{self.name}.sql"


#: In load order. Reordering this breaks referential integrity; adding to it
#: means adding the matching pipeline/export/<name>.sql.
TABLES: tuple[Table, ...] = (
    # No dependencies -- these can go first, in any order.
    Table("billing.customer", TRUNCATE),
    Table("metering.network", TRUNCATE),
    Table("metering.property", TRUNCATE),
    # icp needs property and network.
    Table("metering.icp", TRUNCATE),
    Table("metering.meter_register", TRUNCATE),
    # plan needs network. This is the one that reads like a sibling.
    Table("catalogue.plan", TRUNCATE),
    # Current term-credit offers are catalogue data too. No FK of their own,
    # but keeping them beside plan makes the current offer available before
    # account/plan history is loaded.
    Table("catalogue.term_credit", TRUNCATE),
    Table("catalogue.tou_band", TRUNCATE),
    Table("catalogue.tariff_component", TRUNCATE),
    # account needs customer, icp and plan.
    Table("billing.account", TRUNCATE),
    # Needs both its account and the temporally resolved catalogue plan.
    Table("billing.account_plan_history", TRUNCATE),
    Table("billing.bill", DELETE_BY_PERIOD, period_column="period_start"),
    # Needs account and (where present) bill, so it cannot precede billing.bill.
    Table("billing.payment", TRUNCATE),
    # bill_line needs bill AND tariff_component.
    Table("billing.bill_line", DELETE_BY_PERIOD, period_column=None),
    # No FKs at all.
    Table("metering.meter_read_hh", PARTITION_SWAP),
    # The marts have no FKs either, but they are loaded last because they are
    # denormalised copies of everything above and a reader that sees a mart
    # newer than its sources has no way to tell.
    Table("kea.mart_daily_usage", TRUNCATE),
    Table("kea.mart_hourly_profile", TRUNCATE),
    Table("kea.mart_bill_explanation", TRUNCATE),
    Table("kea.mart_forecast_input", TRUNCATE),
    Table("kea.mart_account_context", TRUNCATE),
    # Sprint 4 (V005): the load-detection evidence.
    Table("kea.mart_daily_detected_load", TRUNCATE),
    Table("kea.mart_charging_session", TRUNCATE),
)

#: The marts, for kea.mart_load_log -- which records one row per mart per run.
MARTS = tuple(t for t in TABLES if t.schema == "kea")


def get(name: str) -> Table:
    for t in TABLES:
        if t.name == name:
            return t
    raise KeyError(f"{name!r} is not a serving table this pipeline loads; "
                   f"known: {', '.join(t.name for t in TABLES)}")
