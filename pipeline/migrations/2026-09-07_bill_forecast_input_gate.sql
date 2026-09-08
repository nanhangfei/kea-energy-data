-- Carry the warehouse's forecast safety decision into serving.
--
-- Existing installations need the default only until the next full mart load
-- replaces the table. Fresh schemas declare the column NOT NULL directly in
-- design/kea_serving_schema.sql.
ALTER TABLE kea.mart_forecast_input
    ADD COLUMN IF NOT EXISTS is_forecastable boolean NOT NULL DEFAULT false;

-- The default is only a bridge for pre-migration rows. Future loads must state
-- the warehouse decision explicitly; silently defaulting a missing export to
-- false would hide a broken contract.
ALTER TABLE kea.mart_forecast_input
    ALTER COLUMN is_forecastable DROP DEFAULT;
