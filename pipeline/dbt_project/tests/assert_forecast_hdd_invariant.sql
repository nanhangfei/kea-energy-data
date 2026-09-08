-- §8.6's stated invariant, as an assertion: across one cycle
--
--     hdd_to_date + hdd_expected_remaining
--
-- is CONSTANT -- "fact displacing forecast, day by day, over a fixed total".
-- The fixture asserts it; so does this, over all 11,440 cycles rather than one.
--
-- It is what fixes the shape of the calculation. The expected total is decided
-- once per period from climate normals and the remainder derived as
-- total − to_date. Forecasting the remaining days independently would let the
-- sum drift as actuals arrived, and the projection would move for reasons that
-- have nothing to do with the household.
--
-- THE ONE LEGITIMATE EXCEPTION is the clamp. A period colder than normal pushes
-- hdd_to_date past the expected total, and greatest(..., 0) stops the remainder
-- going negative -- which would otherwise forecast a household using LESS energy
-- for being cold. Where that engages the sum necessarily moves, so cycles that
-- ever exceeded their expected total are excluded here rather than silently
-- tolerated: the exception is named, not a fudge factor.
--
-- Two cents of tolerance for rounding, as elsewhere.

select
    account_id,
    period_start,
    min(hdd_to_date + hdd_expected_remaining) as lowest_sum,
    max(hdd_to_date + hdd_expected_remaining) as highest_sum
from {{ ref('mart_forecast_input') }}
where hdd_to_date is not null
  and hdd_expected_remaining is not null
group by account_id, period_start
having max(case when hdd_to_date > hdd_expected_total then 1 else 0 end) = 0
   and max(hdd_to_date + hdd_expected_remaining)
     - min(hdd_to_date + hdd_expected_remaining) > 0.02
