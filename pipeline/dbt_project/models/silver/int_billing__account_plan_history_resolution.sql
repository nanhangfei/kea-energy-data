-- Account plan history must resolve a plan CODE to the tariff VERSION that was
-- in force when the account moved onto it. Joining to the active/current plan
-- would turn a 2025 signup into the 2026 tariff version and make history lie
-- while every foreign key still passes.
--
-- No nearest-version fallback: a history row with no bracketed catalogue
-- version keeps a null plan_id, and the dbt test asserts it before any serving
-- export can omit it silently. Catalogue overlaps are also surfaced through
-- versions_matched rather than hidden by the deterministic latest-start choice.
--
-- VALID_TO is exclusive. The source rater uses the same predicate: versions
-- that abut at (say) 2026-04-01 must resolve a history change on that date to
-- the newer version only, rather than count both as a catalogue overlap.

with history as (
    select
        history_id,
        account_id,
        plan_code,
        term_months,
        valid_from,
        valid_to,
        change_reason
    from {{ ref('stg_billing__account_plan_history') }}
),

versions as (
    select plan_code, valid_from, valid_to
    from {{ ref('stg_catalogue__price_plans') }}
),

matched as (
    select
        h.*,
        v.valid_from as effective_from,
        row_number() over (
            partition by h.history_id
            order by v.valid_from desc nulls last
        ) as _rank,
        count(v.valid_from) over (partition by h.history_id) as versions_matched
    from history h
    left join versions v
     on v.plan_code = h.plan_code
     and v.valid_from <= h.valid_from
     and (v.valid_to is null or h.valid_from < v.valid_to)
)

select
    m.history_id,
    m.account_id,
    m.plan_code,
    m.term_months,
    m.valid_from,
    m.valid_to,
    m.change_reason,
    m.effective_from,
    k.plan_id,
    m.versions_matched
from matched m
left join {{ ref('key_map_plan') }} k
  on k.plan_code = m.plan_code
 and k.effective_from = m.effective_from
where m._rank = 1
