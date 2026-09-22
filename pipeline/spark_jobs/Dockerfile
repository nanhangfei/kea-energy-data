# Spark image with S3A support for talking to MinIO (s3a://).
# The official apache/spark image ships Hadoop 3.3.4 but NOT the AWS jars, so
# s3a:// paths fail with ClassNotFoundException until we add these two.
# Versions are locked to the bundled Hadoop: hadoop-aws MUST match Hadoop 3.3.4,
# and aws-java-sdk-bundle 1.12.262 is the version that hadoop-aws 3.3.4 expects.
#
# The PostgreSQL driver is here for the EXTRACT, not the parse. Design 5.1
# gives the meter-read extract to Spark because "a single-threaded JDBC read
# would not finish in a batch window" and a partitioned parallel read is the
# only practical option at 2.45B rows/yr -- so the job that reads
# src_metering.interval_reads by received_at range needs a driver to read it
# with. Without this jar the image can reach s3a:// and cannot reach Postgres
# at all, which is why Stage 3's second checkbox had nothing to point at.
FROM apache/spark:3.5.9

USER root
ARG HADOOP_AWS=3.3.4
ARG AWS_SDK=1.12.262
ARG POSTGRES_JDBC=42.7.4
# OpenLineage's Spark listener. Stage 7's second checkbox needs lineage "from
# Airflow and Spark", and Airflow's provider emits job events with no datasets
# at all -- DockerOperator has no extractor that knows what the container read.
# This listener does know: it reads Spark's own logical plan, so the JDBC source
# and the s3a:// outputs come out as real datasets rather than as labels.
# _2.12 matches the Scala this Spark is built with; 1.34.0 supports Spark 3.5.
ARG OPENLINEAGE=1.34.0
ARG MAVEN=https://repo1.maven.org/maven2
RUN cd /opt/spark/jars && \
    curl -fSL -o openlineage-spark_2.12-${OPENLINEAGE}.jar \
      ${MAVEN}/io/openlineage/openlineage-spark_2.12/${OPENLINEAGE}/openlineage-spark_2.12-${OPENLINEAGE}.jar && \
    curl -fSL -o hadoop-aws-${HADOOP_AWS}.jar \
      ${MAVEN}/org/apache/hadoop/hadoop-aws/${HADOOP_AWS}/hadoop-aws-${HADOOP_AWS}.jar && \
    curl -fSL -o aws-java-sdk-bundle-${AWS_SDK}.jar \
      ${MAVEN}/com/amazonaws/aws-java-sdk-bundle/${AWS_SDK}/aws-java-sdk-bundle-${AWS_SDK}.jar && \
    curl -fSL -o postgresql-${POSTGRES_JDBC}.jar \
      ${MAVEN}/org/postgresql/postgresql/${POSTGRES_JDBC}/postgresql-${POSTGRES_JDBC}.jar

# pyarrow is PySpark's own Arrow bridge, and it is what the conformance test
# uses to read the FOOTER of the Parquet this image writes -- checking the
# file's declared schema against KEA-047 rather than the DataFrame that
# produced it. Without it that check would have to run in a different
# container from the job it is checking.
# openlineage-python, so these jobs can name the datasets they read and wrote.
# The JVM listener above emits application START/COMPLETE and, on these two
# jobs, no dataset facets -- measured, not assumed. Naming them from Python
# makes the graph deterministic instead of dependent on which logical plans
# OpenLineage's visitors happen to cover.
#
# Both pinned, and what they pull in held by constraints.txt: the versions this
# image resolved when it was built on 3 Sep 2026. Unpinned, a rebuild would
# take whatever openlineage-python had released since.
COPY constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir "pyarrow==17.0.0" "openlineage-python==1.53.0" \
      --constraint /tmp/constraints.txt

USER spark
