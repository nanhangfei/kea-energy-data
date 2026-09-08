-- billing.account_plan_history  <-  fct_account_plan_history
--
-- plan_id is resolved at the history row's valid_from in silver. The fact only
-- retains accounts that billing.account exports, so both foreign keys are
-- present when the loader inserts this table.

select
    history_id,
    source_account_id as account_id,
    plan_id,
    term_months,
    valid_from,
    valid_to,
    change_reason
from KEA.KEA_GOLD.FCT_ACCOUNT_PLAN_HISTORY
order by history_id
