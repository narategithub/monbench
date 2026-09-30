# sos backend

Writes the benchmark data set into local SOS containers, one partition per
server node, using the SOS storage library directly. It is the primary write
backend; the `dsos` backend queries what it writes.

## What it supports

| | |
|---|---|
| Write | Yes, one local SOS container per server node |
| Query | No (use the `dsos` backend to query SOS containers) |
| Partitioning | Each server writes partition *i* of the data set to its own container |
| Service | None; SOS is a library, so writing starts no daemon |

Because `sos` has no query adapter, a run uses it as `WRITE_BACKEND` and a
different backend (normally `dsos`) as `QUERY_BACKEND`. The primary real-world
case is `WRITE_BACKEND=sos` with `QUERY_BACKEND=dsos`.

`sos` has no service (`PLUGIN_SERVICE=0`), so the `serve-write` phase is a
no-op for this backend.

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

## Configuration

Settings live in `backends/sos/sos_backend.conf` (override from the environment):

| Setting | Default | Purpose |
|---|---|---|
| `SOS_PREFIX` | `/opt/ovis` | SOS installation prefix; used to build the C writer |
| `SOS_DATA_ROOT` | `/tmp/monbench-<user>/data` | Container root; each server uses `SOS_DATA_ROOT/<server-id>/` |
| `SOS_STORAGE` | `mmos` | SOS storage type for new containers: `mmos` or `lsos` |

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
