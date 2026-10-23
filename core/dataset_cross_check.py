#!/usr/bin/env python3
"""dataset_cross_check.py -- verify dataset.py and dataset.c agree.

Loads each of the suite's real dataset JSON files (dataset/default.json,
dataset/curated_metrics_like.json) through dataset.py's Dataset class,
computes expected()/expected_lists() for a battery of (cid, seq)
coordinates, builds and runs dataset_cross_check.c (linked against
dataset.c, valuegen.c, and the system libjansson) over the same JSON
files and coordinates, and diffs the two.

This is dataset.c's analog of valuegen_cross_check.py -- see that
module's docstring and design/value-generation.md: any change to
dataset.py's attribute-resolution or expected()/expected_lists()
semantics must be mirrored in dataset.c, and this is the test that
catches a divergence.

Run it after touching dataset.py, dataset.c, or dataset.h:

    python3 core/dataset_cross_check.py

Exits 0 and prints "OK" if every case matches; otherwise prints each
mismatch and exits 1.
"""
import os
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dataset import Dataset, IDENTITY_ATTRS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# (dataset json path, t0, interval, job_len, coords to exercise)
DATASETS = [
    (os.path.join(REPO_ROOT, "dataset", "default.json"), 1700000000, 1, 500,
     [(1001, 0), (1001, 12345), (2007, 999), (4000, 500000), (1001, 777)]),
    (os.path.join(REPO_ROOT, "dataset", "curated_metrics_like.json"),
     1700000000, 1, 500,
     [(1001, 0), (2007, 999), (4000, 500000), (9, 42), (1001, 777)]),
]


def double_bits_hex(d):
    return struct.pack(">d", d).hex()


def fmt_value(attr_type, value):
    """Format one attribute's value identically to how
    dataset_cross_check.c formats the same typed value, so the two can
    be string-compared directly."""
    if attr_type == "double":
        return double_bits_hex(value)
    if attr_type == "string":
        return str(value)
    # uintN / intN / timestamp: decimal integer
    return str(int(value))


def build_exp_case(ds, cid, seq):
    values = ds.expected(cid, seq)
    fields = [fmt_value(ds.attr_type(name), values[name]) for name in ds.attr_names]
    return "|".join(fields)


def build_cnt_case(ds, cid, seq):
    entries = ds.expected_lists(cid, seq)
    recs = []
    for rec in entries:
        name = rec["list"]
        la = ds.list_attr(name)
        fields = [name, str(rec["index"])]
        if la["entries"].get("type") == "record":
            for field in la["entries"].get("tuples", []):
                fields.append(fmt_value(field["type"], rec[field["name"]]))
        else:
            fields.append(fmt_value(la["entries"]["type"], rec["value"]))
        recs.append("|".join(fields))
    return ";".join(recs)


def main():
    cases = []  # (kind, ds_path, t0, interval, job_len, cid, seq, expected)
    for ds_path, t0, interval, job_len, coords in DATASETS:
        ds = Dataset(Dataset.load_definition(ds_path), partitions=1,
                      component_ids=16, samples=5000, t0=t0, interval=interval,
                      job_len=job_len)
        for cid, seq in coords:
            cases.append(("EXP", ds_path, t0, interval, job_len, cid, seq,
                           build_exp_case(ds, cid, seq)))
            cases.append(("CNT", ds_path, t0, interval, job_len, cid, seq,
                           build_cnt_case(ds, cid, seq)))

    with tempfile.TemporaryDirectory() as tmp:
        binary_path = os.path.join(tmp, "dataset_cross_check")
        jansson_so = "/usr/lib/x86_64-linux-gnu/libjansson.so.4"
        compile_cmd = ["cc", "-O2", "-Wall", "-I", HERE,
                       "-o", binary_path,
                       os.path.join(HERE, "dataset_cross_check.c"),
                       os.path.join(HERE, "dataset.c"),
                       os.path.join(HERE, "valuegen.c"),
                       jansson_so]
        r = subprocess.run(compile_cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print("dataset_cross_check: C compile failed:", file=sys.stderr)
            print(r.stderr, file=sys.stderr)
            return 2

        results = []
        for kind, ds_path, t0, interval, job_len, cid, seq, _expected in cases:
            r = subprocess.run(
                [binary_path, ds_path, str(t0), str(interval), str(job_len),
                 kind, str(cid), str(seq)],
                capture_output=True, text=True)
            if r.returncode != 0:
                print(f"dataset_cross_check: C run failed for "
                      f"{kind} {ds_path} cid={cid} seq={seq}:", file=sys.stderr)
                print(r.stderr, file=sys.stderr)
                return 2
            results.append(r.stdout.rstrip("\n"))

    failures = 0
    for i, ((kind, ds_path, t0, interval, job_len, cid, seq, expected), got) \
            in enumerate(zip(cases, results)):
        if expected != got:
            failures += 1
            print(f"MISMATCH case {i} [{kind}] {os.path.basename(ds_path)} "
                  f"cid={cid} seq={seq}:\n  python={expected!r}\n  c     ={got!r}")

    if failures:
        print(f"\n{failures} of {len(cases)} cases mismatched")
        return 1
    print(f"OK: {len(cases)} cases match between dataset.py and dataset.c")
    return 0


if __name__ == "__main__":
    sys.exit(main())
