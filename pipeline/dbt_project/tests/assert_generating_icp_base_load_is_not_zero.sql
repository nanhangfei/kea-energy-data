-- A household with panels still has a fridge at 3am. `base_load_kwh` used to
-- read 0 for exactly those households on exactly their sunniest days, because
-- `import = max(0, load - pv)` puts a zero on the IMPORT register whenever the
-- panels cover the draw, and the day's minimum then measured the sun rather
-- than the house. mart_daily_usage now takes the minimum over 22:00-05:00 for
-- any ICP with an export register (design §8.4); this keeps it that way
-- against real data, where the unit test only covers the fixture.
--
-- COMPLETE DAYS ONLY. A day missing its dark half-hours has nothing to take a
-- minimum over, and that is a completeness problem, not this one.
--
-- DAYS WITH SOME IMPORT ONLY, for the same reason. A household that imports
-- nothing across all 48 half-hours has no observable load at all, so 0 is the
-- honest answer rather than a symptom: overnight `pv` is 0, so a zero dark
-- half-hour means the house genuinely drew nothing. This is a property of the
-- LCL source, not of solar -- the 19 Sep 2026 rebuild had 702 such days across
-- 27 households with NO panels, against 4 on generating ICPs, and every one of
-- them reads 0 whichever window is used. Without this filter the test would
-- fail on vacancy.
--
-- DARK ZEROS THE METER REALLY READ ARE NOT THE BUG EITHER. Some LCL meters
-- report 0 for a night half-hour on a day the house otherwise draws power:
-- at 200 households (2 Oct 2026) four of the 20 households with panels did,
-- on 190, 147, 21 and 12 nights in the raw London readings, and their base
-- load is then honestly 0. So the test checks the computation rather than
-- the data: a zero base load fails only when NO 22:00-05:00 import half-hour
-- that day read zero -- which is exactly what taking the whole day's minimum
-- (the bug) would produce on a sunny day.
--
-- Returns offending rows; dbt fails the test if any come back.

with generating_icps as (
    select distinct icp_id
    from {{ ref('dim_register') }}
    where flow_direction = 'export'
),

dark_minimum as (
    select f.icp_id,
           convert_timezone('Pacific/Auckland', f.interval_start)::date as usage_date,
           min(f.kwh)                                                 as dark_min_kwh
    from {{ ref('fct_meter_read') }} f
    join {{ ref('dim_register') }} r on r.register_id = f.register_id
    join generating_icps g on g.icp_id = f.icp_id
    where r.flow_direction = 'import'
      and (hour(convert_timezone('Pacific/Auckland', f.interval_start)) >= 22
           or hour(convert_timezone('Pacific/Auckland', f.interval_start)) < 5)
    group by 1, 2
)

select
    u.icp_id,
    u.usage_date,
    u.base_load_kwh,
    u.import_kwh,
    u.export_kwh,
    u.interval_count
from {{ ref('mart_daily_usage') }} u
join generating_icps g on g.icp_id = u.icp_id
join dark_minimum d on d.icp_id = u.icp_id and d.usage_date = u.usage_date
where u.base_load_kwh = 0
  and d.dark_min_kwh > 0
  and u.import_kwh > 0
  and u.interval_count = u.expected_intervals
