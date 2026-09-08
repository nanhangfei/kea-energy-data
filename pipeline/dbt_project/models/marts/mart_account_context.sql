-- Grain: account. Serving target kea.mart_account_context
-- (kea_serving_schema.sql:513) -- "everything the agent needs to know about who
-- it is talking to, in one row".
--
-- Design §2 calls this the mart that most justifies the whole approach:
-- assembling it needs customer, connection and plan together, which is exactly
-- the join the brief says Kea cannot currently do. Three source systems, one
-- row, one lookup.
--
-- ⚠️ QUARANTINED ACCOUNTS ARE MARKED, NOT DROPPED. Design §6.3's last paragraph
-- is the link most pipelines never make: an account whose ICP is an orphan, or
-- whose plan code is unknown, must make the agent HEDGE OR DECLINE rather than
-- answer confidently. Removing the row would make the agent fail to find the
-- account and say something unhelpful but harmless; keeping it with a flag lets
-- it say "I cannot answer confidently for this account, and here is why", which
-- is the behaviour §6.3 asks for. So the row stays and carries its reason.

with quarantined_subjects as (
    -- Account-level integrity failures already carry the account number.
    select subject_id as account_number, reject_reason
    from {{ ref('int_integrity__rejects') }}
    where subject_type = 'account'

    union all

    -- A register ambiguity is recorded at its honest grain (ICP + register),
    -- but it still makes every account on that ICP unsafe to answer for.  If
    -- this relationship is not propagated, the fact table correctly rejects
    -- every reading while the agent context incorrectly says the account is
    -- healthy.
    select a.account_number, r.reject_reason
    from {{ ref('int_integrity__rejects') }} r
    join {{ ref('stg_billing__accounts') }} a
      on a.icp_number = split_part(r.subject_id, '|', 1)
    where r.subject_type = 'icp_register'
),

quarantined as (
    select
        account_number,
        listagg(distinct reject_reason, ', ')
          within group (order by reject_reason) as reject_reasons
    from quarantined_subjects
    group by 1
)

select
    a.account_id,
    a.account_number,
    kc.customer_id,
    c.customer_name,
    c.is_medically_dependent,
    c.is_vulnerable,
    ki.icp_id,
    a.icp_number,
    i.climate_region,
    i.network_name,
    a.plan_code,
    a.payment_method,
    a.billing_cycle_day,
    a.started_on,
    a.ended_on,
    q.reject_reasons,
    q.account_number is not null           as is_quarantined
from {{ ref('stg_billing__accounts') }} a
join {{ ref('stg_billing__customers') }} c
  on c.customer_id = a.customer_id
join {{ ref('key_map_customer') }} kc
  on kc.customer_number = c.customer_number
left join {{ ref('key_map_icp') }} ki
  on ki.icp_number = a.icp_number
left join {{ ref('dim_icp') }} i
  on i.icp_id = ki.icp_id
left join quarantined q
  on q.account_number = a.account_number
