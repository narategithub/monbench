# Backend plugins

The core of the suite (`bin/`, `core/`, `dataset/`, `queries/`) knows nothing
about any particular database. Everything backend-specific lives in one
directory per backend:

```
backends/<name>/
  backend.conf   settings and declarations
  plugin.sh      lifecycle hooks: build, write, serve, stop, clean
  query.py       query adapters
  ...            anything else the backend needs (writers, tools)
```

Selecting a backend is a single setting, `BACKEND=<name>` in `bench.conf`. If
the directory does not exist, or lacks a required file, the run stops with
an error before doing anything.

To add a backend, copy `backends/template/` to `backends/<name>/` and fill it
in. `backends/sos/` is a complete example.

## backend.conf

Sourced after `bench.conf`, so it can use core settings such as `RUN_DIR`.
Use the `NAME="${NAME:-default}"` form so every value can be overridden from
the environment. Three declarations are read by the core:

| Setting | Meaning |
|---|---|
| `DEFAULT_WRITERS` | Writers used when `WRITERS` is empty, e.g. `"c py"` |
| `PLUGIN_SERVICE` | `1` if the harness must start and stop a service on the server nodes (the `serve-write` or `serve` phase, depending on whether the backend is the write or query backend, and the `stop` phase); `0` if the backend needs no service or its service is managed elsewhere |
| `PLUGIN_VARS` | The plugin's own settings, space-separated, so they are forwarded to remote steps |

## plugin.sh

Sourced by `bin/node.sh` on the node where a step runs. It defines shell
functions; `plugin_write` is required, the others are optional and skipped
when absent.

| Hook | Runs on | Must |
|---|---|---|
| `plugin_build` | each server | Build the backend's writers, if any need building |
| `plugin_write WRITER TARGET MODE INDEX SERVER_ID` | each server, in parallel | Replace any existing data for partition `INDEX` of `TARGET`, then write that partition with writer `WRITER`, and print one JSON summary line (below). `MODE` is `full` or `live`. Return non-zero on failure |
| `plugin_serve_start SERVER_ID` | each server | Start the service in the background and return once it accepts queries |
| `plugin_serve_run SERVER_ID` | each server | Run the service in the foreground (used by the Slurm launcher) |
| `plugin_serve_stop` | each server | Stop the service and clean up after it |
| `plugin_serve_status` | each server | Return 0 when the service is ready |
| `plugin_clean SERVER_ID` | each server | Delete this server's data and run files |

`TARGET` is the name of one written copy of the data set: a container, a
database, a bucket. The harness writes one target per writer,
`<DATA_NAME>_<writer>`, plus `LIVE_TARGET` for the live test. `all_targets`
lists them all.

Available to hooks: `HOST` (short host name), `PLUGIN_DIR`, the helpers
`log` and `die`, every setting from `bench.conf` and `backend.conf`, and, in
`plugin_write`, the array `dataset_args`.

### Writers

A writer writes one partition of the data set defined in `core/dataset.py`.
Partition `INDEX` holds components `(INDEX + 1) * 1000 + c` for `c` in
`[0, COMPONENTS_PER_NODE)`, one record per sample, with the values given
there. The query engine verifies every answer against that definition, so a
writer that deviates from it shows up as failed queries.

`dataset_args` holds the data-set arguments every writer should accept:

```
--components N --samples N --t0 SECS --interval SECS --job-len N --batch N --rate RECS_PER_SEC
```

`--rate 0` means unthrottled; the live test uses a positive rate. Add the
partition index and your backend's own arguments (location, connection).

The writer's summary line is a single JSON object on its own line, starting
with `{`. The harness keeps it as `results/write-<target>-<host>.json` and
prints these fields when present: `writer`, `records`, `write_s`,
`close_sync_s`, `records_per_s`.

After all servers have written a target, the harness writes
`results/meta/<target>.json`, recording the backend, writer and data-set
parameters. The query engine reads it to verify against the data actually
written, and refuses to query a target written for a different backend.

## query.py

Defines one or more query adapters, subclasses of `Adapter` from
`core/adapter.py`, plus two module attributes:

```python
ADAPTERS = {"<adapter-name>": AdapterClass}
DEFAULT_ADAPTER = "<adapter-name>"
```

`QUERY_ADAPTER` selects an adapter; empty selects `DEFAULT_ADAPTER`. The
engine loads the file by path, so nothing needs installing.

| Method | Timed | Must |
|---|---|---|
| `connect()` | no | Connect, and confirm the target exists and holds the data set's records. Raise `BackendError` otherwise; that stops the run |
| `query(text)` | yes | Run one query and yield its result in batches, as the client library delivers them |
| `count(batch)` | yes | Number of rows in a batch. The default is `len(batch)` |
| `records(batch)` | no | Return `(columns, rows)`, rows being dicts keyed by attribute name. Used only for verification |
| `close()` | no | Release the connection |

`self.ctx` holds `target`, `schema` (the data-set name), `servers`,
`run_dir`, and `env` (all settings).

The engine times each run from the call to `query()` until the last batch
has been counted. Do in `query()` only what a normal client of the backend
would do, so the timings stay comparable across backends.

Each adapter has a `name` and a `dialect`, its query language. A query set
gives each question's query string per adapter name or per dialect, so
adapters that share a language share query strings. Add your backend's
strings to a query set under your adapter's name or dialect; questions
without one are reported as SKIP.

## Backends provided

Each backend has its own README with full details: [sos](sos/README.md), [dsos](dsos/README.md), [influxdb](influxdb/README.md).

| Backend | Writes | Queries | Notes |
|---|---|---|---|
| `sos` | C and Python writers, one local SOS container per server | — | The primary writer; DSOS queries its containers |
| `dsos` | — | `dsos` (Python API) and `dsosql` adapters | Queries `sos`-written data across servers through `dsosd` |
| `influxdb` | `influx` writer, to one InfluxDB 3 server | (query adapter: see release status) | Not partitioned across servers; all data goes to `INFLUX_HOST` |

### InfluxDB v3

The `influxdb` backend writes to and queries a single InfluxDB 3 server, so it
is not partitioned the way `sos`/`dsos` are: each server node generates its own
partition of the data set and sends it to the same `INFLUX_HOST`. Together the
nodes write the whole data set once.

Two options in `backends/influxdb/backend.conf`:

- `INFLUX_INTERFACE`: `python` (the `influxdb_client_3` library, default) or
  `http` (POST line protocol to the v3 write API with no extra dependency).
- `INFLUX_QUERY_LANG`: `sql` (default) or `influxql`.

The data set maps to InfluxDB as: measurement = the target name; tags =
`component_id`, `job_id`; fields = `seq`, `mem_free`, `load`. `seq` is kept as a
field so verification can identify each row.

Requirements: an InfluxDB 3 server reachable at `INFLUX_HOST`, a database
(`INFLUX_DATABASE`) created in advance, and a read/write token
(`INFLUX_TOKEN`, best set in the environment). The `python` interface also needs
`pip install influxdb3-python` on the write and query nodes; the `http`
interface needs only Python's standard library.
