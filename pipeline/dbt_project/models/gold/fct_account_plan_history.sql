-- Grain: one account-plan-term interval. The plan_id was temporally resolved
-- in silver at the history row's valid_from date; never substitute the
-- account's active/current plan here.

with eligible_accounts as (
    select a.account_id
    from {{ ref('stg_billing__accounts') }} a
    join {{ ref('key_map_icp') }} i
      on i.icp_number = a.icp_number
    join {{ ref('stg_billing__customers') }} sc
      on sc.customer_id = a.customer_id
    join {{ ref('key_map_customer') }} c
      on c.customer_number = sc.customer_number
    join {{ ref('dim_plan') }} p
      on p.plan_code = a.plan_code
     and p.is_active
)

select
    h.history_id,
    h.account_id as source_account_id,
    h.plan_id,
    h.term_months,
    h.valid_from,
    h.valid_to,
    h.change_reason
from {{ ref('int_billing__account_plan_history_resolution') }} h
join eligible_accounts a
  on a.account_id = h.account_id
where h.plan_id is not null
