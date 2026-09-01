# Soda gets its own image because it cannot share dbt's.
# dbt-snowflake and soda-core-snowflake together are unresolvable for pip
# (ResolutionTooDeep — conflicting pins on snowflake-connector, protobuf, otel).
# The same failure is documented in ../../airflow/Dockerfile; one image per tool
# is the answer in both places.
FROM python:3.11-slim

# Soda Core is not pinned yet — a known gap, tracked as Stage 8 in
# design/kea-data-engineering-stack.md. Pin once a working version is confirmed.
RUN pip install --no-cache-dir soda-core-snowflake soda-core-postgres

ENV HOME=/tmp
WORKDIR /soda
