# data/ — Data Engineering / Data Analysis team

The meter data, what it means, and the analysis done on it.

## Why the analytics code is not in here

`app/analytics/` and `app/db/` live in `services/api/` because the API imports them at
request time. The Data team **owns those paths** via `.github/CODEOWNERS` — ownership is
a review rule, not a directory. Every headline number in the pitch comes out of
`app/analytics/engine.py`, and the Data team reviews changes to it.

This directory is for the things that are not runtime code.

| Path | What goes in it |
|---|---|
| `raw/` | Source extracts as received. **Never edited by hand** — if it needs fixing, the fix is a script |
| `analysis/` | Notebooks and one-off scripts. Name them `YYYY-MM-DD-what-it-answers` |
| `dictionary/` | What each column means, its units, and its known quirks |

## Rules

**Every figure traces to a source.** This is the brief's standing rule and it applies to
a notebook cell exactly as much as to a slide. If a number leaves this directory and
lands in a deck or a demo line, someone must be able to follow it back to the row it
came from.

**Dollar figures are illustrative.** Kea's real tariff is not public. Everything costs
out at **30c/kWh** and any surface showing a `$` has to say so.

**Never name an appliance.** Half-hourly interval data cannot identify appliances. No
amount of clustering changes that — the refusal is deliberate and it is a demo line, not
a limitation to hide.

**No respondent data in this repo.** The customer survey contains data collected for
analysis, not publication. Aggregates and derived figures are fine; raw responses are
not. See the absolute rule in [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

## The July measurement

The analytics engine is validated against real measured July values, not fixtures —
`services/api/tests/data/july_2026_daily.json` is the reference, and
`test_analytics_july.py` fails if the engine drifts from it. Treat that file as a
fixed point: if a change makes that test fail, the change is wrong until proven
otherwise.

## Related

- [`../docs/erd.md`](../docs/erd.md) — the data model
- [`../docs/adr/0006-postgres-in-sprint-2.md`](../docs/adr/0006-postgres-in-sprint-2.md) — why Postgres
- [`../docs/teams/data.md`](../docs/teams/data.md) — team onboarding
