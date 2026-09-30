# sos backend

Writes the benchmark data set into local SOS containers, one partition per
server node, using the SOS storage library directly. It can also query those
containers directly on disk, without going through `dsosd`, providing a
single-node or shared-filesystem comparison point.

## What it supports

| | |
|---|---|
| Write | Yes, one local SOS container per server node |
| Query | Yes, directly on disk (containers must be accessible from the query node) |
| Partitioning | Each server writes partition *i* of the data set to its own container |
| Service | None; SOS is a library, no daemon needed for writing or querying |

Common configurations:

| `WRITE_BACKEND` | `QUERY_BACKEND` | Description |
|---|---|---|
| `sos` | `dsos` | Write locally, query through dsosd (primary use case) |
| `sos` | `sos` | Write and query locally — single-node or shared-filesystem comparison |

`sos` has no service (`PLUGIN_SERVICE=0`), so the `serve-write` and
`serve-query` phases are no-ops for this backend.

## Write methods

Two writers produce identical data; choose with `WRITERS` (space-separated;
empty selects both):

| `WRITERS` value | Writer | Interface |
|---|---|---|
| `c` | `writer/sos_write` | SOS **C API**, compiled by the build phase |
| `py` | `writer/sos_write.py` | SOS **Python bindings** (`sosdb`) |
| `c py` (default) | both, into separate targets `monitoring_c` and `monitoring_py` | for comparison |

The C writer is built by `bin/run.sh build`; the Python writer needs the
`sosdb` module importable on the server nodes.

## Query methods

| `QUERY_ADAPTER` | Adapter | How it queries | Use |
|---|---|---|---|
| `sos` (default) | `sosdb` Python API (`SqlQuery`), returning DataFrames | opens each server's container directly on disk | **timings** without RPC overhead |

The `sos` adapter speaks the same query language (dialect `dsos`) as the
`dsos` backend's adapters, so they share the query strings in a query set.

**Requirement:** the containers must be accessible from the query node.
This works when the query node is also a server node, or on a shared
filesystem. `SOS_DATA_ROOT` must resolve to the same container root from
the query node as it does on the server nodes; if the default includes
`${HOSTNAME}`, override it to a shared path for the query side.

## Configuration

Settings live in `backends/sos/sos_backend.conf` (override from the environment):

| Setting | Default | Purpose |
|---|---|---|
| `SOS_PREFIX` | `/opt/ovis` | SOS installation prefix; used to build the C writer |
| `SOS_DATA_ROOT` | `${RUN_DIR}/sos-dsos/data/${HOSTNAME}/` | Container root; each server uses `SOS_DATA_ROOT/<server-id>/` |
| `SOS_STORAGE` | `mmos` | SOS storage type for new containers: `mmos` or `lsos` |
| `SOS_ROW_LIMIT` | `4096` | Rows per batch fetched by the query adapter |

### Choosing the storage type

```bash
SOS_STORAGE=mmos bin/run.sh -l ssh write     # memory-mapped (default)
SOS_STORAGE=lsos bin/run.sh -l ssh write     # log-structured, for Lustre/GPFS/NFS
```

### Choosing the writer

```bash
WRITERS=c   bin/run.sh -l ssh build write    # C writer only
WRITERS=py  bin/run.sh -l ssh write          # Python writer only (no build needed)
WRITERS="c py" bin/run.sh -l ssh build write # both (default)
```

## Requirements

- SOS installed at `SOS_PREFIX` on the server nodes.
- For the C writer: `jansson` and `libuuid` development files, to build.
- For the Python writer: the `sosdb` module importable, built against the same
  Python that runs it.
