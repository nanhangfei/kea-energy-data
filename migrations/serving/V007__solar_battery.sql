-- =====================================================================
-- SOLAR PAYBACK WITH A HOME BATTERY  --  sprint4-assumptions-v2
--
-- kea.solar_estimate priced panels alone and said so. It now carries one more
-- dimension, battery_kwh: every account and system size is estimated twice,
-- with no battery (0) and with one (13.5 kWh today; the sizes are in
-- pipeline/scripts/decisions/estimate_assumptions.py).
--
-- ⚠️ SIX ROWS PER ACCOUNT PER PUBLICATION, NOT THREE. A reader keyed on
-- (account_id, system_kw) now finds two rows and must add battery_kwh.
-- `battery_kwh = 0` is exactly the rows it read before.
--
-- ⚠️ ON A BATTERY ROW install_cost_nzd_* IS PANELS PLUS BATTERY, and
-- self_used_pct_* counts what the home drew from the battery as well as what
-- it used directly. The battery stores the home's own surplus only: it is
-- never charged from the grid, and the wording must say so.
--
-- ⚠️ A BATTERY ROW IS NOT ALWAYS THE BETTER ROW. Where a plan's buy-back is
-- close to the rate the stored energy avoids, the round-trip loss can make it
-- save less than its battery_kwh = 0 sibling. It is published, not suppressed.
--
-- NO deferred draft graduates here. metering.solar_installation (a record of
-- panels a household already has) and kea.ev_vehicle stay in
-- kea_deferred_schema.sql; see that file's header.
--
-- ⚠️ EVERY NAME IS SCHEMA-QUALIFIED. V001 sets search_path, but a database
-- that baselined V001 never ran that SET, so nothing here may rely on it.
--
-- NO GRANTS: a new column inherits the table's. NO NEW FOREIGN KEYS.
--
-- NOT NULL DEFAULT 0, UNLIKE V006's NULLABLE COLUMNS. Every row already in
-- this table was computed without a battery, so 0 is a fact about it, not a
-- guess. The default also keeps an insert that does not name the column
-- meaning what it meant before.
-- =====================================================================

ALTER TABLE kea.solar_estimate
    ADD COLUMN battery_kwh numeric(4,1) NOT NULL DEFAULT 0;

COMMENT ON COLUMN kea.solar_estimate.battery_kwh IS
    'Usable size of the home battery this estimate assumes; 0 is panels '
    'alone. Above 0, install_cost_nzd_* includes the battery and '
    'self_used_pct_* includes what the home drew from it. Charged from the '
    'home''s own surplus only, never from the grid.';

ALTER TABLE kea.solar_estimate
    ADD CONSTRAINT solar_estimate_battery_non_negative CHECK (battery_kwh >= 0);

-- The key V005 declared inline, under the name PostgreSQL gave it. One
-- publication now writes two rows per account and system size.
ALTER TABLE kea.solar_estimate
    DROP CONSTRAINT solar_estimate_account_id_system_kw_computed_at_key;
ALTER TABLE kea.solar_estimate
    ADD CONSTRAINT solar_estimate_account_size_battery_computed_key
        UNIQUE (account_id, system_kw, battery_kwh, computed_at);

DROP INDEX kea.idx_solar_estimate_account;
CREATE INDEX idx_solar_estimate_account
    ON kea.solar_estimate(account_id, computed_at DESC, system_kw, battery_kwh);
