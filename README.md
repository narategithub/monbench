# monbench

A pluggable benchmark for storing and querying monitoring data, designed for
distributed HPC monitoring deployments.

monbench writes a deterministic monitoring-style data set into a database
backend, then runs a set of queries against it from a separate query node,
verifying every answer and timing every query. The core of the suite knows
nothing about any particular database: each backend is a plugin under
`backends/`. The first plugin is SOS/DSOS; `backends/template/` is the
starting point for others.

```
  server node 1           server node 2           ...      query node
  ─────────────           ─────────────                    ──────────
  writer ─> partition 0   writer ─> partition 1            query engine
  backend service  <────────────── backend protocol ─────  + backend adapter
```

## Layout

| Path | Purpose |
|---|---|
| `bench.conf` | Core settings: backend, nodes, paths, data-set size, query settings |
| `bin/run.sh` | Driver: runs each phase on the right nodes via ssh, Slurm or by hand |
| `bin/node.sh` | Actions executed on a single node; calls the backend's hooks |
| `core/dataset.py` | The data set: value rules, parameters, metadata files |
| `core/bench.py` | Query engine: runs a query set, verifies and times each query |
| `core/adapter.py` | The interface query adapters implement |
| `dataset/default.json` | Data set definition: attributes and keys |
| `queries/default.json` | Default query set, used when `QUERY_FILE` is empty |
| `backends/README.md` | How to write a backend plugin |
| `backends/sos/` | SOS/DSOS plugin: settings, hooks, C and Python writers, query adapters, RPC test tool |
| `backends/template/` | Skeleton for a new backend |
| `slurm/monbench.sbatch` | Batch script for a Slurm job |
| `ROADMAP.md` | Current limitations, and what is planned or deferred, with the reasons |
| `OUTPUT.md` | Result file structure, JSON schema, and timing measurement boundaries |

## Choosing a backend

Set `BACKEND` in `bench.conf` to the name of a directory under `backends/`.
That backend's `<name>_backend.conf` holds its own settings (for SOS: installation
prefix, container location, storage type, `dsosd` command). The run stops
with an error if the backend does not exist, if its data was written for a
different backend, or if the query adapter cannot reach it.

## Requirements

For every backend:

- Python 3 on every node, and on the query node the Python client of the
  backend being queried.
- The suite directory at the same path on all nodes. On a shared filesystem
  nothing needs copying; otherwise use `bin/run.sh -l ssh deploy`.

For the `sos` backend:

- SOS installed on all nodes: `libsos`, the Python `sosdb` module (with the
  fix to `SqlQuery.next()`), `dsosd` and `dsosql`.
- `rpcbind` running on the server nodes, and the query node able to reach the
  servers' portmapper (TCP/UDP 111) and the port `dsosd` registers.
- `jansson` and `libuuid` development files on the server nodes (C writer).

## The data set

The data set is split into one partition per server. Each server writes
`COMPONENTS_PER_NODE × SAMPLES` records (80,000 by default). Every record is
a function of its `component_id` and `seq`, so any row can be verified on its
own. The rules are defined in `core/dataset.py`:

| Attribute | Value |
|---|---|
| `component_id` | `(partition + 1) × 1000 + c`, for c in `[0, COMPONENTS_PER_NODE)` |
| `timestamp` | `T0 + seq × INTERVAL` |
| `job_id` | `1 + seq ÷ JOB_LEN` (integer division; each job spans all servers) |
| `mem_free` | `component_id × 1,000,000 + seq` |
| `load` | `(component_id mod 1000) + seq × 0.25` |

`dataset/default.json` lists the attributes and the **keys** the data set is
meant to be queried by: `timestamp`, `comp_time` (component_id, timestamp)
and `job_comp_time` (job_id, component_id, timestamp). Each backend maps the
keys onto its own indexing; the SOS plugin creates an index for each.

Each writer writes its own **target** (a container, database or bucket),
`<DATA_NAME>_<writer>`, so writers can be compared. A rerun of `write`
replaces the target's data. After each target is written, the harness
records its backend, writer and data-set parameters in
`results/meta/<target>.json`. The query engine verifies against those
parameters, so a later change to `SAMPLES` does not invalidate a check of
data written earlier.

## Quick start

1. Edit `bench.conf`: at minimum `BACKEND`, `SERVER_NODES`, `QUERY_NODE` and,
   if the backend's software is not on the default paths, `ENV_SETUP`.
2. Edit `backends/<BACKEND>/<BACKEND>_backend.conf` for that backend (for SOS,
   `SOS_PREFIX`).
3. Run one of the following.

**ssh** (from any node that can ssh to the other nodes without a password):

```bash
bin/run.sh -l ssh deploy          # only if the directory is not shared
bin/run.sh -l ssh all             # build serve-write write serve-query check query stop
```

**Slurm** (inside an allocation of at least three nodes; the first two
become servers and the third the query node):

```bash
sbatch slurm/monbench.sbatch
# or interactively:
salloc -N 3
bin/run.sh -l slurm all
```

**Manual** (prints each command with the node to run it on, then waits):

```bash
bin/run.sh -l manual all
```

In manual mode, change settings in the configuration files rather than in
the shell environment, because the commands you type on the other nodes read
only the files.

Phases can also be run individually, for example to keep the backend's
service running between checks:

```bash
bin/run.sh serve-write   # start the write backend's service, if it has one
bin/run.sh write
bin/run.sh serve-query   # start the query backend's service
bin/run.sh check         # verify query results (no timing)
bin/run.sh query         # time the query set (no verification)
bin/run.sh live          # concurrent write-and-query test
bin/run.sh stop          # stop both services
bin/run.sh clean         # stop the services and delete the data
```

Settings can be overridden per run, e.g. `SAMPLES=500 WRITERS=c bin/run.sh all`
(ssh and Slurm launchers only).

## What the check phase does

`check` runs a **query set** against each target, through a query adapter
from the backend plugin, and reports a verdict and timings for every query.
For each query the engine:

1. **runs it once, untimed, and verifies the full result** against the
   records the question should return, computed from the data set;
2. **runs it `REPEAT` more times and times each run** from the start of the
   query until its last row has been fetched. It reports the median and p95.

The connection to the backend is made once, before any query, so timings do
not include connecting. It is also where the backend is checked: if the
backend cannot be reached, the target does not exist, or it holds no
records, the run stops with an error.

`QUERY_ADAPTER` selects the adapter within the backend; empty selects the
backend's default. The `sos` plugin provides:

| `QUERY_ADAPTER` | Adapter | Use |
|---|---|---|
| `dsos` (default) | DSOS Python API (`sosdb`), returning DataFrames | Timings |
| `dsosql` | The `dsosql` command, parsing its CSV output | Cross-checking results; its timings include process start-up and connecting |

A verified query passes when:

- the row count is right. With `limit`, a count matching either whole-result
  or per-server semantics passes, and the report says which;
- every returned row is a real record that satisfies the question, with no
  duplicates and every selected value correct. This needs `component_id` and
  `seq` in the result; otherwise only the count is checked;
- rows are in the stated order, when the ordering columns are in the result;
- every timed run returns the same number of rows as the verified run.

Queries without a `match` description, or marked `"verify": false`, are run
and timed and reported as INFO.

`live` writes a new target, `LIVE_TARGET`, at `LIVE_RATE` records per second
per server while the query node polls it with the query set's first query of
type `all`. It passes when every answer contains only valid,
non-duplicated records, the row count never decreases, and the final count is
reached.

## Query sets

A query set is a JSON file. When `QUERY_FILE` is empty, the default set
`queries/default.json` is used. To define your own, copy that file, edit it,
and set `QUERY_FILE` in `bench.conf` or on the command line (ssh and Slurm
launchers).

Each query describes the **question** once, independently of any backend,
and gives the **query string** for each backend:

```json
{
  "name": "one job, one component, part of the job's time",
  "type": "job+component+time",
  "match": { "where": "job_id == {job_mid} and component_id == {comp_slast} and timestamp >= {t_mid}",
             "order_by": ["job_id", "component_id", "timestamp"] },
  "query": { "dsos": "select * from {schema} where job_id == {job_mid} and component_id == {comp_slast} and timestamp >= {t_mid} order_by job_comp_time" }
}
```

| Field | Meaning |
|---|---|
| `name`, `type` | Labels for the report. The default set names types by the conditions the question uses, so the labels apply to every backend |
| `match` | The question, used for verification: `where` conditions, `order_by` (the attributes the result is ordered by) and optionally `limit` |
| `query` | Query strings keyed by adapter name or dialect (`dsos` covers both SOS adapters). A plain string is used for every adapter. An adapter with no string for a question reports it as SKIP |
| `verify` | `false` to run and time the query without verifying it. Default `true` |

`match.where` is conditions of the form `<attribute> <op> <number>` joined by
`and`, with `op` one of `==`, `!=`, `<`, `<=`, `>`, `>=`. Backend-specific
choices, such as which DSOS index to use, belong in the query string only.

The default set covers:

| Type | Questions |
|---|---|
| `all`, `time` | Every record; all components over 25% and 1% of the time span |
| `component`, `component+time` | One component, alone and within a time window. The time-window question is asked three times, through the `comp_time`, `timestamp` and `job_comp_time` indices, to show the effect of the index choice |
| `job`, `job+component`, `job+component+time` | One, two and all three attributes of the `job_comp_time` key |
| `job+time` | Job and time without component: a gap in the `job_comp_time` key |
| `metric-value` | A condition on `load`, which has no index |
| `projection`, `limit` | Selected columns only; the first rows with `limit` |
| `aggregate` | A `group_by` query; timed, not verified |

Query strings and `match` may use these placeholders, filled in from the data
set so that one file works at any size:

| Placeholder | Value |
|---|---|
| `{schema}` | Schema name |
| `{limit}` | `LIMIT_N` |
| `{t_start}`, `{t_end}` | Timestamps of the first and last sample |
| `{t_q1}`, `{t_mid}`, `{t_q3}` | Timestamps at 25%, 50% and 75% of the time span |
| `{t_mid_1pct}` | 1% of the time span after `{t_mid}` |
| `{comp_s0}`, `{comp_s0_last}` | First and last component on the first server |
| `{comp_slast}`, `{comp_last}` | First component on the last server; last component overall |
| `{job_first}`, `{job_mid}`, `{job_last}` | First job, the job at mid-span, last job |

## Output

- `results/write-<target>-<host>.json`: one JSON object per writer run,
  including records written, time to write, time for the final sync, and
  records per second.
- `results/meta/<target>.json`: what was written to each target.
- `results/benchmark-<target>-<adapter>.json`: each query with its
  expanded query string, status, row count, every timed run, median, p95,
  minimum and rows per second.
- `results/logs/`: the full output of every remote step.

See `OUTPUT.md` for the full JSON structure, what each field means, and
exactly what the write and query timing measurements cover.

## Assumptions and limits

Points to be aware of when running the harness or interpreting its results.
Design limitations and plans for later versions are tracked in `ROADMAP.md`.

- **Timings use the Python API.** The `dsos` adapter returns DataFrames, so
  its timings include building them, as an analyst's code would. They do not
  show the fastest a C client could achieve.
- **The Python `SqlQuery` batch fix is required.** Without the fix to
  `SqlQuery.next()` in `Sos.pyx`, the `dsos` adapter loses one row per full
  batch of `SOS_ROW_LIMIT` rows, and verified queries fail.
- **Data written by earlier versions has no metadata file.** The engine then
  verifies against the current settings and says so; rewrite the data to get
  a metadata file.
- **`dsosql` output parsing (`dsosql` adapter only).** The adapter reads the
  CSV block between the `# <columns>` header and the `# Records N.` footer,
  after removing the `dsosql: ` prompt that `dsosql` prints even when its
  input is a pipe.
- **The `timestamp` output format.** The format `dsosql` prints for a
  timestamp is not known. Seconds and `seconds.microseconds` are recognised;
  other formats are reported as a note and are not validated.
- **`group_by` syntax.** The syntax is not documented in the sources reviewed.
  The `group_by` query in the default set is a best guess and is not
  verified. Time binning (resampling) is not tested yet, for the same reason.
- **Registering `dsosd` with `rpcbind` may require root.** If so, set
  `DSOSD="sudo -E dsosd"` in `backends/dsos/dsos_backend.conf`. `-E` is needed so that `DSOSD_DIRECTORY` and
  `DSOSD_SERVER_ID` reach `dsosd`. Stopping it then also requires sudo.
- **The Python writer cannot request a synchronous close.** The bindings do
  not export `SOS_COMMIT_SYNC`, so the writer calls `os.sync()` after closing
  the container to make its timing comparable with the C writer.
- **Where SOS containers are stored.** Containers are placed under
  `SOS_DATA_ROOT/<server-id>/`, so a shared path is safe. A node-local path
  gives the intended configuration.
