"""Land every entity's parsed bronze for one load date into KEA.RAW.

    python3 load_all_bronze_to_snowflake.py 2026-08-25

Loops kea_pipeline.spec.ENTITIES rather than carrying its own list, so this and
extract_small.py can never disagree about which seventeen entities exist.

ONE FAILURE IS NOT A CASCADE. Every entity is attempted even after one fails,
and the failures are named at the end -- the same choice Stage 3's DAGs make by
being three separate DagRuns instead of one (design §9). A billing table that
cannot be loaded should not stop metering from landing, and finding out about
four problems should not take four runs.

A CLEAN SKIP IS REPORTED SEPARATELY FROM A FAILURE, because they mean opposite
things. Weather is a monthly snapshot, so on most load dates two of the
seventeen have no partition and that is correct. Folding those into a failure
count would make a healthy run look half-broken; folding them into successes
would hide a metering partition that never landed.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from kea_pipeline import spec  # noqa: E402

import load_bronze_to_snowflake as loader  # noqa: E402


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    incremental_only = "--incremental-only" in argv
    argv = [a for a in argv if a != "--incremental-only"]
    if len(argv) != 1:
        raise SystemExit("usage: load_all_bronze_to_snowflake.py YYYY-MM-DD "
                         "[--incremental-only]")
    load_date = dt.date.fromisoformat(argv[0])

    # --incremental-only exists for a full rebuild and for nothing else.
    #
    # Fourteen of the seventeen entities are snapshots, and a snapshot stages with
    # overwrite=True -- truncate and reload. So staging one on date D throws
    # away date D-1's and is itself thrown away by D+1: across a 757-date
    # rebuild, 756 of every snapshot's loads exist only to be overwritten,
    # at three Snowflake round trips each.
    #
    # ⚠️ THE LAST DATE MUST STILL STAGE EVERYTHING, or the snapshots never
    # land at all. rebuild_span.sh does that; nothing here enforces it, which
    # is why this flag is not a general-purpose option.
    entities = [e for e in spec.ENTITIES
                if not incremental_only or e.mode != spec.SNAPSHOT]

    # One session for all seventeen rather than one each. Worth ~2 s of 42 --
    # small, and worth having, but see loader.load: the cost is write_pandas's
    # stage/PUT/COPY round trips, not the handshake.
    conn = loader._connect()

    landed, skipped, failed = [], [], []
    for entity in entities:
        try:
            rows = loader.load(entity, load_date, conn=conn)
            (landed if rows else skipped).append(entity.qualified)
        except SystemExit as exc:            # the loader's own refusals
            print(f"  FAILED: {exc}")
            failed.append(entity.qualified)
        except Exception as exc:             # noqa: BLE001 -- report and continue
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            failed.append(entity.qualified)
        print()

    conn.close()

    total = len(entities)
    print(f"load_date={load_date}: {len(landed)}/{total} landed, "
          f"{len(skipped)} skipped (no partition), {len(failed)} failed")
    if skipped:
        print(f"  skipped: {', '.join(skipped)}")
    if failed:
        print(f"  FAILED:  {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
