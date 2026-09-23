-- Grain: icp × half-hour of UNCONTROLLED import. The first step of Sprint 4's
-- load detection (design §15): every half-hour, how far it sits above the
-- day's floor, and which run of high import it belongs to.
--
-- UNCONTROLLED ONLY. A controlled register is already its own category in the
-- backend's breakdown; a load found there would be counted twice. Every ICP
-- has one uncontrolled import register today (UN), but the sum keeps the grain
-- right if a second one appears.
--
-- THE FLOOR IS mart_daily_usage.base_load_kwh / 48 (design §8.4): the day's
-- smallest import half-hour. The base-load driver already counts it, so a
-- detected load is only ever what sits ABOVE it, and the two never count the
-- same kWh.
--
-- ON A GENERATING ICP that minimum is taken over 22:00-05:00 only, because
-- `import = max(0, load - pv)` put a 0 on the import register whenever the
-- panels covered the draw. Until 19 Sep 2026 the floor was therefore 0 for
-- every solar household on its sunniest days, and every run detected there
-- carried the household's whole standing draw as detected kWh.
--
-- A RUN IS CONSECUTIVE CANDIDATES ON THE CONTINUOUS TIMELINE. slot_no counts
-- half-hours since the epoch, so a run crosses midnight and a daylight-saving
-- change without noticing either, and a missing read ends it. A candidate is
-- at least detect_min_excess_kwh above its own date's floor. A run needs
-- detect_min_run_half_hours candidates; a single one is a spike.
--
-- THE TAIL. A load that stops mid-half-hour draws part of one, which can fall
-- below the candidate threshold. The half-hour straight after a run joins it
-- when it is at least detect_tail_min_excess_kwh above the floor; is_tail
-- marks it, and run statistics leave it out.
--
-- WHERE THE METER CANNOT SEE (detect-v2). Only import and export are metered;
-- generation never is. So on a generating ICP the import register understates
-- load by however much the panels covered, and two half-hours are marked:
--
--   is_masked             the export register read above zero. PV exceeded
--                         load, import read 0, and this half-hour carries NO
--                         information about load at all.
--   is_daylight_shoulder  not masked, but between the day's first and last
--                         masked half-hour. The panels demonstrably generated
--                         at both ends of it, so load here is understated by
--                         an unknown amount.
--
-- Both are MEASURED, from the export register the household actually has.
-- Nothing is modelled: a reconstructed PV curve would put an estimate into a
-- published model, which design §15.2 rules out ("evidence, not a
-- breakdown"). A household with no export register has neither flag set, so
-- the 42 non-generating ICPs are byte-identical to detect-v1.
--
-- ⚠️ is_assessed_day is DELIBERATELY UNCHANGED. A solar household's night is
-- as readable as anyone else's, and dropping its daylight would destroy the
-- detection that works to protect the detection that does not. What changes
-- downstream is only what may be CLAIMED about a run touching these
-- half-hours -- never whether the day counts.

with reads as (
    select
        f.icp_id,
        f.interval_start,
        sum(f.kwh)                                                   as kwh
    from {{ ref('fct_meter_read') }} f
    join {{ ref('dim_register') }} r on r.register_id = f.register_id
    where r.flow_direction = 'import'
      and not r.is_controlled
    group by 1, 2
),

-- The same half-hours on the export register, for the ICPs that have one.
-- A generating ICP with no export row this half-hour exported nothing.
exports as (
    select
        f.icp_id,
        f.interval_start,
        sum(f.kwh)                                                   as export_kwh
    from {{ ref('fct_meter_read') }} f
    join {{ ref('dim_register') }} r on r.register_id = f.register_id
    where r.flow_direction = 'export'
    group by 1, 2
),

floored as (
    select
        r.icp_id,
        r.interval_start,
        floor(date_part(epoch_second, r.interval_start) / 1800)     as slot_no,
        d.usage_date,
        r.kwh,
        coalesce(e.export_kwh, 0)                                    as export_kwh,
        d.base_load_kwh / 48                                         as floor_kwh_per_hh,
        d.expected_intervals
    from reads r
    join {{ ref('mart_daily_usage') }} d
      on d.icp_id = r.icp_id
     and d.usage_date = convert_timezone('Pacific/Auckland', r.interval_start)::date
    left join exports e
      on e.icp_id = r.icp_id
     and e.interval_start = r.interval_start
    where d.base_load_kwh is not null
),

masked as (
    select
        f.*,
        f.export_kwh > 0                                             as is_masked,
        -- The day's first and last exporting half-hour. Null on a day that
        -- never exported, which makes the shoulder test below false for
        -- every half-hour of it -- including every day of a household with
        -- no panels at all.
        min(case when f.export_kwh > 0 then f.slot_no end)
            over (partition by f.icp_id, f.usage_date)               as first_masked_slot,
        max(case when f.export_kwh > 0 then f.slot_no end)
            over (partition by f.icp_id, f.usage_date)               as last_masked_slot
    from floored f
),

flagged as (
    select
        m.*,
        m.kwh - m.floor_kwh_per_hh                                   as excess_kwh,
        m.kwh >= m.floor_kwh_per_hh + {{ var('detect_min_excess_kwh') }}
                                                                     as is_candidate,
        -- Between the day's first and last export, but not exporting itself.
        -- ⚠️ coalesce, not the bare comparison: on a day that never exported
        -- both bounds are NULL, and `true and null` is NULL in SQL, not
        -- false. Without it every half-hour of every household with no panels
        -- would carry a NULL flag rather than a false one.
        coalesce(not m.is_masked
                 and m.slot_no > m.first_masked_slot
                 and m.slot_no < m.last_masked_slot, false)          as is_daylight_shoulder,
        -- Assessed: every half-hour of the NZ day was read. Unchanged by
        -- masking, on purpose -- see the header.
        count(*) over (partition by m.icp_id, m.usage_date) = m.expected_intervals
                                                                     as is_assessed_day
    from masked m
),

-- Gaps and islands: consecutive candidates share slot_no - row_number.
islands as (
    select
        g.*,
        case when g.is_candidate
             then g.slot_no - row_number() over (
                      partition by g.icp_id, g.is_candidate order by g.slot_no)
        end                                                          as island
    from flagged g
),

cores as (
    select
        i.*,
        case when i.is_candidate
             then min(i.interval_start) over (partition by i.icp_id, i.island)
        end                                                          as core_run_start,
        case when i.is_candidate
             then count(*) over (partition by i.icp_id, i.island)
        end                                                          as core_half_hours
    from islands i
),

tails as (
    select
        c.*,
        lag(c.slot_no)         over (partition by c.icp_id order by c.slot_no) as prev_slot_no,
        lag(c.core_run_start)  over (partition by c.icp_id order by c.slot_no) as prev_core_run_start,
        lag(c.core_half_hours) over (partition by c.icp_id order by c.slot_no) as prev_core_half_hours
    from cores c
),

assigned as (
    select
        t.*,
        case
            when t.is_candidate
             and t.core_half_hours >= {{ var('detect_min_run_half_hours') }}
            then t.core_run_start
            when not t.is_candidate
             and t.prev_slot_no = t.slot_no - 1
             and t.prev_core_half_hours >= {{ var('detect_min_run_half_hours') }}
             and t.excess_kwh >= {{ var('detect_tail_min_excess_kwh') }}
            then t.prev_core_run_start
        end                                                          as run_start
    from tails t
)

select
    icp_id,
    interval_start,
    slot_no,
    usage_date,
    kwh,
    export_kwh,
    floor_kwh_per_hh,
    excess_kwh,
    expected_intervals,
    is_assessed_day,
    is_masked,
    is_daylight_shoulder,
    is_candidate,
    -- The run this half-hour belongs to, by its first half-hour; null outside one.
    run_start,
    (run_start is not null and not is_candidate)                     as is_tail
from assigned
