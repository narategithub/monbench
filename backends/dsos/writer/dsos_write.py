#!/usr/bin/env python3
"""dsos_write.py -- write the benchmark data set through the DSOS API.

The writer connects to the local dsosd via a session file, opens (or
creates) a container through the distributed API, and inserts the same
records that sos_write.py would write locally.  This measures the write
path through dsosd, including the RPC overhead.

The container ends up on the local server, stored by dsosd under the
path configured in its directory file.  Because dsosd owns the container,
the schema must be added through the session, not by opening the
container directly.
"""
import argparse
import json
import os
import sys
import time


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--session", required=True,
                   help="path to the DSOS session file (one host per line)")
    p.add_argument("--target", required=True,
                   help="container / target name opened through dsosd")
    p.add_argument("--template", required=True,
                   help="SOS schema template JSON file")
    p.add_argument("--node-index", type=int, required=True)
    p.add_argument("--components", type=int, default=16)
    p.add_argument("--samples", type=int, default=5000)
    p.add_argument("--t0", type=int, default=1700000000)
    p.add_argument("--interval", type=int, default=1)
    p.add_argument("--job-len", type=int, default=500)
    p.add_argument("--batch", type=int, default=10000)
    p.add_argument("--rate", type=float, default=0.0,
                   help="records per second, 0 = unthrottled")
    return p.parse_args()


def main():
    a = parse_args()
    from sosdb import Sos

    with open(a.template) as f:
        template = json.load(f)

    t_start = time.monotonic()

    # Connect to the local dsosd through the session file.
    session = Sos.Session(a.session)
    cont = session.open(a.target, o_perm=Sos.PERM_RW)

    schema = cont.schema_by_name(template["name"])
    if schema is None:
        schema = Sos.Schema()
        schema.from_template(template)
        schema.add(cont)
        schema = cont.schema_by_name(template["name"])

    ids = {n: schema.attr_by_name(n).attr_id()
           for n in ("timestamp", "component_id", "job_id", "seq",
                     "mem_free", "load")}
    i_ts   = ids["timestamp"]
    i_comp = ids["component_id"]
    i_job  = ids["job_id"]
    i_seq  = ids["seq"]
    i_mem  = ids["mem_free"]
    i_load = ids["load"]

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
            obj[i_ts]   = ts
            obj[i_comp] = comp
            obj[i_job]  = job
            obj[i_seq]  = seq
            obj[i_mem]  = comp * 1000000 + seq
            obj[i_load] = float(comp % 1000) + seq * 0.25
            rc = obj.index_add()
            obj.release()
            if rc:
                errors += 1
                if errors <= 10:
                    print(f"error {rc} indexing record {written}",
                          file=sys.stderr)
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

    cont.close()
    t_end = time.monotonic()

    print(json.dumps({
        "writer": "dsos", "target": a.target, "backend": "dsos",
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
