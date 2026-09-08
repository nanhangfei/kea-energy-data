-- The source keeps offer history by (term_months, valid_from); serving's
-- catalogue.term_credit intentionally exposes the one open offer per term.
-- An ended offer stays in bronze/RAW for audit but must not collide with the
-- serving table's primary key or be offered to a customer today.
--
-- `valid_to is null` is the source's explicit current-version convention. A
-- duplicate open offer is not arbitrarily ranked: the uniqueness test on this
-- model fails and blocks the export instead.

select
    term_months,
    monthly_credit,
    valid_from,
    valid_to,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ ref('stg_catalogue__term_credits') }}
where valid_to is null
