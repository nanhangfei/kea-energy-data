"""Customers, accounts and invoice periods for the cohort.

Decides WHO is billed and FOR WHICH PERIODS. It deliberately does not
decide HOW MUCH: the amounts are computed in 04_rate_bills.sql by joining
src_catalogue, because pricing that happens in Python cannot be the thing
the pipeline is later tested against.

PLAN MAPPING. The three Kea styles match the Sprint 3 plan page: Anytime
Rate, Time of Use, and Free Weekends. Assignment is deterministic but
independent of the household's measured load shape. That deliberately leaves
some customers on a worse plan so the recommender has a real switch to find.

See docs/sprints/sprint-3/plans-and-terms.md for the screenshot rates and why
the LCL cohort, rather than the nine Sprint 2 fixtures, owns this demo.

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
    """Which of the three screenshot plans this household starts on.

    ⚠️ DELIBERATELY NOT THE PLAN THAT SUITS THEM. This allocates by who the
    household IS -- solar, and the tariff they were on in London -- never by
    which discount window their NZ load actually falls in. That is the whole
    point: plan comparison exists because people are on the wrong plan, and if
    everyone started on their best one the recommender would have nothing to
    find and the feature could not be demonstrated.

    The hash is intentionally unrelated to measured usage, solar, or the
    eventual cheapest plan. Roughly 40 / 30 / 30 across Anytime / Time of Use /
    Free Weekends keeps every choice populated without pre-solving the
    recommendation problem in the generator.
    """
    r = _h(h["lclid"], "plan") % 100
    if r < 40:
        return "ANY"
    if r < 70:
        return "TOU"
    return "WKND"


def term_months(h: dict) -> int:
    """Flexible, six months or twelve.

    The Sprint 3 plan page offers Flexible, six months at $10/month, and twelve
    months at $25/month. docs/sprints/sprint-3/plans-and-terms.md records which
    parts are modelled in the billing and recommendation data.

    Half stay flexible. People do not commit by default, and a cohort where
    everyone had signed a term would make the commitment look costless.
    """
    r = _h(h["lclid"], "term") % 100
    if r < 50:
        return 0
    return 6 if r < 70 else 12


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
    terms: dict[int, int] = {}

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
        # Must match 02_catalogue.sql's format('KEA_%s_%s_%s', ...) exactly.
        # A mismatch does not error: the account simply references a plan code
        # that does not exist, and 99_assert.sql is what notices.
        plan_code = (f"KEA_{shape}_{h['network_code']}_"
                     f"{'LOW' if h['user_category'] == 'LOW' else 'STA'}")
        cycle = _h(lcl, "cycle") % 28 + 1
        # The term runs from the day they joined, so for most accounts it
        # expires partway through the span. That is deliberate: some bills
        # carry a credit and some do not, and "did they stay?" has an answer.
        term = term_months(h)
        terms[term] = terms.get(term, 0) + 1
        wa.writerow([acct_no, cust_no, icp, plan_code, pay, cycle, nz_start,
                     term, nz_start if term else ""])

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
    print(f"  terms      " + "  ".join(
        f"{'flexible' if k == 0 else str(k) + 'mo'}={v}" for k, v in sorted(terms.items())))
    print(f"  long bills {n_long} (delayed-run trap)")


if __name__ == "__main__":
    main()
