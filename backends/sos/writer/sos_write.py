#!/usr/bin/env python3
"""sos_write.py -- write the benchmark data set with the SOS Python bindings.

Produces the data set defined by the DATASET env var (see
core/dataset.py), the same records as sos_write.c, so that containers
written by either writer answer every query identically. The value rules
themselves are not duplicated here: this writer imports core/dataset.py
and calls Dataset.expected() for every record, so it can never drift from
the Python reference.
"""
import argparse
import json
import os
import sys
import time

from sosdb import Sos

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "..", "..", "core"))
from dataset import Dataset              # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--path", required=True)
    p.add_argument("--template", required=True)
    p.add_argument("--node-index", type=int, required=True)
    p.add_argument("--components", type=int, default=16)
    p.add_argument("--samples", type=int, default=5000)
    p.add_argument("--t0", type=int, default=1700000000)
    p.add_argument("--interval", type=int, default=1)
    p.add_argument("--job-len", type=int, default=500)
    p.add_argument("--batch", type=int, default=10000)
    p.add_argument("--rate", type=float, default=0.0,
                   help="records per second, 0 = unthrottled")
    p.add_argument("--storage", choices=["mmos", "lsos"], default="mmos")
    return p.parse_args()


def open_container(path, backend):
    be = Sos.BE_LSOS if backend == "lsos" else Sos.BE_MMOS
    cont = Sos.Container()
    if not os.path.exists(path):
        cont.create(path, o_mode=0o660, backend=be)
    cont.open(path, o_perm=Sos.PERM_RW)
    return cont


def get_schema(cont, template):
    schema = cont.schema_by_name(template["name"])
    if schema is None:
        schema = Sos.Schema()
        schema.from_template(template)
        schema.add(cont)
        schema = cont.schema_by_name(template["name"])
    return schema


def main():
    a = parse_args()
    with open(a.template) as f:
        template = json.load(f)

    # The authoritative attribute list and types come from the dataset
    # definition (DATASET env var -- the same file bench.py and
    # core/dataset.py meta use), not from the SOS schema template: the
    # template only needs to have a same-named, same-typed attribute for
    # each one. This is what lets a new dataset need no change here.
    ds = Dataset(Dataset.load_definition(os.environ["DATASET"]),
                partitions=1, components=a.components, samples=a.samples,
                t0=a.t0, interval=a.interval, job_len=a.job_len)

    t_start = time.monotonic()
    cont = open_container(a.path, a.storage)
    schema = get_schema(cont, template)
    attr_ids = {n: schema.attr_by_name(n).attr_id() for n in ds.attr_names}

    base = (a.node_index + 1) * 1000
    written = errors = in_batch = 0
    t_write = time.monotonic()

    cont.begin()
    for seq in range(a.samples):
        for c in range(a.components):
            comp = base + c
            values = ds.expected(comp, seq)
            obj = schema.alloc()
            for name in ds.attr_names:
                aid = attr_ids[name]
                v = values[name]
                if name == "timestamp":
                    obj[aid] = (int(v), 0)
                elif ds.attr_type(name) == "double":
                    obj[aid] = float(v)
                else:
                    obj[aid] = int(v)
            rc = obj.index_add()
            obj.release()
            if rc:
                errors += 1
                if errors <= 10:
                    print(f"error {rc} indexing record {written}", file=sys.stderr)
            written += 1
            in_batch += 1
            if in_batch == a.batch:
                cont.commit()
                cont.end()
                in_batch = 0
                cont.begin()
            if a.rate > 0:
                wait = t_write + written / a.rate - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
    cont.commit()
    cont.end()
    t_indexed = time.monotonic()
    # The bindings do not export SOS_COMMIT_SYNC, so close() is asynchronous;
    # os.sync() flushes the written pages so the timing is comparable with
    # the C writer's synchronous close.
    cont.close()
    os.sync()
    t_end = time.monotonic()

    print(json.dumps({
        "writer": "py", "container": a.path, "backend": a.storage,
        "node_index": a.node_index, "records": written, "errors": errors,
        "batch": a.batch, "rate": a.rate,
        "open_s": round(t_write - t_start, 3),
        "write_s": round(t_indexed - t_write, 3),
        "close_sync_s": round(t_end - t_indexed, 3),
        "total_s": round(t_end - t_start, 3),
        "records_per_s": round(written / (t_end - t_write)),
    }))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
