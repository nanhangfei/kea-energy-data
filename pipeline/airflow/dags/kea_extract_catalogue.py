"""Nightly: src_catalogue -> bronze -> bronze/parsed. Four entities, no Spark.

All three are full snapshots: hundreds of rows each, far under the ~1M at which
design §4.1 prefers an incremental read, and a snapshot cannot miss a DELETE --
which matters more here than anywhere. A plan version that disappears from the
catalogue but lingers in the lake is exactly §2.1.2's silent failure, a bill
rated against a tariff that no longer exists.

The catalogue is also where the two reshapes with real content happen:
`charge_scope` becomes `charge_group`, and `days_csv` -- a comma-separated
string -- becomes `days_of_week`, an array. The second is worth its own note:
splitting on the separator rather than searching for a substring is what stops
`position('1' in '1,2,3,4,5')` matching the '1' inside '11', which would apply
a weekday-only window on a Sunday and still balance the bill.
"""

import os
import sys

# Airflow 3's DAG processor imports each file without the dags folder on
# sys.path, so a sibling helper is not importable unless we say so. Silent
# when wrong: the file parses, registers nothing, and reports no error.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _kea_common import scripted_dag  # noqa: E402

dag = scripted_dag(
    "src_catalogue",
    "price_plans, tou_windows, rate_components, term_credits -> bronze -> parsed",
    ["stage3", "catalogue", "extract"],
)
