# Output files and timing boundaries

This document describes the files monbench produces, the structure of the
JSON result files, and exactly what each timing measurement covers.


## Files

After a run, `results/` contains:

| Path pattern | Written by | Contents |
|---|---|---|
| `write-<target>-<host>.json` | each writer | one JSON object per writer run on that host |
| `meta/<target>.json` | `core/dataset.py` | the data set parameters and writer identity for that target |
| `benchmark-<target>-<adapter>.json` | `core/bench.py` | the combined benchmark result (see structure below) |
| `benchmark-<target>-<adapter>.log` | `core/bench.py` | everything printed to stdout during the benchmark |
| `benchmark-<target>-live.json` | `core/bench.py` | live-poll result (see structure below) |
| `logs/` | the harness | full stdout/stderr of every remote step |


## Benchmark result JSON

The main result file, `benchmark-<target>-<adapter>.json`, has this
structure:

```
{
  "target":    "<target name>",
  "dataset":   { ... },           # data set parameters (see below)
  "servers":   ["host1", ...],    # server nodes used
  "ingest":    { ... },           # write-side results (see below)
  "query":     { ... },           # query-side results (see below)
  "all_passed": true|false        # false if any check failed
}
```

### `dataset`

The data set parameters used for this run.

| Field | Description |
|---|---|
| `partitions` | number of server partitions (one per server node) |
| `components_per_partition` | components (time series) per partition |
| `samples` | data points per component |
| `t0` | timestamp of the first sample (Unix seconds) |
| `interval` | seconds between consecutive samples |
| `job_len` | samples per job |

### `ingest`

Aggregated write-side results, read from the per-host writer JSON files.

```
"ingest": {
  "backend": "sos",
  "nodes": [
    {
      "host":          "server-01",
      "backend":       "sos",
      "writer":        "c",
      "storage":       "mmos",
      "records":       80000,
      "write_s":       1.23,
      "sync_s":        0.45,
      "records_per_s": 65040
    }
  ],
  "summary": {
    "total_records":          160000,
    "max_write_s":            1.31,
    "max_sync_s":             0.45,
    "aggregate_records_per_s": 90909
  }
}
```

Per-node fields come directly from the writer's JSON output. The summary
aggregates across all nodes: `total_records` is the sum; `max_write_s` and
`max_sync_s` are the worst case; `aggregate_records_per_s` divides the
total records by the slowest node's elapsed time (write + sync).

### `query`

Query-side results from the benchmark engine.

```
"query": {
  "backend":    "dsos",
  "adapter":    "dsos",
  "query_file": "queries/default.json",
  "mode":       "check",
  "repeat":     5,
  "results": [
    {
      "name":       "[all] all records",
      "status":     "PASS",
      "rows":       160000,
      "verified":   true,
      "query":      "SELECT ... WHERE ..."
    }
  ]
}
```

| Field | Description |
|---|---|
| `mode` | `check` (verification only), `query` (timing only), or `both` |
| `repeat` | number of timed runs per query (from `REPEAT` in bench.conf) |
| **Per-query fields** | |
| `name` | query label from the query set file |
| `status` | `PASS`, `FAIL`, `INFO`, or `SKIP` |
| `rows` | number of rows returned |
| `verified` | `true` if the result was verified against the data set |
| `query` | the expanded query string that was executed |
| `times_s` | array of individual run durations in seconds (raw samples) |
| `median_s` | median of `times_s` |
| `p95_s` | 95th percentile of `times_s` |
| `min_s` | minimum of `times_s` |
| `rows_per_s` | `rows / median_s` |
| `detail` | present on `FAIL` entries; describes what went wrong |

When `mode` is `check`, the timing fields (`times_s`, `median_s`, `p95_s`,
`min_s`, `rows_per_s`) are absent. When `mode` is `query`, `verified` is
always `false`.


## Live result JSON

The live result file, `benchmark-<target>-live.json`, uses the same
top-level structure as the standard benchmark result, but its query results
contain no timing fields. Instead, each entry is a pass/fail check on the
live-poll behaviour:

```
"query": {
  "backend":    "dsos",
  "adapter":    "dsos",
  "query_file": null,
  "mode":       "both",
  "repeat":     5,
  "results": [
    { "name": "rows never decrease",       "status": "PASS", "rows": null, "verified": true },
    { "name": "every answer valid",        "status": "PASS", "rows": null, "verified": true },
    { "name": "final count reached",       "status": "PASS", "rows": null, "verified": true },
    { "name": "query errors while writing", "status": "INFO", "rows": null, "verified": false }
  ]
}
```

| Check | Passes when |
|---|---|
| `rows never decrease` | the row count returned by each poll is >= the previous poll's count |
| `every answer valid` | every polled answer contains only valid, non-duplicated records (each row checked against the data set) |
| `final count reached` | the last poll returned the full data set size (all partitions × components × samples) |
| `query errors while writing` | always INFO; reports how many polls returned an error (e.g. the target did not exist yet when the first poll ran) |

The `detail` field on each entry carries the specifics: poll counts for
`rows never decrease`, the number of invalid answers for `every answer
valid`, and the final vs expected count for `final count reached`.


## What is timed

### Write side — self-timed on the server

Each writer is a standalone program (C binary or Python script) that runs
on a server node via ssh or srun. The core harness cannot directly time a
remote process, so each writer measures and reports its own timing.

The writer's JSON output includes:

| Field | What it covers |
|---|---|
| `write_s` | first record inserted → last record committed (the data ingestion itself) |
| `close_sync_s` | container close and `os.sync()` (flushing data to stable storage) |
| `total_s` | container open → sync complete (the full operation) |
| `records_per_s` | `records / total_s` excluding the open step |

The exact boundaries differ slightly between writers (the SOS C writer
includes `sos_container_commit()` in `write_s`; the InfluxDB writer has
no sync step), but each writer's JSON is self-documenting: only the fields
it actually measures are present.

### Query side — core-timed on the client

Query timing is done by the benchmark engine (`core/bench.py`), not by
the adapter. The function `timed_run()` brackets the measurement:

```python
def timed_run(adapter, text):
    begin = time.perf_counter()
    n = 0
    for batch in adapter.query(text):
        n += adapter.count(batch)
    return time.perf_counter() - begin, n
```

The timed region is: **query dispatch → last row fetched**, including all
network round trips, server-side processing, result deserialization, and
the `adapter.count(batch)` call inside the loop (deliberately included;
it is meant to be a trivial operation like `len(batch)`).

The adapter's `connect()` and `close()` calls are outside the timed
region — connection setup is measured once, not per query.

### Why they differ

Write timing is server-local because the writer runs on the server. Query
timing is client-perceived because the benchmark engine runs on the query
node and measures what a real client would experience. These are different
vantage points by design:

- **Writer self-timing** allows each backend to report its own internal
  phases (open, write, sync, close) with granularity that the harness
  cannot observe remotely.
- **Core-timed queries** ensure every adapter is measured the same way,
  from the same vantage point, regardless of backend internals. The
  timing reflects end-to-end latency including network and deserialization,
  which is what matters for query performance.
