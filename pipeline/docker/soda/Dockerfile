# Soda gets its own image because it cannot share dbt's.
# dbt-snowflake and soda-core-snowflake together are unresolvable for pip
# (ResolutionTooDeep — conflicting pins on snowflake-connector, protobuf, otel).
# The same failure is documented in ../../airflow/Dockerfile; one image per tool
# is the answer in both places.
#
# A patch release, not 3.11-slim, which moves; the dbt image uses the same one.
FROM python:3.11.16-slim

# Soda Core 3.5.6, the version every scan so far has run, with every package it
# pulls in held by constraints.txt: this image's `pip freeze` from its build on
# 29 Aug 2026. Unpinned, a rebuild takes whatever Soda and the Snowflake
# connector have released since, and a check can start failing, or quietly
# stop checking, with no change here.
COPY constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir \
      soda-core-snowflake==3.5.6 \
      soda-core-postgres==3.5.6 \
      --constraint /tmp/constraints.txt

ENV HOME=/tmp
WORKDIR /soda
