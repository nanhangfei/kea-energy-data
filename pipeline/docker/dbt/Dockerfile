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
#
# A patch release, not 3.11-slim: that tag moves with every Python and Debian
# update, and CI rebuilds this image on every run.
FROM python:3.11.16-slim

# git is not optional: `dbt debug` checks for it and fails the whole run without
# it, and `dbt deps` needs it for any package resolved from a git ref rather
# than the hub. Cheap to install, and it makes the `dbt debug` that `make
# build-warehouse` opens with a gate that can actually pass.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# psycopg2-binary, not psycopg2: the source build needs pg_config and a
# compiler, and nothing here links against a system libpq worth matching.
#
# Every package, including what these pull in, is held by constraints.txt: the
# versions this image resolved when it was built on 3 Sep 2026. CI rebuilds it
# on every run with no layer cache, and a floating package would let an
# upstream release turn every PR red with no change here. Pinning only the
# names below was not enough: the first rebuild moved the packages they pull
# in, python-slugify by a major version. The named pins repeat the file's so
# the versions that matter read here; dbt-core is named because dbt-snowflake's
# range would move it.
COPY constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir \
      dbt-snowflake==1.12.0 \
      dbt-core==1.12.3 \
      "snowflake-connector-python[pandas]==4.7.3" \
      pyarrow==25.0.1 \
      psycopg2-binary==2.9.12 \
      openlineage-python==1.53.0 \
      openlineage-dbt==1.53.0 \
      --constraint /tmp/constraints.txt

# dbt writes its package cache and target/ into the mounted project; keep a
# stable HOME so it doesn't try to write to a root-owned /.
ENV DBT_PROFILES_DIR=/dbt HOME=/tmp
WORKDIR /dbt
