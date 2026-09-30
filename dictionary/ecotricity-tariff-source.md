# Historical tariff-source investigation

This file records the Ecotricity and Contact Energy research completed before
the final Sprint 3 product design. It is retained as a provenance and modelling
lesson, but **it no longer defines the active generated catalogue**.

As of 8 September 2026, the active LCL demo uses three Kea plans taken directly
from `docs/sprints/sprint-3/plans-1.png` and `plans-2.png`: Anytime Rate, Time of
Use and Free Weekends. Their authoritative specification is
`docs/sprints/sprint-3/plans-and-terms.md`; `02_catalogue.sql` implements that
specification. No Contact or Ecotricity product name or retail rate remains in
the active LCL offer set.

The research below remains useful for three conclusions:

- retail address quotes are network- and date-specific;
- current quote journeys do not establish historical tariff rates;
- a half-hourly wholesale product cannot be represented faithfully by a
  scalar tariff component.

Everything below this point is historical context.

## Historical Contact Energy investigation

Retrieved 5 Sep 2026 from Contact's join journey, for a Mount Eden, Auckland
address on the Vector network. It returned real published rates for **all four
plans and both user types**, `[PUBLISHED]`:

```
                     standard              low
  Basic Plan         25.1c  $2.801/day     29.6c  $1.80/day
  Good Nights        30.7c  $3.001         37.3c  $1.80
  Good Weekends      25.5c  $3.100         32.4c  $1.80
  Good Charge  day   29.5c  $3.101         35.4c  $1.80
               night 14.3c                17.7c
  EA levy             0.19c/kWh on every plan and both user types
```

**Which does not solve the provenance problem, and this file predicted that.**
The section below headed "why that last line is permanent" already recorded, on
3 Sep, that an address quote returns rates only for ONE network and only for
CURRENT and FORWARD periods. Both limits held:

* One network. Vector, because that is where the address is. The compact demo now
  materialises only Vector rather than inventing scaled copies for other networks.
* Current and forward. The cohort span now runs 2025-08-01 to 2027-08-27, and
  these rates are what you can sign up to today. The v1 plan version covering
  2025-04-01 to 2026-04-01 is still `[ASSUMED]` at v2 ÷ 1.06.

So the catalogue's provenance improved from mostly-assumed energy rates to
published ones on one network for the current version, and did not become
complete. Nothing publishes historical retail rates, because retailers advertise
what you can sign up to today.

### Historical Contact-derived solar terms

The previous Kea reference catalogue carried a solar buy-back shape for each of
the four Contact-derived plans. These were `[ASSUMED]` product-design terms,
not values inferred from the import-rate residual or claimed as historic
Ecotricity prices:

| Plan | Export term in `02_catalogue.sql` |
|---|---|
| Basic Plan | 8c/kWh at all times |
| Good Nights | unavailable — no export rate component |
| Good Weekends | 8c/kWh at all times |
| Good Charge | 25c/kWh weekdays 07:00–09:00 and 18:00–22:00; 8c otherwise |

They are explicit `flow_direction = 'export'` energy components. Good Charge
splits the shared energy-window tiling at 21:00 so its export peak can remain
continuous while its import price changes from standard to half-price; it does
not add an overlapping export-only window set.

**Two structural facts these rates settled** that no amount of derivation would
have, both now recorded in `02_catalogue.sql`:

1. Contact quotes ONE bundled variable rate. Energy and network arrive as a
   single c/kWh with only the EA levy broken out, so this catalogue's ENE/NET
   split is finer than the product's own presentation and energy is a residual.
2. Recovery for a discount window lands somewhere different for each user type.
   For that reason the compact demo keeps Standard User only; it does not derive
   or recommend a Low User copy by applying an uplift.

---

## The four plans

| Plan | Shape | Rates published? |
|---|---|---|
| `ecoANYTIME` | One rate 24/7, energy price fixed for 3 years | Only via an address quote |
| `ecoSAVER` | Time of use, seasonal | Only via an address quote |
| `ecoSOLAR` | Flat import, time-of-use buy-back | Only via an address quote |
| `ecoWHOLESALE` | **Spot** — half-hourly wholesale price | **Never. See below** |

---

## ecotricity.co.nz publishes almost no numbers

Fetched and checked. The public pricing page carries exactly three figures:

- `21c peak / 16c off peak` solar buy-back — **already in the catalogue** as
  `[PUBLISHED]`
- *"up to 50% cheaper off peak"* — a relationship, not a rate
- *"as low as 1c/kWh, with uncapped price peaks"* — a marketing floor

Everything else is behind an address:

> As pricing is specific to your lines company and meter type, simply enter your
> address in our onboarding platform to see what price plans are available.

So the `[ASSUMED]` markers are a property of the source, not of the effort spent
looking. There is no page anywhere that lists ecoANYTIME's c/kWh.

---

## The address quote — what it gives, and its two hard limits

`onboarding.ecotricity.co.nz` returns real rates for a real ICP. Checked with
**636 Mount Eden Road, Mount Eden, Auckland 1024** (ICP `0583332248LCD41`,
network **Vector**):

| | |
|---|---|
| User type | Low / Standard, and they differ: `$1.5500/day` vs `$2.7920/day` |
| Seasons | Six periods, 04/2026 through 01–03/2027 |
| Day type | Weekday / Weekend, separately |
| Rates | Import **and** export, per half-hour band |
| GST | Toggle; displayed rates are ex-GST |

One harvested sample — Low ecoSAVER, Vector, Sept 2026, weekday, ex-GST:

```
import   12am–7am 27.12c   7am–11am 28.50c   11am–5pm 27.12c
         5pm–9pm  38.97c   9pm–10pm 37.59c   10pm–12am 27.12c
export   5pm–10pm −18.98c  otherwise −13.74c
```

⚠️ **The rates are baked into server-rendered PNGs**, not the DOM and not any
JSON API. Verified: piercing all four shadow roots yields 72 text nodes and zero
rate strings. The images are addressable and self-describing —
`VECT-ARHLU-VECT_OFPK_STD_251001_T3-RESIDENTIAL-20260901-20260930-WD-NOGST.png`
— but each combination has to be read from a picture. Roughly 96 images per
network.

### Limit 1 — one network per address

The quote is per ICP, so this address gives **Vector only**. That now matches the
generated cohort. The nine fixed regression fixtures retain other networks but do
not use these generated plan variants.

### Limit 2 — the dates do not overlap, and this is the one that bites

The tool publishes **current and forward** pricing. It does not publish history.

```
cohort span   |=================================|
            2025-08                        2027-08
quote tool              |===================|
                     2026-04             2027-03
```

The network now matches, but the quote still covers only the current/forward
part of the generated span and none of the earlier plan version. Retailers
publish what you can sign up to today, not what they charged historically.
This is also why Bill A's plan version cannot be recovered — see below.

---

## ecoWHOLESALE has no rate card, and the current model is the wrong shape

Not "we could not find it". A spot plan's price **is** the half-hourly wholesale
price at the customer's Grid Exit Point, which is a market outcome rather than a
tariff. Ecotricity's own explainer confirms the mechanism and publishes no
numbers: energy is the half-hourly spot price, varying by GXP; distribution
losses are explicitly excluded; billing adds *"a small admin fee"* whose amount,
unit and basis are all unstated, as are lines charges, levies and any daily
fixed charge.

⚠️ **`02_catalogue.sql` models it as banded TOU** — *"spot, so a wide
peak-to-off-peak spread"*, 0.35 / 0.12 / 0.04. That is not a spot plan. It is a
three-band tariff wearing a spot plan's name, and it can never produce either the
sub-2c troughs or the uncapped peaks the product is defined by. The number being
`[ASSUMED]` is fine; the **shape** being wrong is not, and it is recorded here
because nothing else would notice.

### The energy component *is* obtainable, for the whole span

The Electricity Authority's EMI publishes final half-hourly prices per node.
Verified by downloading, not by reading the documentation:

```
TradingDate,TradingPeriod,PointOfConnection,DollarsPerMegawattHour
2026-08-25,1,ABY0111,37.11
```

| | |
|---|---|
| Grain | one row per node × trading period — ~246 nodes × 48 periods |
| Monthly archive | `ByMonth/YYYYMM_FinalEnergyPrices.csv`, **1996-10 → 2026-07** |
| Daily files | `YYYYMMDD_FinalEnergyPrices.csv`, at least 2025-12 → 2026-08-28 |
| Access | free, no login |

Both ends of the cohort span confirmed: `202409` holds 346,559 rows and gives
`PEN0331` exactly 48 trading periods on 2024-09-18; `20260828` exists as a daily
file.

⚠️ **`202512_FinalEnergyPrices.csv` is missing** — it returns HTML while
202411 / 202412 / 202501 / 202506 / 202601 / 202606 / 202607 all return CSV.
December 2025 must come from the 31 daily files. Check these by CONTENT, not by
HTTP status: the server answers `200` with an HTML error page, which is what
made `20240918` look like an available daily file when no such file exists.

All four regions have GXPs:

```
Auckland     PEN0331 PEN0221 PEN1101 OTA0221 OTA2201 HEN0331 MNG0331 ALB0331 WIR0331
Waikato      HAM0111 HAM0331 HAM0551 HAM2201 TWH0331 WKM2201
Wellington   HAY0111 HAY0331 HAY1101 CPK0111 CPK0331 WIL0331 TAK0331
Christchurch ISL0331 ISL0661 ISL2201 BRY0661 SBK0661
```

Filtering the span to four GXPs is 710 × 48 × 4 = **136,320 rows** out of ~8.4M.
Note this is the **energy** component only; the admin fee, lines charges and
levies remain unpublished and would stay `[ASSUMED]`.

---

## The two printed invoices

`data/sample/` holds two pages of a real Ecotricity bill guide. They are the
`[BILL]` provenance, and they are **fully mined** — there is nothing left in them
to extract.

**Page 2 — Bill B**, ICP `0165841877LCC54`, 08–31 Aug 2024, plan
`NZSZ_ZERO_STD_FXTOU_240901_T1`, `$50.54`. Carries a complete rate table:
network peak `0.1361` / off-peak `0.0378` / daily `0.6` / transmission `0.0235`;
energy peak `0.26` / off-peak `0.10` / shoulder `0.14`; export at the same three
rates; EA levy `0.0013`; and line losses of 1.74 kWh on 34.08 kWh = **5.11%**.

**Page 1 — Bill A**, `$248.07`, issued 08 Apr 2023. Totals only:

```
charges ex-GST 215.71   GST 32.36   total 248.07
late fee 37.21 → 285.28   opening 232.05, paid 16-Mar 232.05
electricity delivered 1034.665 kWh   avg $8.00/day incl GST
```

⚠️ **Bill A's line detail was never published.** Page 1 says *"Electricity
charges (please see over for details)"* and the "over" we hold is page 2 — a
different bill, on a different plan, a year later. Design §10.3 already states
this; the images confirm it rather than fixing it.

Neither invoice is on an `ECO_*` plan, so **the sample images cannot supply the
four plans' rates**. What page 1 does supply is nearly every column of
`src_billing.invoices` for Bill A, transcribed.

**Bill A is no longer a reconciliation criterion, and it was not deleted either.**
The Warehouse step's exit criterion was narrowed to Bill B on 3 Sep 2026: reproducing Bill A
would mean inventing a tariff and 31 days of readings that sum to `$215.71` and
then reconciling against the invention, which tests the invention. What Bill A
still is, and what makes deleting it wrong, is **evidence**: its published group
totals — Network `$75.55`, Energy `$116.55`, **Other `$23.61`** — are what
`02_catalogue.sql` back-solves the non-levy `0.7182/day` from, and that rate is
live on all 8 generated plan versions as `0.450000` metering + `0.268200` admin.

### Bill A's plan code is real

Design §10.3 names it `VECT_OFPK_STD_140520`. The live quote tool returns
`VECT_OFPK_STD_251001_T3` — Vector / Off-Peak / Standard / effective 2025-10-01.
Same convention exactly, so `140520` is a genuine 2014-05-20 version, not an
invention. It is simply not offered today, and the quote tool only ever shows the
current version, so it cannot be recovered from any live source.

---

## The provenance ladder, and where each rate sits

| Marker | Meaning |
|---|---|
| `[PUBLISHED]` | printed on ecotricity.co.nz |
| `[BILL]` | transcribed from a real invoice in `data/sample/` |
| `[DERIVED]` | back-solved from a printed total |
| `[ASSUMED]` | plausible, internally consistent, **not evidence** |

Deliberately no counts here. `02_catalogue.sql` is the register — grepping it is
the answer, and a tally copied into prose goes stale the first time a rate moves
(including from prose that merely *mentions* a marker, as this file does).

The distinction lives in comments rather than a column because it is a fact about
provenance, not about pricing — and because a column would invite someone to
filter on it.

**What could still move up the ladder**, in order of effort:

1. ecoWHOLESALE's *shape*, from banded TOU to a spot join — no new data needed to
   record the error; EMI data needed to fix it
2. Three historical Ecotricity plans × Vector, from the address quote — 96 PNG reads
3. Nothing recovers pre-2026-04 retail rates, or Bill A's 2014 tariff. Those stay
   `[ASSUMED]` permanently, and no amount of searching changes it
