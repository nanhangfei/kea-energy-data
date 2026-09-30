"""The DAG import test: every DAG file imports, and exactly the six DAGs exist.

Runs inside the Airflow image (`make ci ONLY=dags`, CI's `data-dags` job) with no
network and no .env, loading the DAG folder the way the dag-processor does. On
the host, where Airflow is not installed, it is skipped, so `make ci ONLY=test` stays
standard library only.

A DAG that fails to import breaks nothing loudly: the scheduler logs it and
carries on without it. So the ids are checked exactly, not just the import
errors, and a DAG that silently stops being built fails here too.
"""

from __future__ import annotations

import os
import unittest

# Otherwise Airflow's own example DAGs would be counted with ours.
os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")

# A try, not importlib's find_spec: a stray airflow/ directory on the path would
# pass for the package.
try:
    from airflow.dag_processing.dagbag import DagBag
except ImportError:
    DagBag = None

EXPECTED = {
    "kea_daily_pipeline",
    "kea_forecast_refresh",
    "kea_extract_metering",
    "kea_extract_billing",
    "kea_extract_catalogue",
    "kea_extract_weather",
}


@unittest.skipIf(DagBag is None, "Airflow is not installed here; run `make ci ONLY=dags`")
class DagImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bag = DagBag(dag_folder=os.environ.get("KEA_DAGS_DIR", "/opt/airflow/dags"))

    def test_no_import_errors(self):
        # The last line of each traceback names the failure; the file names where.
        errors = {
            os.path.basename(path): trace.strip().splitlines()[-1]
            for path, trace in self.bag.import_errors.items()
        }
        self.assertEqual(errors, {})

    def test_exactly_the_six_dags(self):
        self.assertEqual(set(self.bag.dags), EXPECTED)

    def test_every_dag_has_tasks(self):
        empty = sorted(dag_id for dag_id, dag in self.bag.dags.items() if not dag.tasks)
        self.assertEqual(empty, [])


if __name__ == "__main__":
    unittest.main()
