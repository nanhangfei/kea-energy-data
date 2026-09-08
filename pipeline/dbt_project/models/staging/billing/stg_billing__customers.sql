-- 1:1 with src_billing.customers. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- Snapshot. Renaming only: cust_*/addr_* are the billing system's
-- abbreviations, spelled out once here so no downstream model repeats them.

select
    cust_id            as customer_id,
    cust_no            as customer_number,
    cust_name          as customer_name,
    email_addr         as email_address,
    phone_no           as phone_number,
    addr_line1         as address_line_1,
    addr_line2         as address_line_2,
    addr_suburb        as address_suburb,
    addr_city          as address_city,
    addr_postcode      as address_postcode,
    is_medically_dependent,
    vulnerable_flag    as is_vulnerable,
    created_ts         as created_at,
    last_mod_ts        as last_modified_at,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('billing', 'customers') }}
