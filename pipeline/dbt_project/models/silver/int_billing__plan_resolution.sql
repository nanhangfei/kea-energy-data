-- Design §2.1.2 / §6.2 rule 2: THE MOST DANGEROUS STEP IN THE PIPELINE, because
-- the wrong answer is a plausible one.
--
-- A bill must be rated against the plan version IN FORCE AT ITS PERIOD END --
-- not the current version, not the newest, not the nearest. The catalogue holds
-- 73 versions across 41 plan codes, so resolving by code alone silently picks
-- one of up to several tariffs and every downstream number is quietly wrong. No
-- constraint catches it; the bill still reconciles to itself.
--
-- PERIOD END, NOT ISSUE DATE. An invoice issued in April for a March period must
-- be rated on March's tariff. Using issued_at would be right most months and
-- wrong exactly across a price change -- the case that matters.
--
-- HALF-OPEN ON valid_to, AND NULL MEANS OPEN-ENDED. `valid_to >= period_end_nz`
-- rather than `>`, because a version whose validity ends on the period end date
-- still covers that day.
--
-- ⚠️ THE COMPARISON IS AGAINST AN EXPLICIT NZ CALENDAR DATE, AND THAT IS NOT
-- COSMETIC. period_to is a TIMESTAMP_TZ instant; valid_from/valid_to are DATEs.
-- Comparing the two directly makes Snowflake resolve the instant to a date
-- using the SESSION timezone, which defaults to America/Los_Angeles. A period
-- ending at midnight NZ is 05:00 the PREVIOUS DAY in Los Angeles, so the same
-- bill would resolve to a different plan version depending on who ran the
-- query -- and a version boundary falling on that day would flip the answer
-- with nothing to show for it. Design §4.2 already fixes the rule everywhere
-- else in this pipeline: a calendar day is an NZ calendar day.
--
-- NO FALLBACK WHEN NOTHING MATCHES. Design §6.3 is explicit: do not fall back to
-- the nearest version, because that IS the silent failure. Unmatched invoices
-- come out with a null plan_id and are quarantined by int_integrity__rejects.
-- This model's job is to resolve or to say it cannot.

with invoices as (
    select
        invoice_id, invoice_number, account_id, plan_code, period_to, issued_at,
        convert_timezone('Pacific/Auckland', period_to)::date as period_end_nz
    from {{ ref('stg_billing__invoices') }}
),

versions as (
    select plan_code, valid_from, valid_to
    from {{ ref('stg_catalogue__price_plans') }}
),

matched as (
    select
        i.invoice_id,
        i.invoice_number,
        i.account_id,
        i.plan_code,
        i.period_to,
        i.period_end_nz,
        v.valid_from as effective_from,
        -- Ranked, then filtered to 1, so overlapping versions in the catalogue
        -- resolve deterministically to the latest one that legitimately covers
        -- the date rather than failing the whole build on a duplicate. The
        -- count below is what makes an overlap visible instead of silent.
        row_number() over (
            partition by i.invoice_id order by v.valid_from desc
        ) as _rank,
        count(*) over (partition by i.invoice_id) as versions_matched
    from invoices i
    left join versions v
      on v.plan_code = i.plan_code
     and v.valid_from <= i.period_end_nz
     and (v.valid_to is null or v.valid_to >= i.period_end_nz)
)

select
    m.invoice_id,
    m.invoice_number,
    m.account_id,
    m.plan_code,
    m.period_to,
    m.period_end_nz,
    m.effective_from,
    k.plan_id,
    -- 0 = nothing covered the period (quarantine); >1 = the catalogue has
    -- overlapping versions, which is a source defect worth surfacing even
    -- though this model picks one.
    case when m.effective_from is null then 0 else m.versions_matched end
      as versions_matched
from matched m
left join {{ ref('key_map_plan') }} k
  on k.plan_code = m.plan_code
 and k.effective_from = m.effective_from
where m._rank = 1
