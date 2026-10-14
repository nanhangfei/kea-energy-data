"""Pricing half-hourly usage against a real tariff.

`engine.TariffOption` is a flat rate and a daily charge. That was enough while
the only comparison was Low Fixed Charge against standard, and it cannot price
the three Sprint 3 styles: no peak/off-peak/night bands, no free weekend
window, no export, and no term credit.

This module replaces it. Pure functions over data — the tariff arrives as an
argument, never read from a database here, so the tests stay unit tests and the
engine stays testable without a warehouse.

⚠️ ONE BANDING PASS PER CHARGE GROUP, NOT ONE OVERALL.

The engine remains charge-group aware because the nine Sprint 2 fixture
tariffs carry separate retailer and network bands. The three Kea Sprint 3
plans use bundled customer-facing rates, represented as such in the catalogue.

⚠️ A GAP IN THE TILING IS NEVER PRICED AND NEVER ERRORS.

Half-hours that no band covers are silently free. The bill still adds up, the
comparison still returns a number, and the number is too low. `Tariff` therefore
validates its own tiling at construction — per charge group, across all seven
days — rather than trusting whoever assembled it.

CARRIED HERE FROM THE APP. This is a copy of the app's
`services/api/app/analytics/tariff.py` (13 Oct 2026), so that this repository
publishes on its own. The app prices plans with its copy and the pipeline with
this one, and the two must agree to the cent: a change to either belongs in both.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

__all__ = [
    "Band",
    "FlatCharge",
    "Reading",
    "Tariff",
    "CostBreakdown",
    "cost_of",
]

#: Charge groups, matching common.charge_group in the serving schema.
ENERGY, NETWORK, OTHER, CREDIT = "energy", "network", "other", "credit"

#: Groups whose bands must tile the week. `credit` is a fixed monthly amount
#: rather than a per-kWh rate, so it has no bands and no tiling to check.
BANDED_GROUPS = (ENERGY, NETWORK, OTHER)

_WEEK = frozenset(range(1, 8))
_DAY_MINUTES = 24 * 60


def _minutes(t: dt.time) -> int:
    return t.hour * 60 + t.minute


@dataclass(frozen=True)
class Band:
    """A per-kWh rate over a set of weekdays and a span of the clock.

    `end_minute` is minutes from midnight and 1440 means midnight at the end of
    the day. `datetime.time` cannot express that -- 24:00 is not a valid time --
    and the catalogue's windows really do run to 24:00, so the arithmetic is
    done in minutes and the boundary is representable.
    """

    charge_group: str
    label: str
    days: frozenset[int]
    start_minute: int
    end_minute: int
    rate: Decimal

    def covers(self, weekday: int, minute: int) -> bool:
        return (weekday in self.days
                and self.start_minute <= minute < self.end_minute)


@dataclass(frozen=True)
class FlatCharge:
    """A charge with no band: per kWh whatever the hour, or per day.

    Transmission and the Electricity Authority levy are per kWh and un-banded;
    metering, admin and the network's daily charge are per day.

    ⚠️ AN UN-BANDED PER-KWH CHARGE IS STILL CHARGED INSIDE A FREE WINDOW,
    because it is not part of any band that could be zeroed. That is a real
    modelling question rather than an oversight -- 2.68c/kWh on this catalogue
    -- and it is recorded beside _netk in 02_catalogue.sql. Whatever is decided
    there, this module reproduces the catalogue rather than correcting it.
    """

    charge_group: str
    description: str
    rate: Decimal
    per: str  # 'kwh' | 'day'


@dataclass(frozen=True)
class Reading:
    """One half-hour of consumption at a local wall clock.

    LOCAL, not UTC. Every band in a NZ tariff is expressed in local time, and a
    plan that gives away 09:00-17:00 gives away the customer's nine to five on
    both sides of a daylight-saving change. Converting here would rotate every
    window by an hour for half the year.
    """

    at: dt.datetime          # local
    kwh: Decimal
    exported: bool = False


@dataclass(frozen=True)
class CostBreakdown:
    """What a period cost, and where.

    A total on its own cannot be checked against a bill. When the
    reproduction test disagrees it has to say WHICH charge group drifted,
    because that is the difference between a rate that is wrong and a band that
    is misaligned.
    """

    by_group: dict[str, Decimal]
    by_band: dict[tuple[str, str], Decimal]
    kwh_by_band: dict[tuple[str, str], Decimal]
    unpriced_kwh: Decimal

    @property
    def total(self) -> Decimal:
        return sum(self.by_group.values(), Decimal("0"))


class TariffError(ValueError):
    """A tariff that cannot price a week without a gap or an overlap."""


@dataclass(frozen=True)
class Tariff:
    """A plan's whole rate card, validated on construction."""

    name: str
    bands: tuple[Band, ...] = ()
    flat: tuple[FlatCharge, ...] = ()
    export_bands: tuple[Band, ...] = ()
    monthly_credit: Decimal = field(default=Decimal("0"))
    loss_factor: Decimal = field(default=Decimal("0"))
    """Line losses, as a fraction of imported energy kWh.

    ⚠️ ENERGY IS BILLED ON MORE KWH THAN THE METER RECORDED, and leaving this
    at zero understates every plan by roughly 5 % of its energy charge.

    Electricity lost as heat between the grid exit point and the house is
    charged to the customer: `billing.bill_line` carries it as `loss_kwh` beside
    `quantity`, with `billed_quantity` generated as their sum, and the source
    biller applies it to import energy per-kWh lines alone -- not to network,
    not to the levy, not to a daily charge, not to export (gen_source/
    04_rate_bills.sql, "Line losses, import energy only").

    It matters to a COMPARISON and not only to a total, because plans differ in
    what they charge for energy: a 5 % uplift on 25c/kWh is not the same money
    as 5 % on 14c, so omitting it shrinks every gap between plans by a different
    amount. The factor is a property of the catalogue rather than of any one
    plan, so callers read it once -- from the billed data, not a literal -- and
    hand the same value to every `Tariff` they build.
    """

    def __post_init__(self) -> None:
        for group in BANDED_GROUPS:
            bands = [b for b in self.bands if b.charge_group == group]
            if bands:
                _assert_tiles(self.name, group, bands)

    def band_for(self, group: str, weekday: int, minute: int) -> Band | None:
        for b in self.bands:
            if b.charge_group == group and b.covers(weekday, minute):
                return b
        return None


def _assert_tiles(name: str, group: str, bands: list[Band]) -> None:
    """Every minute of every weekday is covered exactly once.

    Checked at half-hour resolution because that is the meter's resolution: a
    band boundary inside a half hour cannot change what a reading is priced at,
    so finer checking would reject tariffs that price identically.
    """
    for weekday in sorted(_WEEK):
        for minute in range(0, _DAY_MINUTES, 30):
            hits = [b for b in bands if b.covers(weekday, minute)]
            if not hits:
                raise TariffError(
                    f"{name}: {group} prices nothing at weekday {weekday} "
                    f"{minute // 60:02d}:{minute % 60:02d} -- an uncovered "
                    f"half hour is billed at zero and never errors")
            if len(hits) > 1:
                labels = ", ".join(b.label for b in hits)
                raise TariffError(
                    f"{name}: {group} has overlapping bands ({labels}) at "
                    f"weekday {weekday} {minute // 60:02d}:{minute % 60:02d}")


def _uplift(tariff: Tariff, group: str) -> Decimal:
    """1 + the loss factor, and only on import energy. See `Tariff.loss_factor`."""
    if group == ENERGY and tariff.loss_factor:
        return Decimal("1") + tariff.loss_factor
    return Decimal("1")


def cost_of(readings: list[Reading], tariff: Tariff, days: int) -> CostBreakdown:
    """Price a period. Import and export both, banded per charge group.

    `days` is passed rather than derived from the readings: a period with a
    missing day still bills its daily charge, and counting distinct dates in
    the data would quietly discount the customer for the gap.
    """
    by_group: dict[str, Decimal] = {}
    by_band: dict[tuple[str, str], Decimal] = {}
    kwh_by_band: dict[tuple[str, str], Decimal] = {}
    unpriced = Decimal("0")

    def add(group: str, label: str, amount: Decimal, kwh: Decimal) -> None:
        by_group[group] = by_group.get(group, Decimal("0")) + amount
        key = (group, label)
        by_band[key] = by_band.get(key, Decimal("0")) + amount
        kwh_by_band[key] = kwh_by_band.get(key, Decimal("0")) + kwh

    for r in readings:
        weekday = r.at.isoweekday()
        minute = _minutes(r.at.time())

        if r.exported:
            band = next((b for b in tariff.export_bands
                         if b.covers(weekday, minute)), None)
            if band is None:
                # Export outside any buy-back window earns nothing. Not an
                # error: a plan may deliberately offer no export credit.
                continue
            add(ENERGY, f"export:{band.label}", -(r.kwh * band.rate), r.kwh)
            continue

        priced_any = False
        # ONE PASS PER GROUP. This loop is the whole point of the module.
        for group in BANDED_GROUPS:
            band = tariff.band_for(group, weekday, minute)
            if band is None:
                continue          # this group may simply not band (see below)
            priced_any = True
            # Losses uplift the QUANTITY CHARGED, not the quantity metered, so
            # `kwh_by_band` keeps reporting what the meter saw -- that is the
            # number a customer recognises and the one a usage share is taken
            # over. Only the amount moves.
            add(group, band.label, r.kwh * band.rate * _uplift(tariff, group), r.kwh)

        for f in tariff.flat:
            if f.per == "kwh":
                priced_any = True
                add(f.charge_group, f.description,
                    r.kwh * f.rate * _uplift(tariff, f.charge_group), r.kwh)

        if not priced_any:
            unpriced += r.kwh

    for f in tariff.flat:
        if f.per == "day":
            add(f.charge_group, f.description, f.rate * days, Decimal("0"))

    if tariff.monthly_credit:
        # NEGATIVE, and in its own group so a re-rating pass cannot re-band it.
        # The credit is GST-inclusive where it is advertised; this is the line
        # amount, which is not. See docs/sprints/sprint-3/plans-and-terms.md.
        add(CREDIT, "term credit", -tariff.monthly_credit, Decimal("0"))

    return CostBreakdown(by_group, by_band, kwh_by_band, unpriced)
