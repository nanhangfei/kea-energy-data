"""Customers, accounts and invoice periods for the cohort.

Decides WHO is billed and FOR WHICH PERIODS. It deliberately does not
decide HOW MUCH: the amounts are computed in 04_rate_bills.sql by joining
src_catalogue, because pricing that happens in Python cannot be the thing
the pipeline is later tested against.

PLAN MAPPING. Ecotricity positions ecoANYTIME at homes without a smart
meter, but that is marketing, not an eligibility bar: a household with a
smart meter can still prefer a fixed rate, and many do. LCL's stdorToU
field records whether a household was on a standard or a time-of-use UK
tariff, so 'Std' maps to the flat plan directly.

A flat tariff also earns its place in the cohort. On a time-of-use plan a
bill can rise because consumption moved into peak, so a rise has two
possible causes tangled together. On ecoANYTIME the rate is constant and
fixed for three years, so a rise is volume or weather and nothing else --
which gives the bill explainer a clean case to be right about before it
has to handle the ambiguous ones.

    python3 pipeline/scripts/gen_source/gen_billing.py
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = Path("raw/lcl")

FIRST = ["Aroha", "Ben", "Claire", "Dev", "Emma", "Finn", "Grace", "Hemi",
         "Isla", "Jack", "Kiri", "Liam", "Mia", "Noa", "Olive", "Pita",
         "Quinn", "Ruby", "Sione", "Tui", "Ana", "Wiremu", "Yuki", "Zara"]
LAST = ["Ahern", "Baker", "Chen", "Duncan", "Ellis", "Fraser", "Gibbs",
        "Harrop", "Iose", "Jamieson", "Kaur", "Lowe", "Munro", "Ngata",
        "Ofelia", "Patel", "Quirke", "Rangi", "Sutton", "Tane", "Ussher",
        "Vaka", "Whitfield", "Young"]


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


def plan_shape(h: dict) -> str:
    """Which of the four plans this household is on.

    LCL's own tariff field drives it: 'ToU' households were on a dynamic
    tariff, so their load SHAPE justifies a time-of-use plan; 'Std'
    households were on a flat one and map to ecoANYTIME.

    ecoWHOLESALE is spot-priced with uncapped peaks, so it stays a small
    minority -- it is a product for people who want the risk.
    """
    if h["has_solar"]:
        return "SOLR"
    if h["stdortou"] == "ToU":
        return "SAVR"
    return "WHSL" if _h(h["lclid"], "spot") % 100 < 10 else "ANYT"


def periods(start: dt.date, end: dt.date, cycle_day: int, skip: int | None):
    """Monthly billing periods, both endpoints inclusive.

    `skip` names a period index that is MERGED into the one before it,
    producing a single ~43-day invoice. That is the delayed-billing trap:
    the customer sees a bill roughly twice the usual size, having used
    nothing unusual, and src_billing.invoices has no period_days column
    to make it obvious.
    """
    bounds, cur = [], start
    while cur <= end:
        nxt = cur.replace(day=cycle_day)
        while nxt <= cur:
            nxt = (nxt.replace(day=28) + dt.timedelta(days=7)).replace(day=cycle_day)
        bounds.append((cur, min(nxt - dt.timedelta(days=1), end)))
        cur = nxt
    if bounds and bounds[-1][0] > bounds[-1][1]:
        bounds.pop()
    if skip is not None and 0 < skip < len(bounds) - 1:
        merged = (bounds[skip - 1][0], bounds[skip][1])
        bounds = bounds[:skip - 1] + [merged] + bounds[skip + 1:]
    return bounds


def main() -> None:
    spec = json.loads((HERE / "cohort.json").read_text())
    cohort = spec["households"]
    nz_start = dt.date.fromisoformat(spec["nz_span"][0])
    nz_end = dt.date.fromisoformat(spec["nz_span"][1])
    OUT.mkdir(parents=True, exist_ok=True)

    fc = (OUT / "customers.csv").open("w", newline="")
    fa = (OUT / "accounts.csv").open("w", newline="")
    fp = (OUT / "invoice_periods.csv").open("w", newline="")
    wc, wa, wp = csv.writer(fc), csv.writer(fa), csv.writer(fp)

    n_inv = n_long = 0
    shapes: dict[str, int] = {}

    for i, h in enumerate(cohort):
        icp, lcl = h["icp"], h["lclid"]
        acorn = h["acorn_grouped"]
        cust_no = f"C{_h(lcl,'cust') % 10**7:07d}"
        acct_no = f"A{_h(lcl,'acct') % 10**8:08d}"
        name = (f"{FIRST[_h(lcl,'fn') % len(FIRST)]} "
                f"{LAST[_h(lcl,'ln') % len(LAST)]}")

        # Consumer Care flags skew with deprivation. Flagged in
        # data/dictionary/ as an assumption: Acorn is a UK geodemographic
        # classifier and NZ has no equivalent. It is here to make the
        # cohort non-uniform defensibly, not because it is evidence.
        vuln = "Y" if acorn == "Adversity" and _h(lcl, "v") % 100 < 18 else "N"
        med = "Y" if acorn == "Adversity" and _h(lcl, "m") % 100 < 7 else "N"
        if acorn == "Affluent":
            pay = "DD"
        elif acorn == "Adversity":
            pay = ["BT", "CARD", "DD"][_h(lcl, "pay") % 3]
        else:
            pay = ["DD", "DD", "BT"][_h(lcl, "pay") % 3]

        # Unclassified households (ACORN-U) keep a NULL email: a genuine
        # nullable the pipeline has to survive rather than an invented one.
        email = "" if acorn is None else f"{name.split()[0].lower()}.{cust_no.lower()}@example.nz"

        wc.writerow([cust_no, name, email, f"02{_h(lcl,'ph') % 10**8:08d}",
                     f"{_h(icp,'num') % 300 + 1} Street", "", h["climate_region"],
                     h["climate_region"], f"{_h(icp,'pc') % 9000 + 1000}", med, vuln])

        shape = plan_shape(h)
        shapes[shape] = shapes.get(shape, 0) + 1
        plan_code = (f"ECO_{shape}_{h['network_code']}_"
                     f"{'LOW' if h['user_category'] == 'LOW' else 'STA'}")
        cycle = _h(lcl, "cycle") % 28 + 1
        wa.writerow([acct_no, cust_no, icp, plan_code, pay, cycle, nz_start])

        # One household in twenty gets a delayed run.
        skip = (_h(lcl, "skip") % 18 + 3) if _h(lcl, "delay") % 20 == 0 else None
        for k, (pf, pt) in enumerate(periods(nz_start, nz_end, cycle, skip)):
            if (pt - pf).days + 1 < 20:      # drop stub periods at either end
                continue
            issue = pt + dt.timedelta(days=1 + _h(lcl, k, "iss") % 3)
            wp.writerow([f"INV{_h(lcl, k, 'no') % 10**9:09d}", acct_no, plan_code,
                         pf, pt, issue, issue + dt.timedelta(days=21)])
            n_inv += 1
            if (pt - pf).days + 1 > 40:
                n_long += 1

    for f in (fc, fa, fp):
        f.close()
    print(f"{len(cohort)} customers, {len(cohort)} accounts, {n_inv} invoice periods")
    print(f"  plans      " + "  ".join(f"{k}={v}" for k, v in sorted(shapes.items())))
    print(f"  long bills {n_long} (delayed-run trap)")


if __name__ == "__main__":
    main()
