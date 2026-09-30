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

ASSUMPTIONS_VERSION = "sprint4-assumptions-v2"

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

#: [ASSUMED] Usable home-battery sizes; one row each per system size. 0 is
#: panels alone, and 13.5 kWh is a common single residential unit.
BATTERY_SIZES_KWH = (Decimal("0"), Decimal("13.5"))

#: [ASSUMED] The most the battery takes in or gives out, so 2.5 kWh in any
#: half-hour. A 7 kW array at noon still exports past a full-rate charge.
BATTERY_POWER_KW = Decimal("5")

#: [ASSUMED] Of each kWh of surplus sent to the battery, the share that comes
#: back out. The loss is taken on the way in, so the state of charge is
#: always energy the home can actually draw.
BATTERY_ROUND_TRIP = Decimal("0.90")

#: [ASSUMED] Installed cost of the 13.5 kWh battery alongside the panels,
#: GST-inclusive, low and high. Not a quote, and the range is wide for that
#: reason. Added to the panels' cost on a battery row.
BATTERY_COST_NZD = (Decimal("12000"), Decimal("18000"))

#: [ASSUMED] The battery stores the home's own surplus and nothing else: it
#: is never charged from the grid, so there is no night-rate arbitrage in any
#: figure. The answer must say so.
BATTERY_DISPATCH = "self-consumption"

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
