# Cohort designs measured against the LCL pool

Run 2026-10-01 23:32 UTC by `measure_cohort_designs.py`. Window 2012-02-03 .. 2014-02-28; eligible = at least 99% of it. Proxies are defined in the script's docstring.

## 1. Coverage threshold and Acorn mix

| Pool | Households | Affluent | Adversity | Comfortable | ToU |
|---|---:|---:|---:|---:|---:|
| All LCL households | 5,566 | 39.4% | 32.6% | 27.1% | 20.2% |
| ≥ 99% coverage | 609 | 50.4% | 23.6% | 25.0% | 15.8% |
| ≥ 97% coverage | 710 | 51.8% | 24.1% | 23.1% | 15.8% |
| ≥ 95% coverage | 828 | 53.0% | 24.2% | 22.0% | 15.7% |
| ≥ 90% coverage | 1,484 | 47.6% | 28.9% | 22.8% | 18.6% |
| ≥ 80% coverage | 3,919 | 41.3% | 32.9% | 25.1% | 20.2% |

## 2. Usage level and feature behaviour, eligible pool

Tertile cuts 6.64 and 11.60 kWh/day (London, unscaled); EV-shaped share at the pool's own scale ×1.89.

| Usage tertile | Households | Heating proxy ≥ 0.5 | EV-shaped on ≤ 0.5% of days |
|---|---:|---:|---:|
| Low | 203 | 55.7% | 84.2% |
| Mid | 203 | 61.1% | 45.3% |
| High | 203 | 78.3% | 7.4% |

Cheapest plan across the pool: Time of Use 580 (95.2%), Free Weekends 17 (2.8%), Anytime 12 (2.0%).

## 3. Designs

`current 50` is the published cohort (`--baseline`). `current rule` is cohort.py's allocation at that size; `proposed` is population quotas, a usage-systematic sample and a fixed 10% solar count. Payment profiles are 08_payments.sql's hash buckets before any minimum is applied.

| Design | N | Scale | Affluent | Adversity | Comfortable | ACORN-U | ToU | Heating proxy | Solar | Solar clean ≤0.5% | Solar clean ≤1.5% | Non-solar clean | Heater runs >2% | Night-heavy | Anytime cheapest | Free Weekends cheapest | LATE | DD | PART | STOPPED | On time | Vulnerable | Medical | Non-solar ≥8,000 kWh/yr | Blocks | Not yet unzipped | Kept from current |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| current 50 | 50 | 1.72 | 52% | 22% | 26% | 0 | 8 | 39 | 8 | 5 | 5 | 25 | 12 | 4 | 3 | 2 | 3 | 3 | 5 | 0 | 39 | 4 | 1 | 11 | 33 | 0 | 50 |
| current rule 50 | 50 | 1.72 | 52% | 22% | 26% | 0 | 8 | 39 | 8 | 5 | 5 | 25 | 12 | 4 | 3 | 2 | 3 | 3 | 5 | 0 | 39 | 4 | 1 | 11 | 33 | 0 | 50 |
| proposed 50 | 50 | 1.97 | 40% | 32% | 24% | 2 | 11 | 32 | 5 | 3 | 4 | 18 | 21 | 7 | 1 | 1 | 3 | 1 | 1 | 2 | 43 | 3 | 0 | 17 | 38 | 6 | 2 |
| current rule 100 | 100 | 1.87 | 50% | 24% | 25% | 1 | 16 | 69 | 13 | 7 | 8 | 38 | 42 | 6 | 4 | 5 | 6 | 4 | 6 | 1 | 83 | 5 | 1 | 32 | 51 | 8 | 50 |
| proposed 100 | 100 | 1.96 | 41% | 31% | 26% | 2 | 21 | 70 | 10 | 10 | 10 | 36 | 43 | 5 | 1 | 4 | 3 | 2 | 3 | 3 | 89 | 5 | 3 | 36 | 60 | 11 | 5 |
| current rule 150 | 149 | 1.98 | 51% | 23% | 25% | 1 | 23 | 100 | 21 | 12 | 14 | 57 | 57 | 9 | 5 | 5 | 9 | 4 | 7 | 1 | 128 | 6 | 1 | 46 | 68 | 13 | 50 |
| proposed 150 | 150 | 1.95 | 40% | 32% | 27% | 2 | 31 | 101 | 15 | 9 | 10 | 63 | 59 | 9 | 1 | 3 | 7 | 3 | 5 | 2 | 133 | 9 | 5 | 49 | 70 | 11 | 13 |
| current rule 200 | 200 | 1.91 | 50% | 24% | 25% | 2 | 31 | 140 | 24 | 14 | 16 | 80 | 76 | 13 | 5 | 7 | 16 | 7 | 8 | 2 | 167 | 8 | 2 | 64 | 74 | 13 | 50 |
| proposed 200 | 200 | 1.95 | 40% | 32% | 26% | 3 | 41 | 137 | 20 | 8 | 10 | 80 | 82 | 12 | 4 | 6 | 10 | 8 | 6 | 7 | 169 | 15 | 6 | 62 | 69 | 0 | 17 |
| current rule 300 | 300 | 2.00 | 51% | 24% | 25% | 2 | 47 | 199 | 31 | 16 | 20 | 112 | 122 | 17 | 5 | 8 | 21 | 8 | 9 | 5 | 257 | 16 | 6 | 98 | 85 | 18 | 50 |
| proposed 300 | 300 | 1.94 | 40% | 32% | 27% | 4 | 60 | 190 | 30 | 15 | 18 | 122 | 116 | 15 | 9 | 5 | 17 | 9 | 9 | 6 | 259 | 20 | 9 | 99 | 86 | 19 | 23 |

Array sizes (kWp: households) and the non-solar daytime (09:00–17:00) share, p10 to p90:

- current 50: {1.5: 2, 3.0: 4, 4.0: 1, 8.0: 1}; daytime 0.26–0.43; largest household 36.1 kWh/day (London)
- proposed 50: {2.0: 2, 3.0: 1, 4.0: 1, 5.0: 1}; daytime 0.13–0.38; largest household 34.9 kWh/day (London)
- proposed 100: {1.5: 3, 2.0: 4, 3.0: 2, 4.0: 1}; daytime 0.26–0.42; largest household 36.4 kWh/day (London)
- proposed 150: {1.5: 3, 2.0: 3, 3.0: 3, 4.0: 4, 5.0: 1, 6.0: 1}; daytime 0.26–0.41; largest household 37.5 kWh/day (London)
- proposed 200: {1.5: 2, 2.0: 4, 3.0: 2, 4.0: 9, 5.0: 2, 6.0: 1}; daytime 0.26–0.43; largest household 41.6 kWh/day (London)
- proposed 300: {1.5: 5, 2.0: 7, 3.0: 7, 4.0: 7, 5.0: 3, 6.0: 1}; daytime 0.26–0.42; largest household 41.6 kWh/day (London)

## 4. Where population quotas stop being exact

| Group | Population share | Eligible | Largest N it can fill |
|---|---:|---:|---:|
| ToU / Adversity | 5.4% | 17 | 317 |
| ToU / Comfortable | 5.8% | 22 | 379 |
| ToU / ACORN-U | 0.2% | 1 | 463 |
| Std / Adversity | 27.3% | 127 | 465 |
| Std / Comfortable | 21.3% | 130 | 611 |
| ToU / Affluent | 8.8% | 56 | 636 |
| Std / ACORN-U | 0.7% | 5 | 713 |
| Std / Affluent | 30.6% | 251 | 820 |

## 5. The EV-shaped proxy against planted.screen()

Today's 35 planting candidates: the proxy calls 24 clean for an EV. The screen itself (docs/sprints/sprint-4/data-plan.md, 17 Sep) passed 10 for an EV and 7 for a hot-water element, so the screen keeps about 42% and 29% of what the proxy calls clean.
