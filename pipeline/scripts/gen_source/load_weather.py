"""Load NIWA daily temperature CSVs into src_weather.

    python3 pipeline/scripts/gen_source/load_weather.py            # load
    python3 pipeline/scripts/gen_source/load_weather.py --report   # measure, load nothing

Reads the zips in data/raw/niwa/ as downloaded from NIWA DataHub. Nothing is
fetched here: DataHub requires a licence to be accepted per dataset, which is a
person's decision and not a script's, so the download is manual and this reads
what it left behind. `data/raw/` is gitignored -- the licence covers use, not
redistribution.

THE ONE THING THAT IS EASY TO GET SILENTLY WRONG.

NIWA stamps each row with the instant of the 9am observation, in UTC:

    2026-08-16T21:00:00Z   ->   09:00 NZST on 2026-08-17
    2025-01-14T20:00:00Z   ->   09:00 NZDT on 2025-01-15   (+13 in summer)

So the NZ date is the Pacific/Auckland date OF THAT INSTANT, and the two clock
times in the files -- 21:00Z and 20:00Z -- are daylight saving, not two
readings a day. Verified against DataHub's own labelling at both ends of the
Auckland series: it calls the first row 1 Jan 1966 and the last 17 Aug 2026,
which is what this rule produces and one day later than reading the UTC date.

Getting it wrong shifts every temperature back a day. No constraint would
catch it, and every cold snap in every bill explanation would move.

THE MEAN IS NIWA'S, NOT COMPUTED. The files carry a Mean Temperature column;
across 69,422 rows it equals (max+min)/2 within 0.05 C on all but one. That is
the meteorological convention for degree days -- not an approximation worth
improving with hourly data -- and it is what the fixtures' 0.93 kWh/HDD was
fitted against. Where the column is blank the arithmetic is the fallback, and
where max or min is missing too the day is simply absent.

Stdlib only, matching the rest of gen_source.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import subprocess
import sys
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

NZ = ZoneInfo("Pacific/Auckland")
RAW = Path(__file__).resolve().parents[3] / "raw" / "niwa"

#: agent_no -> (climate_region, station name as NIWA writes it).
#:
#: THE `AWS` SUFFIX IS NOT A RELIABLE GUIDE and every one of these was checked
#: individually. It marks an automatic station, not a live one: AUCKLAND AERO
#: AWS (10330) and CHRISTCHURCH AERO AWS (10332) hold rain only, ending
#: Jul 2013, while WELLINGTON, KELBURN (3385) died in 2005 and its AWS is the
#: live one. Choosing by name alone would have produced a decade-wide hole and
#: no error anywhere.
STATIONS = {
    1962:  ("Auckland",     "AUCKLAND AERO",           -37.00813, 174.78873,   7),
    2112:  ("Waikato",      "HAMILTON AERO AWS",       -37.86088, 175.33175,  40),
    25354: ("Wellington",   "WELLINGTON, KELBURN AWS", -41.28445, 174.76794, 125),
    4843:  ("Christchurch", "CHRISTCHURCH AERO",       -43.493,   172.537,    37),
}

#: The cohort's NZ-local reading span (uk_to_nz.py). Loaded rows are not
#: limited to it -- src_weather holds what NIWA published, as received -- but
#: coverage is reported against it, because this is the window that has to be
#: explainable.
SPAN_LO, SPAN_HI = dt.date(2025, 8, 1), dt.date(2027, 8, 27)

PSQL = ["docker", "compose", "exec", "-T", "kea-serving-db",
        "psql", "-v", "ON_ERROR_STOP=1", "-U", "kea", "-d", "kea"]


def rows_for(agent: int) -> list[tuple]:
    """Every observation NIWA published for one station, NZ-dated."""
    zips = sorted(RAW.glob(f"{agent}__*.zip"))
    if not zips:
        raise SystemExit(
            f"no zip for agent {agent} in {RAW}.\n"
            "Download the four Temperature datasets from data.niwa.co.nz under the "
            "Non-commercial licence and drop the zips there."
        )
    out = []
    with zipfile.ZipFile(zips[0]) as z:
        name = next(n for n in z.namelist() if n.endswith(".csv"))
        text = io.TextIOWrapper(z.open(name), encoding="utf-8-sig")
        for r in csv.DictReader(text):
            stamp = r["Observation time UTC"]
            if not stamp:
                continue
            observed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            mean = _num(r["Mean Temperature [Deg C]"])
            mx, mn = _num(r["Maximum Temperature [Deg C]"]), _num(r["Minimum Temperature [Deg C]"])
            if mean is None and mx is not None and mn is not None:
                mean = round((mx + mn) / 2, 2)
            out.append((
                agent,
                observed.astimezone(NZ).date(),      # <- the whole trap, in one line
                observed,
                mx, mn, mean,
                _num(r["Grass Temperature [Deg C]"]),
                # NIWA writes humidity as '82.0'. The column is smallint
                # because a relative humidity does not need a decimal, so the
                # rounding happens here rather than widening the type to carry
                # a precision the measurement does not have.
                _pct(r["Mean Relative Humidity [percent]"]),
                zips[0].name,
            ))
    return out


def _num(v):
    v = (v or "").strip()
    return float(v) if v else None


def _pct(v):
    n = _num(v)
    return None if n is None else round(n)


def report(by_agent: dict[int, list[tuple]]) -> None:
    """Coverage against the cohort span, and which days nothing covers.

    Printed rather than assumed, because the fill rule below depends on it: if
    the four stations ever miss the same inner day, nearest-station
    substitution stops being sufficient and the rule has to change.
    """
    days = [SPAN_LO + dt.timedelta(d) for d in range((SPAN_HI - SPAN_LO).days + 1)]
    print(f"\ncohort span {SPAN_LO} .. {SPAN_HI}  ({len(days)} days)\n")
    print(f"  {'region':14} {'usable':>7} {'of span':>8}  {'inner gaps':>10}  {'tail':>5}")
    usable = {}
    for agent, rows in by_agent.items():
        region = STATIONS[agent][0]
        # A day counts as usable only if it has a MEAN. Fifteen rows across
        # the four files exist with no mean and no recoverable max/min, and
        # counting them as present would overstate coverage by exactly the
        # days the fill rule has to handle.
        got = {r[1]: r[5] for r in rows
               if r[5] is not None and SPAN_LO <= r[1] <= SPAN_HI}
        usable[region] = got
        missing = [d for d in days if d not in got]
        inner = [d for d in missing if d < dt.date(2026, 8, 18)]
        print(f"  {region:14} {len(got):>7} {len(got)/len(days)*100:>7.1f}%  {len(inner):>10}  "
              f"{len(missing)-len(inner):>5}")
        if inner:
            print(f"  {'':14} {', '.join(d.isoformat() for d in inner)}")

    nowhere = [d for d in days if not any(d in g for g in usable.values())]
    print(f"\n  days no station covers: {len(nowhere)}"
          + (f"   {nowhere[0]} .. {nowhere[-1]}" if nowhere else ""))
    if nowhere and any(d < dt.date(2026, 8, 18) for d in nowhere):
        print("  ⚠ an INNER day is uncovered -- nearest-station fill is no longer sufficient")
    else:
        print("  -> every inner gap is coverable from another station; only the tail is not")


def load(by_agent: dict[int, list[tuple]]) -> None:
    sql = [
        "BEGIN;",
        "TRUNCATE src_weather.daily_observations;",
        "TRUNCATE src_weather.stations RESTART IDENTITY CASCADE;",
        "INSERT INTO src_weather.stations "
        "(agent_no, station_name, climate_region, latitude, longitude, elevation_m) VALUES",
        ",\n".join(
            f"({a},{_q(n)},{_q(reg)},{lat},{lon},{elev})"
            for a, (reg, n, lat, lon, elev) in STATIONS.items()) + ";",
        "COPY src_weather.daily_observations "
        "(agent_no, obs_date, observed_at_utc, temp_max_c, temp_min_c, temp_mean_c, "
        "grass_min_c, rel_humidity_pct, source_file) FROM STDIN WITH (FORMAT csv);",
    ]
    body = io.StringIO()
    # lineterminator is not the default: csv.writer emits \r\n, and COPY
    # rejects the bare carriage return with "unquoted newline found in data".
    w = csv.writer(body, lineterminator="\n")
    for rows in by_agent.values():
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    payload = "\n".join(sql) + "\n" + body.getvalue() + "\\.\nCOMMIT;\n"

    proc = subprocess.run(PSQL + ["-q", "-f", "-"], input=payload, text=True,
                          capture_output=True, cwd=RAW.parents[1])
    if proc.returncode:
        sys.exit(f"load failed:\n{proc.stderr[:2000]}")
    total = sum(len(v) for v in by_agent.values())
    print(f"\nloaded {total:,} observations across {len(by_agent)} stations")


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true", help="measure coverage, load nothing")
    args = ap.parse_args(argv)

    by_agent = {agent: rows_for(agent) for agent in STATIONS}
    for agent, rows in by_agent.items():
        span = f"{min(r[1] for r in rows)} .. {max(r[1] for r in rows)}"
        print(f"  {STATIONS[agent][0]:14} agent {agent:>5}  {len(rows):>7,} rows  {span}")
    report(by_agent)
    if not args.report:
        load(by_agent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
