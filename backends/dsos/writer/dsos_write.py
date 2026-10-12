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

Produces the data set defined by the DATASET env var (see
core/dataset.py): the value rules are not duplicated here, this writer
imports core/dataset.py and calls Dataset.expected() for every record,
the same as sos_write.py and sos_write.c, so that containers written by
any of the three answer every query identically.

If the dataset declares component kinds (design/value-generation.md Sec
6), each kind's instances are also written through the same dsosd
session, one schema per kind -- see sos_write.py's KindWriter (mirrored
here) and backends/sos/README.md's "Component-kind data model mapping".
Every attribute type maps onto a real SOS schema type
(backends/sos/make_schema.py's sos_attr()): a string attribute is
written as the fixed-length CHAR_ARRAY the template declares, and an
unsigned 8/16/32/64-bit attribute is written as a plain Python int into
whichever widened SOS integer attribute the template created for it --
nothing is dropped.
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


def get_schema(cont, tmpl, Sos):
    schema = cont.schema_by_name(tmpl["name"])
    if schema is None:
        schema = Sos.Schema()
        schema.from_template(tmpl)
        schema.add(cont)
        schema = cont.schema_by_name(tmpl["name"])
    return schema


class KindWriter:
    """Mirrors sos_write.py's KindWriter -- one component kind's schema
    plus the attr-name -> attr-id map needed to write an instance.
    *attr_names* is the kind's declared attributes that the per-kind
    template has a same-named SOS attribute for (every one of them, in
    practice -- see make_schema.py's sos_attr())."""
    def __init__(self, cont, tmpl, ds, kind, Sos):
        self.schema = get_schema(cont, tmpl, Sos)
        self.attr_ids = {n: self.schema.attr_by_name(n).attr_id()
                         for n in ("timestamp", "component_id", "job_id",
                                   "seq", "component_index")}
        self.attr_names = [a["name"] for a in ds.component_kind(kind)["attributes"]
                           if self.schema.attr_by_name(a["name"]) is not None]
        for n in self.attr_names:
            self.attr_ids[n] = self.schema.attr_by_name(n).attr_id()

    def write(self, comp, job_id, seq, ts, rec, ds, kind):
        obj = self.schema.alloc()
        obj[self.attr_ids["timestamp"]] = (int(ts), 0)
        obj[self.attr_ids["component_id"]] = int(comp)
        obj[self.attr_ids["job_id"]] = int(job_id)
        obj[self.attr_ids["seq"]] = int(seq)
        obj[self.attr_ids["component_index"]] = int(rec["index"])
        for name in self.attr_names:
            v = rec[name]
            t = ds.component_attr_type(kind, name)
            if t == "double":
                obj[self.attr_ids[name]] = float(v)
            elif t == "string":
                obj[self.attr_ids[name]] = str(v)
            else:
                obj[self.attr_ids[name]] = int(v)
        rc = obj.index_add()
        obj.release()
        return rc


def main():
    a = parse_args()
    from sosdb import Sos

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

    # Connect to the local dsosd through the session file.
    session = Sos.Session(a.session)
    cont = session.open(a.target, o_perm=Sos.PERM_RW)

    schema = get_schema(cont, template["node"], Sos)
    attr_ids = {n: schema.attr_by_name(n).attr_id() for n in ds.attr_names}
    kind_writers = {kind: KindWriter(cont, template["components"][kind], ds, kind, Sos)
                    for kind in template["components"]}

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
                elif ds.attr_type(name) == "string":
                    obj[aid] = str(v)
                else:
                    obj[aid] = int(v)
            rc = obj.index_add()
            obj.release()
            if rc:
                errors += 1
                if errors <= 10:
                    print(f"error {rc} indexing record {written}",
                          file=sys.stderr)
            written += 1
            for rec in ds.expected_components(comp, seq):
                rc = kind_writers[rec["kind"]].write(
                    comp, values["job_id"], seq, values["timestamp"],
                    rec, ds, rec["kind"])
                if rc:
                    errors += 1
                    if errors <= 10:
                        print(f"error {rc} indexing {rec['kind']}[{rec['index']}] "
                              f"for record {written}", file=sys.stderr)
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
