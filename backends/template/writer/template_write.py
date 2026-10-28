#!/usr/bin/env python3
"""Template writer. Copy this directory to backends/<name>/writer/ and
rename this file to <name>_write.py.

A writer produces one partition of the benchmark data set (the partition
assigned by node.sh via --node-index). It reads the dataset definition
from the DATASET env var, iterates over every (component_id, seq) pair
in that partition, and writes the expected values to the backend's
storage.

After writing, it prints exactly one JSON summary line to stdout (see
backends/README.md for the required fields).

See core/dataset.py for the Dataset API, core/valuegen.py for the
value-generation design, and EXTENDING.md for the full extension guide.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "..", "..", "core"))
from dataset import Dataset              # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    # --target: the storage name for this partition, constructed by
    # plugin_write from ds_name() -- e.g. "monitoring_py".
    p.add_argument("--target", required=True,
                   help="storage name (measurement/container/table)")

    # Standard dataset flags, passed via ${dataset_args[@]} in the plugin.
    p.add_argument("--node-index", type=int, required=True)
    p.add_argument("--components", type=int, default=16)
    p.add_argument("--samples", type=int, default=5000)
    p.add_argument("--t0", type=int, default=1700000000)
    p.add_argument("--interval", type=int, default=1)
    p.add_argument("--job-len", type=int, default=500)
    p.add_argument("--batch", type=int, default=10000,
                   help="records per write batch")
    p.add_argument("--rate", type=float, default=0.0,
                   help="records/s throttle, 0 = unthrottled")

    # Backend-specific flags -- add your own here (host, port, token, path, …).
    # p.add_argument("--host", required=True)
    return p.parse_args()


def connect(a):
    """Open a connection to the backend.

    Use a.target as the storage name (measurement, container, table --
    whatever the backend calls it).  Raise on failure.
    """
    raise NotImplementedError("template backend: connect() not implemented")


def write_record(conn, comp, seq, values, ds):
    """Write one flat (node-level) record.

    *values* is the dict returned by ds.expected(comp, seq) -- keys are
    the dataset's attribute names, values are the expected typed values.
    """
    raise NotImplementedError("template backend: write_record() not implemented")


def write_list_entries(conn, comp, seq, list_records, ds):
    """Write every list-attribute entry instance for one (comp, seq).

    *list_records* is the list returned by ds.expected_lists(comp, seq);
    each element is a dict with at least 'list' (name), 'index'
    (entry_index), and the entry's own attribute values.

    A dataset with no list attributes produces an empty list, so this
    function may be a no-op for the simplest backends.
    """
    raise NotImplementedError("template backend: write_list_entries() not implemented")


def close(conn):
    """Flush and close the connection.  Return the wall-clock seconds
    spent in the close/sync step (0 if nothing to sync)."""
    raise NotImplementedError("template backend: close() not implemented")


def main():
    a = parse_args()

    # Load the dataset definition from the DATASET env var.
    ds = Dataset(Dataset.load_definition(os.environ["DATASET"]),
                 partitions=1, component_ids=a.components, samples=a.samples,
                 t0=a.t0, interval=a.interval, job_len=a.job_len)

    conn = connect(a)

    written = 0
    errors = 0
    base = (a.node_index + 1) * 1000
    t_start = time.monotonic()

    for seq in range(a.samples):
        for c in range(a.components):
            comp = base + c

            # Flat (node-level) attributes.
            values = ds.expected(comp, seq)
            try:
                write_record(conn, comp, seq, values, ds)
            except Exception:
                errors += 1
                continue

            # List-attribute entry instances (empty for datasets without lists).
            list_records = ds.expected_lists(comp, seq)
            if list_records:
                try:
                    write_list_entries(conn, comp, seq, list_records, ds)
                except Exception:
                    errors += 1

            written += 1

            # Optional throttle.
            if a.rate > 0:
                due = t_start + written / a.rate
                wait = due - time.monotonic()
                if wait > 0:
                    time.sleep(wait)

    t_written = time.monotonic()
    close_sync_s = close(conn)
    t_end = time.monotonic()

    # One JSON summary line to stdout -- required by the harness.
    print(json.dumps({
        "writer": "template", "target": a.target,
        "backend": "template", "node_index": a.node_index,
        "records": written, "errors": errors, "batch": a.batch, "rate": a.rate,
        "write_s": round(t_written - t_start, 3),
        "close_sync_s": round(close_sync_s, 3),
        "total_s": round(t_end - t_start, 3),
        "records_per_s": (round(written / (t_written - t_start))
                          if t_written > t_start else 0),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
