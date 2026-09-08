-- 1:1 with src_billing.account_plan_history. A history row records the plan
-- code and the date the account moved onto it; silver resolves that code to a
-- catalogue VERSION at this date. Resolving it here would mix key resolution
-- into staging and make a missing version look like a dropped source row.

select
    hist_id            as history_id,
    acct_id            as account_id,
    plan_code,
    term_months,
    valid_from,
    valid_to,
    change_rsn         as change_reason,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('billing', 'account_plan_history') }}
