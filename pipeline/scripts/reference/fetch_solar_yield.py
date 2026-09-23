"""Regenerate solar_yield_nz.csv from PVGIS. Run by hand; the CSV is committed.

    python3 fetch_solar_yield.py > solar_yield_nz.csv

One row per weather region x month: the daily output of 1 kW of rooftop
panels, low and high. PVGIS 5.3 (EU JRC), radiation PVGIS-ERA5 2005-2023,
crystalline silicon, 14% system loss, building-mounted, 25 degree pitch,
at each region's NIWA station (dictionary/niwa-weather-source.md).

    high = north-facing mean + one year-to-year standard deviation
    low  = the worse of east- and west-facing mean - one standard deviation

The roof's direction is unknown, and so is next year's weather: the range
covers both, which is why it is wide.
"""

import calendar
import csv
import datetime as dt
import json
import sys
import urllib.request

API = "https://re.jrc.ec.europa.eu/api/v5_3/PVcalc"
# The NIWA station of each weather region, as used for degree days.
STATIONS = {
    "Auckland": (-37.008, 174.789),       # AUCKLAND AERO
    "Waikato": (-37.866, 175.336),        # HAMILTON AERO AWS
    "Wellington": (-41.286, 174.768),     # WELLINGTON, KELBURN AWS
    "Christchurch": (-43.489, 172.532),   # CHRISTCHURCH AERO
}
# PVGIS aspect: 0 is south, 90 west, -90 east, 180 north.
NORTH, WEST, EAST = 180, 90, -90


def monthly(lat: float, lon: float, aspect: int) -> dict[int, tuple[float, float]]:
    """month -> (mean kWh per kW per day, one standard deviation per day)."""
    url = (f"{API}?lat={lat}&lon={lon}&peakpower=1&loss=14&angle=25"
           f"&aspect={aspect}&mountingplace=building&outputformat=json")
    with urllib.request.urlopen(url, timeout=60) as r:
        rows = json.load(r)["outputs"]["monthly"]["fixed"]
    # A typical year: February's standard deviation spread over 28 days.
    return {m["month"]: (m["E_d"], m["SD_m"] / calendar.monthrange(2023, m["month"])[1])
            for m in rows}


def main() -> None:
    out = csv.writer(sys.stdout, lineterminator="\n")
    out.writerow(["weather_region", "month", "kwh_per_kw_day_low",
                  "kwh_per_kw_day_high", "latitude", "longitude", "source",
                  "retrieved_on"])
    today = dt.date.today().isoformat()
    for region, (lat, lon) in STATIONS.items():
        north, west, east = (monthly(lat, lon, a) for a in (NORTH, WEST, EAST))
        for month in range(1, 13):
            worse = min(west[month], east[month])
            low = max(worse[0] - worse[1], 0.0)
            high = north[month][0] + north[month][1]
            out.writerow([region, month, f"{low:.2f}", f"{high:.2f}", lat, lon,
                          "PVGIS 5.3 PVGIS-ERA5 2005-2023, 25deg building-mounted, "
                          "14% loss", today])


if __name__ == "__main__":
    main()
