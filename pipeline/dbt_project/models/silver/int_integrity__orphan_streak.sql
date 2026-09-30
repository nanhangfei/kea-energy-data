{{ config(materialized='incremental', unique_key='subject_key') }}

-- Design §6.3 distinguishes an ORPHAN from TIMING SKEW, and the difference is
-- not detectable in a single run -- both look like "billing references an ICP
-- metering has never heard of". The only thing that separates them is time:
--
--   billing extracted 02:00, metering 03:00, account created 02:30
--     -> metering's next extract will contain it. Retry.
--   billing references an ICP that does not exist
--     -> it will never appear. Quarantine.
--
-- So this model is a COUNTER, and it exists only because that distinction
-- cannot be made without one. An orphan is retried until it has missed
-- {{ var('integrity_max_misses') }} consecutive metering extracts, and is then
-- a real fault.
--
-- A MISS IS ONE METERING LOAD DATE, NOT ONE RUN. What resolves timing skew is
-- metering's NEXT extract, so the clock this counter keeps is the extract each
-- miss was judged against: max(_load_date) of connection_points, a snapshot, so
-- RAW holds exactly one. A second build over the same RAW -- a retry, a deploy,
-- `make build-warehouse` run twice -- sees the same date and leaves every count
-- alone. Counting runs let three builds of one date quarantine an orphan that
-- one build would only have retried. A date OLDER than the one last counted, as
-- a backfill stages, is not a new miss either.
--
-- RESOLVED SUBJECTS ARE DROPPED, NOT KEPT AT ZERO. `delete+insert` on the
-- current orphan set means an account that appears in metering tomorrow simply
-- stops being here -- if it went missing again a week later, its streak would
-- restart, which is what "consecutive" means. Keeping a zero row would make the
-- next miss look like a continuation of an old problem.

-- ⚠️ AND `delete+insert` ALONE DOES NOT DO THAT. It deletes target rows whose
-- unique_key appears IN THE INCOMING BATCH, and a resolved subject is precisely
-- the one that is no longer in it -- so without the hook below, a subject that
-- stops being an orphan keeps its row and its frozen `consecutive_misses`
-- forever, and an empty orphan set deletes nothing at all.
--
-- Measured after the 2027 span rebuild: 493 accounts sat in here reading
-- "miss 1 of 3" while the live orphan set was EMPTY, quarantining 97 % of the
-- cohort and failing soda's orphan-rate gate. They were recorded by one run
-- that landed while RAW was mid-reload, and nothing could ever clear them.
--
-- Every subject missed by THIS run is stamped current_timestamp(), so anything
-- older than the run's own start is by definition resolved. That is the whole
-- rule, it needs no second copy of the orphan query, and it empties the table
-- correctly when nothing is orphaned.
--
-- ⚠️ THE PRE-HOOK MIGRATES A TABLE BUILT BEFORE last_missed_load_date EXISTED.
-- The incremental SQL below reads that column from {{ this }}, so an older
-- table would fail the build rather than gain it, and dbt's on_schema_change
-- acts too late to help: it compares columns AFTER running that SQL. A row
-- migrated without a date keeps its count and takes this build's date -- one
-- miss too few at worst, never one too many. IF NOT EXISTS makes the hook a
-- no-op on every build after the first.
{{ config(
    incremental_strategy='delete+insert',
    pre_hook="{% if is_incremental() %}
                alter table {{ this }} add column if not exists last_missed_load_date date
              {% endif %}",
    post_hook="{% if is_incremental() %}
                 delete from {{ this }}
                  where last_missed_at < '{{ run_started_at }}'::timestamp_tz
               {% endif %}"
) }}

with current_orphans as (
    select
        'account:' || a.account_number as subject_key,
        'account'                      as subject_type,
        a.account_number               as subject_id,
        a.icp_number                   as missing_reference
    from {{ ref('stg_billing__accounts') }} a
    left join {{ ref('key_map_icp') }} k
      on k.icp_number = a.icp_number
    where k.icp_number is null
),

-- The metering extract this build judges every orphan against.
metering_extract as (
    select max(_load_date) as load_date
    from {{ ref('stg_metering__connection_points') }}
)

select
    o.subject_key,
    o.subject_type,
    o.subject_id,
    o.missing_reference,
    -- CAST EXPLICITLY, and not for tidiness. A bare `1` on the first build
    -- makes Snowflake infer NUMBER(1,0) from the literal, and the tenth
    -- consecutive miss then fails with "Number out of representable range".
    -- Same trap as the loader's auto_create_table: whatever runs first defines
    -- the column, and the failure arrives much later than the cause.
    --
    -- One more only when this extract is LATER than the last one counted. The
    -- comparison is NULL, so the count holds, both for a row migrated without
    -- a date and for a RAW with no metering extract in it at all.
    {% if is_incremental() %}
    cast(case
        when p.subject_key is null                     then 1
        when m.load_date > p.last_missed_load_date     then p.consecutive_misses + 1
        else p.consecutive_misses
    end as number(9,0))
    {% else %}
    cast(1 as number(9,0))
    {% endif %} as consecutive_misses,
    current_timestamp() as last_missed_at,
    -- The latest extract counted so far. Never moves backwards, so a backfill
    -- of an older date cannot make a later one count twice.
    {% if is_incremental() %}
    cast(case
        when p.last_missed_load_date is null
          or m.load_date > p.last_missed_load_date     then m.load_date
        else p.last_missed_load_date
    end as date)
    {% else %}
    cast(m.load_date as date)
    {% endif %} as last_missed_load_date
from current_orphans o
cross join metering_extract m
{% if is_incremental() %}
left join {{ this }} p on p.subject_key = o.subject_key
{% endif %}
