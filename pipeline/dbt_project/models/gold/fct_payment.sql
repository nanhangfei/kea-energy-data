-- Grain: one source payment attempt. Failed direct debits stay facts: absence
-- of a payment cannot distinguish "not paid yet" from "payment attempted and
-- bounced", and both the bill balance and future hardship rules need that
-- distinction.
--
-- account_id is the source identity carried unchanged by billing.account's
-- serving export. invoice_id can only become bill_id when that invoice itself
-- is exportable; otherwise it is null because billing.payment.bill_id is an
-- optional FK and a dangling reference would abort the whole serving load.

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
    p.payment_id,
    p.account_id as source_account_id,
    case when r.plan_id is not null then p.invoice_id end as source_bill_id,
    p.paid_at,
    p.amount,
    p.payment_method,
    case p.failed_flag
        when 'Y' then true
        when 'N' then false
    end as is_failed,
    p.failure_reason
from {{ ref('int_billing__payments_deduped') }} p
join eligible_accounts a
  on a.account_id = p.account_id
left join {{ ref('int_billing__plan_resolution') }} r
  on r.invoice_id = p.invoice_id
 and r.plan_id is not null
