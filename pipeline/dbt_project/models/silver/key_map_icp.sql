{{ config(materialized='incremental', unique_key='icp_number') }}

-- Design §6.2 rule 1: surrogate keys must be STABLE ACROSS RUNS. If they are
-- regenerated each load, every FK in the serving Postgres breaks and anything
-- that stored a reference becomes wrong.
--
-- APPEND-ONLY IS THE WHOLE MECHANISM, and `incremental` is what enforces it:
-- on a re-run this model selects only business keys it has never seen, so an
-- existing icp_number cannot be renumbered even by a bug in the numbering
-- below. A `table` materialisation would rebuild every id from scratch on
-- every run and satisfy the letter of "stable" only while the input never
-- changed order.
--
-- A SEQUENCE, NOT A HASH. dbt_utils.generate_surrogate_key would be stable by
-- construction and need no state at all -- but kea_serving_schema.sql types
-- these as `bigint GENERATED ALWAYS AS IDENTITY` (line 110), and Stage 5 has
-- to land in that column. A hash would not fit and would not be an integer.
--
-- SEEDED FROM METERING ONLY. billing.accounts also carries an icp_number, but
-- an ICP that exists in billing and not in metering is design §6.3's orphan
-- reference -- a quarantine case, not a new key. Minting an id for it here
-- would silently manufacture the connection the quarantine exists to refuse.

with source as (
    select distinct icp_identifier as icp_number
    from {{ ref('stg_metering__connection_points') }}
    where icp_identifier is not null
),

new_keys as (
    select icp_number from source
    {% if is_incremental() %}
    where icp_number not in (select icp_number from {{ this }})
    {% endif %}
)

select
    icp_number,
    {% if is_incremental() %}
    (select coalesce(max(icp_id), 0) from {{ this }})
    {% else %}
    0
    {% endif %}
      + row_number() over (order by icp_number) as icp_id,
    current_timestamp() as first_seen
from new_keys
