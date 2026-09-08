# Image for the Airflow DockerOperator tasks that can't live in the Airflow
# image itself (dbt + the Snowflake connector conflict with Airflow's pinned
# deps — see airflow/Dockerfile). One image serves three tasks:
#   * dbt build                      (dbt-snowflake)
#   * bronze parsed -> Snowflake RAW (snowflake-connector[pandas] + pyarrow, reading MinIO)
#   * Stage 5's round trip           (the same three, plus psycopg2 for the serving load)
#
# It is bronze that crosses the boundary, not gold. Snowflake is SaaS and cannot
# reach the local MinIO, so parsed Parquet is pushed up with the connector's
# PUT+COPY (write_pandas) and everything above it -- silver, gold, marts -- is
# built by dbt INSIDE Snowflake and never leaves it until Stage 5 exports.
FROM python:3.11-slim

# git is not optional: `dbt debug` checks for it and fails the whole run without
# it, and `dbt deps` needs it for any package resolved from a git ref rather
# than the hub. Cheap to install, and it makes `make dbt-debug` a gate that can
# actually pass.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# psycopg2-binary, not psycopg2: the source build needs pg_config and a
# compiler, and nothing here links against a system libpq worth matching.
RUN pip install --no-cache-dir \
      dbt-snowflake==1.12.0 \
      "snowflake-connector-python[pandas]" \
      pyarrow \
      psycopg2-binary \
      openlineage-python \
      openlineage-dbt

# dbt writes its package cache and target/ into the mounted project; keep a
# stable HOME so it doesn't try to write to a root-owned /.
ENV DBT_PROFILES_DIR=/dbt HOME=/tmp
WORKDIR /dbt
