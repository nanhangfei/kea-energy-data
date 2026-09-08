-- 1:1 with src_catalogue.tou_windows. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- Snapshot. days_of_week is an array at source (design §5 reshapes
-- '1,2,3,4,5' into smallint[]); time_from/time_to are times of DAY that recur,
-- not instants, which is why the generator refused to flatten them to a
-- timestamp and gave `time without time zone` a deliberate answer.

select
    window_id,
    plan_id,
    charge_group,
    band_label,
    days_of_week,
    time_from,
    time_to,
    months_csv,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('catalogue', 'tou_windows') }}
