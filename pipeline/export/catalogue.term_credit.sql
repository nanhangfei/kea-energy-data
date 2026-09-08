-- catalogue.term_credit  <-  dim_term_credit
--
-- The source keeps historical offer versions, but the serving table has one
-- current row per term_months. Silver explicitly selects the open version and
-- dbt asserts that this produces no duplicate term before this export runs.

select
    term_months,
    monthly_credit,
    effective_from,
    effective_to
from KEA.KEA_GOLD.DIM_TERM_CREDIT
order by term_months
