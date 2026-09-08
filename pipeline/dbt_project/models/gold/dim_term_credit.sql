-- One current credit offer per commitment term. The source offer history is
-- preserved in staging/RAW; this dimension is deliberately the current
-- catalogue shape consumed by serving and plan comparison.

select
    term_months,
    monthly_credit,
    valid_from as effective_from,
    valid_to as effective_to
from {{ ref('int_catalogue__term_credits_current') }}
