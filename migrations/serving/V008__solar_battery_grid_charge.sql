-- =====================================================================
-- A HOME BATTERY THAT ALSO CHARGES FROM THE GRID  --  sprint4-assumptions-v3
--
-- V007's battery stored only the home's own solar surplus. NZ allows charging
-- a home battery from the grid, and on a plan with a cheap window it pays:
-- Kea's Time of Use sells night energy at 14.9c and peak at 32.3c (ex-GST), so
-- a kWh bought at night and stored at 90% avoids far more than it cost.
--
-- kea.solar_estimate gains battery_dispatch, saying what a battery row does:
--
--   NULL              no battery (battery_kwh = 0)
--   self_consumption  stores only the home's surplus -- every V007 battery row
--   grid_charge       also tops up from the grid in the plan's cheapest
--                     half-hours and gives the energy back where the rate,
--                     after the loss, still beats them. Written only for an
--                     account whose plan has more than one import rate.
--
-- ⚠️ UP TO NINE ROWS PER ACCOUNT PER PUBLICATION: each system size with no
-- battery, a self_consumption battery and, on a time-varying plan, a
-- grid_charge one. A reader keyed on (account_id, system_kw, battery_kwh)
-- must add battery_dispatch; `battery_dispatch IS DISTINCT FROM
-- 'grid_charge'` gives exactly the rows V007 wrote.
--
-- ⚠️ self_used_pct_* NEVER COUNTS GRID ENERGY. A grid_charge battery holds
-- solar and grid energy together; only the solar share of what it gives back
-- counts as solar used at home.
--
-- ⚠️ EVERY NAME IS SCHEMA-QUALIFIED. V001 sets search_path, but a database
-- that baselined V001 never ran that SET, so nothing here may rely on it.
--
-- NO GRANTS: a new column inherits the table's. NO NEW FOREIGN KEYS.
--
-- THE BACKFILL IS A FACT, NOT A GUESS. Every battery row written before this
-- migration came from V007's surplus-only battery, so those rows get
-- 'self_consumption' and panels-only rows stay NULL. It rewrites no figure.
-- =====================================================================

ALTER TABLE kea.solar_estimate
    ADD COLUMN battery_dispatch text;

COMMENT ON COLUMN kea.solar_estimate.battery_dispatch IS
    'What the battery does beyond storing the home''s own surplus. NULL: no '
    'battery. self_consumption: nothing. grid_charge: also tops up from the '
    'grid in the plan''s cheapest half-hours; only on a plan with more than '
    'one import rate. Grid energy is never counted in self_used_pct_*.';

UPDATE kea.solar_estimate
   SET battery_dispatch = 'self_consumption'
 WHERE battery_kwh > 0;

ALTER TABLE kea.solar_estimate
    ADD CONSTRAINT solar_estimate_battery_dispatch
        CHECK ((battery_kwh = 0) = (battery_dispatch IS NULL)
           AND (battery_dispatch IS NULL
                OR battery_dispatch IN ('self_consumption', 'grid_charge')));

-- NULLS NOT DISTINCT (PG15+): without it, two panels-only rows of one size in
-- one publication would both be allowed, their NULL dispatch never equal.
ALTER TABLE kea.solar_estimate
    DROP CONSTRAINT solar_estimate_account_size_battery_computed_key;
ALTER TABLE kea.solar_estimate
    ADD CONSTRAINT solar_estimate_account_size_battery_dispatch_computed_key
        UNIQUE NULLS NOT DISTINCT
            (account_id, system_kw, battery_kwh, battery_dispatch, computed_at);

DROP INDEX kea.idx_solar_estimate_account;
CREATE INDEX idx_solar_estimate_account
    ON kea.solar_estimate(account_id, computed_at DESC, system_kw, battery_kwh,
                          battery_dispatch);
