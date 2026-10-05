"""The modelling assumptions behind kea.solar_estimate and kea.ev_scenario.

Sprint 4 item B. Every row those jobs write names ASSUMPTIONS_VERSION, so a
figure a customer was shown can be traced back to the numbers below. Change a
value, change the version.

Each value is tagged as in gen_source/02_catalogue.sql:

  [PUBLISHED]  read off a source that can be cited, named beside it
  [ASSUMED]    a modelling choice; the reason is beside it

The customer-facing wording must say "estimate" for anything that rests on
an [ASSUMED] value, which is all of the dollar figures.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

ASSUMPTIONS_VERSION = "sprint4-assumptions-v3"

#: [ASSUMED] Every figure is annual, and a year with holes would understate
#: generation and charging alike, so an account whose readings cover less
#: than this share of the window's half-hours gets no estimate.
MIN_WINDOW_COVERAGE = 0.9

# ------------------------------------------------------------------ solar

#: [PUBLISHED] Output of 1 kW of rooftop panels per day, by weather region and
#: month, low and high. PVGIS 5.3 (EU JRC, PVGIS-ERA5 2005-2023) at each NIWA
#: station; reference/fetch_solar_yield.py says how low and high are taken
#: (roof direction and year-to-year weather) and regenerates the file.
SOLAR_YIELD_CSV = Path(__file__).resolve().parents[1] / "reference" / "solar_yield_nz.csv"

#: [ASSUMED] The climate region -> weather region seed dim_icp uses, so the
#: solar job picks the same NIWA region the degree days come from.
REGION_SEED_CSV = (Path(__file__).resolve().parents[2] / "dbt_project" / "seeds"
                   / "climate_region_weather_region.csv")

#: [ASSUMED] Common residential sizes; one row each.
SYSTEM_SIZES_KW = (Decimal("3"), Decimal("5"), Decimal("7"))

#: [ASSUMED] Installed cost per kW, GST-inclusive, low and high: a whole
#: system price divided by its size, so inverter, scaffolding and labour are
#: inside it. Not a quote, and the range is wide for that reason.
INSTALL_COST_NZD_PER_KW = (Decimal("2000"), Decimal("3000"))

#: [ASSUMED] A day's output follows a half-sine between sunrise and sunset.
#: Sunrise and sunset come from the NOAA solar equations at the station's
#: latitude and longitude. Cloud is in the yield table, not in the shape.
SOLAR_SHAPE = "half-sine"

# ------------------------------------------------------------------ battery
#
# The battery modelled is one common 13.5 kWh AC-coupled home unit. Its size,
# power and round trip are read off one product's datasheet, so the three
# describe a battery that exists rather than three unrelated round numbers:
#
#   Tesla Powerwall 2 AC datasheet
#   https://www.tesla.com/sites/default/files/pdfs/powerwall/Powerwall%202_AC_Datasheet_en_AU.pdf
#
# Checked 2026-10-01 against the North American edition of the same
# datasheet, which gives the same three figures. tesla.com refuses requests
# that are not from a browser; this copy of that edition opens for anyone:
#
#   https://www.solarcrowdsource.com/wp-content/uploads/2021/06/Powerwall-2_AC_Datasheet_EN_NA.pdf

#: [PUBLISHED] Usable home-battery sizes; one row each per system size. 0 is
#: panels alone. 13.5 kWh is the datasheet's "Usable Energy" (of 14 kWh
#: total).
BATTERY_SIZES_KWH = (Decimal("0"), Decimal("13.5"))

#: [PUBLISHED] The most the battery takes in or gives out: the datasheet's
#: "Real Power, max continuous", 5 kW charge and discharge. So 2.5 kWh in any
#: half-hour; a 7 kW array at noon still exports past a full-rate charge.
BATTERY_POWER_KW = Decimal("5")

#: [PUBLISHED] Of each kWh of surplus sent to the battery, the share that
#: comes back out: the datasheet's "Round Trip Efficiency", 90%, AC to battery
#: to AC, at beginning of life and 25 degrees C. The model applies no
#: degradation, so it uses the new-battery figure throughout. The loss is
#: taken on the way in, so the state of charge is always energy the home can
#: actually draw.
BATTERY_ROUND_TRIP = Decimal("0.90")

#: [ASSUMED] Installed cost of the 13.5 kWh battery alongside the panels,
#: GST-inclusive, low and high. Added to the panels' cost on a battery row.
#: Not a quote: a range chosen to bracket published NZ prices.
#:
#:   My Solar Quotes, "How much does a solar power system cost?", updated
#:   2026-09-20, from 63 verified NZ battery installations in the prior 12
#:   months, GST and installation included:
#:   https://www.mysolarquotes.co.nz/about-solar-power/residential/how-much-does-a-solar-power-system-cost/
#:   10 kWh $10,000-$13,000; 15 kWh $14,000-$18,000; 13.5 kWh roughly $16,500;
#:   $1,219 per kWh on average.
#:
#: Read between its 10 and 15 kWh bands, 13.5 kWh costs about $12,800-$16,500;
#: this range runs a little below that and up to the 15 kWh band's top. It is
#: not narrower at the top because a single named unit can cost more: another
#: NZ guide puts a Powerwall 3 at $19,000-$24,000 installed (solarscout.co.nz,
#: updated 2026-08-30). Both checked 2026-10-01.
BATTERY_COST_NZD = (Decimal("12000"), Decimal("18000"))

#: [ASSUMED] What a battery row assumes it does beyond storing the home's own
#: surplus. self_consumption: nothing. grid_charge: it also tops up from the
#: grid in the plan's cheapest half-hours of the week and gives the energy
#: back where the rate, after the round-trip loss, still beats them. A rule
#: read off the plan's own rate card. The grid fills it only to leave room
#: for as much surplus as the panels made the day before -- a persistence
#: forecast, from the past only -- so a sunny day after a sunny day still
#: finds space. grid_charge is written only for a plan with more than one
#: import rate. Neither sells stored energy back to the grid.
BATTERY_DISPATCHES = ("self_consumption", "grid_charge")

# ------------------------------------------------------------------ EV

#: [ASSUMED] Kilometres a year, close to the average for a NZ light
#: passenger vehicle. The answer should say "about 11,000 km a year".
ANNUAL_KM = Decimal("11000")

#: [ASSUMED] Energy from the wall per km: about 0.17 kWh/km at the battery
#: for a mid-size EV, plus about 10% charging loss.
KWH_PER_KM_AT_PLUG = Decimal("0.19")

#: [ASSUMED] A 32 A single-phase wallbox, the charger the planted test
#: households use (gen_source/planted.py).
CHARGER_KW = Decimal("7.2")

#: [ASSUMED] Charging on arriving home: the add_ev baseline.
EVENING_START = dt.time(18, 0)

#: [PUBLISHED] Kea Time of Use prices 23:00-07:00 as Night
#: (gen_source/02_catalogue.sql, "Night Usage (11PM-7AM)"). The night pattern
#: starts at 23:00, and a detected session that already starts inside the
#: window is left where it is.
NIGHT_START = dt.time(23, 0)
NIGHT_END = dt.time(7, 0)

#: [PUBLISHED] The catalogue code of Kea's Time of Use plan, the plan a
#: shifted session is also priced on.
TIME_OF_USE_PREFIX = "KEA_TOU_"


def annual_ev_kwh() -> Decimal:
    return ANNUAL_KM * KWH_PER_KM_AT_PLUG
