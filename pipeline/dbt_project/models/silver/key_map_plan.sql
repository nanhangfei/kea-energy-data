{{ config(materialized='incremental', unique_key=['plan_code', 'effective_from']) }}

-- Design §6.2 RULE 2, and the reason this map is not like the other two.
--
-- KEYED ON (plan_code, effective_from), NOT plan_code ALONE. One plan code has
-- many versions. Resolving a bill means finding the version whose validity
-- brackets the bill's period end (§2.1.2) -- resolving by code alone re-rates
-- history against current tariffs, and the design doc calls that the most
-- dangerous step in the pipeline precisely because nothing about it looks
-- wrong. A map keyed on the code alone would make that mistake impossible to
-- avoid, so the key is the version.
--
-- `effective_from` IS THE SOURCE'S `valid_from`. The design names the map
-- column effective_from and the catalogue names its own column valid_from;
-- this is the one place the two words meet, so the rename happens here rather
-- than being carried into every downstream model.
--
-- ⚠️ THE SOURCE ALSO HAS A COLUMN CALLED plan_id, AND IT IS NOT THIS ONE.
-- src_catalogue.price_plans.plan_id is that system's own identity column; this
-- plan_id is the conformed surrogate the serving store's
-- catalogue.plan(plan_id) FK points at (kea_serving_schema.sql:175). They are
-- unrelated, and joining on the wrong one would work today -- one catalogue,
-- one numbering -- and break the moment a second source of tariffs exists.
-- Nothing downstream may join on the source's.

with source as (
    select distinct
        plan_code,
        valid_from as effective_from
    from {{ ref('stg_catalogue__price_plans') }}
    where plan_code is not null
      and valid_from is not null
),

new_keys as (
    select plan_code, effective_from from source
    {% if is_incremental() %}
    where (plan_code, effective_from) not in (
        select plan_code, effective_from from {{ this }}
    )
    {% endif %}
)

select
    plan_code,
    effective_from,
    {% if is_incremental() %}
    (select coalesce(max(plan_id), 0) from {{ this }})
    {% else %}
    0
    {% endif %}
      + row_number() over (order by plan_code, effective_from) as plan_id,
    current_timestamp() as first_seen
from new_keys
