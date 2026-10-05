"""Turn Low Carbon London half-hours into NZ-local interval reads.

Reads the cohort chosen by cohort.py, streams the LCL block files, and writes
a COPY-ready gzipped CSV for src_metering.interval_reads plus the reference
rows (connection points, meters, registers) those reads hang off.

Three things here are easy to get wrong and silent when wrong:

  * THE NZ DAY IS NOT 48 HALF-HOURS. It is 46, 48 or 50. The grid is built by
    uk_to_nz, per local day, in absolute time. See that module's docstrings.

  * SOLAR IS A SECOND REGISTER, NEVER A NEGATIVE NUMBER. A household with
    panels has its LCL load treated as GROSS demand and split against a
    generation curve: import = max(0, load - gen), export = max(0, gen - load).
    Both are non-negative, which is what ck_kwh_non_negative exists to enforce.

  * `received_at` IS NOT now(). The whole extract design in the platform doc
    keys off it, and stamping every row with the load time collapses 710 days
    of arrivals into one, making the watermark untestable. Rows arrive the
    morning after the reading, with jitter and a late tail.

Stdlib only. Streams; never holds more than one household in memory.

    python3 pipeline/scripts/gen_source/gen_metering.py --stats
    python3 pipeline/scripts/gen_source/gen_metering.py --out raw/lcl/reads.csv.gz

Planted loads (planted.py) are added to a few households' load, and their
sessions are written to planted_loads.csv.gz next to --out. The households are
screened first: one more pass over the candidates' blocks, which planted.py
reads for loads detect-v1 would misread. KEA_GEN_OUT moves the default output
directory; the LCL input is always read from raw/lcl.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import planted  # noqa: E402
import uk_to_nz as tz  # noqa: E402

HERE = Path(__file__).resolve().parent
RAW = Path("raw/lcl/extracted")
#: Where the generated CSVs go. Overridable so a dry run never touches raw/lcl.
OUT = Path(os.environ.get("KEA_GEN_OUT", "raw/lcl"))
COHORT = HERE / "cohort.json"

#: Mean daily kWh the scaled cohort should land on. NZ households use roughly
#: twice what London ones did in the trial, so without this the bills come out
#: at half a plausible NZ amount and every dollar figure downstream is wrong.
#: One GLOBAL multiplier, deliberately: scaling per household would flatten
#: the between-household variation, which is the main thing the real dataset
#: is here to supply.
NZ_TARGET_MEAN_KWH_DAY = 20.0

#: Auckland-ish yield. A household's array is sized by cohort.pv_kwp() and
#: carried on its cohort row, so a 6.6 kWh/day house no longer gets the same
#: 4 kWp roof as a 47 kWh/day one -- which it did until this constant became
#: per-household, and which made it sell 80% of what it generated. The three
#: households planted.SOLAR_PLAN gives a car to are the exception: it
#: declares their arrays, because a roof sized before the EV is not the roof
#: that household would have.
NZ_YIELD_KWH_PER_KWP = 1300.0
#: Summer/winter swing about that mean. NZ peak is early January.
PV_SEASONAL_AMPLITUDE = 0.45
PV_PEAK_DOY = 5


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


def _u(seed_parts, lo: float, hi: float) -> float:
    """Deterministic uniform. No `random`, so a re-run reproduces exactly."""
    return lo + (_h(*seed_parts) % 10**6) / 10**6 * (hi - lo)


# ---------------------------------------------------------------- solar

def pv_daily_kwh(nz_date: dt.date, kwp: float) -> float:
    """The day's generation for an array of `kwp`. Shape is global, size is not."""
    doy = nz_date.timetuple().tm_yday
    season = 1 + PV_SEASONAL_AMPLITUDE * math.cos(2 * math.pi * (doy - PV_PEAK_DOY) / 365.25)
    return kwp * NZ_YIELD_KWH_PER_KWP / 365.25 * season


def pv_half_hour(instant: dt.datetime, daily_kwh: float) -> float:
    """A clipped sine over the daylight window, centred on solar noon.

    Solar noon sits an hour later on the clock while daylight saving is in
    force, so it is read from the instant's own UTC offset rather than
    assumed - the same reason everything else here works in absolute time.
    """
    doy = instant.date().timetuple().tm_yday
    daylight = 12 + 2.8 * math.cos(2 * math.pi * (doy - PV_PEAK_DOY) / 365.25)
    dst = instant.utcoffset() > dt.timedelta(hours=12)
    noon = 12.0 + (1.0 if dst else 0.0)
    hours = instant.hour + instant.minute / 60
    half = daylight / 2
    if not (noon - half < hours < noon + half):
        return 0.0
    shape = math.sin(math.pi * (hours - (noon - half)) / daylight)
    # Normalise so the day's slots sum to daily_kwh: the mean of a half sine
    # is 2/pi, over `daylight * 2` slots.
    return daily_kwh * shape / (2 / math.pi) / (daylight * 2)


# ---------------------------------------------------------------- arrival

def received_at(icp: str, nz_date: dt.date) -> dt.datetime:
    """When the retailer's feed delivered this household-day.

    The morning after, jittered, with a small late tail. Never now(): the
    extract's watermark is tested against the SPREAD of this column, and a
    constant makes COUNT(DISTINCT received_at::date) equal 1.
    """
    delay = 1
    if _h(icp, nz_date, "late") % 100 < 2:
        delay += 1 + _h(icp, nz_date, "howlate") % 4
    base = dt.datetime.combine(nz_date + dt.timedelta(days=delay), dt.time(8, 0), tzinfo=tz.NZ)
    return base + dt.timedelta(hours=_u((icp, nz_date, "jitter"), -3, 3))


# ---------------------------------------------------------------- reading

def household_readings(rows):
    """Group one household's raw rows by London-local date and wall clock.

    Returns (readings, estimated), where `estimated` names the wall clocks
    that were filled rather than measured.

    Two UK daylight-saving edges are handled here, and both are silent if they
    are not:

    * FALL BACK (last Sunday in October). Two UTC readings an hour apart share
      one London wall clock. The first is kept - the second would otherwise
      overwrite it, quietly substituting 02:00 BST's value for 02:00 GMT's.

    * SPRING FORWARD (last Sunday in March). London has no 01:00 or 01:30 at
      all, but the NZ day this maps onto still asks for them, leaving every
      household two readings short on the same two days of the span. They are
      filled from the preceding half-hour and marked 'E' - which is honest,
      since they are estimated, and gives the pipeline real estimated flags to
      handle rather than only injected ones.
    """
    out: dict[dt.date, dict[dt.time, float]] = defaultdict(dict)
    for _, tstp, val in rows:
        if val in ("Null", "", "null"):
            continue
        try:
            stamp = dt.datetime.strptime(tstp[:19], "%Y-%m-%d %H:%M:%S")
            kwh = float(val)
        except ValueError:
            continue
        d, t = tz.uk_wall_clock(stamp)
        if t not in out[d]:              # first occurrence wins
            out[d][t] = kwh

    estimated: dict[dt.date, set] = defaultdict(set)
    for d, slots in out.items():
        for t in tz.uk_absent_wall_clocks(d):
            prev = dt.time(t.hour - 1, t.minute) if t.hour else None
            src = slots.get(prev) if prev else None
            if src is None:
                src = next(iter(slots.values()), None)
            if src is not None:
                slots[t] = src
                estimated[d].add(t)
    return out, estimated


def stream_block(path: Path, wanted: set[str]):
    """Yield (lclid, rows) per household. Blocks are sorted by LCLid."""
    cur, buf = None, []
    with path.open(newline="") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) < 3:
                continue
            if row[0] != cur:
                if cur in wanted and buf:
                    yield cur, buf
                cur, buf = row[0], []
            if row[0] in wanted:
                buf.append(row)
    if cur in wanted and buf:
        yield cur, buf


def nz_days(reads, nz_start: dt.date, nz_end: dt.date, shift: int):
    """Yield (nz_date, uk_date, [(instant, raw kWh or None), ...]) for every NZ
    day in the span that the household's UK day has any reading for."""
    nz = nz_start
    while nz <= nz_end:
        uk = tz.uk_local_date_for(nz, shift)
        day = reads.get(uk)
        if day:
            yield nz, uk, tz.nz_day_from_uk_readings(nz, day, shift)
        nz += dt.timedelta(days=1)


def screen_candidates(households: list[dict], nz_start: dt.date, nz_end: dt.date,
                      shift: int, scale: float) -> dict[str, planted.Screen]:
    """planted.screen() for every household planted.select() may pick, keyed
    by LCL id. The load is scaled exactly as it is written below.

    A solar household is screened on its NET IMPORT, not on its gross load.
    The screen's promise is that the detector will almost never find an
    EV-shaped run the household did not have, and the detector only ever sees
    the import register -- so screening the gross load would promise
    something the register cannot keep, in either direction: generation both
    hides real runs and, at the edges of the day, carves new ones out of an
    ordinary load. It is the same subtraction the writer below makes.

    A candidate FOR SOLAR_PLAN is screened at the LARGEST array that plan
    declares, whatever its own cohort row says. The plan re-sizes the roof of
    whichever three households it picks, so the size and the screen would
    otherwise chase each other: the screen decides who is eligible, and
    eligibility decides the size it should have screened at. Screening
    everyone at the biggest declared array breaks the loop in the safe
    direction -- more generation subtracted is more of the day masked and
    more edges carved, so a household clean at 8 kWp is clean at 3 or 5 --
    and it costs nothing, because the screen barely moves with size: the
    eligible three score 9, 4 and 0 EV-shaped days at every size from 1.5
    to 8 kWp. It is their own load, not the roof, that the screen measures.
    """
    solar_screen_kwp = max(planted.SOLAR_ARRAY_KWP.values())
    kwp: dict[str, float] = {}
    blocks: dict[str, set[str]] = defaultdict(set)
    for h in planted.candidates(households):
        blocks[h["block"]].add(h["lclid"])
    for h in planted.solar_candidates(households):
        blocks[h["block"]].add(h["lclid"])
        kwp[h["lclid"]] = solar_screen_kwp
    for h in planted.demo_candidates(households):
        blocks[h["block"]].add(h["lclid"])
    out = {}
    for block, wanted in sorted(blocks.items()):
        for lcl, rows in stream_block(RAW / f"{block}.csv", wanted):
            reads, _ = household_readings(rows)
            days = []
            for nz, _, grid in nz_days(reads, nz_start, nz_end, shift):
                if lcl in kwp:
                    gen_day = pv_daily_kwh(nz, kwp[lcl])
                    slots = [(i, None if raw is None else
                              max(0.0, raw * scale - pv_half_hour(i, gen_day)))
                             for i, raw in grid]
                else:
                    slots = [(i, None if raw is None else raw * scale)
                             for i, raw in grid]
                days.append((nz, slots))
            out[lcl] = planted.screen(lcl, days, nz_start, nz_end)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true",
                    help="pass 1 only: report the cohort's LCL mean and the scale factor")
    ap.add_argument("--out", default=str(OUT / "reads.csv.gz"))
    ap.add_argument("--limit", type=int, help="first N households, for a smoke run")
    args = ap.parse_args()

    spec = json.loads(COHORT.read_text())
    cohort = spec["households"][:args.limit] if args.limit else spec["households"]
    shift = spec["shift_days"]
    nz_start = dt.date.fromisoformat(spec["nz_span"][0])
    nz_end = dt.date.fromisoformat(spec["nz_span"][1])
    by_lcl = {c["lclid"]: c for c in cohort}

    # Fail here rather than 40 minutes in. A cohort.json written before arrays
    # were sized has no pv_kwp, and the first solar household would then raise
    # a TypeError from inside the read loop with the output half written.
    missing = [c for c in cohort if c["has_solar"] and not c.get("pv_kwp")]
    if missing:
        sys.exit(f"{len(missing)} solar households have no pv_kwp: "
                 f"re-run cohort.py, which sizes each array to its load")

    blocks: dict[str, set[str]] = defaultdict(set)
    for c in cohort:
        blocks[c["block"]].add(c["lclid"])

    scale = None
    if not args.stats:
        stats_path = HERE / "scale.json"
        if not stats_path.exists():
            sys.exit("run --stats first: the scale factor must be measured before generating")
        scale = json.loads(stats_path.read_text())["scale"]
        print(f"scale factor k = {scale:.4f}")

    out = None
    extra: dict[str, dict[int, float]] = {}
    #: ICP -> the array SOLAR_PLAN declared for it, empty under --stats.
    planted_kwp: dict[str, float] = {}
    if not args.stats:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        out = gzip.open(args.out, "wt", newline="", compresslevel=1)
        w = csv.writer(out)
        # Chosen from the whole cohort, so --limit plants the same households.
        # Never in --stats: scale.json is measured on the LCL load alone.
        screens = screen_candidates(spec["households"], nz_start, nz_end, shift, scale)
        print(f"screened {len(screens)} candidates: "
              f"{sum(sc.fits('ev_wallbox_7kw') for sc in screens.values())} clean for an EV, "
              f"{sum(sc.fits('hot_water_3kw') for sc in screens.values())} for a hot-water element")
        solar_pool = {h["lclid"] for h in planted.solar_candidates(spec["households"])}
        print(f"  of those, {len(solar_pool)} have panels and were screened on "
              f"net import: "
              f"{sum(sc.fits('ev_solar_wallbox_7kw') for lcl, sc in screens.items() if lcl in solar_pool)}"
              f" clean for a solar+EV wallbox")
        plants = planted.select(spec["households"], nz_start, nz_end, screens)
        planted_path = Path(args.out).parent / "planted_loads.csv.gz"
        n_sessions = 0
        with gzip.open(planted_path, "wt", newline="") as pf:
            pw = csv.writer(pf)
            for plant in plants:
                if plant.lclid not in by_lcl:
                    continue
                found = planted.sessions(plant, nz_start, nz_end)
                extra[plant.icp] = planted.slot_kwh(found)
                if plant.pv_kwp:
                    planted_kwp[plant.icp] = plant.pv_kwp
                pw.writerows(planted.csv_row(s) for s in found)
                n_sessions += len(found)
        print(f"planted: {len(extra)} households, {n_sessions:,} sessions -> {planted_path}")
        for plant in plants:
            if plant.pv_kwp:
                was = by_lcl[plant.lclid]["pv_kwp"] if plant.lclid in by_lcl else None
                print(f"  {plant.pattern}: solar+EV host re-roofed "
                      f"{was} -> {plant.pv_kwp} kWp (sized for the car)")

    total_kwh = total_days = 0.0
    n_rows = n_hh = 0

    for block, wanted in sorted(blocks.items()):
        path = RAW / f"{block}.csv"
        for lcl, rows in stream_block(path, wanted):
            hh = by_lcl[lcl]
            reads, estimated = household_readings(rows)
            n_hh += 1

            if args.stats:
                for d, slots in reads.items():
                    if tz.LCL_START <= d <= tz.LCL_END and len(slots) >= 46:
                        total_kwh += sum(slots.values())
                        total_days += 1
                continue

            icp, solar = hh["icp"], hh["has_solar"]
            # SOLAR_PLAN's hosts sized their roof for the car, so the plan's
            # array wins over the pre-EV one cohort.py derived from load.
            kwp = planted_kwp.get(hh["icp"]) or hh.get("pv_kwp")
            added = extra.get(icp, {})
            for nz, uk, grid in nz_days(reads, nz_start, nz_end, shift):
                recv = received_at(icp, nz).isoformat()
                src = f"lcl-{nz:%Y%m%d}.csv"
                gen_day = pv_daily_kwh(nz, kwp) if solar else 0.0
                est = estimated.get(uk, ())
                for instant, raw in grid:
                    if raw is None:
                        continue          # a genuine gap; do not invent a reading
                    status = "E" if instant.time() in est else "A"
                    # Planted kW joins the load BEFORE the solar split, as a
                    # real appliance would: the three SOLAR_PLAN households
                    # charge under their own panels, and what reaches the
                    # import register is whatever the array did not cover.
                    load = raw * scale + added.get(int(instant.timestamp()), 0.0)
                    if solar:
                        g = pv_half_hour(instant, gen_day)
                        imp, exp = max(0.0, load - g), max(0.0, g - load)
                    else:
                        imp, exp = load, 0.0
                    ts = instant.isoformat()
                    w.writerow([icp, "UN", ts, f"{imp:.4f}", status, 1, recv, src])
                    n_rows += 1
                    if solar:
                        w.writerow([icp, "EX", ts, f"{exp:.4f}", status, 1, recv, src])
                        n_rows += 1
            if n_hh % 50 == 0:
                print(f"    {n_hh}/{len(cohort)} households, {n_rows/1e6:.1f} M rows",
                      file=sys.stderr)

    if args.stats:
        mean = total_kwh / total_days
        k = NZ_TARGET_MEAN_KWH_DAY / mean
        (HERE / "scale.json").write_text(json.dumps({
            "lcl_mean_kwh_day": round(mean, 4),
            "nz_target_kwh_day": NZ_TARGET_MEAN_KWH_DAY,
            "scale": round(k, 6),
            "household_days": int(total_days),
            "households": n_hh,
        }, indent=2) + "\n")
        print(f"households        {n_hh}")
        print(f"household-days    {int(total_days):,}")
        print(f"LCL mean          {mean:.3f} kWh/day")
        print(f"NZ target         {NZ_TARGET_MEAN_KWH_DAY:.1f} kWh/day")
        print(f"scale factor k    {k:.4f}   -> scale.json")
        return

    out.close()
    size = Path(args.out).stat().st_size
    print(f"\n{n_rows:,} reads for {n_hh} households -> {args.out} ({size/1e6:.0f} MB gz)")


if __name__ == "__main__":
    main()
