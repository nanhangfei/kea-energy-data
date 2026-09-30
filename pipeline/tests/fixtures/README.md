# Bronze fixtures

`bronze_interval_reads.csv` is six rows in the shape the extract lands in bronze.
Committed as CSV rather than Parquet so a reviewer can read it; the conformance
test materialises it to Parquet before feeding it to the parse job.

| read_id | what it proves |
|---|---|
| ...001 | `A` -> `actual`, an ordinary row (the control -- a test where everything is rejected is also passed by a broken reader) |
| ...002 | `E` -> `estimated`, and `kwh = 0` is fine: a stuck meter is silver's problem, not a parse reject |
| ...003 | `F` -> `final`. Design 5's reshape table lists only A and E, and `common.read_quality` has no `final`; the source produces 1,010 of them |
| ...004 | **negative kwh.** `ck_kwh_non_negative` means this CANNOT be a source row, so it can only ever arrive in a file -- which is exactly why the source generator left it out of the injected defects and left it here |
| ...005 | `read_status = 'X'`, outside the source's own CHECK. Same argument: impossible in the table, possible in a file |
| ...006 | a null `kwh`, in a column KEA-047 declares non-nullable |

Rows 004-006 must land in quarantine with their `_reject_reason` and must NOT
appear in parsed. Rows 001-003 must parse.
