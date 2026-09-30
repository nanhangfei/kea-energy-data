"""Monthly: src_weather -> bronze -> bronze/parsed. NIWA's climate database.

The fourth lane, and the one that is not Kea's system at all. Design §9 names
it `kea_weather_load` and puts it among the "separate DAGs for anything with a
different cadence" — this is that cadence.

**SCHEDULED MONTHLY, NOT DAILY, AND THAT IS THE POINT.** NIWA publishes the
full historical record on a monthly cycle and runs roughly two weeks behind. A
daily run would re-extract an unchanged 69,569-row snapshot twenty-nine times
out of thirty and land twenty-nine identical partitions, which is not a cost
problem so much as a lie: every partition would claim to be a day's news.

Both entities are SNAPSHOTS, and for a stronger reason than size. NIWA
republishes the whole history each cycle, so a corrected historical observation
arrives as a CHANGED ROW in the next file — nothing about it says it moved.
An incremental read keyed on anything would keep the superseded value forever.

The data is an INPUT to the marts and is never exported to the serving store
(§6.4): what the agent needs is already denormalised into
`mart_daily_usage.heating_degree_days` and the period sums in
`mart_bill_explanation`.

⚠️ **The files are downloaded by hand, not by this DAG.** DataHub requires a
licence to be accepted per dataset, which is a person's decision; the loader in
`gen_source/load_niwa_weather.py` reads what that left in `data/raw/niwa/`. So this
DAG extracts what is already in `src_weather` — it does not refresh it from
NIWA. Automating the fetch means a licence agreement made by a scheduler, and
that is not a thing to build casually.
"""

from __future__ import annotations

import os
import sys

# Airflow's DAG processor runs in DAG_DISCOVERY_SAFE_MODE, which opens a file
# only if it contains BOTH the strings "dag" and "airflow". A file that says
# "DAG" throughout but never names Airflow is skipped silently: no import
# error, no log line, nothing in the dag table -- it simply never appears.
# This module's other three siblings pass the check by accident, through a
# comment that happens to mention Airflow. Naming it here is deliberate.
#
# The processor also refuses to rescan the folder between bundle refreshes
# ("Not time to refresh bundle dags-folder"), and the interval is tracked in
# the database rather than the process, so restarting the container does not
# shorten the wait. A new DAG can take up to core.dag_processor.refresh_interval
# seconds to appear even once it is correct.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _kea_common import scripted_dag  # noqa: E402

dag = scripted_dag(
    "src_weather",
    "NIWA daily temperature: stations + daily_observations -> bronze -> parsed",
    ["lake", "ingest-date", "weather"],
    schedule="@monthly",
)
