#!/usr/bin/env python3
"""valuegen.py -- generic, hash-based value generation and verification.

Implements the design in design/value-generation.md: every attribute's
value is a pure, deterministic function of its identifying coordinates
(dataset name, metric name, node id, list name/entry index, job id, seq),
computed with a fast non-cryptographic hash (FNV-1a, 64-bit). There is no
per-attribute hand-tuned formula and no AST/expression language -- a
dataset is defined as pure JSON data (see dataset/default.json and
design/value-generation.md Sec 6.1), and this module is the one evaluator
every writer and the verifier both call.

This module has a bit-identical C port, valuegen.c / valuegen.h, for
writers that cannot import Python (see backends/sos/writer/sos_write.c).
Any change to the hash, key construction, or decode rules here MUST be
mirrored there -- valuegen_cross_check.py exercises both implementations
over the same inputs and fails loudly on any divergence.

Public API:
    fnv1a64(data)                                          -> int
    le64(x)                                                 -> bytes
    digest(key, i)                                          -> int
    key_bytes(dataset_name, metric_name, list_name,
              node_id, entry_index, job_id, seq, stride)    -> bytes
    default_range(attr_type)                                -> (lo, hi)
    decode(attr, key)                                        -> value
    evaluate(attr, dataset_name, node_id, list_name,
             entry_index, job_id, seq, metric_name=None)    -> value
    list_length(dataset_name, list_attr, node_id,
                job_id, seq)                                 -> int
    generate_node_sample(dataset_def, node_id, job_id, seq)  -> dict
"""
from __future__ import annotations

MASK64 = (1 << 64) - 1
FNV_OFFSET = 0xcbf29ce484222325
FNV_PRIME = 0x100000001b3

INT_TYPES = ("uint8", "uint16", "uint32", "uint64",
             "int8", "int16", "int32", "int64")
UNSIGNED_TYPES = ("uint8", "uint16", "uint32", "uint64")
SIGNED_TYPES = ("int8", "int16", "int32", "int64")

DEFAULT_STRING_LENGTH = 8
DEFAULT_STRING_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
DEFAULT_DOUBLE_BASE = "uint64"
DEFAULT_DOUBLE_RANGE = (0, 9999)
DEFAULT_DOUBLE_SCALE = 100.0
DEFAULT_DOUBLE_OFFSET = 0.0

# A very large length_stride effectively fixes a list attribute's length
# for any realistic run length, which is the default when a dataset's
# JSON omits length_stride (design doc Sec 7, item 2).
DEFAULT_LENGTH_STRIDE = 10 ** 15

_RANGE_BY_TYPE = {
    "uint8":  (0, 255),
    "uint16": (0, 65535),
    "uint32": (0, 2 ** 32 - 1),
    "uint64": (0, 2 ** 64 - 1),
    "int8":   (-128, 127),
    "int16":  (-32768, 32767),
    "int32":  (-2 ** 31, 2 ** 31 - 1),
    "int64":  (-2 ** 63, 2 ** 63 - 1),
}


# ----------------------------------------------------------------- hashing

def fnv1a64(data: bytes) -> int:
    """FNV-1a, 64-bit. Fast, non-cryptographic; see design doc Sec 4.2."""
    h = FNV_OFFSET
    for byte in data:
        h ^= byte
        h = (h * FNV_PRIME) & MASK64
    return h


def le64(x: int) -> bytes:
    """8-byte little-endian encoding of a 64-bit unsigned integer."""
    return (x & MASK64).to_bytes(8, "little")


def digest(key: bytes, i: int) -> int:
    """digest(K, i) = fnv1a64(K || le64(i)) -- chained block i of K's stream."""
    return fnv1a64(key + le64(i))


# ----------------------------------------------------------------- key construction

def key_bytes(dataset_name, metric_name, list_name,
              node_id, entry_index, job_id, seq, stride=1):
    """Build K (design doc Sec 4.1).

    list_name="" and entry_index=0 are the sentinel values for a
    node-level (non-list) attribute. metric_name="" is the sentinel for a
    list whose entries are a bare scalar (there is no sub-field name in
    that case). stride=1 means the value changes every sample; a larger
    stride buckets seq into coarser chunks ("sticky" attributes).
    """
    bucket = seq // stride if stride else seq
    return (dataset_name.encode("utf-8") + b"\x00" +
            metric_name.encode("utf-8") + b"\x00" +
            list_name.encode("utf-8") + b"\x00" +
            le64(node_id) + le64(entry_index) +
            le64(job_id) + le64(bucket))


def _length_key_bytes(dataset_name, list_name, node_id, job_id, seq, length_stride):
    """Key for a list attribute's per-sample length (design doc Sec 6.3).

    Uses the reserved metric name "__length__" so a list's length digest
    cannot collide with any real attribute's digest (metric names are
    admin-chosen but "__length__" is reserved).
    """
    bucket = seq // length_stride if length_stride else seq
    return (dataset_name.encode("utf-8") + b"\x00" +
            b"__length__" + b"\x00" +
            list_name.encode("utf-8") + b"\x00" +
            le64(node_id) + le64(job_id) + le64(bucket))


# ----------------------------------------------------------------- decoding

def default_range(attr_type):
    """Default [lo, hi] (inclusive) for an integer type (design doc Sec 4.3)."""
    try:
        return _RANGE_BY_TYPE[attr_type]
    except KeyError:
        raise ValueError(f"valuegen: no default range for type {attr_type!r}")


def _decode_int_range(d0, lo, hi):
    span = hi - lo + 1
    return lo + (d0 % span)


def _decode_string(key, length, alphabet):
    chars = []
    i = 0
    while len(chars) < length:
        block = le64(digest(key, i))
        for byte in block:
            if len(chars) == length:
                break
            chars.append(alphabet[byte % len(alphabet)])
        i += 1
    return "".join(chars)


def decode(attr, key):
    """Decode a digest of *key* into the typed value *attr* declares.

    *attr* is a dataset-attribute dict, e.g. {"name": "free", "type": "double"}
    (or, for a bare-scalar list entry, a dict with no "name" -- just
    "type" and any overrides). Optional per-attribute overrides:
    "range": [lo, hi] for int types; "length"/"alphabet" for string;
    "base"/"range"/"scale"/"offset" for double.
    """
    t = attr["type"]
    if t in INT_TYPES:
        d0 = digest(key, 0)
        lo, hi = attr.get("range", default_range(t))
        return _decode_int_range(d0, lo, hi)
    if t == "string":
        length = attr.get("length", DEFAULT_STRING_LENGTH)
        alphabet = attr.get("alphabet", DEFAULT_STRING_ALPHABET)
        return _decode_string(key, length, alphabet)
    if t == "double":
        base = attr.get("base", DEFAULT_DOUBLE_BASE)
        if "range" in attr:
            lo, hi = attr["range"]
        elif "base" not in attr:
            # Fully unconfigured double: Sec 4.3's table entry, not
            # default_range(base) (uint64's own range is far too wide to
            # be a useful double).
            lo, hi = DEFAULT_DOUBLE_RANGE
        else:
            # Admin chose a base type but no explicit range: that type's
            # own full range, per the Sec 4.3 pseudocode.
            lo, hi = default_range(base)
        d0 = digest(key, 0)
        base_value = _decode_int_range(d0, lo, hi)
        scale = attr.get("scale", DEFAULT_DOUBLE_SCALE)
        offset = attr.get("offset", DEFAULT_DOUBLE_OFFSET)
        return base_value / scale + offset
    raise ValueError(f"valuegen: unknown attribute type {t!r}")


# ----------------------------------------------------------------- evaluator

def evaluate(attr, dataset_name, node_id, list_name, entry_index,
             job_id, seq, metric_name=None):
    """Every attribute's value is this one call (design doc Sec 4.4).

    *metric_name* defaults to attr["name"] (the ordinary case: a flat
    attribute or a record-tuple field). Pass metric_name="" explicitly
    for a list whose entries are a bare scalar -- *attr* there is just
    the entries' type spec and has no "name" of its own (design doc
    Sec 4.1's metric_name sentinel).
    """
    if metric_name is None:
        metric_name = attr["name"]
    stride = attr.get("stride", 1)
    key = key_bytes(dataset_name, metric_name, list_name,
                     node_id, entry_index, job_id, seq, stride)
    return decode(attr, key)


def list_length(dataset_name, list_attr, node_id, job_id, seq):
    """Deterministic, per-sample length for one list attribute (Sec 6.3).

    *list_attr* is one entry of a dataset definition's "attributes" list
    with "type": "list": {"name": "cpu", "min_list_length": 2,
    "max_list_length": 2, "length_stride": ..., "entries": {...}}. When
    min/max are omitted, falls back to "list_length" (a fixed length,
    default 1).
    """
    lo = list_attr.get("min_list_length")
    hi = list_attr.get("max_list_length")
    if lo is None or hi is None:
        fixed = list_attr.get("list_length", 1)
        lo = hi = fixed
    stride = list_attr.get("length_stride", DEFAULT_LENGTH_STRIDE)
    key = _length_key_bytes(dataset_name, list_attr["name"], node_id, job_id,
                             seq, stride)
    span = hi - lo + 1
    return lo + (fnv1a64(key) % span)


def generate_node_sample(dataset_def, node_id, job_id, seq):
    """One full sample for one node: every declared attribute, flat or list.

    Deterministic and order-independent: the same (node_id, job_id, seq)
    always produces the same result, regardless of write order or which
    writer (Python or C) produced it. See design doc Sec 6.3. Nothing in
    this function names any particular list attribute -- a dataset with
    no lists, one list, or ten runs through the same loop.
    """
    name = dataset_def["name"]
    sample = {}
    for attr in dataset_def.get("attributes", []):
        if attr.get("type") != "list":
            sample[attr["name"]] = evaluate(attr, name, node_id, "", 0,
                                             job_id, seq)
            continue
        list_name = attr["name"]
        n = list_length(name, attr, node_id, job_id, seq)
        entries_spec = attr["entries"]
        items = []
        for i in range(n):
            if entries_spec.get("type") == "record":
                entry = {}
                for field in entries_spec.get("tuples", []):
                    entry[field["name"]] = evaluate(
                        field, name, node_id, list_name, i, job_id, seq)
            else:
                entry = evaluate(entries_spec, name, node_id, list_name, i,
                                  job_id, seq, metric_name="")
            items.append(entry)
        sample[list_name] = items
    return sample
