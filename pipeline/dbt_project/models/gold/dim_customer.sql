-- Design §6.4. One row per customer, carrying the CONFORMED surrogate.
--
-- customer_number stays as an attribute because it appears on the bill and
-- customers quote it -- but nothing downstream may join on it. Design §6.2:
-- "Gold and marts carry only surrogate keys. Business keys are retained as
-- attributes but never used for joining downstream."

select
    k.customer_id,
    c.customer_number,
    c.customer_name,
    c.email_address,
    c.phone_number,
    c.address_line_1,
    c.address_line_2,
    c.address_suburb,
    c.address_city,
    c.address_postcode,
    c.is_medically_dependent,
    c.is_vulnerable,
    c.created_at,
    c.last_modified_at
from {{ ref('stg_billing__customers') }} c
join {{ ref('key_map_customer') }} k
  on k.customer_number = c.customer_number
