# Image for the Airflow DockerOperator tasks that can't live in the Airflow
# image itself (dbt + the Snowflake connector conflict with Airflow's pinned
# deps — see airflow/Dockerfile). One image serves two tasks:
#   * dbt build              (dbt-snowflake)
#   * gold -> Snowflake load (snowflake-connector[pandas] + pyarrow, reading MinIO)
FROM python:3.11-slim

RUN pip install --no-cache-dir \
      dbt-snowflake==1.12.0 \
      "snowflake-connector-python[pandas]" \
      pyarrow

# dbt writes its package cache and target/ into the mounted project; keep a
# stable HOME so it doesn't try to write to a root-owned /.
ENV DBT_PROFILES_DIR=/dbt HOME=/tmp
WORKDIR /dbt
