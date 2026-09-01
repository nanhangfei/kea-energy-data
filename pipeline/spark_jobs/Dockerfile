# Spark image with S3A support for talking to MinIO (s3a://).
# The official apache/spark image ships Hadoop 3.3.4 but NOT the AWS jars, so
# s3a:// paths fail with ClassNotFoundException until we add these two.
# Versions are locked to the bundled Hadoop: hadoop-aws MUST match Hadoop 3.3.4,
# and aws-java-sdk-bundle 1.12.262 is the version that hadoop-aws 3.3.4 expects.
FROM apache/spark:3.5.9

USER root
ARG HADOOP_AWS=3.3.4
ARG AWS_SDK=1.12.262
ARG MAVEN=https://repo1.maven.org/maven2
RUN cd /opt/spark/jars && \
    curl -fSL -o hadoop-aws-${HADOOP_AWS}.jar \
      ${MAVEN}/org/apache/hadoop/hadoop-aws/${HADOOP_AWS}/hadoop-aws-${HADOOP_AWS}.jar && \
    curl -fSL -o aws-java-sdk-bundle-${AWS_SDK}.jar \
      ${MAVEN}/com/amazonaws/aws-java-sdk-bundle/${AWS_SDK}/aws-java-sdk-bundle-${AWS_SDK}.jar
USER spark
