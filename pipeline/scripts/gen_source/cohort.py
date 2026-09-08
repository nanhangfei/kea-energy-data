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


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:12], 16)


def scan_coverage() -> dict:
    """One pass over the blocks -> a presence bitmask per household.

    A bit per published-span day, so every later coverage question is answered
    from ~1 MB of cache instead of re-reading 6.7 GB. The date is taken as the
    UTC prefix of `tstp` (which IS UTC - see probe_tstp.py); it differs from
    the London date only for the 23:00-00:00 UTC hour under BST, which is well
    inside the tolerance of a coverage ranking.
    """
    start, end = tz.LCL_PUBLISHED_START, tz.LCL_END
    ndays = (end - start).days + 1
    masks: dict[str, int] = defaultdict(int)
    blockof: dict[str, str] = {}

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
        "span": [str(start), str(end)],
        "ndays": ndays,
        "households": {k: {"block": blockof[k], "mask": format(v, "x")}
                       for k, v in masks.items()},
    }
    CACHE.write_text(json.dumps(cache) + "\n")
    return cache


def load_coverage(rescan: bool = False) -> tuple[dict[str, int], dict[str, str], int]:
    cache = scan_coverage() if (rescan or not CACHE.exists()) else json.loads(CACHE.read_text())
    if cache["span"][0] != str(tz.LCL_PUBLISHED_START):
        cache = scan_coverage()
    masks = {k: int(v["mask"], 16) for k, v in cache["households"].items()}
    blocks = {k: v["block"] for k, v in cache["households"].items()}
    return masks, blocks, cache["ndays"]


def window_band(ndays: int) -> tuple[int, int]:
    """Bit mask and width for the chosen UK window inside the published span."""
    off = (tz.LCL_START - tz.LCL_PUBLISHED_START).days
    width = ndays - off
    return (((1 << width) - 1) << off), width


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

    masks, blocks, ndays = load_coverage(args.rescan)
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

    cohort = []
    for i, h in enumerate(chosen):
        lcl = h["LCLid"]
        net, region = DEMO_NETWORK
        acorn = h["Acorn_grouped"]
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
            # Affluent households are likelier to have panels.
            "has_solar": _h(lcl, "solar") % 100 < (14 if acorn == "Affluent" else 6),
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
    print(f"  {'solar':<14} {sum(c['has_solar'] for c in cohort)} households")
    print(f"  {'reads':<14} ~{len(cohort) * width * 48 / 1e6:.1f} M")


if __name__ == "__main__":
    main()
