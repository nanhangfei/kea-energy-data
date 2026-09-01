-- =====================================================================
-- src_catalogue -- the four Ecotricity residential plans
--
-- Ecotricity publishes four plans for the home (ecotricity.co.nz/for-home):
--
--   ecoANYTIME     one rate 24/7, fixed for three years. Sold to homes
--                  without a smart meter, so it cannot be time-of-use.
--   ecoSAVER       time-of-use; "up to 50% cheaper off peak, depending
--                  on your network".
--   ecoSOLAR       flat import rate, time-of-use buy-back.
--   ecoWHOLESALE   spot; "as low as 1c/kWh, with uncapped price peaks".
--
-- WHAT IS REAL AND WHAT IS NOT. Retail rates are quoted per delivery
-- address, so only some of this could be sourced. Marked inline as:
--
--   [PUBLISHED]  printed on ecotricity.co.nz
--   [BILL]       transcribed from a real invoice (see data/sample/)
--   [ASSUMED]    plausible, internally consistent, NOT evidence
--
-- The distinction is kept in the comments rather than a column because
-- it is a fact about provenance, not about pricing. data/dictionary/
-- carries the same list.
--
-- WHAT THIS EXERCISES that the nine fixtures cannot:
--
--   * TWO VERSIONS PER PLAN CODE. Every fixture plan has a single
--     open-ended version, so the temporal resolution the platform doc
--     calls "the dangerous one" (valid_from <= period_end < valid_to)
--     has nothing to resolve and its test passes vacuously.
--   * A COMPONENT-LEVEL rate change INSIDE a plan version -- the EA
--     levy moves on 2025-07-01, three months after the version boundary.
--     rate_components carries its own valid_from precisely for this,
--     and nothing currently uses it.
--   * A WEEKDAY-ONLY WINDOW. Every fixture window is all-week, so a
--     days_csv parsed with position() instead of string_to_array()
--     would band every weekend wrongly and no test would fail.
--   * user_category = 'LOW'. A CHECK value nothing in the repo produces.
--
-- Ids >= 1,000,000 so a re-run's DELETE cannot reach a fixture
-- (oid()-allocated fixture ids are <= 339,999) and does not have to
-- dodge their nine-row assertions.
--
--   plan_id   = 1000000 + shape*100000 + network*10000 + usercat*1000 + version
--   window_id = plan_id*100 + seq
--   component_id = plan_id*100 + seq
--
-- Run after 01_schemas.sql, before 03_reference.sql.
-- =====================================================================

BEGIN;

DELETE FROM src_catalogue.rate_components WHERE plan_id >= 1000000;
DELETE FROM src_catalogue.tou_windows     WHERE plan_id >= 1000000;
DELETE FROM src_catalogue.price_plans     WHERE plan_id >= 1000000;


-- ---------------------------------------------------------------------
-- 1. The plans: 4 shapes x 4 networks x 2 user categories x 2 versions.
--
-- The code is stable across versions -- UNIQUE(code, valid_from) is what
-- makes a version a version. Version 1 closes exactly where version 2
-- opens, so no period falls in both and none falls in neither.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _shape(sh int, shape_code text, plan_name text, is_tou boolean) ON COMMIT DROP;
INSERT INTO _shape VALUES
    (1, 'ANYT', 'ecoANYTIME',   false),  -- flat energy; network is still banded
    (2, 'SAVR', 'ecoSAVER',     true),
    (3, 'SOLR', 'ecoSOLAR',     true),   -- flat import, banded buy-back
    (4, 'WHSL', 'ecoWHOLESALE', true);

CREATE TEMP TABLE _net(nt int, network_code text, factor numeric) ON COMMIT DROP;
INSERT INTO _net VALUES
    (1, 'VECT', 1.00),   -- [BILL] Vector rates are transcribed; factor 1
    (2, 'ORON', 0.88),   -- [ASSUMED] the other three are scaled variants
    (3, 'WELL', 1.06),
    (4, 'AURA', 1.12);

CREATE TEMP TABLE _uc(uc int, user_category text) ON COMMIT DROP;
INSERT INTO _uc VALUES (1, 'LOW'), (2, 'STANDARD');

CREATE TEMP TABLE _ver(vn int, valid_from date, valid_to date) ON COMMIT DROP;
INSERT INTO _ver VALUES
    (1, DATE '2024-08-01', DATE '2025-04-01'),
    (2, DATE '2025-04-01', NULL);

INSERT INTO src_catalogue.price_plans
    (plan_id, code, name, network_code, user_category, is_tou,
     ppd_percent, valid_from, valid_to, active)
OVERRIDING SYSTEM VALUE
SELECT 1000000 + s.sh*100000 + n.nt*10000 + u.uc*1000 + v.vn,
       format('ECO_%s_%s_%s', s.shape_code, n.network_code, left(u.user_category, 3)),
       format('%s (%s, %s user)', s.plan_name, n.network_code, initcap(u.user_category)),
       n.network_code, u.user_category, s.is_tou,
       0, v.valid_from, v.valid_to, v.valid_to IS NULL
FROM _shape s CROSS JOIN _net n CROSS JOIN _uc u CROSS JOIN _ver v
ON CONFLICT (code, valid_from) DO UPDATE SET
    name = EXCLUDED.name, network_code = EXCLUDED.network_code,
    user_category = EXCLUDED.user_category, is_tou = EXCLUDED.is_tou,
    valid_to = EXCLUDED.valid_to, active = EXCLUDED.active;


-- ---------------------------------------------------------------------
-- 2. TOU windows.
--
-- GAPLESS AND NON-OVERLAPPING per (plan_id, charge_scope) -- asserted at
-- the end of this file. An uncovered half-hour is not an error anywhere
-- in the schema: it is simply never priced, and the invoice still adds
-- up. That is the failure this constraint exists to prevent.
--
-- Network bands are the lines company's and apply whatever the retail
-- plan is, which is why ecoANYTIME has flat ENERGY but banded NETWORK.
-- One plan, two different band sets -- the per-charge-group scoping the
-- platform doc calls out, and the reason charge_scope is on this table.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _win(sh int, charge_scope text, band_label text,
                       days_csv text, tf time, tt time, seq int) ON COMMIT DROP;

-- NET: identical across all four shapes.  [BILL] Vector's 2-band shape.
INSERT INTO _win
SELECT s.sh, w.* FROM _shape s, (VALUES
    ('NET','PEAK',    '1,2,3,4,5,6,7', TIME '07:00', TIME '11:00', 1),
    ('NET','PEAK',    '1,2,3,4,5,6,7', TIME '17:00', TIME '21:30', 2),
    ('NET','OFFPEAK', '1,2,3,4,5,6,7', TIME '00:00', TIME '07:00', 3),
    ('NET','OFFPEAK', '1,2,3,4,5,6,7', TIME '11:00', TIME '17:00', 4),
    ('NET','OFFPEAK', '1,2,3,4,5,6,7', TIME '21:30', TIME '24:00', 5)
) AS w(charge_scope, band_label, days_csv, tf, tt, seq);

INSERT INTO _win VALUES
    -- ecoANYTIME: one rate 24/7. A single window, trivially gapless.
    (1,'ENE','ALLDAY',  '1,2,3,4,5,6,7', TIME '00:00', TIME '24:00', 11),

    -- ecoSAVER: peak on WEEKDAYS ONLY; the weekend daytime is shoulder.
    -- This is the only weekday-restricted window in the catalogue, and
    -- the only thing that can catch a days_csv parsed with position().
    (2,'ENE','PEAK',     '1,2,3,4,5',    TIME '07:00', TIME '11:00', 11),
    (2,'ENE','PEAK',     '1,2,3,4,5',    TIME '17:00', TIME '21:00', 12),
    (2,'ENE','SHOULDER', '1,2,3,4,5',    TIME '11:00', TIME '17:00', 13),
    (2,'ENE','SHOULDER', '1,2,3,4,5',    TIME '21:00', TIME '23:00', 14),
    (2,'ENE','SHOULDER', '6,7',          TIME '07:00', TIME '23:00', 15),
    (2,'ENE','OFFPEAK',  '1,2,3,4,5,6,7',TIME '00:00', TIME '07:00', 16),
    (2,'ENE','OFFPEAK',  '1,2,3,4,5,6,7',TIME '23:00', TIME '24:00', 17),

    -- ecoSOLAR: the plan is "flat import, time-of-use buy-back", so the
    -- bands exist for the EXPORT side. Import is expressed as the same
    -- rate in both bands rather than as one window, which keeps the
    -- gapless-per-scope invariant true and still prices import flat.
    (3,'ENE','PEAK',     '1,2,3,4,5,6,7',TIME '07:00', TIME '11:00', 11),
    (3,'ENE','PEAK',     '1,2,3,4,5,6,7',TIME '17:00', TIME '21:00', 12),
    (3,'ENE','OFFPEAK',  '1,2,3,4,5,6,7',TIME '00:00', TIME '07:00', 13),
    (3,'ENE','OFFPEAK',  '1,2,3,4,5,6,7',TIME '11:00', TIME '17:00', 14),
    (3,'ENE','OFFPEAK',  '1,2,3,4,5,6,7',TIME '21:00', TIME '24:00', 15),

    -- ecoWHOLESALE: see the limitation noted at the foot of this file.
    (4,'ENE','PEAK',     '1,2,3,4,5,6,7',TIME '07:00', TIME '11:00', 11),
    (4,'ENE','PEAK',     '1,2,3,4,5,6,7',TIME '17:00', TIME '21:00', 12),
    (4,'ENE','SHOULDER', '1,2,3,4,5,6,7',TIME '11:00', TIME '17:00', 13),
    (4,'ENE','SHOULDER', '1,2,3,4,5,6,7',TIME '21:00', TIME '23:00', 14),
    (4,'ENE','OFFPEAK',  '1,2,3,4,5,6,7',TIME '00:00', TIME '07:00', 15),
    (4,'ENE','OFFPEAK',  '1,2,3,4,5,6,7',TIME '23:00', TIME '24:00', 16);

INSERT INTO src_catalogue.tou_windows
    (window_id, plan_id, charge_scope, band_label, days_csv, time_from, time_to)
OVERRIDING SYSTEM VALUE
SELECT p.plan_id*100 + w.seq, p.plan_id, w.charge_scope, w.band_label,
       w.days_csv, w.tf, w.tt
FROM src_catalogue.price_plans p
JOIN _win w ON w.sh = (p.plan_id - 1000000) / 100000
WHERE p.plan_id >= 1000000;


-- ---------------------------------------------------------------------
-- 3. Rate components.
--
-- Provenance, in one place:
--
--   [BILL]  Vector network peak 0.1361 / off-peak 0.0378, daily 0.6000,
--           transmission 0.0235, energy 0.2600 / 0.1400 / 0.1000, export
--           at the import rate, EA levy 0.0013 -- all transcribed from
--           the invoice modelled in data/sample/01-*.sql, which the
--           rating step reproduces to $50.54.
--   [PUBLISHED] ecoSOLAR import 18c (Low User) / 17c (Standard User),
--           export 21c peak / 16c off-peak, from ecotricity.co.nz.
--   [DERIVED] the 0.7182/day of non-levy "Other" charges, back-solved
--           from a real invoice: $23.61 over 31 days less 1034.665 kWh
--           at the 0.0013 levy. Ecotricity's bill guide names the two
--           components (metering, and admin & climate-positive
--           certification); how the 0.7182 splits BETWEEN them is
--           [ASSUMED] and does not change any total.
--   [ASSUMED] everything else. Retail rates are quoted per address, so
--           the other three networks, the Low User variants and the
--           version-2 uplift are plausible, not sourced.
-- ---------------------------------------------------------------------

-- Network, per kWh. VECT is the transcribed base; _net.factor scales it.
CREATE TEMP TABLE _netk(uc int, vn int, band_label text, rate numeric, seq int) ON COMMIT DROP;
INSERT INTO _netk VALUES
    (2,1,'PEAK',    0.136100, 1),   -- [BILL]
    (2,1,'OFFPEAK', 0.037800, 2),   -- [BILL]
    (1,1,'PEAK',    0.149700, 1),   -- [ASSUMED] low user pays more per kWh,
    (1,1,'OFFPEAK', 0.041600, 2),   --           less per day
    (2,2,'PEAK',    0.144300, 1),   -- [ASSUMED] +6% at the 2025-04-01 version
    (2,2,'OFFPEAK', 0.040100, 2),
    (1,2,'PEAK',    0.158700, 1),
    (1,2,'OFFPEAK', 0.044100, 2);

-- Network, per day and the un-banded transmission charge.
CREATE TEMP TABLE _netf(uc int, vn int, component_desc text, rate_uom text,
                        rate numeric, seq int) ON COMMIT DROP;
INSERT INTO _netf VALUES
    (2,1,'VECT + Daily charges',  'DAY', 0.600000, 3),   -- [BILL]
    (1,1,'VECT + Daily charges',  'DAY', 0.300000, 3),   -- [ASSUMED]
    (2,2,'VECT + Daily charges',  'DAY', 0.636000, 3),
    (1,2,'VECT + Daily charges',  'DAY', 0.318000, 3),
    (2,1,'Transmission Charges',  'KWH', 0.023500, 4),   -- [BILL]
    (1,1,'Transmission Charges',  'KWH', 0.023500, 4),
    (2,2,'Transmission Charges',  'KWH', 0.024900, 4),
    (1,2,'Transmission Charges',  'KWH', 0.024900, 4);

-- Energy, per kWh, by plan shape. Export rows carry flow_direction
-- 'export'; the rating step signs their line amount negative.
CREATE TEMP TABLE _ene(sh int, uc int, vn int, band_label text, flow text,
                       component_desc text, rate numeric, seq int) ON COMMIT DROP;
INSERT INTO _ene VALUES
    -- ecoANYTIME: one rate, 24/7, fixed three years.
    --
    -- [DERIVED] from the bill, not guessed. The first version of this
    -- used 0.28/0.26, which made the FLAT plan 29% dearer per kWh than
    -- the time-of-use one -- visible only by comparing effective rates
    -- across plans, since each bill was internally consistent.
    --
    -- The reconciliation bill's own band split gives the blended energy
    -- rate a time-of-use customer actually paid:
    --
    --   (34.08*0.26 + 71.39*0.14 + 74.96*0.10) / 180.43 = 0.1461
    --
    -- A fixed plan prices a little above that blend, because the
    -- retailer carries the shape risk the customer no longer does.
    -- Low User pays more per kWh and less per day, as everywhere here.
    (1,1,1,'ALLDAY','import','Anytime Usage', 0.165000, 11),
    (1,2,1,'ALLDAY','import','Anytime Usage', 0.155000, 11),
    (1,1,2,'ALLDAY','import','Anytime Usage', 0.174900, 11),
    (1,2,2,'ALLDAY','import','Anytime Usage', 0.164300, 11),

    -- ecoSAVER: the shape the reconciliation bill was rated on.
    (2,2,1,'PEAK',    'import','Peak Usage',     0.260000, 11),  -- [BILL]
    (2,2,1,'SHOULDER','import','Shoulder Usage', 0.140000, 12),  -- [BILL]
    (2,2,1,'OFFPEAK', 'import','Off Peak Usage', 0.100000, 13),  -- [BILL]
    (2,1,1,'PEAK',    'import','Peak Usage',     0.270000, 11),  -- [ASSUMED]
    (2,1,1,'SHOULDER','import','Shoulder Usage', 0.150000, 12),
    (2,1,1,'OFFPEAK', 'import','Off Peak Usage', 0.110000, 13),
    (2,2,2,'PEAK',    'import','Peak Usage',     0.275600, 11),
    (2,2,2,'SHOULDER','import','Shoulder Usage', 0.148400, 12),
    (2,2,2,'OFFPEAK', 'import','Off Peak Usage', 0.106000, 13),
    (2,1,2,'PEAK',    'import','Peak Usage',     0.286200, 11),
    (2,1,2,'SHOULDER','import','Shoulder Usage', 0.159000, 12),
    (2,1,2,'OFFPEAK', 'import','Off Peak Usage', 0.116600, 13),
    -- ...and its buy-back, at the import rate.        [BILL]
    (2,2,1,'PEAK',    'export','Export On-Peak',       0.260000, 21),
    (2,2,1,'SHOULDER','export','Export Shoulder-Peak', 0.140000, 22),
    (2,2,1,'OFFPEAK', 'export','Export Off-Peak',      0.100000, 23),
    (2,1,1,'PEAK',    'export','Export On-Peak',       0.270000, 21),
    (2,1,1,'SHOULDER','export','Export Shoulder-Peak', 0.150000, 22),
    (2,1,1,'OFFPEAK', 'export','Export Off-Peak',      0.110000, 23),
    (2,2,2,'PEAK',    'export','Export On-Peak',       0.275600, 21),
    (2,2,2,'SHOULDER','export','Export Shoulder-Peak', 0.148400, 22),
    (2,2,2,'OFFPEAK', 'export','Export Off-Peak',      0.106000, 23),
    (2,1,2,'PEAK',    'export','Export On-Peak',       0.286200, 21),
    (2,1,2,'SHOULDER','export','Export Shoulder-Peak', 0.159000, 22),
    (2,1,2,'OFFPEAK', 'export','Export Off-Peak',      0.116600, 23),

    -- ecoSOLAR. Import is FLAT: the same rate in both bands, which is
    -- how a flat rate is expressed on a table whose windows must tile
    -- the day. Version 2 is the currently published pricing, and the
    -- move of export off the import rate onto a banded 21c/16c is a
    -- REAL rate change, not a synthetic one.
    (3,1,1,'PEAK',    'import','Anytime Usage', 0.175000, 11),  -- [ASSUMED]
    (3,1,1,'OFFPEAK', 'import','Anytime Usage', 0.175000, 12),
    (3,2,1,'PEAK',    'import','Anytime Usage', 0.165000, 11),
    (3,2,1,'OFFPEAK', 'import','Anytime Usage', 0.165000, 12),
    (3,1,1,'PEAK',    'export','Solar Buy-Back',0.165000, 21),
    (3,1,1,'OFFPEAK', 'export','Solar Buy-Back',0.165000, 22),
    (3,2,1,'PEAK',    'export','Solar Buy-Back',0.165000, 21),
    (3,2,1,'OFFPEAK', 'export','Solar Buy-Back',0.165000, 22),
    (3,1,2,'PEAK',    'import','Anytime Usage', 0.180000, 11),  -- [PUBLISHED]
    (3,1,2,'OFFPEAK', 'import','Anytime Usage', 0.180000, 12),  -- [PUBLISHED]
    (3,2,2,'PEAK',    'import','Anytime Usage', 0.170000, 11),  -- [PUBLISHED]
    (3,2,2,'OFFPEAK', 'import','Anytime Usage', 0.170000, 12),  -- [PUBLISHED]
    (3,1,2,'PEAK',    'export','Solar Buy-Back',0.210000, 21),  -- [PUBLISHED]
    (3,1,2,'OFFPEAK', 'export','Solar Buy-Back',0.160000, 22),  -- [PUBLISHED]
    (3,2,2,'PEAK',    'export','Solar Buy-Back',0.210000, 21),  -- [PUBLISHED]
    (3,2,2,'OFFPEAK', 'export','Solar Buy-Back',0.160000, 22),  -- [PUBLISHED]

    -- ecoWHOLESALE: spot, so a wide peak-to-off-peak spread.  [ASSUMED]
    (4,1,1,'PEAK',    'import','Spot Usage (Peak)',     0.350000, 11),
    (4,1,1,'SHOULDER','import','Spot Usage (Shoulder)', 0.120000, 12),
    (4,1,1,'OFFPEAK', 'import','Spot Usage (Off Peak)', 0.040000, 13),
    (4,2,1,'PEAK',    'import','Spot Usage (Peak)',     0.350000, 11),
    (4,2,1,'SHOULDER','import','Spot Usage (Shoulder)', 0.120000, 12),
    (4,2,1,'OFFPEAK', 'import','Spot Usage (Off Peak)', 0.040000, 13),
    (4,1,2,'PEAK',    'import','Spot Usage (Peak)',     0.371000, 11),
    (4,1,2,'SHOULDER','import','Spot Usage (Shoulder)', 0.127200, 12),
    (4,1,2,'OFFPEAK', 'import','Spot Usage (Off Peak)', 0.042400, 13),
    (4,2,2,'PEAK',    'import','Spot Usage (Peak)',     0.371000, 11),
    (4,2,2,'SHOULDER','import','Spot Usage (Shoulder)', 0.127200, 12),
    (4,2,2,'OFFPEAK', 'import','Spot Usage (Off Peak)', 0.042400, 13);

INSERT INTO src_catalogue.rate_components
    (component_id, plan_id, charge_scope, component_desc, rate_uom,
     window_id, flow_direction, unit_rate, valid_from, valid_to)
OVERRIDING SYSTEM VALUE
-- NET, banded
SELECT p.plan_id*100 + k.seq, p.plan_id, 'NET',
       CASE k.band_label WHEN 'PEAK' THEN 'Peak Usage' ELSE 'Off Peak Usage' END,
       'KWH', b.window_id, 'import', ROUND(k.rate * n.factor, 6), p.valid_from, p.valid_to
FROM src_catalogue.price_plans p
JOIN _net n ON n.nt = (p.plan_id / 10000) % 10
JOIN _uc  u ON u.user_category = p.user_category
JOIN _ver v ON v.valid_from = p.valid_from
JOIN _netk k ON k.uc = u.uc AND k.vn = v.vn
JOIN LATERAL (SELECT MIN(window_id) AS window_id FROM src_catalogue.tou_windows w
              WHERE w.plan_id = p.plan_id AND w.charge_scope = 'NET'
                AND w.band_label = k.band_label) b ON true
WHERE p.plan_id >= 1000000

UNION ALL
-- NET, un-banded (daily charge, transmission)
SELECT p.plan_id*100 + f.seq, p.plan_id, 'NET',
       replace(f.component_desc, 'VECT', n.network_code), f.rate_uom,
       NULL, 'import', ROUND(f.rate * n.factor, 6), p.valid_from, p.valid_to
FROM src_catalogue.price_plans p
JOIN _net n ON n.nt = (p.plan_id / 10000) % 10
JOIN _uc  u ON u.user_category = p.user_category
JOIN _ver v ON v.valid_from = p.valid_from
JOIN _netf f ON f.uc = u.uc AND f.vn = v.vn
WHERE p.plan_id >= 1000000

UNION ALL
-- ENE, banded, both flow directions
SELECT p.plan_id*100 + e.seq, p.plan_id, 'ENE', e.component_desc, 'KWH',
       b.window_id, e.flow, e.rate, p.valid_from, p.valid_to
FROM src_catalogue.price_plans p
JOIN _uc  u ON u.user_category = p.user_category
JOIN _ver v ON v.valid_from = p.valid_from
JOIN _ene e ON e.sh = (p.plan_id - 1000000) / 100000
           AND e.uc = u.uc AND e.vn = v.vn
JOIN LATERAL (SELECT MIN(window_id) AS window_id FROM src_catalogue.tou_windows w
              WHERE w.plan_id = p.plan_id AND w.charge_scope = 'ENE'
                AND w.band_label = e.band_label) b ON true
WHERE p.plan_id >= 1000000

UNION ALL
-- OTH: the three components Ecotricity's bill guide names.
--
-- The levy is split into TWO rows on version-2 plans, changing on
-- 2025-07-01 -- three months INSIDE the version. rate_components has its
-- own valid_from for exactly this, and nothing else in the repo uses it,
-- so a rating step that resolved rates by plan version alone would price
-- every post-July bill with the old levy and still balance.
SELECT p.plan_id*100 + o.seq, p.plan_id, 'OTH', o.component_desc, o.rate_uom,
       NULL, 'import', o.rate,
       GREATEST(p.valid_from, o.from_date),
       CASE WHEN o.to_date IS NULL THEN p.valid_to
            ELSE LEAST(COALESCE(p.valid_to, o.to_date), o.to_date) END
FROM src_catalogue.price_plans p
CROSS JOIN (VALUES
    ('ELECTRICITY AUTHORITY Levy', 'KWH', 0.001300, DATE '2024-08-01', DATE '2025-07-01', 31),
    ('ELECTRICITY AUTHORITY Levy', 'KWH', 0.001500, DATE '2025-07-01', NULL::date,        32),
    ('Metering charge',            'DAY', 0.450000, DATE '2024-08-01', NULL::date,        33),
    ('Admin & climate-positive certification fee', 'DAY',
                                          0.268200, DATE '2024-08-01', NULL::date,        34)
) AS o(component_desc, rate_uom, rate, from_date, to_date, seq)
WHERE p.plan_id >= 1000000
  AND (o.to_date IS NULL OR p.valid_from < o.to_date)
  AND (p.valid_to IS NULL OR o.from_date < p.valid_to);


-- ---------------------------------------------------------------------
-- 4. Assertions. These are the invariants no CHECK constraint can hold.
-- ---------------------------------------------------------------------
DO $$
DECLARE n int; bad text;
BEGIN
    SELECT count(*) INTO n FROM src_catalogue.price_plans WHERE plan_id >= 1000000;
    IF n <> 64 THEN
        RAISE EXCEPTION 'expected 64 plan rows (4 shapes x 4 networks x 2 user categories x 2 versions), got %', n;
    END IF;

    -- Every plan code must have exactly two versions, and they must abut.
    SELECT string_agg(code, ', ') INTO bad FROM (
        SELECT code FROM src_catalogue.price_plans WHERE plan_id >= 1000000
        GROUP BY code HAVING count(*) <> 2) t;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'plan codes without exactly two versions: %', bad;
    END IF;

    SELECT string_agg(code, ', ') INTO bad FROM (
        SELECT a.code FROM src_catalogue.price_plans a
        JOIN src_catalogue.price_plans b ON b.code = a.code AND b.valid_from > a.valid_from
        WHERE a.plan_id >= 1000000 AND a.valid_to IS DISTINCT FROM b.valid_from) t;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'plan versions leave a gap or overlap: %', bad;
    END IF;

    -- THE IMPORTANT ONE. Every local half-hour of every weekday must fall
    -- in exactly ONE window per (plan, charge_scope). Zero means the
    -- interval is never priced and the invoice still adds up; two means it
    -- is priced twice. Neither raises an error anywhere else.
    SELECT string_agg(DISTINCT format('plan %s %s dow %s %s (%s windows)',
                                      plan_id, charge_scope, dow, hh, c), '; ')
      INTO bad
    FROM (
        SELECT p.plan_id, s.charge_scope, d.dow, g.hh,
               count(w.window_id) AS c
        FROM src_catalogue.price_plans p
        CROSS JOIN (VALUES ('NET'),('ENE')) AS s(charge_scope)
        CROSS JOIN generate_series(1,7) AS d(dow)
        CROSS JOIN generate_series(0, 47) AS i(i)
        CROSS JOIN LATERAL (SELECT (i.i * interval '30 min')::time AS hh) g
        LEFT JOIN src_catalogue.tou_windows w
               ON w.plan_id = p.plan_id
              AND w.charge_scope = s.charge_scope
              AND d.dow::text = ANY(string_to_array(w.days_csv, ','))
              AND g.hh >= w.time_from AND g.hh < w.time_to
        WHERE p.plan_id >= 1000000
        GROUP BY p.plan_id, s.charge_scope, d.dow, g.hh
        HAVING count(w.window_id) <> 1
    ) t;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'TOU windows are not a gapless tiling: %', left(bad, 900);
    END IF;

    -- Every component that names a window must name one of its OWN scope.
    -- No foreign key can express this: the FK only proves the window exists.
    SELECT count(*) INTO n
    FROM src_catalogue.rate_components rc
    JOIN src_catalogue.tou_windows w ON w.window_id = rc.window_id
    WHERE rc.plan_id >= 1000000
      AND (w.plan_id <> rc.plan_id OR w.charge_scope <> rc.charge_scope);
    IF n > 0 THEN
        RAISE EXCEPTION '% rate components point at a window of another plan or scope', n;
    END IF;

    -- The weekday-only window must exist, or nothing tests days_csv parsing.
    SELECT count(*) INTO n FROM src_catalogue.tou_windows
    WHERE plan_id >= 1000000 AND days_csv = '1,2,3,4,5';
    IF n = 0 THEN
        RAISE EXCEPTION 'no weekday-only window: days_csv parsing is untested';
    END IF;

    -- A component-level rate change strictly inside a plan version.
    SELECT count(*) INTO n
    FROM src_catalogue.rate_components rc
    JOIN src_catalogue.price_plans p ON p.plan_id = rc.plan_id
    WHERE rc.plan_id >= 1000000 AND rc.valid_to IS NOT NULL
      AND (p.valid_to IS NULL OR rc.valid_to < p.valid_to);
    IF n = 0 THEN
        RAISE EXCEPTION 'no component-level rate change inside a plan version';
    END IF;

    SELECT count(*) INTO n FROM src_catalogue.price_plans
    WHERE plan_id >= 1000000 AND user_category = 'LOW';
    IF n = 0 THEN
        RAISE EXCEPTION 'no LOW user_category plan';
    END IF;

    RAISE NOTICE 'catalogue OK: % plans, % windows, % components',
        (SELECT count(*) FROM src_catalogue.price_plans   WHERE plan_id >= 1000000),
        (SELECT count(*) FROM src_catalogue.tou_windows    WHERE plan_id >= 1000000),
        (SELECT count(*) FROM src_catalogue.rate_components WHERE plan_id >= 1000000);
END $$;

COMMIT;

-- =====================================================================
-- KNOWN LIMITATION -- ecoWHOLESALE cannot be modelled faithfully.
--
-- Spot pricing changes every half hour. rate_components holds a SCALAR
-- unit_rate with a valid_from date, so representing it truthfully would
-- need one component row per trading period -- ~17,500 a year per plan.
-- What is modelled here is a wide peak/shoulder/off-peak spread standing
-- in for spot's average shape.
--
-- This is a real gap in the source schema that the four published plans
-- exposed, not a shortcut: a spot plan's bill CANNOT be reproduced from
-- this catalogue. Filed as an open item; it does not block Stage 2,
-- because no reconciliation bill is on a spot plan.
-- =====================================================================
