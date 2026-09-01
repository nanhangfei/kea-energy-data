"""Decide empirically whether LCL's `tstp` is UTC or London local time.

Sources describe it both ways and the whole transform depends on the answer,
so it is settled from the data rather than assumed.

The test: count readings per household per LONDON-LOCAL calendar date.
  * If `tstp` is London local, the clocks-change days are short and long -
    46 and 50 readings - because the local day itself is.
  * If `tstp` is UTC, every local day still holds 48 readings; the 46/50 only
    appear after converting, and the raw dates are all 48.

UK transitions: last Sunday in March (forward), last Sunday in October (back).
"""

from __future__ import annotations

import csv
import datetime as dt
import sys
from collections import Counter, defaultdict
from pathlib import Path

BLOCK = Path(sys.argv[1] if len(sys.argv) > 1
             else "raw/lcl/extracted/block_0.csv")

UK_DST = {
    dt.date(2012, 3, 25): "spring forward", dt.date(2012, 10, 28): "fall back",
    dt.date(2013, 3, 31): "spring forward", dt.date(2013, 10, 27): "fall back",
}

per_day: dict[tuple[str, dt.date], int] = defaultdict(int)
with BLOCK.open(newline="") as f:
    r = csv.reader(f)
    next(r)
    for row in r:
        if len(row) < 2:
            continue
        per_day[(row[0], dt.date.fromisoformat(row[1][:10]))] += 1

lengths = Counter(per_day.values())
print(f"file        {BLOCK}")
print(f"households  {len({h for h, _ in per_day})}")
print(f"days        {len(per_day):,}\n")
print("readings per household-day (top 6):")
for n, count in lengths.most_common(6):
    print(f"  {n:>3} readings   {count:>8,} days")

print("\non the UK clock-change days themselves:")
verdict_local = 0
for day, kind in sorted(UK_DST.items()):
    counts = Counter(v for (_, d), v in per_day.items() if d == day)
    if not counts:
        continue
    shown = "  ".join(f"{n}x{c}" for n, c in counts.most_common(3))
    print(f"  {day} {kind:<15} most common: {shown}")
    top = counts.most_common(1)[0][0]
    if (kind == "spring forward" and top == 46) or (kind == "fall back" and top == 50):
        verdict_local += 1

print()
if verdict_local >= 2:
    print("VERDICT: tstp is LONDON LOCAL time.")
    print("  Clock-change days are 46 and 50 readings in the raw dates, so the")
    print("  timestamps carry UK daylight saving. uk_to_nz must localise them to")
    print("  Europe/London, and the UK 46/50 days need the fill/first-occurrence")
    print("  handling described in the module docstring.")
else:
    print("VERDICT: tstp is UTC.")
    print("  Every raw local date holds 48 readings, so the timestamps are")
    print("  absolute. uk_to_nz must convert to Europe/London first; the UK")
    print("  46/50 days appear only after that conversion.")
