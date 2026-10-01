#!/usr/bin/env python3
"""influx_write.py -- write one partition of the benchmark data set to an
InfluxDB 3 server.

Produces the data set defined in core/dataset.py, the same records the SOS
writers produce, so the query engine verifies InfluxDB answers against the same
expected values. The measurement (table) name is passed via --measurement; the
key attributes component_id and job_id become tags; the remaining attributes
become fields; seq is kept as a field so rows can be identified in
verification.

Two interfaces, chosen by --interface:
  python  the influxdb_client_3 library (batched writes)
  http    POST line protocol to the InfluxDB 3 write API with urllib

The write timing (write_s) covers sending every record and flushing; there is
no separate durable-sync step as with SOS, so close_sync_s is 0.
"""
import argparse
import json
import sys
import time
import urllib.request
import urllib.error


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


def records(a):
    """Yield (component_id, seq, ts, job_id, mem_free, load) for this partition."""
    base = (a.node_index + 1) * 1000
    for seq in range(a.samples):
        ts = a.t0 + seq * a.interval
        job = 1 + seq // a.job_len
        for c in range(a.components):
            comp = base + c
            yield comp, seq, ts, job, comp * 1000000 + seq, (comp % 1000) + seq * 0.25


def line(measurement, comp, seq, ts, job, mem_free, load):
    """One line-protocol record. Tags: component_id, job_id. Fields: seq,
    mem_free, load. Integer fields carry an 'i' suffix; load is a float."""
    return (f"{measurement},component_id={comp},job_id={job} "
            f"seq={seq}i,mem_free={mem_free}i,load={load} {ts}")


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
    writer = HttpWriter(a) if a.interface == "http" else PythonWriter(a)

    written = 0
    buf = []
    t_start = time.monotonic()
    for rec in records(a):
        buf.append(line(a.measurement, *rec))
        if len(buf) >= a.batch:
            writer.write(buf)
            written += len(buf)
            buf.clear()
            if a.rate > 0:
                due = t_start + written / a.rate
                wait = due - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
    if buf:
        writer.write(buf)
        written += len(buf)
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
