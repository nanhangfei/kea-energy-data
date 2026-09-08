-- Design §6.4. Grain: icp × register × interval — the same key
-- kea_serving_schema.sql:330 declares as metering.meter_read_hh's PK, so Stage 5
-- exports this without regrouping.
--
-- AMBIGUOUS ICPs ARE EXCLUDED, NOT RESOLVED. Where one
-- (icp_identifier, register_code) maps to more than one live register there is
-- nothing in a read to choose on -- KEA-047 carries no meter_id. Joining anyway
-- does not error, it FANS OUT: measured at +750 rows and +447 kWh on the
-- current data, all of it on one household, with a grand total that still looks
-- reasonable. int_integrity__rejects quarantines those subjects; this model
-- leaves them out, so the fact table never contains a reading it cannot
-- attribute.
--
-- The exclusion is a semi-join on the candidate count, not a join to the
-- registers themselves, because joining to registers is precisely what causes
-- the fan-out this model exists to avoid.

select
    ki.icp_id,
    kr.register_id,
    d.interval_start,
    d.kwh,
    d.quality,
    d.version_no,
    -- >1 means this reading was corrected at least once. Carried so a bill
    -- explanation can say so without going back to the lake.
    d.version_count,
    d.received_at,
    d.source_file,
    d.icp_identifier            as icp_number,
    d.register_code
from {{ ref('int_metering__reads_deduped') }} d
join {{ ref('key_map_icp') }} ki
  on ki.icp_number = d.icp_identifier
join {{ ref('key_map_register') }} kr
  on kr.icp_number = d.icp_identifier
 and kr.register_code = d.register_code
join {{ ref('int_metering__register_candidates') }} rc
  on rc.icp_number = d.icp_identifier
 and rc.register_code = d.register_code
where rc.candidates = 1
