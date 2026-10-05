# Cohort selection: the evidence

This folder holds the measurements behind [`design/kea-cohort-selection.md`](../../design/kea-cohort-selection.md):
- **The pool:** which LCL households a cohort can be drawn from, and how their load shapes differ.
- **Designs:** how each candidate rule and size would serve the features.
- **Serving today:** what the published cohort does now.

```bash
cd data
python3 analysis/cohort_selection/measure_cohort_designs.py             # results/designs.md, ~70 s
python3 analysis/cohort_selection/measure_cohort_designs.py --serving   # + results/serving_now.md
python3 analysis/cohort_selection/measure_cohort_designs.py --sizes 50,200   # other sizes
python3 analysis/cohort_selection/measure_cohort_designs.py --baseline old/cohort.json   # once cohort.json has moved on
```

- **Code:** `measure_cohort_designs.py`, stdlib only. `serving_now.sql` holds the `--serving` queries,
  one block per `-- @<title>` line.
- **Reads:**
  - `raw/lcl/smart-meters-in-london.zip`, streamed; no block is extracted;
  - `pipeline/scripts/gen_source/coverage.json`, cohort.py's cache. The script refuses to run
    without it rather than rescan a partial extraction;
  - `pipeline/scripts/gen_source/cohort.json`;
  - with `--serving`, kea-db through `docker exec`, each query in a `READ ONLY` transaction that is
    rolled back.
- **Writes:** only `results/`.
- **Privacy:** aggregates only. No LCL id, ICP or account number is printed.
- **Reproducing the designs:** the designs reuse the generator's own code — `cohort.py` for the
  coverage window, the seed and the array sizing, and `planted.py` for the planting candidates and
  thresholds. They also re-implement the generator's hashes: `gen_billing.py` for account numbers
  and hardship flags, `08_payments.sql` for payment profiles. So a design's households, solar and
  payment profiles are the ones the generator would give them.

## The proxies

Three measures stand in for rules that need the warehouse. Each is checked against the rule it
replaces.

| Proxy | Stands in for | Checked against |
|---|---|---|
| R² of the 12 calendar-month means against a January-peaking cosine, cut at 0.5 | the monthly heating fit, R² ≥ 0.30 against Auckland HDD, which gates `is_explainable` and `is_forecastable` | `results/serving_now.md`: it marks 35 of the 45 published accounts, all 28 forecastable ones among them |
| Days whose largest two-half-hour excess over the day's floor, scaled, reaches 2.5 kWh | `planted.screen()`'s EV test (detect-v1's high-power rule) | `results/designs.md` §5: the screen keeps about 42% (EV) and 29% (hot water) of what the proxy calls clean |
| The cheapest of the three Kea plans from the household's band shares at displayed prices | `build_plan_recommendations.py` | only that it ignores solar, term credits and line losses; it ranks, it doesn't price |

⚠️ **`results/` is a snapshot.** `serving_now.md` describes whatever cohort is published when it
runs. After the cohort changes, re-run it and commit both files together, because the design
document quotes them.
