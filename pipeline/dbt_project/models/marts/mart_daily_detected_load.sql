-- Grain: icp × NZ day. Serving target kea.mart_daily_detected_load
-- (V005), PK (icp_id, usage_date).
--
-- EVIDENCE, NOT A BREAKDOWN (design §15). What the half-hourly import shows
-- that daily totals cannot: a load with an EV's charging pattern, and a load
-- that switches on at the same time most nights. The backend composes the
-- breakdown from these rows; per day, because any billing period is then a
-- plain sum.
--
-- ONE ROW FOR EVERY ASSESSED DAY, zeros when nothing was found. An assessed
-- day read every half-hour on the uncontrolled import register. No row means
-- "not assessed", which is not the same answer as "nothing there".
--
-- ⚠️ A RANGE, NEVER ONE FIGURE. low and high are the sums of
-- int_metering__detected_half_hours' bounds; high sits above the floor, which
-- the base-load driver counts, so the two never overlap.
--
-- The day's class and confidence come from its largest run of that class.
--
-- masked_intervals (detect-v2) is the day's half-hours the meter could not
-- see in full: exporting, or between the day's first and last export. It is
-- 0 for every household without panels and for every night. It is a property
-- of the DAY, not of the runs in it -- a day with no detection at all can
-- still be one the panels hid, and a reader deciding how much to trust
-- "nothing found here" needs to know that.
--
-- ⚠️ ev_kwh_low/high are UNCHANGED by masking, deliberately. detected_class
-- is still 'ev'; only ev_charger_class and ev_confidence weaken. So
-- mart_forecast_input and hdd-linear-v3 see exactly what they saw under
-- detect-v1, and there is no forecast regression to explain.

with assessed as (
    select distinct
        icp_id,
        usage_date,
        floor_kwh_per_hh
    from {{ ref('int_metering__import_half_hours') }}
    where is_assessed_day
),

-- The day's masking, over the whole day rather than over its runs.
day_masking as (
    select
        icp_id,
        usage_date,
        count_if(is_masked or is_daylight_shoulder)                  as masked_intervals
    from {{ ref('int_metering__import_half_hours') }}
    where is_assessed_day
    group by 1, 2
),

run_days as (
    select
        icp_id,
        usage_date,
        run_start,
        detected_class,
        charger_class,
        confidence,
        sum(kwh_low)                                                 as kwh_low,
        sum(kwh_high)                                                as kwh_high,
        count(*)                                                     as half_hours
    from {{ ref('int_metering__detected_half_hours') }}
    where is_assessed_day
    group by 1, 2, 3, 4, 5, 6
),

ranked as (
    select
        d.*,
        row_number() over (
            partition by d.icp_id, d.usage_date, d.detected_class
            order by d.kwh_high desc, d.run_start)                   as size_rank
    from run_days d
),

daily as (
    select
        icp_id,
        usage_date,
        sum(iff(detected_class = 'ev', kwh_low, 0))                  as ev_kwh_low,
        sum(iff(detected_class = 'ev', kwh_high, 0))                 as ev_kwh_high,
        sum(iff(detected_class = 'ev', half_hours, 0))               as ev_half_hours,
        max(iff(detected_class = 'ev' and size_rank = 1, charger_class, null))
                                                                     as ev_charger_class,
        max(iff(detected_class = 'ev' and size_rank = 1, confidence, null))
                                                                     as ev_confidence,
        sum(iff(detected_class = 'timed_night', kwh_low, 0))         as timed_night_kwh_low,
        sum(iff(detected_class = 'timed_night', kwh_high, 0))        as timed_night_kwh_high,
        sum(iff(detected_class = 'timed_night', half_hours, 0))      as timed_night_half_hours,
        max(iff(detected_class = 'timed_night' and size_rank = 1, confidence, null))
                                                                     as timed_night_confidence
    from ranked
    group by 1, 2
)

select
    a.icp_id,
    a.usage_date,
    round(a.floor_kwh_per_hh, 4)                                     as floor_kwh_per_hh,
    round(coalesce(d.ev_kwh_low, 0), 4)                              as ev_kwh_low,
    round(coalesce(d.ev_kwh_high, 0), 4)                             as ev_kwh_high,
    coalesce(d.ev_half_hours, 0)                                     as ev_half_hours,
    d.ev_charger_class,
    d.ev_confidence,
    round(coalesce(d.timed_night_kwh_low, 0), 4)                     as timed_night_kwh_low,
    round(coalesce(d.timed_night_kwh_high, 0), 4)                    as timed_night_kwh_high,
    coalesce(d.timed_night_half_hours, 0)                            as timed_night_half_hours,
    d.timed_night_confidence,
    coalesce(m.masked_intervals, 0)                                  as masked_intervals,
    '{{ var("detect_method_version") }}'                             as method_version
from assessed a
left join daily d
       on d.icp_id = a.icp_id
      and d.usage_date = a.usage_date
left join day_masking m
       on m.icp_id = a.icp_id
      and m.usage_date = a.usage_date
