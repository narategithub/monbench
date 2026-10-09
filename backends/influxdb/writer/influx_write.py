#!/usr/bin/env python3
"""influx_write.py -- write one partition of the benchmark data set to an
InfluxDB 3 server.

Produces the data set defined by the DATASET env var (see
core/dataset.py), the same records the SOS writers produce, so the query
engine verifies InfluxDB answers against the same expected values. The
measurement (table) name is passed via --measurement; the key attributes
component_id and job_id become tags; the remaining attributes become
fields, their line-protocol encoding (integer/float/string) taken from
each attribute's declared type, so a dataset with different or additional
attributes needs no change here.

If the dataset declares component kinds (design/value-generation.md Sec
6), each kind's instances are also written, one measurement per kind
named "<measurement>_<kind>" -- see component_lines() below and
backends/influxdb/README.md's "Component-kind data model mapping" for
the exact tag/field layout. A dataset with no "components" (e.g. the
default data set) writes exactly as before.

Two interfaces, chosen by --interface:
  python  the influxdb_client_3 library (batched writes)
  http    POST line protocol to the InfluxDB 3 write API with urllib

The write timing (write_s) covers sending every record and flushing; there is
no separate durable-sync step as with SOS, so close_sync_s is 0.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "..", "..", "core"))
from dataset import Dataset              # noqa: E402

# Attributes that become InfluxDB tags rather than fields.
_TAGS = ("component_id", "job_id")
# Integer-typed dataset types; formatted with the 'i' line-protocol suffix.
_INT_TYPES = ("uint8", "uint16", "uint32", "uint64",
             "int8", "int16", "int32", "int64")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--measurement", required=True, help="measurement/table name")
    p.add_argument("--node-index", type=int, required=True)
    p.add_argument("--components", type=int, default=16)
    p.add_argument("--samples", type=int, default=5000)
    p.add_argument("--t0", type=int, default=1700000000)
    p.add_argument("--interval", type=int, default=1)
    p.add_argument("--job-len", type=int, default=500)
    p.add_argument("--batch", type=int, default=10000, help="records per request/batch")
    p.add_argument("--rate", type=float, default=0.0, help="records/s, 0 = unthrottled")
    p.add_argument("--interface", choices=["python", "http"], default="python")
    p.add_argument("--host", required=True)
    p.add_argument("--database", required=True)
    p.add_argument("--token", required=True, help="InfluxDB admin token")
    p.add_argument("--precision", choices=["ns", "us", "ms", "s"], default="s")
    return p.parse_args()


def records(a, ds):
    """Yield (component_id, seq, values) for this partition, where *values*
    is the Dataset.expected() dict for that (component_id, seq)."""
    base = (a.node_index + 1) * 1000
    for seq in range(a.samples):
        for c in range(a.components):
            comp = base + c
            yield comp, seq, ds.expected(comp, seq)


def _escape_string_field(v):
    return '"' + str(v).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _field(name, v, attr_type):
    """One name=value line-protocol field, encoded per its declared type:
    integer types get the 'i' suffix, string types are quoted, everything
    else (double) is a bare float. Shared by line() (flat attributes) and
    component_lines() (component-kind attributes) so the two encode
    identically."""
    if attr_type in _INT_TYPES:
        return f"{name}={int(v)}i"
    if attr_type == "string":
        return f"{name}={_escape_string_field(v)}"
    return f"{name}={v}"                                # double


def line(measurement, comp, seq, values, ds):
    """One line-protocol record for the flat (node-level) measurement.
    Tags: component_id, job_id. Fields: every other dataset attribute
    (seq included, so rows can be identified)."""
    fields = []
    for name in ds.attr_names:
        if name in _TAGS or name == "timestamp":
            continue
        fields.append(_field(name, values[name], ds.attr_type(name)))
    ts = int(values["timestamp"])
    return (f"{measurement},component_id={comp},job_id={values['job_id']} "
            f"{','.join(fields)} {ts}")


def component_lines(measurement_base, comp, seq, values, ds):
    """Line-protocol records for every component-kind instance at this
    sample (design/value-generation.md Sec 6), one measurement per kind
    named "<measurement_base>_<kind>" -- see
    backends/influxdb/README.md's component-kind mapping table.

    Tags: component_id, job_id, component_index (the design doc's
    node_id/job_id/component_index). Fields: the kind's own attributes,
    plus seq (kept, like the flat measurement's seq field, so a component
    row can be tied back to the sample it belongs to without relying on
    point time alone). Point time is the same timestamp as the node
    sample this component instance belongs to, so a native query can join
    a component measurement back to the flat one on (component_id, time)
    -- it already uniquely identifies (component_id, seq), and job_id is
    a deterministic function of seq, so it never disagrees with that
    join.

    Returns an empty list for a dataset with no declared component kinds
    (e.g. the default data set), so callers need no special-casing.
    """
    job_id = values["job_id"]
    ts = int(values["timestamp"])
    lines = []
    for rec in ds.expected_components(comp, seq):
        kind = rec["kind"]
        measurement = f"{measurement_base}_{kind}"
        fields = [f"seq={seq}i"]
        for attr in ds.component_kind(kind)["attributes"]:
            name = attr["name"]
            fields.append(_field(name, rec[name], ds.component_attr_type(kind, name)))
        lines.append(f"{measurement},component_id={comp},job_id={job_id},"
                     f"component_index={rec['index']} {','.join(fields)} {ts}")
    return lines


class HttpWriter:
    """POST line protocol to the InfluxDB 3 write API."""
    def __init__(self, a):
        self.url = (f"{a.host.rstrip('/')}/api/v3/write_lp"
                    f"?db={a.database}&precision={a.precision}")
        self.headers = {"Content-Type": "text/plain; charset=utf-8",
                        "Authorization": f"Bearer {a.token}"}

    def write(self, lines):
        body = "\n".join(lines).encode()
        req = urllib.request.Request(self.url, data=body, headers=self.headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"write failed ({e.code}): {e.read().decode(errors='replace')[:200]}")

    def close(self):
        pass


class PythonWriter:
    """Batched writes through the influxdb_client_3 library."""
    def __init__(self, a):
        try:
            from influxdb_client_3 import InfluxDBClient3
        except ImportError as e:
            sys.exit(f"influx_write.py: the influxdb_client_3 module is not "
                     f"available: {e}. Use --interface http, or pip install "
                     f"influxdb3-python.")
        self.precision = a.precision
        self.client = InfluxDBClient3(host=a.host, database=a.database,
                                      token=a.token)

    def write(self, lines):
        self.client.write(record=lines, write_precision=self.precision)

    def close(self):
        self.client.close()


def main():
    a = parse_args()
    # The authoritative attribute list and types come from the dataset
    # definition (DATASET env var), same as every other writer -- not a
    # hardcoded name list, so a different or expanded dataset needs no
    # change here.
    ds = Dataset(Dataset.load_definition(os.environ["DATASET"]),
                partitions=1, components=a.components, samples=a.samples,
                t0=a.t0, interval=a.interval, job_len=a.job_len)
    writer = HttpWriter(a) if a.interface == "http" else PythonWriter(a)

    # 'written' counts node-level samples only (what DS.size() and every
    # other writer's "records" field count), even though buf also carries
    # each sample's component-kind lines -- a dataset with component
    # kinds sends more lines than 'written' implies, but 'written' stays
    # comparable across backends and across datasets with or without
    # component kinds.
    written = 0
    buf = []
    t_start = time.monotonic()
    for comp, seq, values in records(a, ds):
        buf.append(line(a.measurement, comp, seq, values, ds))
        buf.extend(component_lines(a.measurement, comp, seq, values, ds))
        written += 1
        if len(buf) >= a.batch:
            writer.write(buf)
            buf.clear()
            if a.rate > 0:
                due = t_start + written / a.rate
                wait = due - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
    if buf:
        writer.write(buf)
    t_written = time.monotonic()
    writer.close()

    print(json.dumps({
        "writer": "influx", "measurement": a.measurement,
        "backend": f"influxdb/{a.interface}", "node_index": a.node_index,
        "records": written, "errors": 0, "batch": a.batch, "rate": a.rate,
        "write_s": round(t_written - t_start, 3),
        "close_sync_s": 0.0,
        "total_s": round(time.monotonic() - t_start, 3),
        "records_per_s": round(written / (t_written - t_start)) if t_written > t_start else 0,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
