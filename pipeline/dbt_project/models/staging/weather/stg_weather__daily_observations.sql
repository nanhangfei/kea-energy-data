-- 1:1 with src_weather.daily_observations. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- Snapshot: NIWA republishes the whole history monthly, so a corrected
-- observation arrives as a changed row with nothing to say it moved. An
-- incremental read would keep the superseded value forever.
--
-- obs_date IS THE NZ DATE, DERIVED. NIWA stamps each row with the instant of
-- the 9am observation in UTC, so 2026-08-16T21:00:00Z is 09:00 NZST on the
-- 17th. observed_at_utc is kept beside it so the conversion stays auditable.
--
-- rel_humidity_pct is cast to a whole number, and it is now belt-and-braces
-- rather than a repair. The loader used to round-trip through plain pandas,
-- which cannot hold nulls in an integer column and silently widened this to
-- float; it now keeps the Parquet's own Arrow types end to end, so the column
-- arrives as the contract declares it. The cast stays because narrowing to the
-- source's smallint is what design §6.1 says staging is for, and because it
-- documents the intended width where a reader will look for it.

select
    obs_id,
    agent_no,
    obs_date,
    observed_at_utc,
    temp_max_c,
    temp_min_c,
    temp_mean_c,
    grass_min_c,
    cast(rel_humidity_pct as number(3,0)) as rel_humidity_pct,
    source_file,
    received_at,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('weather', 'daily_observations') }}
