"""Nightly: src_billing -> bronze -> bronze/parsed. Six entities, no Spark.

Independent of metering and catalogue by construction: its own DagRun, its own
schedule, its own credential. Stage 3's first criterion is that a billing
failure does not block metering, and this file existing separately is what
makes that true rather than intended.

`invoices` and `payments` are incremental, on ISSUE_DT and PAID_TS respectively.
Both columns are naive `timestamp`s, so their windows are expressed in NZ WALL
CLOCK rather than as instants -- the same calendar day the metering windows
cover, in the only terms the source has. See kea_pipeline.spec.

`invoice_lines` is a SNAPSHOT, against §4.1, and the reason is recorded in
spec.py: the table has no timestamp column at all, so an incremental read could
only reach ISSUE_DT by joining to `invoices` -- at which point a bug in the
join silently drops billing lines. At 101,284 rows §4.1's own "under ~1M rows,
prefer a snapshot" applies. Production is over that threshold and will have to
face the join.
"""

import os
import sys

# Airflow 3's DAG processor imports each file without the dags folder on
# sys.path, so a sibling helper is not importable unless we say so. Silent
# when wrong: the file parses, registers nothing, and reports no error.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _kea_common import scripted_dag  # noqa: E402

dag = scripted_dag(
    "src_billing",
    "customers, accounts, invoices, invoice_lines, payments, account_plan_history -> bronze -> parsed",
    ["lake", "ingest-date", "billing"],
)
