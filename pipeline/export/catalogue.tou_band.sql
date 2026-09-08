-- catalogue.tou_band  <-  stg_catalogue__tou_windows
--
-- Bands and rate components exist only in staging: gold models the plan
-- VERSION as a dimension but never decomposes its tariff, because no mart
-- needs the decomposition. Serving does -- §7.1 keeps these namespaces
-- source-shaped precisely so the rating engine can re-rate, and a
-- denormalised plan would destroy that -- so the export reaches past gold.
--
-- The source's window_id becomes band_id unchanged. It is already a stable
-- per-version integer, and minting a second surrogate for it would create a
-- key with no source to reconcile against.
--
-- days_of_week IS A SNOWFLAKE ARRAY and serving declares smallint[]; months_csv
-- is a plain string. So the two need different renderings into the '{1,2,3}'
-- literal COPY parses natively -- array_to_string for the array, and stripping
-- everything that is not a digit or a comma for the text. See the loader's
-- note on why the temp table is created LIKE the target.
select
    w.window_id                                              as band_id,
    p.plan_id,
    w.charge_group,
    w.band_label                                             as band_name,
    '{' || array_to_string(w.days_of_week, ',') || '}'       as days_of_week,
    w.time_from                                              as start_time,
    w.time_to                                                as end_time,
    case when w.months_csv is null then null
         else '{' || regexp_replace(w.months_csv, '[^0-9,]', '') || '}' end as months
from KEA.KEA_STAGING.STG_CATALOGUE__TOU_WINDOWS w
join KEA.KEA_GOLD.DIM_PLAN p
  on p.source_plan_id = w.plan_id
order by band_id
