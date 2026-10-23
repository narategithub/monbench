#!/usr/bin/env python3
"""valuegen_cross_check.py -- verify valuegen.py and valuegen.c agree.

Generates a battery of test cases spanning every type and decode path in
design/value-generation.md Sec 4 and Sec 6.3 (uintN/intN at default and
overridden ranges, string at default and overridden length/alphabet,
double at default and overridden base/scale/offset, and list_length at
fixed and variable lengths), computes the expected value with
valuegen.py, builds and runs valuegen_cross_check.c (linked against
valuegen.c) over the same cases, and diffs the two.

This is the test referenced by design/value-generation.md's note that any
change to the hash, key construction, or decode rules must be mirrored in
both implementations. Run it after touching valuegen.py or valuegen.c:

    python3 core/valuegen_cross_check.py

Exits 0 and prints "OK" if every case matches; otherwise prints each
mismatch and exits 1.
"""
import os
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import valuegen as vg  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def double_bits_hex(d):
    return struct.pack(">d", d).hex()


# ----------------------------------------------------------------- test cases
# Each case is (kind, fields_for_c, expected_computed_with_valuegen_py).
# "fields_for_c" are the exact strings written to the vectors file, '|'-
# delimited; see valuegen_cross_check.c's header comment for the format.

def build_cases():
    cases = []

    # A spread of coordinates, reused across type cases, to also exercise
    # key construction (Sec 4.1) -- not just decoding.
    coords = [
        dict(dataset="monitoring", metric="free", list_name="", node_id=1001,
             entry_index=0, job_id=1, seq=0, stride=1),
        dict(dataset="monitoring", metric="free", list_name="", node_id=1001,
             entry_index=0, job_id=1, seq=12345, stride=1),
        dict(dataset="monitoring", metric="cpu_name", list_name="cpu",
             node_id=2007, entry_index=1, job_id=3, seq=999, stride=1),
        dict(dataset="monitoring", metric="idle", list_name="cpu",
             node_id=2007, entry_index=1, job_id=3, seq=999, stride=1),
        dict(dataset="monitoring", metric="rx_bytes", list_name="netdev",
             node_id=4000, entry_index=0, job_id=7, seq=500000, stride=1),
        # A "sticky" attribute: stride > 1 buckets seq into coarser chunks.
        dict(dataset="monitoring", metric="iface_name", list_name="netdev",
             node_id=4000, entry_index=1, job_id=7, seq=777, stride=100),
    ]

    def c_fields(c, *extra):
        return "|".join([str(c["dataset"]), str(c["metric"]), str(c["list_name"]),
                          str(c["node_id"]), str(c["entry_index"]), str(c["job_id"]),
                          str(c["seq"]), str(c["stride"])] + [str(e) for e in extra])

    # uintN / intN, default range and an overridden range, for every width.
    for c in coords:
        for utype in ("uint8", "uint16", "uint32", "uint64"):
            lo, hi = vg.default_range(utype)
            attr = {"name": c["metric"], "type": utype, "stride": c["stride"]}
            expected = vg.evaluate(attr, c["dataset"], c["node_id"], c["list_name"],
                                    c["entry_index"], c["job_id"], c["seq"])
            cases.append(("UINT", c_fields(c, lo, hi), str(expected)))
        for itype in ("int8", "int16", "int32", "int64"):
            lo, hi = vg.default_range(itype)
            attr = {"name": c["metric"], "type": itype, "stride": c["stride"]}
            expected = vg.evaluate(attr, c["dataset"], c["node_id"], c["list_name"],
                                    c["entry_index"], c["job_id"], c["seq"])
            cases.append(("INT", c_fields(c, lo, hi), str(expected)))
        # overridden ranges
        attr_u = {"name": c["metric"], "type": "uint32", "range": [10, 19], "stride": c["stride"]}
        expected_u = vg.evaluate(attr_u, c["dataset"], c["node_id"], c["list_name"],
                                  c["entry_index"], c["job_id"], c["seq"])
        cases.append(("UINT", c_fields(c, 10, 19), str(expected_u)))
        attr_i = {"name": c["metric"], "type": "int16", "range": [-50, 49], "stride": c["stride"]}
        expected_i = vg.evaluate(attr_i, c["dataset"], c["node_id"], c["list_name"],
                                  c["entry_index"], c["job_id"], c["seq"])
        cases.append(("INT", c_fields(c, -50, 49), str(expected_i)))

    # string: default (length 8, a-z0-9) and overridden length/alphabet.
    for c in coords:
        attr = {"name": c["metric"], "type": "string", "stride": c["stride"]}
        expected = vg.evaluate(attr, c["dataset"], c["node_id"], c["list_name"],
                                c["entry_index"], c["job_id"], c["seq"])
        cases.append(("STR", c_fields(c, vg.DEFAULT_STRING_LENGTH,
                                       vg.DEFAULT_STRING_ALPHABET), expected))
        attr2 = {"name": c["metric"], "type": "string", "length": 16,
                 "alphabet": "ABCDEFGHIJKLMNOP", "stride": c["stride"]}
        expected2 = vg.evaluate(attr2, c["dataset"], c["node_id"], c["list_name"],
                                 c["entry_index"], c["job_id"], c["seq"])
        cases.append(("STR", c_fields(c, 16, "ABCDEFGHIJKLMNOP"), expected2))

    # double: default (base uint64, range [0,9999], scale 100, offset 0)
    # and overridden base/scale/offset.
    for c in coords:
        attr = {"name": c["metric"], "type": "double", "stride": c["stride"]}
        expected = vg.evaluate(attr, c["dataset"], c["node_id"], c["list_name"],
                                c["entry_index"], c["job_id"], c["seq"])
        lo, hi = vg.DEFAULT_DOUBLE_RANGE
        cases.append(("DBL", c_fields(c, lo, hi, vg.DEFAULT_DOUBLE_SCALE,
                                       vg.DEFAULT_DOUBLE_OFFSET),
                      double_bits_hex(expected)))
        attr2 = {"name": c["metric"], "type": "double", "base": "uint32",
                  "range": [0, 199], "scale": 2.0, "offset": -10.0, "stride": c["stride"]}
        expected2 = vg.evaluate(attr2, c["dataset"], c["node_id"], c["list_name"],
                                 c["entry_index"], c["job_id"], c["seq"])
        cases.append(("DBL", c_fields(c, 0, 199, 2.0, -10.0),
                      double_bits_hex(expected2)))

    # list_length: fixed (lo==hi) and variable, a few lists/strides.
    length_cases = [
        dict(dataset="monitoring", list_name="cpu", node_id=1001, job_id=1, seq=0,
             stride=vg.DEFAULT_LENGTH_STRIDE, lo=2, hi=2),
        dict(dataset="monitoring", list_name="netdev", node_id=1001, job_id=1, seq=0,
             stride=vg.DEFAULT_LENGTH_STRIDE, lo=1, hi=2),
        dict(dataset="monitoring", list_name="netdev", node_id=4000, job_id=7, seq=500000,
             stride=1000, lo=1, hi=4),
        dict(dataset="monitoring", list_name="gpu", node_id=9, job_id=2, seq=42,
             stride=50, lo=0, hi=7),
    ]
    for lc in length_cases:
        list_attr = {"name": lc["list_name"], "min_list_length": lc["lo"],
                     "max_list_length": lc["hi"], "length_stride": lc["stride"]}
        expected = vg.list_length(lc["dataset"], list_attr, lc["node_id"],
                                   lc["job_id"], lc["seq"])
        fields = "|".join(str(x) for x in (lc["dataset"], lc["list_name"], lc["node_id"],
                                            lc["job_id"], lc["seq"], lc["stride"],
                                            lc["lo"], lc["hi"]))
        cases.append(("CNT", fields, str(expected)))

    # default-range / type-check helpers (vg_default_range_uint/int,
    # vg_is_uint_type/int_type): every known int type plus a few type
    # strings that are neither, to exercise the "no match" path too.
    for t in list(vg.UNSIGNED_TYPES) + list(vg.SIGNED_TYPES) + \
            ["double", "string", "timestamp", "bogus"]:
        is_u = 1 if t in vg.UNSIGNED_TYPES else 0
        is_i = 1 if t in vg.SIGNED_TYPES else 0
        if is_u or is_i:
            lo, hi = vg.default_range(t)
        else:
            lo, hi = "NA", "NA"
        cases.append(("DFLT", t, f"{is_u}|{is_i}|{lo}|{hi}"))

    return cases


def main():
    cases = build_cases()

    with tempfile.TemporaryDirectory() as tmp:
        vectors_path = os.path.join(tmp, "vectors.txt")
        with open(vectors_path, "w") as f:
            for kind, fields, _expected in cases:
                f.write(f"{kind}|{fields}\n")

        binary_path = os.path.join(tmp, "valuegen_cross_check")
        compile_cmd = ["cc", "-O2", "-Wall", "-I", HERE, "-o", binary_path,
                       os.path.join(HERE, "valuegen_cross_check.c"),
                       os.path.join(HERE, "valuegen.c")]
        r = subprocess.run(compile_cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print("valuegen_cross_check: C compile failed:", file=sys.stderr)
            print(r.stderr, file=sys.stderr)
            return 2

        r = subprocess.run([binary_path, vectors_path], capture_output=True, text=True)
        if r.returncode != 0:
            print("valuegen_cross_check: C run failed:", file=sys.stderr)
            print(r.stderr, file=sys.stderr)
            return 2
        c_lines = r.stdout.split("\n")
        if c_lines and c_lines[-1] == "":
            c_lines.pop()

    if len(c_lines) != len(cases):
        print(f"valuegen_cross_check: expected {len(cases)} result lines from C, "
              f"got {len(c_lines)}", file=sys.stderr)
        return 2

    failures = 0
    for i, ((kind, fields, expected), got) in enumerate(zip(cases, c_lines)):
        if str(expected) != got:
            failures += 1
            print(f"MISMATCH case {i} [{kind}] fields={fields!r}: "
                  f"python={expected!r} c={got!r}")

    if failures:
        print(f"\n{failures} of {len(cases)} cases mismatched")
        return 1
    print(f"OK: {len(cases)} cases match between valuegen.py and valuegen.c")
    return 0


if __name__ == "__main__":
    sys.exit(main())
