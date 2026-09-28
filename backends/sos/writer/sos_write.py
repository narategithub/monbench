#!/usr/bin/env python3
"""sos_write.py -- write the benchmark data set with the SOS Python bindings.

Produces the data set defined in core/dataset.py, the same records as
sos_write.c, so that containers written by either writer answer every
query identically.
"""
import argparse
import json
import os
import sys
import time

from sosdb import Sos


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

    t_start = time.monotonic()
    cont = open_container(a.path, a.storage)
    schema = get_schema(cont, template)
    ids = {n: schema.attr_by_name(n).attr_id()
           for n in ("timestamp", "component_id", "job_id", "seq",
                     "mem_free", "load")}
    i_ts, i_comp, i_job, i_seq, i_mem, i_load = (
        ids["timestamp"], ids["component_id"], ids["job_id"],
        ids["seq"], ids["mem_free"], ids["load"])

    base = (a.node_index + 1) * 1000
    written = errors = in_batch = 0
    t_write = time.monotonic()

    cont.begin()
    for seq in range(a.samples):
        ts = (a.t0 + seq * a.interval, 0)
        job = 1 + seq // a.job_len
        for c in range(a.components):
            comp = base + c
            obj = schema.alloc()
            obj[i_ts] = ts
            obj[i_comp] = comp
            obj[i_job] = job
            obj[i_seq] = seq
            obj[i_mem] = comp * 1000000 + seq
            obj[i_load] = float(comp % 1000) + seq * 0.25
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
