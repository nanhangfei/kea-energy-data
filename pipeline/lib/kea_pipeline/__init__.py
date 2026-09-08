"""Shared pipeline logic, mounted into both Airflow and Spark.

Kept out of `scripts/` and `spark_jobs/` because the Spark jobs and the DAGs
must agree exactly on what a load date means; two copies of that would drift.
"""
