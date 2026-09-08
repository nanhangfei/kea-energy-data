-- =====================================================================
-- Sprint 3 demo catalogue -- the three Kea pricing styles shown in
-- docs/sprints/sprint-3/plans-1.png and plans-2.png.
--
-- The generated LCL cohort deliberately has one network (Vector), one user
-- category (Standard User), and these three choices. The nine hand-written
-- Sprint 2 fixtures keep their original historical tariffs and are outside
-- the generated id range used here.
--
-- The screenshots are customer-facing, GST-inclusive, bundled prices:
--
--   ANY   Anytime Rate   32.5c/kWh                         $1.45/day
--   TOU   Time of Use    39.0c peak / 27.0c off / 18.0c   $1.50/day
--   WKND  Free Weekends  36.5c outside Sat/Sun 09-17      $1.55/day
--
-- Source bill lines are stored ex-GST, and import energy receives the 5.11%
-- line-loss uplift proved by the printed Sprint 2 bill. The stored import
-- rates therefore divide the displayed rate by 1.15 and 1.0511; after the
-- existing biller applies loss and GST, one metered kWh lands back on the
-- displayed price. Daily charges divide by GST only.
--
-- The screenshots do not specify solar buy-back. All three plans use the
-- same assumed flat 8c/kWh customer-facing credit, so solar households remain
-- billable without letting an invented export difference choose the winner.
--
-- Two validity rows per code remain. They make invoices straddling 2026-04-01
-- exercise the platform's period-end version resolution, while the price is
-- deliberately unchanged: the screenshots define one tariff, not invented
-- historical inflation.
-- =====================================================================

BEGIN;

DELETE FROM src_catalogue.term_credits;
INSERT INTO src_catalogue.term_credits (TERM_MONTHS, MONTHLY_CREDIT, VALID_FROM)
VALUES
    (6,  10.00, DATE '2025-04-01'),
    (12, 25.00, DATE '2025-04-01');

DELETE FROM src_catalogue.rate_components WHERE plan_id >= 1000000;
DELETE FROM src_catalogue.tou_windows     WHERE plan_id >= 1000000;
DELETE FROM src_catalogue.price_plans     WHERE plan_id >= 1000000;


-- ---------------------------------------------------------------------
-- 1. Plans: 3 pricing styles x 1 network x 1 user category x 2 versions.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _shape(
    sh int, shape_code text, plan_name text, is_tou boolean
) ON COMMIT DROP;
INSERT INTO _shape VALUES
    (1, 'ANY',  'Anytime Rate',  false),
    (2, 'TOU',  'Time of Use',   true),
    (3, 'WKND', 'Free Weekends', true);

CREATE TEMP TABLE _ver(vn int, valid_from date, valid_to date) ON COMMIT DROP;
INSERT INTO _ver VALUES
    (1, DATE '2025-04-01', DATE '2026-04-01'),
    (2, DATE '2026-04-01', NULL);

INSERT INTO src_catalogue.price_plans
    (plan_id, code, name, network_code, user_category, is_tou,
     ppd_percent, valid_from, valid_to, active)
OVERRIDING SYSTEM VALUE
SELECT 1000000 + s.sh*100000 + 10000 + 2000 + v.vn,
       format('KEA_%s_VECT_STA', s.shape_code),
       format('%s (VECT, Standard user)', s.plan_name),
       'VECT', 'STANDARD', s.is_tou, 0,
       v.valid_from, v.valid_to, v.valid_to IS NULL
FROM _shape s CROSS JOIN _ver v;


-- ---------------------------------------------------------------------
-- 2. Windows. Each scope tiles every half-hour of all seven local days.
--
-- The product prices are bundled, so NET has a zero-valued all-day band.
-- It remains explicit because the source contract requires charge-scoped
-- banding and the analytics engine must continue proving each scope tiles.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _win(
    sh int, charge_scope text, band_label text,
    days_csv text, tf time, tt time, seq int
) ON COMMIT DROP;

INSERT INTO _win VALUES
    (1, 'NET', 'BUNDLED', '1,2,3,4,5,6,7', TIME '00:00', TIME '24:00', 1),
    (2, 'NET', 'BUNDLED', '1,2,3,4,5,6,7', TIME '00:00', TIME '24:00', 1),
    (3, 'NET', 'BUNDLED', '1,2,3,4,5,6,7', TIME '00:00', TIME '24:00', 1),

    (1, 'ENE', 'ANYTIME', '1,2,3,4,5,6,7', TIME '00:00', TIME '24:00', 11),

    (2, 'ENE', 'NIGHT',   '1,2,3,4,5,6,7', TIME '00:00', TIME '07:00', 11),
    (2, 'ENE', 'PEAK',    '1,2,3,4,5,6,7', TIME '07:00', TIME '11:00', 12),
    (2, 'ENE', 'OFFPEAK', '1,2,3,4,5,6,7', TIME '11:00', TIME '17:00', 13),
    (2, 'ENE', 'PEAK',    '1,2,3,4,5,6,7', TIME '17:00', TIME '21:00', 14),
    (2, 'ENE', 'OFFPEAK', '1,2,3,4,5,6,7', TIME '21:00', TIME '23:00', 15),
    (2, 'ENE', 'NIGHT',   '1,2,3,4,5,6,7', TIME '23:00', TIME '24:00', 16),

    (3, 'ENE', 'STANDARD', '1,2,3,4,5', TIME '00:00', TIME '24:00', 11),
    (3, 'ENE', 'STANDARD', '6,7',       TIME '00:00', TIME '09:00', 12),
    (3, 'ENE', 'FREE',     '6,7',       TIME '09:00', TIME '17:00', 13),
    (3, 'ENE', 'STANDARD', '6,7',       TIME '17:00', TIME '24:00', 14);

INSERT INTO src_catalogue.tou_windows
    (window_id, plan_id, charge_scope, band_label, days_csv, time_from, time_to)
OVERRIDING SYSTEM VALUE
SELECT p.plan_id*100 + w.seq, p.plan_id, w.charge_scope, w.band_label,
       w.days_csv, w.tf, w.tt
FROM src_catalogue.price_plans p
JOIN _win w ON w.sh = (p.plan_id - 1000000) / 100000
WHERE p.plan_id >= 1000000;


-- ---------------------------------------------------------------------
-- 3. Components. Import rates are ex-GST and pre-loss; export rates are
-- ex-GST and do not receive line loss. One component represents one label,
-- even when that label has two disconnected windows.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _import_rate(
    sh int, band_label text, description text, rate numeric, seq int
) ON COMMIT DROP;
INSERT INTO _import_rate VALUES
    (1, 'ANYTIME',  'Anytime Usage',                       0.268869, 11),
    (2, 'PEAK',     'Peak Usage (7-11AM, 5-9PM)',          0.322643, 11),
    (2, 'OFFPEAK',  'Off-Peak Usage (11AM-5PM, 9-11PM)',  0.223368, 12),
    (2, 'NIGHT',    'Night Usage (11PM-7AM)',              0.148912, 13),
    (3, 'STANDARD', 'Usage Outside Free Weekends',        0.301961, 11),
    (3, 'FREE',     'Free Sat & Sun 9AM-5PM',              0.000000, 12);

CREATE TEMP TABLE _daily(sh int, rate numeric) ON COMMIT DROP;
INSERT INTO _daily VALUES
    (1, 1.260870),
    (2, 1.304348),
    (3, 1.347826);

CREATE TEMP TABLE _export_rate(
    sh int, band_label text, seq int
) ON COMMIT DROP;
INSERT INTO _export_rate VALUES
    (1, 'ANYTIME',  21),
    (2, 'PEAK',     21),
    (2, 'OFFPEAK',  22),
    (2, 'NIGHT',    23),
    (3, 'STANDARD', 21),
    (3, 'FREE',     22);

INSERT INTO src_catalogue.rate_components
    (component_id, plan_id, charge_scope, component_desc, rate_uom,
     window_id, flow_direction, unit_rate, valid_from, valid_to)
OVERRIDING SYSTEM VALUE
-- Explicit zero network band: do not charge a second price on top of the
-- screenshot's bundled usage rate.
SELECT p.plan_id*100 + 1, p.plan_id, 'NET', 'Included in bundled usage rate',
       'KWH', w.window_id, 'import', 0, p.valid_from, p.valid_to
FROM src_catalogue.price_plans p
JOIN src_catalogue.tou_windows w
  ON w.plan_id = p.plan_id AND w.charge_scope = 'NET'
WHERE p.plan_id >= 1000000

UNION ALL
SELECT p.plan_id*100 + r.seq, p.plan_id, 'ENE', r.description, 'KWH',
       w.window_id, 'import', r.rate, p.valid_from, p.valid_to
FROM src_catalogue.price_plans p
JOIN _import_rate r ON r.sh = (p.plan_id - 1000000) / 100000
JOIN LATERAL (
    SELECT min(window_id) AS window_id
    FROM src_catalogue.tou_windows w
    WHERE w.plan_id = p.plan_id AND w.charge_scope = 'ENE'
      AND w.band_label = r.band_label
) w ON w.window_id IS NOT NULL
WHERE p.plan_id >= 1000000

UNION ALL
-- Neutral, common 8c/kWh customer-facing buy-back on every plan.
SELECT p.plan_id*100 + r.seq, p.plan_id, 'ENE', 'Solar buy-back', 'KWH',
       w.window_id, 'export', 0.069565, p.valid_from, p.valid_to
FROM src_catalogue.price_plans p
JOIN _export_rate r ON r.sh = (p.plan_id - 1000000) / 100000
JOIN LATERAL (
    SELECT min(window_id) AS window_id
    FROM src_catalogue.tou_windows w
    WHERE w.plan_id = p.plan_id AND w.charge_scope = 'ENE'
      AND w.band_label = r.band_label
) w ON w.window_id IS NOT NULL
WHERE p.plan_id >= 1000000

UNION ALL
SELECT p.plan_id*100 + 31, p.plan_id, 'OTH', 'Daily charge', 'DAY',
       NULL, 'import', d.rate, p.valid_from, p.valid_to
FROM src_catalogue.price_plans p
JOIN _daily d ON d.sh = (p.plan_id - 1000000) / 100000
WHERE p.plan_id >= 1000000;


-- ---------------------------------------------------------------------
-- 4. Assertions: product count, validity, tiling and screenshot rates.
-- ---------------------------------------------------------------------
DO $$
DECLARE n int; bad text;
BEGIN
    SELECT count(*) INTO n
    FROM src_catalogue.price_plans WHERE plan_id >= 1000000;
    IF n <> 6 THEN
        RAISE EXCEPTION 'expected 6 rows (3 styles x 2 versions), got %', n;
    END IF;

    SELECT string_agg(code, ', ') INTO bad FROM (
        SELECT code FROM src_catalogue.price_plans WHERE plan_id >= 1000000
        GROUP BY code HAVING count(*) <> 2
    ) x;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'plan codes without exactly two versions: %', bad;
    END IF;

    SELECT string_agg(code, ', ') INTO bad FROM (
        SELECT a.code
        FROM src_catalogue.price_plans a
        JOIN src_catalogue.price_plans b
          ON b.code = a.code AND b.valid_from > a.valid_from
        WHERE a.plan_id >= 1000000
          AND a.valid_to IS DISTINCT FROM b.valid_from
    ) x;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'plan versions leave a gap or overlap: %', bad;
    END IF;

    SELECT string_agg(DISTINCT format(
               'plan %s %s dow %s %s (%s windows)',
               plan_id, charge_scope, dow, hh, c), '; ')
      INTO bad
    FROM (
        SELECT p.plan_id, s.charge_scope, d.dow, g.hh,
               count(w.window_id) AS c
        FROM src_catalogue.price_plans p
        CROSS JOIN (VALUES ('NET'), ('ENE')) s(charge_scope)
        CROSS JOIN generate_series(1, 7) d(dow)
        CROSS JOIN generate_series(0, 47) i(i)
        CROSS JOIN LATERAL (
            SELECT (i.i * interval '30 min')::time AS hh
        ) g
        LEFT JOIN src_catalogue.tou_windows w
          ON w.plan_id = p.plan_id
         AND w.charge_scope = s.charge_scope
         AND d.dow::text = ANY(string_to_array(w.days_csv, ','))
         AND g.hh >= w.time_from AND g.hh < w.time_to
        WHERE p.plan_id >= 1000000
        GROUP BY p.plan_id, s.charge_scope, d.dow, g.hh
        HAVING count(w.window_id) <> 1
    ) x;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'TOU windows are not a gapless tiling: %', left(bad, 900);
    END IF;

    SELECT count(*) INTO n
    FROM src_catalogue.rate_components rc
    JOIN src_catalogue.tou_windows w ON w.window_id = rc.window_id
    WHERE rc.plan_id >= 1000000
      AND (w.plan_id <> rc.plan_id OR w.charge_scope <> rc.charge_scope);
    IF n <> 0 THEN
        RAISE EXCEPTION '% components point at a window of another plan/scope', n;
    END IF;

    WITH wanted(shape, band_label, displayed) AS (
        VALUES
            ('ANY'::text, 'ANYTIME'::text, 0.325::numeric),
            ('TOU',       'PEAK',          0.390::numeric),
            ('TOU',       'OFFPEAK',       0.270::numeric),
            ('TOU',       'NIGHT',         0.180::numeric),
            ('WKND',      'STANDARD',      0.365::numeric),
            ('WKND',      'FREE',          0.000::numeric)
    ), actual AS (
        SELECT DISTINCT split_part(p.code, '_', 2) AS shape,
               w.band_label,
               round(rc.unit_rate * 1.0511 * 1.15, 3) AS displayed
        FROM src_catalogue.price_plans p
        JOIN src_catalogue.rate_components rc ON rc.plan_id = p.plan_id
        JOIN src_catalogue.tou_windows w ON w.window_id = rc.window_id
        WHERE p.plan_id >= 1000000 AND rc.charge_scope = 'ENE'
          AND rc.flow_direction = 'import'
    )
    SELECT count(*) INTO n FROM (
        (SELECT * FROM wanted EXCEPT SELECT * FROM actual)
        UNION ALL
        (SELECT * FROM actual EXCEPT SELECT * FROM wanted)
    ) diff;
    IF n <> 0 THEN
        RAISE EXCEPTION 'import rate map differs from the Sprint 3 screenshots (% rows)', n;
    END IF;

    WITH wanted(shape, displayed) AS (
        VALUES ('ANY'::text, 1.45::numeric),
               ('TOU',       1.50::numeric),
               ('WKND',      1.55::numeric)
    ), actual AS (
        SELECT DISTINCT split_part(p.code, '_', 2) AS shape,
               round(rc.unit_rate * 1.15, 2) AS displayed
        FROM src_catalogue.price_plans p
        JOIN src_catalogue.rate_components rc ON rc.plan_id = p.plan_id
        WHERE p.plan_id >= 1000000 AND rc.rate_uom = 'DAY'
    )
    SELECT count(*) INTO n FROM (
        (SELECT * FROM wanted EXCEPT SELECT * FROM actual)
        UNION ALL
        (SELECT * FROM actual EXCEPT SELECT * FROM wanted)
    ) diff;
    IF n <> 0 THEN
        RAISE EXCEPTION 'daily charges differ from the Sprint 3 screenshots (% rows)', n;
    END IF;

    SELECT count(*) INTO n
    FROM src_catalogue.rate_components
    WHERE plan_id >= 1000000 AND flow_direction = 'export';
    IF n <> 12 THEN
        RAISE EXCEPTION 'expected 12 common buy-back components, got %', n;
    END IF;

    SELECT count(*) INTO n
    FROM src_catalogue.price_plans
    WHERE plan_id >= 1000000 AND user_category <> 'STANDARD';
    IF n <> 0 THEN
        RAISE EXCEPTION 'generated catalogue contains % non-STANDARD plans', n;
    END IF;

    RAISE NOTICE 'catalogue OK: % plans, % windows, % components',
        (SELECT count(*) FROM src_catalogue.price_plans WHERE plan_id >= 1000000),
        (SELECT count(*) FROM src_catalogue.tou_windows WHERE plan_id >= 1000000),
        (SELECT count(*) FROM src_catalogue.rate_components WHERE plan_id >= 1000000);
END $$;

COMMIT;
