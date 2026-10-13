# Tariff sources — the plan catalogue and where its rates come from

`src_catalogue` holds the plans every generated bill is priced on. This document records what the
active catalogue is, the provenance of each rate, and what the tariff research established about what
can and cannot be sourced. The implementation is
[`pipeline/scripts/gen_source/02_catalogue.sql`](../pipeline/scripts/gen_source/02_catalogue.sql),
which asserts its own prices.

**Dollar figures are illustrative.** The three plans are product-design inputs, not any retailer's
current or historical offer.

## 1. The active catalogue

The generated cohort has one network (Vector), one customer category (Standard User) and three plans.
Customer-facing prices, GST inclusive:

| Plan | Code | Import | Daily charge |
|---|---|---|---|
| Anytime Rate | `KEA_ANY_VECT_STA` | 32.5c/kWh at all times | $1.45 |
| Time of Use | `KEA_TOU_VECT_STA` | 39.0c peak (07–11, 17–21) · 27.0c off-peak (11–17, 21–23) · 18.0c night (23–07) | $1.50 |
| Free Weekends | `KEA_WKND_VECT_STA` | 36.5c, free Sat & Sun 09:00–17:00 | $1.55 |

- **Solar export:** a flat 8c/kWh on all three plans, so an invented export difference never decides
  which plan wins.
- **Term credits:** $10/month on a 6-month term, $25/month on 12 months.
- **Two versions per plan** (from 2025-04-01 and 2026-04-01) with identical prices. They exist so
  invoices straddling the boundary exercise period-end version resolution (design §2.1.2) without
  inventing price history.

**How the stored rates are derived.** Source bill lines are stored ex-GST and import energy carries a
5.11% line-loss uplift. Stored import rates are therefore the displayed rate ÷ 1.15 ÷ 1.0511 (for
example, Anytime 32.5c → 0.268869), so that after the biller applies losses and GST, one metered kWh
lands back on the displayed price. Daily charges are divided by GST only. Network charges are bundled
into energy, so the network scope carries a zero-rated all-day band that keeps the charge-group
structure intact.

The nine hand-built fixtures keep their own historical tariffs, outside the generated id range.

## 2. The provenance ladder

| Marker | Meaning | Used for |
|---|---|---|
| `[DESIGN]` | a product-design input | the three plans' import prices, windows, daily charges and terms |
| `[BILL]` | transcribed from a real printed invoice | the 5.11% line-loss factor; Bill B's tariff |
| `[DERIVED]` | back-solved from a printed or designed total | the stored ex-GST, pre-loss rates |
| `[ASSUMED]` | plausible and internally consistent, **not evidence** | the 8c export rate; identical prices across the two versions |

Markers live in `02_catalogue.sql` comments, which is the register: grep it rather than trusting a
count in prose. They are comments rather than a column because they describe provenance, not pricing,
and a column would invite filtering on it.

## 3. The two printed invoices

[`sample/`](../sample/) holds two pages of a retailer's public "how to read your bill" guide. They are the
`[BILL]` evidence, and they are fully mined.

**Bill B** (page 2), 8–31 Aug 2024, TOU with solar, plan `NZSZ_ZERO_STD_FXTOU_240901_T1`, **$50.54** ex
GST. It carries a complete rate table:

| Component | Rate |
|---|---|
| Network peak / off-peak | 0.1361 / 0.0378 per kWh |
| Network daily · transmission | 0.60 per day · 0.0235 per kWh |
| Energy peak / off-peak / shoulder | 0.26 / 0.10 / 0.14 per kWh (export at the same rates) |
| Electricity Authority levy | 0.0013 per kWh |
| Line losses | 1.74 kWh on 34.08 kWh = **5.11%** |

Bill B is the pipeline's reconciliation target: it reproduces from metered half-hours to the cent, with
one documented cent on a levy line that contradicts itself (design §10.3).

**Bill A** (page 1), issued 8 Apr 2023, **$248.07**: totals only (ex GST $215.71, 1,034.665 kWh). Its
line detail was never published, so it is kept as documented evidence, not a reconciliation target.
Its plan code `VECT_OFPK_STD_140520` follows the real code convention (a 2014-05-20 version), and no
live source can recover that tariff.

## 4. What tariff research established

Before the catalogue became a design input, real retail rates were researched. Three findings shaped
the decision and are worth keeping.

**Published pricing pages carry almost no rates.** Retailers publish a solar buy-back, relative
claims ("up to 50% cheaper off-peak") and marketing floors. Everything else sits behind an address
quote, because pricing depends on the lines company and meter.

**An address quote has two hard limits:**
- **One network per address.** The quote is per ICP.
- **Current and forward periods only.** Retailers publish what you can sign up to today, not what they
  charged before, so no source covers a historical span.

Even where quotes work, the rates are server-rendered images (about 96 per network), not text or an
API. Some quotes bundle energy and network into one rate with only the levy broken out, so any
energy/network split is finer than the product presents.

**Conclusion:** historical retail rates cannot be sourced, so the catalogue is an explicit design input
with every assumption marked, rather than a partial reconstruction presented as fact.

## 5. Spot-priced plans cannot be a scalar tariff

A wholesale ("spot") plan's energy price **is** the half-hourly wholesale price at the customer's grid
exit point: a market outcome, not a tariff. A scalar `rate_components.unit_rate` can never represent it,
and modelling one as banded TOU would produce neither its troughs nor its uncapped peaks.

If such a plan is ever needed, the energy component is obtainable: the Electricity Authority's EMI
publishes final half-hourly prices per node, free and without a login.

| | |
|---|---|
| Grain | one row per node × trading period (~246 nodes × 48) |
| Archive | monthly `ByMonth/YYYYMM_FinalEnergyPrices.csv`, plus daily files |
| Format | `TradingDate,TradingPeriod,PointOfConnection,DollarsPerMegawattHour` |

Example grid exit points per region: Auckland `PEN0331`, Waikato `HAM0331`, Wellington `HAY0331`,
Christchurch `ISL0331`. **Check files by content, not HTTP status:** the server answers `200` with an
HTML error page for missing files (December 2025's monthly file is one). The admin fee, lines charges
and levies would remain `[ASSUMED]`.
