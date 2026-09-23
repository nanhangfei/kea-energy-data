"""Choose the households that become Kea customers, and record why.

Two properties matter more than the choice itself:

  * DETERMINISTIC. Re-running picks the identical households. The selection is
    a hash of the LCLid against a fixed seed - never `random`, never the clock.
    Everything downstream keys off these ids, so a cohort that drifts makes
    every generated bill and every assertion drift with it.

  * COVERAGE-FIRST, NOT RANDOM. LCL recruited in waves through 2012-13, so a
    randomly chosen household may hold a few months rather than the full span.
    Sampling at random would quietly fill the cohort with short histories and
    the year-on-year comparisons would have nothing to compare. See
    uk_to_nz.LCL_START for how the window was chosen.

Writes cohort.json (committed - it IS the selection) and caches coverage.json
(derived, gitignored). Stdlib only.

    python3 pipeline/scripts/gen_source/cohort.py --report-windows
    python3 pipeline/scripts/gen_source/cohort.py --size 50
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import uk_to_nz as tz  # noqa: E402

HERE = Path(__file__).resolve().parent
RAW = Path("raw/lcl/extracted")
CACHE = HERE / "coverage.json"
OUT = HERE / "cohort.json"

#: Fixed. Changing it re-rolls the entire cohort, so change it deliberately.
SEED = "kea-stage2-v1"

#: Keep the generated demo cohort deliberately narrow. The nine fixed fixtures
#: retain the other networks needed by regression scenarios; the 50-household
#: cohort exists to demonstrate three retail plan families, not copies of
#: each family for different lines companies.
DEMO_NETWORK = ("VECT", "Auckland")

#: Reserved so a generated ICP can never collide with a fixture's. Fixture ICPs
#: are '0165841877LCC54' and the DEMO...NNAA family; none start 09.
ICP_PREFIX = "09"

#: Bumped whenever scan_coverage() records something new. An older cache is
#: rescanned rather than read for a field it never wrote - a missing key would
#: otherwise surface as a KeyError deep in the sizing, or worse, as a zero.
SCAN_VERSION = 2

#: The NZ mean the cohort is scaled to. Restated from gen_metering, because the
#: array sizing below needs NZ kWh and cohort.py runs BEFORE scale.json exists
#: (the scale is measured FROM this cohort). Dividing a household's LCL mean by
#: the cohort's own LCL mean and multiplying by this is the same number the
#: global scale factor produces, without the ordering problem.
#: tests/test_cohort_solar.py fails if the two constants drift apart.
NZ_TARGET_MEAN_KWH_DAY = 20.0

#: Sizing a rooftop array. A household does not buy panels to cover its whole
#: year: it buys roughly what it can use, because what it exports earns about a
#: third of what it avoids. PV_LOAD_FRACTION is that share of annual load, and
#: NZ_YIELD is the Auckland-ish yield gen_metering generates against.
#:
#: ⚠️ This sizes the ARRAY only. It deliberately does NOT decide WHO has solar:
#: has_solar picks from the whole cohort and planted.candidates() picks the
#: planting pool from whoever is left, so moving a household in or out of solar
#: would reshuffle every planted load and break Sprint 4's comparability.
PV_LOAD_FRACTION = 0.6
NZ_YIELD_KWH_PER_KWP = 1300.0
#: Real product sizes. The sized need is rounded to the NEAREST of these, not
#: down: rounding down turned a 2.9 kWp need into 2 kWp and a 7.9 into 6, which
#: is a rounding artefact rather than a household's decision. A tie takes the
#: smaller size, so the rule stays deterministic.
PV_SIZES_KWP = (1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0)


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:12], 16)


def scan_coverage() -> dict:
    """One pass over the blocks -> a presence bitmask and a kWh total per household.

    A bit per published-span day, so every later coverage question is answered
    from ~1 MB of cache instead of re-reading 6.7 GB. The date is taken as the
    UTC prefix of `tstp` (which IS UTC - see probe_tstp.py); it differs from
    the London date only for the 23:00-00:00 UTC hour under BST, which is well
    inside the tolerance of a coverage ranking.

    The kWh total rides along in the SAME pass. It is what sizes each solar
    household's array, and a second 6.7 GB read to get it would be the only
    expensive thing in the whole generator.
    """
    start, end = tz.LCL_PUBLISHED_START, tz.LCL_END
    ndays = (end - start).days + 1
    masks: dict[str, int] = defaultdict(int)
    blockof: dict[str, str] = {}
    kwh: dict[str, float] = defaultdict(float)

    blocks = sorted(RAW.glob("block_*.csv"))
    if not blocks:
        sys.exit(f"no blocks in {RAW} - run fetch_lcl.sh, then unzip halfhourly_dataset")
    print(f"scanning {len(blocks)} blocks (one-off, ~1 min)...", file=sys.stderr)

    for i, b in enumerate(blocks, 1):
        seen = set()
        with b.open(newline="") as f:
            r = csv.reader(f)
            next(r, None)
            for row in r:
                if len(row) >= 2:
                    seen.add((row[0], row[1][:10]))
                if len(row) >= 3:
                    try:
                        kwh[row[0]] += float(row[2])
                    except ValueError:
                        pass      # 'Null': the reading is missing, not zero
        for lcl, d in seen:
            try:
                off = (dt.date.fromisoformat(d) - start).days
            except ValueError:
                continue
            if 0 <= off < ndays:
                masks[lcl] |= 1 << off
                blockof[lcl] = b.stem
        if i % 20 == 0:
            print(f"    {i}/{len(blocks)}", file=sys.stderr)

    cache = {
        "version": SCAN_VERSION,
        "span": [str(start), str(end)],
        "ndays": ndays,
        "households": {k: {"block": blockof[k], "mask": format(v, "x"),
                           "kwh": round(kwh[k], 3)}
                       for k, v in masks.items()},
    }
    CACHE.write_text(json.dumps(cache) + "\n")
    return cache


def load_coverage(rescan: bool = False) -> tuple[dict[str, int], dict[str, str],
                                                 dict[str, float], int]:
    cache = scan_coverage() if (rescan or not CACHE.exists()) else json.loads(CACHE.read_text())
    if cache["span"][0] != str(tz.LCL_PUBLISHED_START):
        cache = scan_coverage()
    if cache.get("version", 1) < SCAN_VERSION:
        cache = scan_coverage()   # written before kWh was recorded
    masks = {k: int(v["mask"], 16) for k, v in cache["households"].items()}
    blocks = {k: v["block"] for k, v in cache["households"].items()}
    kwh = {k: v["kwh"] for k, v in cache["households"].items()}
    return masks, blocks, kwh, cache["ndays"]


def window_band(ndays: int) -> tuple[int, int]:
    """Bit mask and width for the chosen UK window inside the published span."""
    off = (tz.LCL_START - tz.LCL_PUBLISHED_START).days
    width = ndays - off
    return (((1 << width) - 1) << off), width


def pv_kwp(nz_annual_kwh: float) -> float:
    """The array a household of this size would actually install, in kWp.

    Sized to what it can USE, not to what it consumes: exported kWh earn about
    a third of what avoided kWh save, so a roof scaled to the whole year's load
    sells most of what it makes. The result is the nearest real product size,
    and the smallest of those is the floor -- a household below it gets small
    panels, never none, because who has solar is settled elsewhere and must not
    move (see PV_LOAD_FRACTION).
    """
    need = nz_annual_kwh * PV_LOAD_FRACTION / NZ_YIELD_KWH_PER_KWP
    return min(PV_SIZES_KWP, key=lambda s: (abs(s - need), s))


def report_windows(masks: dict[str, int], ndays: int) -> None:
    """Households vs window start - the evidence behind uk_to_nz.LCL_START."""
    shift = tz.derive_shift_days()
    dst = [dt.date(2024, 9, 29), dt.date(2025, 4, 6),
           dt.date(2025, 9, 28), dt.date(2026, 4, 5)]
    print(f"{'UK start':<12}{'days':>6}{'NZ start':>12}{'DST':>5}"
          f"{'>=99%':>7}{'>=97%':>7}{'>=95%':>7}")
    print("-" * 62)
    for m in range(27):
        s = tz.LCL_PUBLISHED_START + dt.timedelta(days=((m * 30) // 7) * 7)
        off = (s - tz.LCL_PUBLISHED_START).days
        if off >= ndays:
            break
        width = ndays - off
        band = ((1 << width) - 1) << off
        n99 = n97 = n95 = 0
        for mask in masks.values():
            p = bin(mask & band).count("1") / width
            n95 += p >= 0.95
            n97 += p >= 0.97
            n99 += p >= 0.99
        nz = s + dt.timedelta(days=shift)
        ndst = sum(1 for d in dst if nz <= d <= tz.NZ_SPAN_END)
        print(f"{s!s:<12}{width:>6}{nz!s:>12}{ndst:>5}{n99:>7}{n97:>7}{n95:>7}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=50)
    ap.add_argument("--min-coverage", type=float, default=0.99,
                    help="fraction of the chosen UK window a household must hold")
    ap.add_argument("--rescan", action="store_true")
    ap.add_argument("--report-windows", action="store_true")
    args = ap.parse_args()

    masks, blocks, kwh, ndays = load_coverage(args.rescan)
    if args.report_windows:
        report_windows(masks, ndays)
        return

    band, width = window_band(ndays)
    shift = tz.derive_shift_days()
    nz_start = tz.nz_local_date_for(tz.LCL_START, shift)

    with (RAW / "informations_households.csv").open(newline="") as f:
        households = list(csv.DictReader(f))

    frac = {h["LCLid"]: bin(masks.get(h["LCLid"], 0) & band).count("1") / width
            for h in households}

    # Report the distribution BEFORE applying the threshold, so a threshold
    # that would starve the cohort is visible rather than surprising.
    print(f"UK window  {tz.LCL_START} .. {tz.LCL_END}  ({width} days)")
    print(f"NZ window  {nz_start} .. {tz.NZ_SPAN_END}  (shift {shift} days)\n")
    print("coverage of that window:")
    for cut in (1.0, 0.99, 0.97, 0.95, 0.90, 0.50):
        print(f"  >= {cut:>5.0%}   {sum(1 for v in frac.values() if v >= cut):>5} households")

    eligible = {k for k, v in frac.items() if v >= args.min_coverage}
    print(f"\neligible at >= {args.min_coverage:.0%}: {len(eligible)}")
    if len(eligible) < args.size:
        sys.exit(f"only {len(eligible)} clear {args.min_coverage:.0%}, need {args.size}. "
                 f"Lower --min-coverage, or move uk_to_nz.LCL_START later "
                 f"(see --report-windows) and re-check the DST column.")

    # Stratify on the two attributes that carry real signal, so the cohort
    # keeps the eligible pool's proportions rather than the hash's.
    strata: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for h in households:
        if h["LCLid"] in eligible:
            strata[(h["stdorToU"], h["Acorn_grouped"])].append(h)

    chosen: list[dict] = []
    for _, members in sorted(strata.items()):
        take = round(args.size * len(members) / len(eligible))
        members.sort(key=lambda h: _h(h["LCLid"], SEED))
        chosen.extend(members[:take])
    chosen.sort(key=lambda h: _h(h["LCLid"], SEED))
    chosen = chosen[:args.size]

    # NZ-scaled annual load, household by household. gen_metering measures ONE
    # global scale factor from this cohort so its mean lands on
    # NZ_TARGET_MEAN_KWH_DAY; a household's NZ load is therefore its own LCL
    # mean over the cohort's, times that target. The same arithmetic, one step
    # earlier -- which is what lets the array be sized before scale.json exists.
    span_days = {h["LCLid"]: bin(masks.get(h["LCLid"], 0)).count("1") for h in chosen}
    total_kwh = sum(kwh.get(lcl, 0.0) for lcl in span_days)
    total_days = sum(span_days.values())
    cohort_mean = total_kwh / total_days          # weighted by household-days
    nz_annual = {lcl: (kwh.get(lcl, 0.0) / d) / cohort_mean
                      * NZ_TARGET_MEAN_KWH_DAY * 365.25
                 for lcl, d in span_days.items() if d}

    cohort = []
    for i, h in enumerate(chosen):
        lcl = h["LCLid"]
        net, region = DEMO_NETWORK
        acorn = h["Acorn_grouped"]
        has_solar = _h(lcl, "solar") % 100 < (14 if acorn == "Affluent" else 6)
        cohort.append({
            "lclid": lcl,
            "icp": f"{ICP_PREFIX}{_h(lcl, 'icp') % 10**8:08d}{net[:2]}{i % 100:02d}",
            "stdortou": h["stdorToU"],
            # ACORN-U is unclassified. Left NULL rather than guessed - a free
            # nullable case for the derived-attribute path.
            "acorn_grouped": None if acorn.startswith("ACORN-") else acorn,
            "network_code": net,
            "climate_region": region,
            "coverage": round(frac[lcl], 4),
            "block": blocks[lcl],
            # The household's own load in NZ terms, which is what sizes the
            # array below and, in planted.py, orders the solar+EV pool. Written
            # down rather than left implicit so pv_kwp can be checked against
            # its input without re-scanning 115 blocks of LCL readings.
            "nz_annual_kwh": round(nz_annual.get(lcl, 0.0), 1),
            # Affluent households are likelier to have panels. WHO has panels
            # is a hash and stays one: see PV_LOAD_FRACTION for why this must
            # not become load-dependent.
            "has_solar": has_solar,
            # HOW BIG the array is does depend on load. Null without panels,
            # so a reader that forgets to check has_solar gets a TypeError
            # rather than a plausible-looking 4 kWp.
            #
            # This is the roof of a household as the cohort finds it, BEFORE
            # any planted load. planted.SOLAR_PLAN gives three of these
            # households a car and declares a bigger array to go with it,
            # which gen_metering prefers over this figure; cohort.py cannot
            # compute that, because it does not know which households will be
            # planted and must not, or membership would depend on load.
            "pv_kwp": pv_kwp(nz_annual.get(lcl, 0.0)) if has_solar else None,
            # One customer category keeps the demo catalogue focused on the
            # three retail plan families. Low-user policy can be added later as
            # a separate eligibility feature rather than doubling every plan.
            "user_category": "STANDARD",
        })

    OUT.write_text(json.dumps({
        "seed": SEED,
        "size": len(cohort),
        "min_coverage": args.min_coverage,
        "shift_days": shift,
        "uk_span": [str(tz.LCL_START), str(tz.LCL_END)],
        "nz_span": [str(nz_start), str(tz.NZ_SPAN_END)],
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "households": cohort,
    }, indent=2) + "\n", encoding="utf-8")

    print(f"\nchose {len(cohort)} households -> {OUT.name}")
    for label, key in (("plan basis", "stdortou"), ("user category", "user_category"),
                       ("acorn", "acorn_grouped"), ("network", "network_code")):
        d = Counter(str(c[key]) for c in cohort)
        print(f"  {label:<14} " + "  ".join(f"{k}={v}" for k, v in sorted(d.items())))
    arrays = Counter(c["pv_kwp"] for c in cohort if c["has_solar"])
    print(f"  {'solar':<14} {sum(c['has_solar'] for c in cohort)} households  "
          + "  ".join(f"{k:g}kWp={v}" for k, v in sorted(arrays.items())))
    print(f"  {'reads':<14} ~{len(cohort) * width * 48 / 1e6:.1f} M")


if __name__ == "__main__":
    main()
