# dsos backend

Writes to and queries SOS containers across all server nodes through the
`dsosd` RPC service. A single query fans out to every server, each scans its
own partition, and the client merges the results in index order.

## What it supports

| | |
|---|---|
| Write | Yes, through the `dsosd` API (`dsos` writer). Each server's writer connects to the local `dsosd` and inserts records through the distributed API |
| Query | Yes, across all servers through `dsosd` |
| Reads data written by | `sos`, `dsos` (declared in `PLUGIN_READS`) |
| Service | `dsosd` on each server node; started in `serve-write` and stopped in `stop` |
| Component kinds (design/value-generation.md Sec 6) | Yes, same mapping as the `sos` backend -- see `backends/sos/README.md`'s "Component-kind data model mapping" |

The DSOS writer stores data in the same SOS container format as the `sos`
backend, so data written by either backend can be queried by either.

Common configurations:

| `WRITE_BACKEND` | `QUERY_BACKEND` | Description |
|---|---|---|
| `sos` | `dsos` | Write locally, query through dsosd |
| `dsos` | `dsos` | Write and query through dsosd |

## Query methods

Two adapters, selected with `QUERY_ADAPTER` (empty selects the default):

| `QUERY_ADAPTER` | Adapter | How it queries | Use |
|---|---|---|---|
| `dsos` (default) | `sosdb` Python API (`SqlQuery`), returning DataFrames | one client, results merged in the client | **timings** |
| `dsosql` | the `dsosql` command, CSV output parsed | starts a `dsosql` process per query | cross-checking results; its timings include process start-up and connecting, so not for performance comparison |

Both speak the same query language (dialect `dsos`), so they share the query
strings in a query set -- including the component-kind `kind[i].attr`/
`kind[*].attr` query strings, since `dsos_write.py` writes the same
per-kind schemas the `sos` writers do (see `backends/sos/README.md`'s
"Component-kind data model mapping"; `count(kind)` is not natively
queryable here either, for the same reason).

## Configuration

Settings live in `backends/dsos/dsos_backend.conf` (override from the environment):

| Setting | Default | Purpose |
|---|---|---|
| `DSOSD` | `dsosd` | The `dsosd` command. Set to `sudo -E dsosd` if registering with `rpcbind` needs root |
| `DSOSQL` | `dsosql` | The `dsosql` command (used by the `dsosql` adapter) |
| `SOS_ROW_LIMIT` | `4096` | Rows per batch the `dsos` adapter fetches |
| `SOS_DATA_ROOT` | `${RUN_DIR}/data/${HOSTNAME}/` | Where each server's containers are; must match the `sos` backend's value so `dsosd` finds them |

### Choosing the query adapter

```bash
QUERY_ADAPTER=dsos   bin/run.sh -l ssh serve check stop   # Python API (default)
QUERY_ADAPTER=dsosql bin/run.sh -l ssh serve check stop   # dsosql, for cross-checking
```

When `dsos` is also the write backend, use `serve-write` instead of `serve`.

## Requirements

- `dsosd` and `dsosql` installed on the nodes.
- `rpcbind` running on the server nodes, and the query node able to reach it
  (TCP/UDP 111) and the port `dsosd` registers.
- For the `dsos` adapter: the `sosdb` Python module importable on the query
  node, built with the `SqlQuery.next()` batch fix (without it, one row per full
  `SOS_ROW_LIMIT` batch is dropped and verification fails).

## Notes

- The harness removes `dsosd`'s `rpcbind` registration when it stops the
  service, since `dsosd` does not deregister itself.
- `limit` in a query applies per server, so `limit N` can return up to N per
  server; the verifier accepts this and reports it.
