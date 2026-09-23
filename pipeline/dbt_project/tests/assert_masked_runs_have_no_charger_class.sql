-- THE CLAIM detect-v2 EXISTS TO REFUSE. Only `import = max(0, load - pv)` and
-- `export = max(0, pv - load)` are metered; generation never is. So in a
-- half-hour the house exports, load is invisible, and in a daylight half-hour
-- it does not, load is understated by exactly the PV nothing on the meter
-- reveals. A 7 kW wallbox under a generating array can present as 2 kW of
-- import -- under the 5 kW `high_power` threshold -- and detect-v1 published
-- that as a portable charger.
--
-- Three claims are therefore forbidden on a session with any masked half-hour:
--
--   charger_class = 'low_power'   a shaved wallbox reads the same. 'high_power'
--                                 is still allowed: PV only ever subtracts, so
--                                 a run that cleared 5 kW anyway really did.
--   confidence above 'low'        the reading is short by an unknown amount.
--   masked_half_hours > half_hours  arithmetic, not judgement.
--
-- MEASURED, NOT MODELLED. `masked_half_hours` counts half-hours the export
-- register actually recorded, or half-hours between the day's first and last
-- export. No reconstructed PV enters a published model (design §15.2).
--
-- The serving CHECK `charging_session_masking` (V006) says the same thing, so
-- this is not the only guard -- but a warehouse build should fail here rather
-- than at the end of `make publish-serving`, an hour later.
--
-- ⚠️ THIS TEST IS SILENT WITHOUT A SOLAR+EV HOUSEHOLD. On a cohort with none,
-- every session has masked_half_hours = 0 and nothing is exercised; that is
-- what planted.SOLAR_PLAN is for.
--
-- Returns offending rows; dbt fails the test if any come back.

select
    icp_id,
    session_start,
    half_hours,
    masked_half_hours,
    charger_class,
    confidence
from {{ ref('mart_charging_session') }}
where masked_half_hours > half_hours
   or (masked_half_hours > 0
       and (charger_class = 'low_power' or confidence != 'low'))
