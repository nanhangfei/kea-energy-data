# Validating dashboard 11 — Sprint 4, what the meter can see

Issue #183. Builder `analysis/metabase/build_sprint4_dashboard.py`, 40 data-bearing
cards, collection *Sprint 4 — detected loads, solar and EV*.

Everything below was measured against **one serving release**, identified in §0.
Every query is in §6 and can be re-run without Metabase.

⚠️ Identifiers. The only household key any card returns is `icp_id`, a surrogate
integer (`1027`, `1051`). It is not an ICP: a real one is ten digits, two letters
and two more characters, and the builder's `IDENTIFIERS` guard refuses
`icp_number`, `icp_identifier`, `customer_name`, `email`, `address_display` and
`account_number` outright. All 37 serving card queries were run and their full
output scanned: **0** strings of real-ICP shape, 0 LCL meter ids, 0 email
addresses, 0 account numbers.

## 0. The release these numbers belong to

No serving table records a release label, so the release is identified by its
load timestamps instead:

| Table | Rows | Generations | `computed_at` (UTC) |
|---|---|---|---|
| `kea.solar_estimate` | 108 | 1 | 2026-09-20 05:58:13.008643 |
| `kea.ev_scenario` | 387 | 1 | 2026-09-20 05:58:13.008643 |
| `kea.hardship_assessment` | 45 | 1 | 2026-09-20 05:58:36.505022 |
| `kea.bill_forecast` | 9,354 | 1 (`created_at`) | 2026-09-20 05:58:13.008643 |

05:58:13 UTC is 17:58:13 NZST on 20 September 2026, which is the release the
design doc calls **`demo-20260920T175812`**. Version strings agree:
`sprint4-assumptions-v1` (solar, EV), `hardship-identification-v2`,
`hdd-linear-v3` (forecast), `detect-v2` (detection).

Window: `mart_daily_usage` ends **2027-08-27**, so every card whose window is
`LAST_YEAR` covers **2026-08-28 to 2027-08-27**. Cohort: 45 open generated
accounts, 45 distinct ICPs, of which 8 have an export register and 7 of those
have a published charging run.

## 1. Criterion 1 — source, calculation, unit, population, window per card

Recorded, but **not** by transcription. A hand-copied inventory is stale the
first time a card changes, and a stale inventory reads like a record of what the
page does. `analysis/metabase/inventory.py` derives it from the builder's own SQL
at run time, needs no database and no Metabase session, and works on any builder:

    python3 analysis/metabase/inventory.py                     # dashboard 11
    python3 analysis/metabase/inventory.py build_dashboard.py  # any builder

It reports per card: display kind, database, schema-qualified source tables, the
shared population CTEs the card actually *defines* (matching the bare word would
credit a card for a population it never splices), the window, which `LATEST_*`
pin it carries, and the units it declares.

⚠️ It reports what a card **reads**, never whether the number is right. That is
§2's job, and only the database can answer it.

Units are read from all three places a unit actually lives, because the column
header alone gets it wrong:

* a `$` prefix in `column_settings` — where every money measure on a **chart**
  declares it (cards 23, 26, 27, 32), since a header saying NZD would repeat
  itself on every bar;
* a literal concatenated into the value in SQL — where every money and energy
  measure on a **table** declares it (`'$' || to_char(…)`, `|| ' kWh'`, `|| '%'`
  on cards 25, 28), a table having no per-column prefix to hang it on, and where
  a banded dimension declares it (`'under 2 kWh a day'`, card 22);
* the header wording, for what is left.

Two parser corrections worth recording, because both would have produced a wrong
inventory rather than an obvious failure:

* **"a year" is a rate, not a unit.** `median saving a year` and
  `worst case, a year` are NZD; only card 24's `best case, years` measures years.
  A naive `\byears?\b` rule labelled six cards `years`.
* **an apostrophe in a quoted identifier shifts every literal after it.** The
  alias `a year's generation` (card 25) pairs with the next real quote unless
  double-quoted identifiers are stripped first, silently reporting another card's
  units. Fixed by stripping `"…"` before extracting `'…'`.

Result: no card on the page leaves a measure without a unit. A `scalar` says
`the title is the label` — it has one column named after the card, so the title
carries the unit and there is nowhere else for it to go.

## 2. Criterion 2 — dashboard values against their source queries

Each figure was recomputed from base tables **without sharing the cards' CTEs**,
so a wrong selector shows up as a disagreement instead of being reproduced on
both sides. Expected = the card; observed = the independent recompute.

| Card | Displays | Expected | Observed | |
|---|---|---|---|---|
| 1 | Households with a detected load | 42 of 45 | 42 of 45 | match |
| 2 | Days carrying a detection | 4,002 of 15,778 | 4,002 of 15,778 | match |
| 4 | Runs by start hour (all bars) | 2,580 | 2,580 | match |
| 5 | Runs by duration (all bars) | 2,580 | 2,580 | match |
| 6 | Detection days by shape and confidence | 4,002 | 4,002 | match |
| 7 | Share-of-day bands | 137 / 224 / 618 / 1,375 / 1,648 | identical, recomputed from kWh | match |
| 15 | Detection days by shape | 4,002 | 4,002 | match |
| 17 | Bills with a detected-load driver | 120 of 218 | 120 of 218 | match |
| 18 | Forecast snapshots servable | 9,883 of 35,812 | 9,883 of 35,812 | match |
| 20 | Driver size bands | 98 / 63 / 32 / 12 / 13 | identical, recomputed from NZD | match |
| 21 | Biggest driver | 96 / 50 / 35 / 24 / 13 | identical | match |
| 23 | Generation worth, 3/5/7 kW | 660·817 / 904·1,116 / 1,115·1,404 | identical (median) | match |
| 24 | Payback years, 3/5/7 kW | 7.4·13.6 / 9.0·16.6 / 10.0·18.9 | identical (median) | match |
| 26 | Adding an EV, median saving by plan | 435 / 0 / 0 | 435 / 0 / 0 | match |
| 27 | Moving charging, median saving by plan | 8 / 0 / −4 | 8 / 0 / −4 | match |
| 36 | Warning signs (all bars) | 36 | 36 signals | match |
| 37 | What the rule says (all bars) | 45 | 45 assessed, 27 with a signal | match |
| 38 | Households assessed | 45 on 2027-08-28 | 45 | match |
| 39 | Early warnings with a projected date | 0 | 0 outlook rows | match — see F3 |

**No card disagreed with its source.** Three apparent disagreements were my own
recompute, not the dashboard, and are recorded because each is a way to get a
validation wrong:

* `mart_daily_detected_load` holds a row per assessed ICP-day, not only per
  detection. Counting row presence gave 15,778 "detections" instead of 4,002.
  The predicate is `ev_half_hours > 0 OR timed_night_half_hours > 0`.
* Cards 17, 20 and 21 are restricted to `is_explainable` (218 of 1,175 bills).
  Omitting it moved every bucket and made `unexplained` look five times larger.
* Cards 23, 24, 26, 27 and 32 use the **median**, not the mean. The mean differs
  by up to 8 NZD and 0.4 years — close enough to look like agreement.

Cross-card consistency, which no single recompute can show:

* cards 4 and 5 are the same 2,580 runs on two dimensions, and both totals match;
* cards 2, 6, 7 and 15 all total 4,002 detection days;
* detection kinds are disjoint in this release — 2,568 EV days + 1,434 night-block
  days = 4,002, with **0** days carrying both;
* card 35's twelve-month totals for `icp_id` 1027 (6,203 kWh bought, 3,870 sold)
  equal card 33's row for the same household, and its two components
  (2,866–3,011 for the car, 3,192–3,336 for the rest of the house) bracket the
  6,203 bought;
* card 18's documented conjunct holds exactly: **0** snapshots are forecastable
  without clearing the seven-assessed-day rule.

Coverage: the detector assessed 15,778 of 16,032 cohort ICP-days in the window
(98.4%); the 254 it has no row for are spread across the whole window, not
clustered at either end.

Local time is grouped correctly: every time-of-day and per-day card converts with
`AT TIME ZONE 'Pacific/Auckland'` before truncating, so no run is attributed to
the wrong day or hour. The generated-cohort filter
(`end_date IS NULL AND account_id >= 1000000 AND icp_id IS NOT NULL`) is present
on every card whose population is the cohort.

Build-time resolution was re-run and agrees with what the cards draw:
`A → 1074, B → 1041, C → 1050, D → 1051, E → 1051`, demo day **8 January 2027**.
D and E landing on the same household is the documented collision that card 30
collapses into one row (see F1).

## 3. Criterion 3 — latest-generation filters, duplicates, staleness

| Table | Pin | Natural key | Duplicate groups | Generations |
|---|---|---|---|---|
| `kea.solar_estimate` | `LATEST_SOLAR` | `(account_id, plan_id, system_kw)` | 0 | 1 |
| `kea.ev_scenario` | `LATEST_EV` | `(account_id, scenario, charging_pattern, plan_id)` | 0 | 1 |
| `kea.hardship_assessment` | `LATEST_HARDSHIP` | `(account_id, assessed_on)` | 0 | 1 |
| `gen_audit.planted_load` | `LATEST_PLANT` | `(run_id, …)` | 0 | 1 |
| `kea.hardship_signal` | inherited over the assessment join | `(assessment_id, signal_code)` | 0 | – |
| `kea.hardship_outlook` | inherited over the assessment join | `(assessment_id)` | 0 (empty) | – |
| `kea.bill_forecast` | **deliberately none** | `(account_id, period_start, forecast_date)` | 0 | 1 |

* **No duplicates and no stale rows anywhere**, on every correct natural key.
* **Every pin is currently an unexercised no-op.** Exactly one generation exists
  in each append-only table, so `= (SELECT max(computed_at) …)` selects
  everything. The pins are right by inspection; today's data cannot prove it.
  The first table to gain a second generation is the first real test.
* `kea.bill_forecast` is unpinned **on purpose**: card 19 "The forecast model
  published" exists to reveal two coexisting `model_version`s, and a pin would
  hide exactly what it is there to show. It reads `hdd-linear-v3` alone.
* `kea.solar_estimate`'s key is `(account_id, plan_id, system_kw)` — 36 accounts
  × 3 sizes (3.0/5.0/7.0 kW) = 108 rows. An earlier pass of mine reported 36
  duplicate groups on `(account_id, plan_id)`; that was my wrong key, not a data
  defect. On the correct key there are none.

## 4. Criterion 4 — the solar-masked class, separately from `low_power`

Met, and quantified. Of 2,580 runs in the window:

| Published class | Runs | Share |
|---|---|---|
| wallbox-shaped (`high_power`) | 1,797 | 69.7% |
| portable-shaped (`low_power`) | 753 | 29.2% |
| **charger class unknown (solar-masked)** (NULL) | **30** | **1.2%** |

The 30 NULL-class runs carry their **own** label on cards 4, 5 and 30 and are
never counted as portable-shaped. detect-v2 withholds the class on a masked run
rather than guessing it, so a withheld class is not a missing EV. All 30 sit on a
single household (`icp_id` 1051), which is also why they appear in one bar on each
chart: 11:00 on card 4, "2 to 4 h" on card 5, and "cannot be told apart" on card
30. 62 runs are partly masked while keeping a class.

The builder carries a second label, plain `charger class unknown`, for a NULL
class on a household with no export register. It draws **0** runs in this release
— every unclassified run is solar-masked. The evaluation agrees from the other
direction: `low_power_claims_on_masked_runs = 0`.

## 5. Reconciling with `analysis/sprint4_evaluation/results/report.md`

Same release, different populations — not a mismatch.

* **Release.** The report records no release label (**F6**), so the tie is by
  time: it ran 2026-09-20 06:00 UTC, about two minutes after this release's
  05:58 publication timestamps. It therefore reads *this* release.
* **Population.** The report's detection sections score **11 planted** households
  against the generator's answers; the dashboard's detection cards cover the
  **45**-household cohort. The report's hardship section uses the same 45.
* **Method.** The report **replays 93 weekly dates** from 2025-11-22 to
  2027-08-28, re-running the rules at each. The dashboard shows the **one**
  published assessment, as of 2027-08-28. This single difference explains the
  dashboard's empty early-warning panel (**F3**) and means the report's
  `outlooks_not_after_their_date = 0` is a replay result, not a statement about
  the empty `kea.hardship_outlook` the dashboard reads.
* Figures that differ in kind rather than value: the report's
  `daily_bounds share_inside = 0.616` (MISS) is scored against planted answers
  and has no counterpart on the dashboard, which shows bounds without a truth to
  compare them to. `hardship median_lead_days = 18` vs a target of 21 (MISS) is
  likewise a replay measure.

## 6. Findings

**F1 — card 30's heading over-claims (wording, medium).** "The three generating
households: what each one publishes" is wrong twice over: the cohort has **8**
generating households, and the card shows **2** rows. Neither is an arithmetic
error — the card applies 3 masking rules and two of them select the same
household, which the builder documents and deliberately collapses into one row
wearing two reasons. The row values are correct. The heading is what a reader
believes. Suggested: *"Three masking rules, and the households they pick"*.
Renaming creates a new question and archives the old one, which is the intended
behaviour.

**F2 — card 18's stated interpretation, not its number (medium).** 9,883 of
35,812 servable is correct. The card's comment calls that fraction "the cost and
the benefit of the change in one number", but **32,948 (92%)** of snapshots clear
the new seven-assessed-day detection gate, and **23,065** are blocked by the
older gates instead (3,106 have no usable heating sensitivity or baseline, 2,122
no expected HDD, plus the prior-bill and completeness conditions). The v3
detection requirement is not what binds this fraction. Recommend rewording; the
number stays.

**F3 — the hardship early-warning panel is empty on demo day (medium, demo
risk).** Cards 37 ("early warning": 0) and 39 ("Early warnings with a projected
date": 0) are correct and `kea.hardship_outlook` is genuinely empty. The cause is
the **assessment date**, not the rule: an outlook is written only for
`payment_drift` or `first_shortfall`, and only when the projection reaches arrears
and the account is **not already in arrears**. The three planted gradual accounts
have onsets of 2026-06-22, 2026-07-29 and 2026-08-04 — more than a year before the
published as-of date of 2027-08-28 — so by then there is nothing ahead to warn
about. The capability works: the evaluation replay catches 3 of 3 gradual
accounts, 17 to 51 days before onset. Card 36 is honest about it, showing "a first
shortfall — 0" and "payments drifting later — 0" against "added in Sprint 4". For
the demo, either publish an assessment as-of a date the story supports, or say on
the page that the zero is the date's doing and point at the replay.

**F4 — card 31 cannot support the claim its title makes (medium).** "Detections
against masking" shows two bands: no panels (36 households) at 59.4 detected runs
per household-year, and panels with 26–50% of the year hidden (8 households) at
61.1. Three of its five bands are empty. More to the point, only **2 of those 8**
households have any masked run at all, so the "panels" band is mostly households
whose panels never hid a run — and the near-identical rates say nothing about
masking either way. Restricting the comparison to households with masked runs
would leave 2, which is not a comparison. This is evidence for the open decision
about the card: as drawn, it invites a conclusion the data cannot carry.

**F5 — a pin that works but nothing holds (low, test gap).**
`kea.hardship_signal` inherits `LATEST_HARDSHIP` over its join to
`kea.hardship_assessment`, and cards 36 and 37 both do this correctly. There is
no test asserting it, although `kea.hardship_outlook` has
`test_hardship_outlook_is_pinned_through_its_assessment`. Signals have 36 rows and
the outlook has none, so the untested inheritance is the one that currently
matters. Cheap to close: one test in the same shape as the existing one.

**F6 — the evaluation report names no release (low).**
`analysis/sprint4_evaluation/results/report.md` records a wall-clock run time but
no release label or `computed_at`, so relating it to a release means comparing
timestamps. Recording the release it read would make that tie explicit.

**F7 — informational.** Every `LATEST_*` pin is a no-op today (one generation
each); 0 duplicates on every correct natural key; 15,778 of 16,032 cohort
ICP-days assessed (98.4%); detection kinds disjoint (0 days carry both).

## 7. Reproducing this

    # the inventory (§1) -- no database, no Metabase session
    python3 analysis/metabase/inventory.py

    # every card's own query against the serving release (§2)
    docker run --rm -i --network kea_default --env-file .env \
      --add-host=host.docker.internal:host-gateway -v /path/to/cards.sql:/q.sql:ro \
      postgres:17.10 sh -c 'exec psql -X -f /q.sql "$KEA_PUBLISH_DSN"'

`cards.sql` is generated from the builder, so it cannot drift from the page:

    python3 - <<'PY'
    import importlib.util
    s = importlib.util.spec_from_file_location("m", "analysis/metabase/build_sprint4_dashboard.py")
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    cards = [it for _, items in m.SECTIONS for it in items if it["kind"] == "card"]
    print("\\pset pager off\n\\pset footer off")
    for i, c in enumerate(cards, 1):
        if c["db"] != m.SERVING_DB:
            continue
        print(f"\\echo '===== {i} [{c['display']}] {c['name']}'")
        print(c["sql"].rstrip().rstrip(";") + ";")
    PY

The independent recomputes of §2, the generation census of §3 and the class
counts of §4 are single queries against the same serving DSN; each is quoted in
full in the issue comment on #183.
