-- Design §6.5's one table the pipeline writes about itself: one row per mart
-- per load, so "how fresh is this, and did it succeed" is answerable without
-- inspecting the data. The agent checks staleness before answering; ops
-- diagnose a partial load.
--
-- BUILT LAST, BY DEPENDING ON EVERY MART. The ref()s below are not decoration:
-- they are what puts this model last in dbt's DAG, so the counts it records are
-- of marts that have finished building in THIS invocation. A model that
-- counted rows without depending on them would happily report yesterday's
-- numbers as today's.
--
-- ⚠️ WHAT THIS DOES AND DOES NOT CAPTURE. started_at is the dbt INVOCATION's
-- start and completed_at is when this model ran -- so the window covers the
-- whole build, not each mart individually. Per-mart timing would need dbt's
-- run_results artifact parsed after the fact, which is a different mechanism
-- (an on-run-end hook writing run_results.json) and belongs with Stage 6's
-- orchestration rather than inside the build it is measuring.
--
-- status is 'succeeded' for every row by construction: if a mart had failed,
-- dbt would not have reached this model. That is a real limitation and it is
-- stated rather than implied -- a failed load leaves NO row for that mart in
-- this invocation, so absence is the failure signal, not a 'failed' value.
-- Stage 6 is where a wrapper can record the failure case.

{% set marts = [
    'mart_account_context',
    'mart_daily_usage',
    'mart_hourly_profile',
    'mart_bill_explanation',
] %}

{% for m in marts %}
select
    '{{ m }}'                                   as mart_name,
    cast('{{ run_started_at }}' as timestamp_tz) as started_at,
    current_timestamp()                          as completed_at,
    count(*)                                     as row_count,
    '{{ invocation_id }}'                        as warehouse_run_id,
    'succeeded'                                  as status,
    cast(null as varchar)                        as error_message
from {{ ref(m) }}
{% if not loop.last %}union all{% endif %}
{% endfor %}
