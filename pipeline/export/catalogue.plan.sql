-- catalogue.plan  <-  dim_plan (+ network id via staging)
--
-- ONE ROW PER PLAN VERSION, not per plan code. 73 versions across 41 codes;
-- a table keyed on the code would make the temporal resolution design §2.1.2
-- requires impossible downstream, and mis-rate 2,878 of 11,534 invoices.
--
-- dim_plan carries network_code; serving's FK wants network_id, so the network
-- is joined from staging for that column alone. This is §7.4's dependency that
-- "reads like a sibling and is not" -- metering.network must already be loaded.
select
    p.plan_id,
    p.plan_code,
    p.plan_name,
    n.network_id,
    case p.user_category when 'LOW' then 'low_user' else 'standard' end as user_type,
    p.is_tou,
    p.prompt_payment_discount_pct,
    p.effective_from,
    p.effective_to,
    p.is_active
from KEA.KEA_GOLD.DIM_PLAN p
join KEA.KEA_STAGING.STG_METERING__NETWORKS n
  on n.network_code = p.network_code
order by p.plan_id
