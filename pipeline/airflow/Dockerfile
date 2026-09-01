FROM apache/airflow:3.3.0-python3.11

# Providers deliberately UNPINNED here: the constraints file pins the exact
# provider versions tested with Airflow 3.3.0. Adding our own pins contradicts
# it and pip's resolver melts down (ResolutionTooDeep) — same failure the old
# _PIP_ADDITIONAL_REQUIREMENTS startup install hit.
# fab provider: Airflow 3's new default is Simple Auth Manager; fab keeps the
# admin/password login this project documents everywhere.
RUN pip install --no-cache-dir \
      apache-airflow-providers-fab \
      apache-airflow-providers-snowflake \
      apache-airflow-providers-apache-spark \
      apache-airflow-providers-openlineage \
      apache-airflow-providers-docker \
      --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.3.0/constraints-3.11.txt"

# dbt and Soda are NOT in this image. Installing
# dbt-snowflake + soda-core-snowflake together is unresolvable for pip
# (ResolutionTooDeep — conflicting pins on snowflake-connector/protobuf/otel).
# When those stages arrive, give each its own venv built from a compiled
# lockfile (pip-compile), or run them in dedicated containers instead of
# inside Airflow.
