# monbench extension guide

Oct 2, 2026 · @Nichamon

How to add a new database backend, define a custom dataset, or write a query file for the monbench benchmark suite.

## Overview

The benchmark suite has three extension points, each defined by one or more files in the repository:

| Extension point | What it controls | Where it lives |
| --- | --- | --- |
| Backend | How data is written, stored, queried, and how the database service is managed | `backends/<n>/` (at least three files: a config, a plugin script, a query adapter; plus writers and any tools the backend needs) |
| Dataset | The schema — attribute names, types, and access keys — and the deterministic value rules every writer must follow | `dataset/<n>.json` (definition) + `core/dataset.py` (value rules) |
| Query file | The benchmark questions: what to query, in each backend's dialect, and what to verify the answer against | `queries/<n>.json` |

The three are independent. A new backend works with the existing default dataset and query file. A new dataset can be benchmarked by any backend that writes it correctly. A new query file can target any backend that has query strings for its questions.

The connection between them is the dataset definition: it names the attributes that query files reference in their `{placeholders}` and `match` specs, and it lists the attribute types that a backend's `type_overrides` map to its native conventions.

## Writing a new backend

Copy `backends/template/` to `backends/<n>/` and rename each file with your backend's name as a prefix. A backend has three required files.

### Lifecycle

The harness runs phases in a fixed order. Each phase calls specific plugin hooks:

1. **`clean`** — `plugin_clean`: delete previous data (optional, only when requested)
2. **`build`** — `plugin_build`: compile writers if needed
3. **`serve-write`** — `plugin_serve_start`: start the database service (only if `PLUGIN_SERVICE=1`)
4. **`write`** — `plugin_write`: write each partition of the dataset, one call per server
5. **`meta`** — the harness records what was written to `results/meta/<target>.json`; no plugin hook
6. **`serve-query`** — `plugin_serve_start` on the query backend (skipped if it is the same as the write backend)
7. **`query`** — the engine connects the adapter (`connect()`), loads the query set, and runs each query through `query()` / `count()` / `records()`
8. **`stop`** — `plugin_serve_stop`: stop the service

A backend that manages its own service externally (`PLUGIN_SERVICE=0`) simply skips the serve and stop phases — the harness calls `plugin_write` and the query adapter directly.

### The config: `<n>_backend.conf`

Sourced after `bench.conf`, so it can use core settings such as `RUN_DIR`. Use the `VAR="${VAR:-default}"` form so every value can be overridden from the environment. These declarations are read by the core:

| Declaration | Purpose |
| --- | --- |
| `DEFAULT_WRITERS` | Writer names used when `WRITERS` is empty in `bench.conf`, e.g. `"py"` |
| `PLUGIN_SERVICE` | `1` if the harness must start and stop a service (the `serve-write`/`serve-query` and `stop` phases); `0` if the service is managed externally |
| `PLUGIN_READS` | Space-separated list of backends whose written data this backend can query (e.g. `"sos dsos"`). The harness checks this against the target's metadata file and rejects incompatible write→query pairs before any query runs. Omit or leave empty if not applicable |
| `PLUGIN_DESC` | Short description of what this backend does, shown in the benchmark output |
| `PLUGIN_VARS` | Space-separated list of this backend's own settings, so they are forwarded to remote nodes |

All other settings (connection URLs, database names, tokens, batch sizes) are your own and follow the same `"${VAR:-default}"` pattern.

### The plugin script: `<n>_plugin.sh`

Sourced by `bin/node.sh` on the node where a step runs. It defines shell functions (hooks). `plugin_write` is required; the others are optional.

| Hook | Signature | Purpose |
| --- | --- | --- |
| `plugin_build` | (none) | Compile writers if needed |
| `plugin_write` | `WRITER TARGET MODE INDEX SERVER_ID` | Write partition `INDEX` of the dataset, print one JSON summary line |
| `plugin_serve_start` | `SERVER_ID` | Start the database service in the background |
| `plugin_serve_run` | `SERVER_ID` | Run the service in the foreground (Slurm launcher) |
| `plugin_serve_stop` | (none) | Stop the service |
| `plugin_serve_status` | (none) | Return 0 when the service is ready |
| `plugin_clean` | `SERVER_ID` | Delete this server's data and run files |

The `plugin_write` hook receives `dataset_args`, an array with the flags every writer should accept: `--components N --samples N --t0 SECS --interval SECS --job-len N --batch N --rate RECS_PER_SEC`. Your writer must produce records that match the value rules in `core/dataset.py` exactly — the query engine verifies every answer against them.

The `plugin_write` arguments in order: `$1` = WRITER (the writer name), `$2` = TARGET (the data name, e.g. `monitoring_influx`), `$3` = MODE (`full` or `live`), `$4` = INDEX (the partition index, 0-based), `$5` = SERVER\_ID. Most backends use `$1`, `$2`, and `$4`; MODE is only relevant if the backend distinguishes between a full write and a live (streaming) write. In `live` mode, `dataset_args` includes a positive `--rate` so the writer throttles its output; in `full` mode, `--rate 0` means unthrottled.

The writer's summary line is a single JSON object on stdout, for example:

```json
{"writer": "influx", "measurement": "monitoring", "backend": "influxdb/python", "node_index": 0, "records": 80000, "errors": 0, "batch": 10000, "write_s": 7.73, "close_sync_s": 0.0, "total_s": 7.73, "records_per_s": 10344}
```

The harness records it and prints these fields: `writer`, `records`, `write_s`, `close_sync_s`, `records_per_s`.

### The query adapter: `query/<n>_query.py`

A Python module that defines one or more subclasses of `Adapter` from `core/adapter.py`, plus two module-level attributes:

```python
ADAPTERS = {"<adapter-name>": AdapterClass, ...}
DEFAULT_ADAPTER = "<adapter-name>"
```

The engine loads this file by path, so nothing needs installing.

Every adapter instance receives `self.ctx`, a dict with these keys:

| Key | Contains | Example |
| --- | --- | --- |
| `target` | The name of the written data (the container path for SOS, or the measurement name constructed as `{DATA_NAME}_{writer}`) | `monitoring_influx` |
| `schema` | The dataset name from the JSON definition — the table name used in query strings and the name verification matches against | `monitoring` |
| `servers` | List of server hostnames from `SERVER_HOSTS` | `["node-1", "node-2"]` |
| `run_dir` | Scratch directory on the current node | `/tmp/monbench-user/run` |
| `env` | The full environment (`os.environ`), including all settings from `bench.conf` and the backend's own config |  |

The distinction between `target` and `schema` matters: `target` is the backend-specific storage name (used for connecting and probing), while `schema` is the dataset's logical name (used in query text via `{schema}` and in the verification engine). For backends where these are the same concept (like InfluxDB after the measurement-naming fix), `schema` is the correct one for query-related work.

#### Adapter methods

Each adapter subclass must implement these methods:

| Method | Timed? | Purpose |
| --- | --- | --- |
| `connect()` | No | Connect and confirm the target holds data. Raise `BackendError` to stop the run |
| `query(text)` | Yes | Run one query and yield its result in batches |
| `count(batch)` | Yes | Return the number of rows in a batch. Default: `len(batch)` |
| `records(batch)` | No | Return `(columns, rows)` where rows are dicts keyed by attribute name. Used for verification only |
| `postprocess(text, columns, rows)` | No | Optional. Backend-specific shaping of a query's full, assembled result (after `records()` and column-map reversal), for verification only. Default: no-op (`return rows`). See `core/adapter.py`'s docstring and `backends/sos/query/sos_query.py` for a concrete use: collapsing a list-attribute query's possible multiple rows per node sample into one, since the SOS/DSOS query language has no JOIN/DISTINCT to do it in the query text itself |
| `close()` | No | Release the connection |

The engine times each run from the call to `query()` until the last batch has been counted. Do only what a normal client of the backend would do inside `query()`, so timings stay comparable.

#### Class attributes

Every adapter has two identity attributes and one optional translation attribute:

| Attribute | Purpose |
| --- | --- |
| `name` | The adapter's name, used as the `QUERY_ADAPTER` setting and as a key in query files |
| `dialect` | The query language (e.g. `"sql"`, `"influxql"`, `"dsos"`). Query files can key on this instead of the adapter name, so adapters that share a language share query strings |
| `type_overrides` | A dict mapping dataset attribute types to backend-specific conventions (see below) |

`dialect` can be set on the class for adapters that always use the same query
language, or set in `__init__` for backends that support more than one
language at runtime. For example, the InfluxDB adapter sets `dialect = None`
on the class and then sets `self.dialect = self.lang` in `__init__` based on
`INFLUX_QUERY_LANG` (which can be `"sql"` or `"influxql"`), so query files
can key strings on either `"sql"` or `"influxql"` and the right one is
selected at runtime.

#### `type_overrides`

Databases differ in how they name and represent certain data types. Rather than requiring query authors to know each backend's conventions, the adapter declares its differences by attribute type, and the engine handles translation automatically.

`type_overrides` is a dict keyed by dataset attribute type (e.g. `"timestamp"`, `"uint64"`, `"string"`). Each value is a dict with two optional keys:

| Key | What it does | Example |
| --- | --- | --- |
| `"column"` | Renames every dataset attribute of this type to the backend's column name in query text, and reverses the rename in result columns | InfluxDB stores the timestamp as `time` regardless of what the dataset calls it |
| `"literal"` | Wraps placeholder values of this type with a format string so the generated SQL uses the backend's native literal syntax | InfluxDB needs `to_timestamp_seconds(1700000000)` instead of a bare integer |

The engine resolves which attributes are affected using the dataset definition's `attributes` list. Translation happens outside the timed region — before query text is finalized and after result columns are returned — so it never contaminates benchmark measurements.

Example from the InfluxDB adapter:

```python
class InfluxPythonAdapter(Adapter):
    name = "influxdb"
    dialect = None
    type_overrides = {
        "timestamp": {"column": "time", "literal": "to_timestamp_seconds({})"},
    }
```

With this declaration and a dataset attribute `{"name": "sample_time", "type": "timestamp"}`, the engine would rename `sample_time` to `time` in queries and wrap its placeholder values with `to_timestamp_seconds()` — with no change to any query string. Backends with no type differences (like SOS/DSOS) leave `type_overrides = None`.

### Testing a new backend

Test incrementally by running phases individually with the SSH launcher. The `bin/run.sh` command accepts a phase name as its last argument:

```bash
# 1. Start the service (if PLUGIN_SERVICE=1)
bin/run.sh -c bench-mydb.conf -l ssh serve-write

# 2. Write the data and confirm the writer's JSON summary
bin/run.sh -c bench-mydb.conf -l ssh write

# 3. Run queries without verification to confirm connectivity
bin/run.sh -c bench-mydb.conf -l ssh query

# 4. Run queries with verification to confirm correctness
bin/run.sh -c bench-mydb.conf -l ssh check

# 5. Run the full sequence including timing
bin/run.sh -c bench-mydb.conf -l ssh all

# 6. Clean up
bin/run.sh -c bench-mydb.conf -l ssh stop clean
```

Start with a small dataset (`SAMPLES=100 COMPONENT_IDS_PER_SERVER=4`) to iterate quickly. Increase once the queries pass verification.

## Defining a custom dataset

The default dataset (`dataset/default.json` + the value rules in
`core/dataset.py`) is a monitoring shape: one record per component per
sample, with attribute names and units following the flat (non-record)
metrics of the `curated_metrics` LDMS sampler plugin. To benchmark a
different data shape, you define a new dataset.

A dataset has two parts:

### The definition file: `dataset/<n>.json`

A JSON file that lists the attributes, their types, and the access keys. The
default dataset's own `dataset/default.json` looks like this (trimmed to a
few attributes for illustration — the real file has 19 metric attributes;
see `dataset/default.json` for the full list):

```json
{
  "name": "monitoring",
  "description": "...",
  "attributes": [
    { "name": "timestamp",    "type": "timestamp" },
    { "name": "component_id", "type": "uint64" },
    { "name": "job_id",       "type": "uint64" },
    { "name": "seq",          "type": "uint64" },
    { "name": "MemTotal",     "type": "uint64" },
    { "name": "free",         "type": "double" },
    { "name": "load1min",     "type": "double" }
  ],
  "keys": [
    { "name": "timestamp",     "attrs": ["timestamp"] },
    { "name": "comp_time",     "attrs": ["component_id", "timestamp"] },
    { "name": "job_comp_time", "attrs": ["job_id", "component_id", "timestamp"] }
  ]
}
```

| Field | Purpose |
| --- | --- |
| `name` | The schema name; used in query strings as `{schema}` and as the record type the verifier matches against |
| `attributes` | The columns. `name` is how queries and the verifier refer to each one. `type` is used by `type_overrides` to apply per-backend column renaming and literal formatting |
| `keys` | The access paths the data is meant to be queried by; each backend maps them onto its own indexing (e.g. SOS creates an index for each, InfluxDB maps tags) |

Any entry in `attributes` may instead be a **list attribute**: a per-sample
nested list, identified by its own name like any other attribute, with
`"type": "list"`, a per-sample length (`min_list_length`/`max_list_length`,
or the `list_length` fixed-length shorthand, plus an optional
`length_stride`), and an `entries` spec that is either
`{"type": "record", "tuples": [...]}` (a flat record of named scalar
fields) or a bare scalar type spec (just `{"type": ..., ...}`, with no
`name` of its own) — see `dataset/curated_metrics_like.json` for a worked
example (`cpu`, `netdev`, design/value-generation.md Sec 6). This is
generated and verified generically by `core/dataset.py`/`core/valuegen.py`
and `core/bench.py` with no code change needed per dataset; whether a
*backend* can write and natively query a given dataset's list attributes
is a separate, per-backend question (see "Writing a new backend" above and
`STATUS.md` for which backends currently do).

Set `DATASET` in `bench.conf` to point to your definition file:

```bash
DATASET="${BENCH_DIR}/dataset/my-shape.json"
```

### The value rules: `core/dataset.py` / `core/valuegen.py`

The value rules are a deterministic, hash-based function of each attribute's
identifying coordinates (dataset name, attribute name, list name/entry
index, node id, job id, sample sequence) — see `design/value-generation.md`.
They need no per-dataset code: any dataset JSON under `dataset/` is
generated and verified by the same `core/valuegen.py` (plus its C port,
`core/valuegen.c`, used by C writers) and `core/dataset.py`/`core/
dataset.c`. Every Python writer and `core/bench.py` itself call
`Dataset.expected()`/`Dataset.expected_lists()` directly, so they pick up a
new dataset automatically; `backends/sos/writer/sos_write.c` likewise calls
into `core/dataset.c`/`core/valuegen.c` rather than reimplementing anything.

Per-CPU-core and per-network-interface metrics (which `curated_metrics`
models as nested LDMS records — a variable-length list of per-core or
per-interface values per sample) are supported as list attributes (above),
one level of nesting below the node; a list attribute's own entries
containing a further nested list is out of scope — see
design/value-generation.md Sec 6.6.

## Writing a query file

A query file is a JSON file that the benchmark engine runs against any
backend that has query strings for its questions. Copy `queries/default.json`
as a starting point.

### Structure

```json
{
  "description": "...",
  "queries": [
    {
      "name": "one component, 25% of the time span",
      "type": "component+time",
      "match": {
        "where": "component_id == {comp_s0} and timestamp >= {t_q1} and timestamp < {t_mid}",
        "order_by": ["component_id", "timestamp"]
      },
      "query": {
        "dsos": "select * from {schema} where component_id == {comp_s0} and timestamp >= {t_q1} and timestamp < {t_mid} order_by comp_time",
        "influxdb": "SELECT * FROM {schema} WHERE component_id = {comp_s0} AND timestamp >= {t_q1} AND timestamp < {t_mid} ORDER BY component_id, timestamp"
      }
    }
  ]
}
```

### Per-query fields

| Field | Required | Purpose |
| --- | --- | --- |
| `name` | Yes | Label for the report |
| `type` | No | Category label (e.g. `"component+time"`, `"job"`); appears in the report alongside the name |
| `match` | No | The question in backend-neutral terms, used for verification (see below). Without it, the query is run, timed, and reported as INFO |
| `query` | Yes | Query strings keyed by adapter name or dialect. A plain string is used for every adapter. An adapter with no string for a question reports it as SKIP |
| `verify` | No | `false` to run and time the query without verifying it. Default `true` |

### The `match` description

`match` describes what the question should return, independently of any
backend's query language:

| Key | Purpose |
| --- | --- |
| `where` | Conditions joined by `and`, with op one of `==`, `!=`, `<`, `<=`, `>`, `>=`. No `or`, no aggregates. Three condition forms, generic over any attribute or declared list attribute the dataset defines (see `design/value-generation.md` Sec 6.5): a flat condition `<attribute> <op> <value>`; a list condition `<name>[<i>].<attribute> <op> <value>` (a specific entry instance) or `<name>[*].<attribute> <op> <value>` (true if any entry of that list matches) — `.{attribute}` is omitted for a list whose entries are a bare scalar (the entry's own value is compared directly); and a cardinality condition `count(<name>) <op> <n>`. `<value>` is a number, or a quoted string (`'...'` or `"..."`) for a `string`-typed attribute |
| `order_by` | List of attribute names the result must be ordered by |
| `limit` | Maximum number of rows (the verifier accepts both whole-result and per-server semantics) |

List conditions describe the question in backend-neutral terms for
verification; a backend also needs its own native query against however it
stores that list attribute to actually answer the question (the backend
mapping in `design/value-generation.md` Sec 6.5). **InfluxDB, SOS and DSOS
all have this now**: a list attribute is written to its own measurement
or schema (`backends/influxdb/README.md`'s and `backends/sos/README.md`'s
"List attribute data model mapping" sections), and a native query answers
`name[i].attr`/`name[*].attr` against it — InfluxDB with a SQL join back to
the node measurement on `(component_id, timestamp)`; SOS/DSOS (whose query
language has no JOIN) by selecting straight from the per-list-attribute
schema, with the existential `name[*].attr` form's possible
multiple-rows-per-sample collapsed by the query engine's
`Adapter.postprocess()` hook (`core/adapter.py`) rather than in the query
text — see `queries/curated_metrics.json` for worked examples against
`dataset/curated_metrics_like.json`, each with `"influxdb"` and `"dsos"`
query strings. `count(name)` is native only on InfluxDB: SOS's query
language has no `GROUP BY`/`HAVING`-style count filter, so that one
condition form still has no native query string for the `sos`/`dsos`
adapters and reports SKIP (see `STATUS.md`). Every list attribute's entry
attribute type maps onto a real SOS schema type (`backends/sos/
make_schema.py`'s `sos_attr()`): `string` becomes a fixed-length
CHAR_ARRAY and `uint8`/`int8` widen to the closest wider SOS integer
type SOS has (`UINT16`/`INT16`) — nothing is dropped from the schema.

Backend-specific choices — which index to use, SQL syntax differences,
function calls — belong in the query string only, not in `match`.

### Keying query strings

The `query` dict can be keyed by adapter name (e.g. `"dsos"`, `"influxdb"`,
`"influxdb-http"`) or by dialect (e.g. `"sql"`, `"dsos"`). The engine looks
up the adapter's name first, then its dialect. This means:

- Adapters that share a dialect share query strings. The `dsos`, `dsosql` and
  `sos` adapters all use dialect `"dsos"`, so one `"dsos"` string covers all
  three.
- An adapter that sets its dialect at runtime (like InfluxDB, where the
  dialect is `"sql"` or `"influxql"` depending on `INFLUX_QUERY_LANG`) picks
  up strings keyed to either language.
- If both the adapter name and dialect have a string, the adapter name wins.

### Placeholders

Query strings and `match` use `{placeholder}` syntax. Placeholders are
filled from the dataset so that one file works at any data-set size:

| Placeholder | Value |
| --- | --- |
| `{schema}` | Schema name from the dataset definition |
| `{limit}` | `LIMIT_N` from `bench.conf` |
| `{t_start}`, `{t_end}` | Timestamps of the first and last sample |
| `{t_q1}`, `{t_mid}`, `{t_q3}` | Timestamps at 25%, 50% and 75% of the time span |
| `{t_mid_1pct}` | 1% of the time span after `{t_mid}` |
| `{comp_s0}`, `{comp_s0_last}` | First and last component on the first server |
| `{comp_slast}`, `{comp_last}` | First component on the last server; last component overall |
| `{job_first}`, `{job_mid}`, `{job_last}` | First job, the job at mid-span, last job |

A list attribute's measurement name is not a separate placeholder: write it
as `{schema}_<name>` directly in the query string (e.g. `{schema}_cpu`) —
the `{schema}` placeholder still expands normally, leaving the list
attribute's name suffix as literal text, exactly like
`queries/curated_metrics.json`'s queries do.

Placeholder values for types that have a `"literal"` entry in the adapter's
`type_overrides` are automatically wrapped with the backend's native syntax
(e.g. InfluxDB timestamps become `to_timestamp_seconds(...)`) — you write
bare `{t_mid}` in the query string, and the engine handles the wrapping.
Column renaming from `type_overrides` is applied to the query template before
placeholders are expanded, so that literal format strings that contain
dataset attribute names (e.g. `to_timestamp_seconds`) are not mangled. This
also means a dataset attribute name that is a plain-text substring of a
list attribute's measurement name (none of the current datasets have one)
would be rewritten too — not a concern today, but worth knowing if you name
attributes and measurements close together.

### Tips

- Start with a copy of `queries/default.json` and modify the questions.
- Add query strings for your backend under its adapter name or dialect.
- Test with `bin/run.sh check` (verification, no timing) before running
  `bin/run.sh query` (timing).
- Use `"verify": false` for queries the verifier cannot check (aggregates,
  grouping, queries with `or` conditions).
- The `type` field groups queries in the report; choose types that describe
  the conditions, not the backend.
