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
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import uk_to_nz as tz  # noqa: E402

HERE = Path(__file__).resolve().parent
RAW = Path("raw/lcl/extracted")
COHORT = HERE / "cohort.json"

#: Mean daily kWh the scaled cohort should land on. NZ households use roughly
#: twice what London ones did in the trial, so without this the bills come out
#: at half a plausible NZ amount and every dollar figure downstream is wrong.
#: One GLOBAL multiplier, deliberately: scaling per household would flatten
#: the between-household variation, which is the main thing the real dataset
#: is here to supply.
NZ_TARGET_MEAN_KWH_DAY = 20.0

#: 4 kWp rooftop at an Auckland-ish yield of ~1,300 kWh/kWp/yr.
PV_ANNUAL_KWH = 5200.0
#: Summer/winter swing about that mean. NZ peak is early January.
PV_SEASONAL_AMPLITUDE = 0.45
PV_PEAK_DOY = 5


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


def _u(seed_parts, lo: float, hi: float) -> float:
    """Deterministic uniform. No `random`, so a re-run reproduces exactly."""
    return lo + (_h(*seed_parts) % 10**6) / 10**6 * (hi - lo)


# ---------------------------------------------------------------- solar

def pv_daily_kwh(nz_date: dt.date) -> float:
    doy = nz_date.timetuple().tm_yday
    season = 1 + PV_SEASONAL_AMPLITUDE * math.cos(2 * math.pi * (doy - PV_PEAK_DOY) / 365.25)
    return PV_ANNUAL_KWH / 365.25 * season


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true",
                    help="pass 1 only: report the cohort's LCL mean and the scale factor")
    ap.add_argument("--out", default="raw/lcl/reads.csv.gz")
    ap.add_argument("--limit", type=int, help="first N households, for a smoke run")
    args = ap.parse_args()

    spec = json.loads(COHORT.read_text())
    cohort = spec["households"][:args.limit] if args.limit else spec["households"]
    shift = spec["shift_days"]
    nz_start = dt.date.fromisoformat(spec["nz_span"][0])
    nz_end = dt.date.fromisoformat(spec["nz_span"][1])
    by_lcl = {c["lclid"]: c for c in cohort}

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
    if not args.stats:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        out = gzip.open(args.out, "wt", newline="", compresslevel=1)
        w = csv.writer(out)

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
            nz = nz_start
            while nz <= nz_end:
                uk = tz.uk_local_date_for(nz, shift)
                day = reads.get(uk)
                if not day:
                    nz += dt.timedelta(days=1)
                    continue
                recv = received_at(icp, nz).isoformat()
                src = f"lcl-{nz:%Y%m%d}.csv"
                gen_day = pv_daily_kwh(nz) if solar else 0.0
                est = estimated.get(uk, ())
                for instant, raw in tz.nz_day_from_uk_readings(nz, day, shift):
                    if raw is None:
                        continue          # a genuine gap; do not invent a reading
                    status = "E" if instant.time() in est else "A"
                    load = raw * scale
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
                nz += dt.timedelta(days=1)
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
