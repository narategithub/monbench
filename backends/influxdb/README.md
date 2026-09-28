# influxdb backend

Writes the benchmark data set to, and queries it from, a single **InfluxDB 3**
server. Unlike `sos`/`dsos`, it is not partitioned across the server nodes: each
server node generates its own partition and sends it to the same InfluxDB
server, so together the nodes write the whole data set once.

## What it supports

| | |
|---|---|
| Write | Yes, to one InfluxDB 3 server (`INFLUX_HOST`) |
| Query | Query adapter is in progress; selecting it reports a clear "not implemented yet" until Chunk B lands |
| Reads data written by | `influxdb` |
| Service | None (`PLUGIN_SERVICE=0`); the InfluxDB server runs on its own. Both `serve-write` and `serve` are no-ops for this backend |
| Partitioning | Data is generated per server but all sent to one server, not sharded |

## Data model mapping

The data set maps to InfluxDB as:

| Data set | InfluxDB |
|---|---|
| target name | measurement (table) |
| `component_id`, `job_id` | tags |
| `seq`, `mem_free`, `load` | fields (`seq` kept so rows can be identified in verification) |
| `timestamp` | point time |

## Write methods

One writer, `writer/influx_write.py`, with a selectable interface:

| `INFLUX_INTERFACE` | How it writes | Extra dependency |
|---|---|---|
| `python` (default) | the `influxdb_client_3` library, batched | `pip install influxdb3-python` |
| `http` | POST line protocol to the v3 write API | none (standard library only) |

Select the writer with `WRITERS=influx` (it is the default for this backend).

## Query methods

The query adapter will support InfluxDB 3's two query languages, selected with
`INFLUX_QUERY_LANG`:

| `INFLUX_QUERY_LANG` | Language |
|---|---|
| `sql` (default) | SQL |
| `influxql` | InfluxQL |

The interface for querying follows `INFLUX_INTERFACE` (the `python` client uses
Arrow Flight; `http` uses the query API). Query strings for InfluxDB go in a
query set under the `influxdb` adapter name.

## Configuration

Settings live in `backends/influxdb/backend.conf` (override from the
environment):

| Setting | Default | Purpose |
|---|---|---|
| `INFLUX_HOST` | `http://localhost:8181` | InfluxDB 3 server URL, reachable from all nodes |
| `INFLUX_DATABASE` | `monbench` | Database to write to and query (create it in advance) |
| `INFLUX_TOKEN` | (empty) | Read/write token; best set in the environment, not committed |
| `INFLUX_INTERFACE` | `python` | `python` or `http` (write, and query when implemented) |
| `INFLUX_QUERY_LANG` | `sql` | `sql` or `influxql` |
| `INFLUX_PRECISION` | `s` | Line-protocol timestamp precision: `ns`/`us`/`ms`/`s` |
| `INFLUX_WRITE_BATCH` | `10000` | Records per write request/batch |

### Examples

```bash
# Write with the Python client (default)
WRITE_BACKEND=influxdb INFLUX_TOKEN=... bin/run.sh -l ssh write

# Write with the HTTP interface (no client library needed)
WRITE_BACKEND=influxdb INFLUX_INTERFACE=http INFLUX_TOKEN=... bin/run.sh -l ssh write

# Choose the query language (once the query adapter is available)
QUERY_BACKEND=influxdb INFLUX_QUERY_LANG=influxql bin/run.sh -l ssh check
```

## Requirements

- An InfluxDB 3 server reachable at `INFLUX_HOST`, with `INFLUX_DATABASE`
  created and a read/write token.
- For the `python` interface: `pip install influxdb3-python` on the write (and
  query) nodes. The `http` interface needs only the Python standard library.
