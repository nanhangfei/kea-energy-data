-- billing.customer  <-  dim_customer
--
-- §7.4: customer has no dependencies at all and can load first, in parallel
-- with everything else.
--
-- dim_customer also carries the customer's address. It is dropped here on
-- purpose: serving puts an address on metering.property, reached through the
-- ICP, and carrying a second copy on the customer would create two addresses
-- that can disagree with no rule for which one wins.
select
    customer_id,
    customer_number,
    customer_name    as full_name,
    email_address    as email,
    phone_number     as phone,
    is_medically_dependent,
    is_vulnerable,
    created_at,
    last_modified_at as updated_at
from KEA.KEA_GOLD.DIM_CUSTOMER
order by customer_id
