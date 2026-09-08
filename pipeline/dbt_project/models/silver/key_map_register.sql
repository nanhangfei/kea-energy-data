{{ config(materialized='incremental', unique_key='register_key') }}

-- Design §6.2's fourth key map, which the design doc does not list -- because
-- the ambiguity below only becomes visible once something tries to build
-- fct_meter_read, whose serving PK is (icp_id, register_id, interval_start).
--
-- KEYED ON (icp_number, register_code), WHICH IS THE GRAIN READS ARRIVE AT.
-- src_metering.registers is UNIQUE(meter_id, register_code), but interval_reads
-- carries no meter_id -- it is keyed on (icp_identifier, register_code). So the
-- read grain and the register grain are not the same grain, and this map is
-- deliberately built on the READ's, so every read resolves to exactly one
-- surrogate.
--
-- WHICH PHYSICAL METER that surrogate corresponds to is a separate question,
-- and for at least one ICP it has no answer. See int_integrity__rejects'
-- ambiguous_register: that is where the unanswerable case is routed, rather
-- than being smuggled into the numbering here.

with source as (
    select distinct
        icp_identifier as icp_number,
        register_code
    from {{ ref('int_metering__reads_deduped') }}
    where icp_identifier is not null
      and register_code is not null
),

new_keys as (
    select icp_number, register_code from source
    {% if is_incremental() %}
    where (icp_number, register_code) not in (
        select icp_number, register_code from {{ this }}
    )
    {% endif %}
)

select
    icp_number || '|' || register_code as register_key,
    icp_number,
    register_code,
    {% if is_incremental() %}
    (select coalesce(max(register_id), 0) from {{ this }})
    {% else %}
    0
    {% endif %}
      + row_number() over (order by icp_number, register_code) as register_id,
    current_timestamp() as first_seen
from new_keys
