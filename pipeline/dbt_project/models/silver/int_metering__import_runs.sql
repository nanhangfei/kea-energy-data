-- Grain: one run of high import. What each run looked like, and what it is
-- taken to be (design §15). Only 'ev' and 'timed_night' are published;
-- 'seasonal_night' and 'other' stay here for whoever debugs a detection.
--
-- CLASSIFICATION, FIRST MATCH WINS. Rules 1 and 3 read the ICP's history:
-- for each start slot, the nights on which a run started within
-- detect_slot_tolerance of it. A slot counts half-hours from 12:00 local, so a
-- night is one slot sequence and a 23:00 timer is slot 22 on every date,
-- daylight saving or not.
--
--   1 seasonal_night  starts in the same slot on most nights of the coldest
--                     months, AND an assessed warm month exists after its
--                     first run, AND it (almost) never runs in those warm
--                     months. LCL households may have UK storage heaters;
--                     those belong to the heating slope, not here. An EV that
--                     arrives after summer has no warm month after its first
--                     run, so it cannot be mistaken for one.
--   2 ev, high_power  two half-hours at 5 kW above the floor, WHATEVER THE
--                     START TIME: a timer-charged EV starts at 23:00 each time.
--   3 timed_night     starts at night, draws 1.5 to 4 kW, and starts in the
--                     same slot on most assessed nights since its first run,
--                     warm months included. A wallbox on a timer charges two
--                     to four nights a week, below the share, and rule 2 has
--                     already taken it.
--   4 ev, low_power   a steady 1.8 to 4 kW for three hours or more.
--   5 other           ovens, heaters, one-off spikes. Not published.
--
-- ⚠️ WHAT PV MASKING CHANGES (detect-v2). masked_half_hours counts the
-- half-hours of a run the import register did not see in full. Where it is
-- above zero:
--
--   * charger_class is NULL rather than 'low_power'. This is the one false
--     claim detect-v2 exists to remove: a 7 kW wallbox shaved by 4 kW of sun
--     and a 2 kW portable charger are THE SAME SIGNAL on this meter, and
--     saying 'low_power' picks one of them without evidence. NULL is not
--     "no EV found" -- detected_class is still 'ev'.
--   * 'high_power' still stands where it is met. PV only ever SUBTRACTS from
--     the reading, so a run that cleared 5 kW after the subtraction really
--     did draw at least that.
--   * confidence is capped at 'low'.
--
-- Classification itself is untouched. Masking is about what may be said of a
-- run, not about whether it happened; a rule that changed with the sun would
-- make a household's class move across the year for no reason in the house.
-- seasonal_night and timed_night are unaffected in practice -- their night
-- slots (18-37, about 21:00 to 02:30) never overlap daylight, which the
-- planted 23:00 solar control exists to prove rather than assume.
--
-- ⚠️ THE ADJACENT LEVEL is the mean import of the half-hour just before the
-- run and the one just after it: what the household drew around the load, so
-- the low bound is the draw above it. The one before is never a candidate.
-- The one after can be, when a tail is followed at once by the next run; the
-- level is then too high and the low bound too low, which is the safe side.
-- Where both are missing (a gap either side) the candidate threshold stands
-- in: the most a non-candidate half-hour could have drawn.

{% set cold = var('detect_cold_months') | join(', ') %}
{% set warm = var('detect_warm_months') | join(', ') %}
{% set tol = var('detect_slot_tolerance') %}

with hh as (
    select * from {{ ref('int_metering__import_half_hours') }}
),

runs as (
    select
        icp_id,
        run_start,
        min(slot_no)                                                 as first_slot,
        max(slot_no) + 1                                             as end_slot,
        count(*)                                                     as half_hours,
        count_if(not is_tail)                                        as core_half_hours,
        sum(kwh)                                                     as kwh,
        avg(case when not is_tail then excess_kwh end)               as mean_excess_kwh,
        coalesce(stddev_pop(case when not is_tail then excess_kwh end), 0)
                                                                     as sd_excess_kwh,
        max(excess_kwh)                                              as max_excess_kwh,
        count_if(not is_tail
                 and excess_kwh >= {{ var('detect_high_power_excess_kwh') }})
                                                                     as high_power_half_hours,
        -- Half-hours of this run the meter could not see in full: exporting,
        -- or between the day's first and last export. Zero for every run on
        -- a household without panels, and for every night run.
        count_if(is_masked or is_daylight_shoulder)                  as masked_half_hours
    from hh
    where run_start is not null
    group by 1, 2
),

adjacent as (
    select
        r.*,
        b.kwh                                                        as kwh_before,
        a.kwh                                                        as kwh_after,
        case when b.kwh is not null or a.kwh is not null
             then (coalesce(b.kwh, 0) + coalesce(a.kwh, 0))
                  / (iff(b.kwh is not null, 1, 0) + iff(a.kwh is not null, 1, 0))
        end                                                          as adjacent_kwh
    from runs r
    left join hh b on b.icp_id = r.icp_id and b.slot_no = r.first_slot - 1
    left join hh a on a.icp_id = r.icp_id and a.slot_no = r.end_slot
),

-- The run's night: its local start moved back 12 hours, so 12:00 to 12:00 is
-- one date and one slot sequence. Wall-clock arithmetic, so a DST night keeps
-- its 23:00 in slot 22.
local_start as (
    select
        a.*,
        dateadd(hour, -12,
                convert_timezone('Pacific/Auckland', a.run_start)::timestamp_ntz)
                                                                     as night_clock
    from adjacent a
),

timed as (
    select
        l.*,
        l.night_clock::date                                          as night_date,
        hour(l.night_clock) * 2 + floor(minute(l.night_clock) / 30)  as night_slot
    from local_start l
),

slots as (
    select row_number() over (order by seq4()) - 1                   as center_slot
    from table(generator(rowcount => 48))
),

-- Each ICP × slot: the nights with a run starting within the tolerance.
pattern_nights as (
    select distinct
        t.icp_id,
        s.center_slot,
        t.night_date
    from timed t
    join slots s on abs(t.night_slot - s.center_slot) <= {{ tol }}
),

assessed as (
    select distinct icp_id, usage_date
    from hh
    where is_assessed_day
),

slot_first as (
    select
        icp_id,
        center_slot,
        min(night_date)                                              as first_night,
        count(*)                                                     as nights_with,
        count_if(month(night_date) in ({{ cold }}))                  as cold_nights_with
    from pattern_nights
    group by 1, 2
),

slot_assessed as (
    select
        f.icp_id,
        f.center_slot,
        count(a.usage_date)                                          as assessed_nights,
        count_if(month(a.usage_date) in ({{ cold }}))                as assessed_cold_nights
    from slot_first f
    left join assessed a
           on a.icp_id = f.icp_id
          and a.usage_date >= f.first_night
    group by 1, 2
),

-- Warm months with enough assessed days to say a block did not run in them.
warm_months as (
    select
        icp_id,
        date_trunc('month', usage_date)::date                        as month_start,
        count(*)                                                     as assessed_days
    from assessed
    where month(usage_date) in ({{ warm }})
    group by 1, 2
    having count(*) >= {{ var('detect_min_days_in_month') }}
),

-- Only months that start after the first run: before it, the load was not there.
slot_warm as (
    select
        f.icp_id,
        f.center_slot,
        count(*)                                                     as warm_months_observed,
        sum(w.assessed_days)                                         as warm_nights_assessed
    from slot_first f
    join warm_months w
      on w.icp_id = f.icp_id
     and w.month_start > f.first_night
    group by 1, 2
),

slot_warm_with as (
    select
        f.icp_id,
        f.center_slot,
        count(*)                                                     as warm_nights_with
    from slot_first f
    join warm_months w
      on w.icp_id = f.icp_id
     and w.month_start > f.first_night
    join pattern_nights p
      on p.icp_id = f.icp_id
     and p.center_slot = f.center_slot
     and date_trunc('month', p.night_date)::date = w.month_start
    group by 1, 2
),

slot_stats as (
    select
        f.icp_id,
        f.center_slot,
        f.first_night,
        f.nights_with,
        sa.assessed_nights,
        least(f.nights_with / nullif(sa.assessed_nights, 0), 1)      as night_share,
        least(f.cold_nights_with / nullif(sa.assessed_cold_nights, 0), 1)
                                                                     as cold_share,
        coalesce(sw.warm_months_observed, 0)                         as warm_months_observed,
        coalesce(sww.warm_nights_with, 0) / nullif(sw.warm_nights_assessed, 0)
                                                                     as warm_share
    from slot_first f
    join slot_assessed sa
      on sa.icp_id = f.icp_id and sa.center_slot = f.center_slot
    left join slot_warm sw
      on sw.icp_id = f.icp_id and sw.center_slot = f.center_slot
    left join slot_warm_with sww
      on sww.icp_id = f.icp_id and sww.center_slot = f.center_slot
),

classified as (
    select
        t.*,
        st.first_night                                               as pattern_first_night,
        st.night_share,
        st.cold_share,
        st.warm_share,
        st.warm_months_observed,
        case
            when st.cold_share > {{ var('detect_seasonal_min_cold_share') }}
             and st.warm_months_observed >= 1
             and st.warm_share <= {{ var('detect_seasonal_max_warm_share') }}
            then 'seasonal_night'
            when t.high_power_half_hours >= {{ var('detect_high_power_min_half_hours') }}
            then 'ev'
            when t.night_slot between {{ var('detect_night_first_slot') }}
                                  and {{ var('detect_night_last_slot') }}
             and t.mean_excess_kwh between {{ var('detect_timed_min_excess_kwh') }}
                                       and {{ var('detect_timed_max_excess_kwh') }}
             and st.night_share >= {{ var('detect_timed_min_night_share') }}
            then 'timed_night'
            when t.core_half_hours >= {{ var('detect_low_power_min_half_hours') }}
             and t.mean_excess_kwh between {{ var('detect_low_power_min_excess_kwh') }}
                                       and {{ var('detect_low_power_max_excess_kwh') }}
             and t.sd_excess_kwh <= {{ var('detect_low_power_max_excess_sd') }}
            then 'ev'
            else 'other'
        end                                                          as detected_class
    from timed t
    join slot_stats st
      on st.icp_id = t.icp_id
     and st.center_slot = t.night_slot
)

select
    icp_id,
    run_start,
    dateadd(minute, 30 * half_hours, run_start)                      as run_end,
    half_hours,
    core_half_hours,
    kwh,
    mean_excess_kwh,
    sd_excess_kwh,
    max_excess_kwh,
    high_power_half_hours,
    masked_half_hours,
    kwh_before,
    kwh_after,
    adjacent_kwh,
    night_date,
    night_slot,
    pattern_first_night,
    night_share,
    cold_share,
    warm_share,
    warm_months_observed,
    detected_class,
    case
        when detected_class = 'ev'
         and high_power_half_hours >= {{ var('detect_high_power_min_half_hours') }}
        then 'high_power'
        -- Not 'low_power' when the sun was in the way: see the header.
        when detected_class = 'ev' and masked_half_hours = 0 then 'low_power'
    end                                                              as charger_class,
    case
        when masked_half_hours > 0
        then iff(detected_class in ('ev', 'timed_night'), 'low', null)
        when detected_class = 'ev' then
            case
                when high_power_half_hours >= {{ var('detect_high_power_min_half_hours') }} + 1
                then 'high'
                when high_power_half_hours >= {{ var('detect_high_power_min_half_hours') }}
                then 'medium'
                -- low_power is never more than medium: a heater can look the same.
                when core_half_hours >= {{ var('detect_low_power_medium_half_hours') }}
                then 'medium'
                else 'low'
            end
        when detected_class = 'timed_night' then
            iff(night_share >= {{ var('detect_timed_high_confidence_share') }}, 'high', 'medium')
    end                                                              as confidence,
    '{{ var("detect_method_version") }}'                             as method_version
from classified
