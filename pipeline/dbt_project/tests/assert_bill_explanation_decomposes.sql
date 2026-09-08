-- The invariant the whole mart rests on: the four drivers plus the
-- unexplained remainder must equal the amount delta. If they do not, the
-- decomposition is not a decomposition -- it is five numbers that happen to
-- sit in the same row, and the agent would present them as an explanation.
--
-- Two cents of tolerance, because each driver is rounded to cents
-- independently and five roundings can drift by more than one.
--
-- Returns offending rows; dbt fails the test if any come back.

select
    bill_id,
    amount_delta,
    coalesce(delta_from_period_length, 0)
      + coalesce(delta_from_weather, 0)
      + coalesce(delta_from_base_load, 0)
      + coalesce(delta_from_tariff, 0)
      + coalesce(delta_unexplained, 0) as drivers_sum
from {{ ref('mart_bill_explanation') }}
where prior_bill_id is not null
  and abs(
        amount_delta - (
          coalesce(delta_from_period_length, 0)
          + coalesce(delta_from_weather, 0)
          + coalesce(delta_from_base_load, 0)
          + coalesce(delta_from_tariff, 0)
          + coalesce(delta_unexplained, 0))
      ) > 0.02
