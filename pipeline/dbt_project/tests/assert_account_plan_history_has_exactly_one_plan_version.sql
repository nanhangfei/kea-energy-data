-- A historical plan code must resolve at its own valid_from date. This is a
-- gate, not a filter: exporting fewer history rows would preserve FK integrity
-- but hide an unresolved tariff version from the audit trail.

select
    history_id,
    account_id,
    plan_code,
    valid_from,
    versions_matched
from {{ ref('int_billing__account_plan_history_resolution') }}
where versions_matched <> 1
   or plan_id is null
