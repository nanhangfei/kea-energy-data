# Tests that need the lake

`make test-conformance` — run under `spark-submit`, against objects in MinIO.

Deliberately NOT under `pipeline/tests/`, where `unittest discover` would find
them: `make test` is stdlib-only and runs on the host in milliseconds,
and the host has neither pyspark nor pyarrow. Keeping the two apart is what
lets the fast suite stay honest about what it does and does not cover.
