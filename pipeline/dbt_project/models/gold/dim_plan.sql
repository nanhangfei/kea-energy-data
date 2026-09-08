-- Design §6.4. ONE ROW PER PLAN VERSION, not per plan code.
--
-- 73 versions across 41 codes. A dimension keyed on the code would make
-- temporal resolution impossible downstream and re-rate history against
-- current tariffs -- design §2.1.2's silent failure. valid_from/valid_to are
-- carried so a consumer can see the bracket that was resolved on, rather than
-- having to trust that it was.

select
    k.plan_id,
    p.plan_code,
    p.plan_name,
    p.network_code,
    p.user_category,
    p.is_tou,
    p.prompt_payment_discount_pct,
    p.valid_from                as effective_from,
    p.valid_to                  as effective_to,
    p.is_active,
    -- The source's own identity column, kept for tracing back to
    -- src_catalogue only. NOT a join key: see key_map_plan's warning.
    p.plan_id                   as source_plan_id
from {{ ref('stg_catalogue__price_plans') }} p
join {{ ref('key_map_plan') }} k
  on k.plan_code = p.plan_code
 and k.effective_from = p.valid_from
