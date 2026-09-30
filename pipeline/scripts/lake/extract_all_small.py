"""Extract and parse every scripted entity for one load date. No Spark.

    python3 extract_all_small.py 2026-08-25 [--force] [--full-incremental]

The extract-side twin of load_all_bronze_to_snowflake.py. That one stages all
seventeen entities FROM bronze; this one produces the sixteen of them that are
not Spark's. `interval_reads` is deliberately absent: design §5.1 gives it a
partitioned Spark read of its own, and extract_small refuses it by name.

WHY THIS EXISTS AT ALL. The DAGs run these entities one PythonOperator each,
and rebuild_span.sh inlines the same loop. A person
with a single load date to repair had neither -- `make ingest-date` runs this
script, so the manual path covers what the scheduled one does instead of one
entity in seventeen.

Loops kea_pipeline.spec.ENTITIES rather than carrying its own list, so an
entity added to the spec is picked up here the day it lands.

--full-incremental IS APPLIED PER ENTITY, NOT PASSED THROUGH. extract_small
refuses the flag on a snapshot, so a caller that forwarded it blindly would
fail on the first of the fourteen. The rule -- incrementals take it, snapshots
do not -- belongs in one place rather than at every call site.

⚠️ AND ONLY WHEN ASKED. It is the demo generation's flag (`make ingest-date
FROM_DATE=...`): every invoice and payment in one partition. An ordinary load
date must extract its own window, or staging it appends the whole table under
that date and every other date's rows twice over.

ONE FAILURE IS NOT A CASCADE, and a clean skip is not a failure: both are the
same choices load_all_bronze_to_snowflake.py documents, for the same reasons.
A sealed partition that extract_small declines to rewrite is a correct outcome
on a re-run; only --force asks for it back.

Runs in the Airflow or dbt image, which already carry psycopg2, pyarrow and
boto3.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kea_pipeline import spec  # noqa: E402

import extract_small  # noqa: E402


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in argv
    full_incremental = "--full-incremental" in argv
    argv = [a for a in argv if a not in ("--force", "--full-incremental")]
    if len(argv) != 1:
        raise SystemExit("usage: extract_all_small.py YYYY-MM-DD [--force] [--full-incremental]")
    load_date = dt.date.fromisoformat(argv[0])

    entities = [e for e in spec.ENTITIES if e.qualified not in spec.SPARK_ENTITIES]
    done, skipped, failed = [], [], []

    for entity in entities:
        args = [entity.qualified, load_date.isoformat()]
        if force:
            args.append("--force")
        if full_incremental and entity.mode == spec.INCREMENTAL:
            args.append("--full-incremental")
        try:
            # 0 is both "wrote it" and "sealed already, nothing to do"; the
            # script says which on stdout, and the distinction is not worth a
            # second exit code when the summary below counts what landed.
            extract_small.main(args)
            done.append(entity.qualified)
        except SystemExit as exc:            # the script's own refusals
            if exc.code in (0, None):
                skipped.append(entity.qualified)
            else:
                print(f"  FAILED: {exc}")
                failed.append(entity.qualified)
        except Exception as exc:             # noqa: BLE001 -- report and continue
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            failed.append(entity.qualified)
        print()

    print(f"load_date={load_date}: {len(done)}/{len(entities)} extracted, "
          f"{len(skipped)} skipped, {len(failed)} failed")
    if failed:
        print(f"  FAILED:  {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
