{{ config(materialized='incremental', unique_key='customer_number') }}

-- Design §6.2 rule 1, same mechanism as key_map_icp: append-only, sequential,
-- because kea_serving_schema.sql:73 types customer_id as bigint identity.
-- See key_map_icp for why `incremental` is what makes "append-only" true
-- rather than merely intended.

with source as (
    select distinct customer_number
    from {{ ref('stg_billing__customers') }}
    where customer_number is not null
),

new_keys as (
    select customer_number from source
    {% if is_incremental() %}
    where customer_number not in (select customer_number from {{ this }})
    {% endif %}
)

select
    customer_number,
    {% if is_incremental() %}
    (select coalesce(max(customer_id), 0) from {{ this }})
    {% else %}
    0
    {% endif %}
      + row_number() over (order by customer_number) as customer_id,
    current_timestamp() as first_seen
from new_keys
