-- 1:1 with src_metering.interval_reads. Staging renames and casts; it does not join, filter or dedup (design §6.1).
-- KEA-047, the one hand-written contract. Incremental on received_at, which
-- is design §4.1's one deliberately non-obvious watermark: a correction
-- arrives as a NEW row with a higher version_no, so an extract watermarked on
-- reading time never sees it.
--
-- BUSINESS KEYS AND version_no ARE CARRIED THROUGH UNRESOLVED AND UNDEDUPED.
-- Resolving (icp_identifier, register_code) to surrogates, and keeping only the
-- highest version_no per (icp_identifier, register_code, interval_start), are
-- both silver's job (design §6.2). Doing either here would destroy the evidence
-- Stage 3's replay proof rests on before silver ever sees it.
--
-- The grain is therefore (icp_identifier, register_code, interval_start,
-- version_no) -- not the triple alone.

select
    read_id,
    icp_identifier,
    register_code,
    interval_start,
    kwh,
    quality,
    version_no,
    received_at,
    source_file,
    _extracted_at,
    _source_system,
    _source_file,
    _load_date
from {{ source('metering', 'interval_reads') }}
