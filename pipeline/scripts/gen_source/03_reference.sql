-- =====================================================================
-- Resolve staged business keys into src_metering's local ids.
--
-- Run after the CSVs have been copied into gen_audit.stg_*.
--
-- Every id here is GENERATED ALWAYS AS IDENTITY, so nothing outside this
-- database can know one. The CSVs therefore carry only business keys and
-- this file does the joining -- which is the same seam the extract has to
-- cross, and the reason interval_reads is keyed on (icp_identifier,
-- register_code) rather than on register_id.
--
-- Idempotent: every insert is an upsert on the business key.
-- =====================================================================

BEGIN;

-- Identity sequences still sit below the fixtures' hand-assigned ids
-- unless this runs. See 01_schemas.sql.
SELECT gen_audit.sync_sequences();

INSERT INTO src_metering.networks (network_code, network_name, region)
SELECT network_code, network_name, region FROM gen_audit.stg_networks
ON CONFLICT (network_code) DO UPDATE SET
    network_name = EXCLUDED.network_name, region = EXCLUDED.region;

INSERT INTO src_metering.connection_points
    (icp_identifier, network_id, address_full, suburb, city, postcode,
     climate_region, latitude, longitude, connection_date)
SELECT s.icp_identifier, n.network_id, s.address_full, s.suburb, s.city,
       s.postcode, s.climate_region, s.latitude, s.longitude, s.connection_date
FROM gen_audit.stg_connection_points s
JOIN src_metering.networks n ON n.network_code = s.network_code
ON CONFLICT (icp_identifier) DO UPDATE SET
    network_id = EXCLUDED.network_id,
    address_full = EXCLUDED.address_full,
    suburb = EXCLUDED.suburb,
    city = EXCLUDED.city,
    postcode = EXCLUDED.postcode,
    climate_region = EXCLUDED.climate_region,
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude,
    connection_date = EXCLUDED.connection_date,
    updated_at = now();

-- meters has no unique constraint on (icp_id, serial_number), so the
-- upsert is expressed as a guarded insert rather than ON CONFLICT.
INSERT INTO src_metering.meters (icp_id, serial_number, is_smart, installed_on)
SELECT c.icp_id, s.serial_number, s.is_smart, s.installed_on
FROM gen_audit.stg_meters s
JOIN src_metering.connection_points c ON c.icp_identifier = s.icp_identifier
WHERE NOT EXISTS (
    SELECT 1 FROM src_metering.meters m
    WHERE m.icp_id = c.icp_id AND m.serial_number = s.serial_number);

INSERT INTO src_metering.registers
    (meter_id, register_code, description, is_controlled, flow_direction)
SELECT m.meter_id, s.register_code, s.description, s.is_controlled, s.flow_direction
FROM gen_audit.stg_registers s
JOIN src_metering.connection_points c ON c.icp_identifier = s.icp_identifier
JOIN src_metering.meters m ON m.icp_id = c.icp_id AND m.serial_number = s.serial_number
ON CONFLICT (meter_id, register_code) DO UPDATE SET
    description = EXCLUDED.description,
    is_controlled = EXCLUDED.is_controlled,
    flow_direction = EXCLUDED.flow_direction;

DO $$
DECLARE n_cp int; n_rd int;
BEGIN
    SELECT count(*) INTO n_cp FROM src_metering.connection_points
    WHERE icp_identifier LIKE '09%';
    SELECT count(*) INTO n_rd FROM src_metering.registers r
    JOIN src_metering.meters m ON m.meter_id = r.meter_id
    JOIN src_metering.connection_points c ON c.icp_id = m.icp_id
    WHERE c.icp_identifier LIKE '09%';

    -- The orphan-read check that belongs here CANNOT live here: reads are
    -- loaded after this file runs, so it would pass against an empty set
    -- and prove nothing. It is in 99_assert.sql.
    RAISE NOTICE 'reference OK: % connection points, % registers', n_cp, n_rd;
END $$;

COMMIT;
