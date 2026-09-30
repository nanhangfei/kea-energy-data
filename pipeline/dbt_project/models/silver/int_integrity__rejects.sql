-- ONE QUARANTINE TABLE, ONE VOCABULARY (design §6.3). Parse failures, dbt
-- validation rejections and cross-system integrity failures all land in the
-- same place with the same `_reject_reason` words, because fragmenting them
-- across three systems is exactly what makes a data issue undiagnosable at 9am
-- on demo day. This model is the integrity third of that; the codes it emits
-- are declared in pipeline/contracts/reject_reasons.json and nowhere else.
--
-- THESE ARE THE REJECTS THAT NEED A SECOND TABLE TO SEE. Parse can decide its
-- own group from one row of one file. None of these can: an orphan needs the
-- metering extract, an unknown plan code needs the catalogue, and a missing
-- plan version needs the catalogue AND the bill's period.
--
-- A QUARANTINED ACCOUNT IS NOT A DROPPED ACCOUNT. Rows are routed, never
-- deleted -- the row stays in staging and the lake. What this table changes is
-- what the AGENT is allowed to do: design §6.3's last paragraph makes a
-- quarantined account a reason to hedge or decline rather than answer
-- confidently, which is the link most pipelines never make.

with orphan_icp as (
    -- Design §6.3 class 1, but only once it has stopped looking like class 2.
    select
        'account'                       as subject_type,
        a.account_number                as subject_id,
        'integrity.orphan_icp'          as reject_reason,
        'icp_number ' || a.icp_number || ' has no match in the metering extract after '
          || s.consecutive_misses || ' consecutive metering extracts' as detail
    from {{ ref('stg_billing__accounts') }} a
    join {{ ref('int_integrity__orphan_streak') }} s
      on s.subject_key = 'account:' || a.account_number
    where s.consecutive_misses >= {{ var('integrity_max_misses') }}
),

timing_skew as (
    -- Design §6.3 class 2. Reported, not yet a fault: the next metering extract
    -- is expected to resolve it. Present in the table so "why is this account
    -- not answerable yet" has an answer today rather than three extracts later.
    select
        'account'                       as subject_type,
        a.account_number                as subject_id,
        'integrity.timing_skew'         as reject_reason,
        'icp_number ' || a.icp_number || ' absent from metering; miss '
          || s.consecutive_misses || ' of {{ var('integrity_max_misses') }}' as detail
    from {{ ref('stg_billing__accounts') }} a
    join {{ ref('int_integrity__orphan_streak') }} s
      on s.subject_key = 'account:' || a.account_number
    where s.consecutive_misses < {{ var('integrity_max_misses') }}
),

unknown_plan_code as (
    -- Design §6.3 class 3. Distinct from class 4: the code itself is absent, so
    -- there is no version to be temporal about.
    select
        'account'                       as subject_type,
        a.account_number                as subject_id,
        'integrity.unknown_plan_code'   as reject_reason,
        'plan_code ' || a.plan_code || ' is not in the catalogue extract' as detail
    from {{ ref('stg_billing__accounts') }} a
    where a.plan_code is not null
      and not exists (
        select 1 from {{ ref('key_map_plan') }} k where k.plan_code = a.plan_code
      )
),

ambiguous_register as (
    -- Design §6.3 has four classes; this is a fifth, and it was planted for
    -- Stage 4 to find. 05_defects.sql injects an ICP whose register_code maps
    -- to more than one meter and says so directly: "no rule is documented for
    -- picking one ... Stage 4 needs the rule."
    --
    -- THE RULE IS TO REFUSE. A read carries no meter_id, so when two live
    -- meters share a register_code there is genuinely nothing to resolve on --
    -- and a join that picks one is not a guess, it is a FAN-OUT: every reading
    -- at that ICP is multiplied by the number of candidates, and the total
    -- still looks like a plausible number. Quarantining is the only honest
    -- answer, and fct_meter_read excludes these rather than inflating them.
    --
    -- Live-at-the-instant filtering is applied first, in the fact model, so
    -- this fires only on ICPs that stay ambiguous after that.
    select
        'icp_register'                  as subject_type,
        r.icp_number || '|' || r.register_code as subject_id,
        'integrity.ambiguous_register'  as reject_reason,
        'register_code ' || r.register_code || ' at icp ' || r.icp_number
          || ' resolves to ' || r.candidates || ' live registers' as detail
    from {{ ref('int_metering__register_candidates') }} r
    where r.candidates > 1
),

no_plan_version as (
    -- Design §6.3 class 4, and the one that must never fall back to the nearest
    -- version. int_billing__plan_resolution deliberately resolves to null
    -- rather than guessing; this is where that null becomes a decision.
    select
        'invoice'                       as subject_type,
        r.invoice_number                as subject_id,
        'integrity.no_plan_version'     as reject_reason,
        'no version of plan ' || r.plan_code || ' brackets NZ period end '
          || to_varchar(r.period_end_nz, 'YYYY-MM-DD') as detail
    from {{ ref('int_billing__plan_resolution') }} r
    where r.versions_matched = 0
)

select *, current_timestamp() as quarantined_at from orphan_icp
union all select *, current_timestamp() from timing_skew
union all select *, current_timestamp() from unknown_plan_code
union all select *, current_timestamp() from no_plan_version
union all select *, current_timestamp() from ambiguous_register
