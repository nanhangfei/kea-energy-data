-- 1:1 with src_catalogue.term_credits. The source is versioned by
-- (term_months, valid_from); serving holds the current offer per term, so the
-- choice of current version belongs in silver rather than in this source view.

select
    term_months,
    monthly_credit,
    valid_from,
    valid_to,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('catalogue', 'term_credits') }}
