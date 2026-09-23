-- The invariant the whole mart rests on: the five drivers plus the
-- unexplained remainder must equal the amount delta. If they do not, the
-- decomposition is not a decomposition -- it is six numbers that happen to
-- sit in the same row, and the agent would present them as an explanation.
--
-- Three cents of tolerance, because each driver is rounded to cents
-- independently and six roundings can drift by up to three. Sprint 4 added
-- delta_from_detected_loads, the sixth term.
--
-- Returns offending rows; dbt fails the test if any come back.

select
    bill_id,
    amount_delta,
    coalesce(delta_from_period_length, 0)
      + coalesce(delta_from_weather, 0)
      + coalesce(delta_from_base_load, 0)
      + coalesce(delta_from_detected_loads, 0)
      + coalesce(delta_from_tariff, 0)
      + coalesce(delta_unexplained, 0) as drivers_sum
from {{ ref('mart_bill_explanation') }}
where prior_bill_id is not null
  and abs(
        amount_delta - (
          coalesce(delta_from_period_length, 0)
          + coalesce(delta_from_weather, 0)
          + coalesce(delta_from_base_load, 0)
          + coalesce(delta_from_detected_loads, 0)
          + coalesce(delta_from_tariff, 0)
          + coalesce(delta_unexplained, 0))
      ) > 0.03
