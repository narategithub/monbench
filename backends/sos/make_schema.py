#!/usr/bin/env python3
"""make_schema.py DATASET OUT -- write the SOS schema template(s) for a data set.

Converts the backend-neutral data set definition into the template format
that Sos.Schema.from_template() and the C writer read. Each key becomes an
index: a single-attribute key indexes that attribute, a multi-attribute key
becomes an indexed JOIN attribute with the key's name. The schema UUID is
derived from the data set name, so every writer creates an identical schema.

Every dataset-JSON attribute type maps onto a real SOS schema type via
sos_attr() below -- SOS has no 8-bit integer type, so uint8/int8 are
widened to the closest wider SOS integer type it does have (UINT16/
INT16), and no bare "string" type, so a string attribute becomes a
fixed-length SOS CHAR_ARRAY. No attribute is ever dropped from the
template: every dataset-declared attribute, flat or in a list attribute's
entries, gets a same-named SOS attribute one way or another.

If the dataset declares list attributes (design/value-generation.md Sec
6), OUT also gets one schema template per list attribute, named
"<dataset_name>_<name>" (the same naming convention
backends/influxdb/writer/influx_write.py uses for its per-list-attribute
measurements) -- see convert_list() and backends/sos/README.md's "List
attribute data model mapping". OUT is a single JSON document shaping
both:

    {"node": <node schema template>, "lists": {<name>: <template>, ...}}

rather than a bare node template, since there is no backward-compatibility
constraint on this pre-release suite (see design/value-generation.md Sec 3).
"""
import json
import sys
import uuid

# Dataset-JSON type name -> SOS schema type name. uint8/int8 have no SOS
# type of their own, so they widen to the closest wider SOS integer type
# SOS does have (UINT16/INT16) -- the value itself still only ever takes
# an 8-bit-range value, so nothing is lost, it just costs a little extra
# storage. "string" has no bare SOS scalar type either; it becomes a
# fixed-length CHAR_ARRAY (see sos_attr()'s "size" field below).
_SOS_TYPE = {
    "timestamp": "TIMESTAMP",
    "uint8": "UINT16", "uint16": "UINT16", "uint32": "UINT32", "uint64": "UINT64",
    "int8": "INT16", "int16": "INT16", "int32": "INT32", "int64": "INT64",
    "double": "DOUBLE",
    "string": "CHAR_ARRAY",
}
# Mirrors valuegen.py's DEFAULT_STRING_LENGTH -- the longest string
# valuegen ever decodes when an attribute's own "length" is unset.
DEFAULT_STRING_LENGTH = 8


def sos_attr(a):
    """One dataset-JSON attribute ({"name", "type", ...}) -> its SOS
    schema template entry ({"name", "type", ["size"]}). Shared by convert()
    (flat/node attributes) and convert_list() (a list attribute's own
    entry attributes) -- the mapping is identical either way."""
    entry = {"name": a["name"], "type": _SOS_TYPE[a["type"]]}
    if a["type"] == "string":
        # The template's "size" field is schema_from_template_obj()'s cue
        # (sos_write.c) / Schema.from_template()'s (sos_write.py/
        # dsos_write.py, via the sosdb bindings) to create a fixed-length
        # CHAR_ARRAY of exactly this many characters -- matching the
        # longest value valuegen.py/.c will ever decode into it, so a
        # generated string is never truncated.
        entry["size"] = a.get("length", DEFAULT_STRING_LENGTH)
    return entry


def convert(ds):
    flat = [a for a in ds["attributes"] if a.get("type") != "list"]
    attrs = [sos_attr(a) for a in flat]
    by_name = {a["name"]: a for a in attrs}
    for key in ds.get("keys", []):
        if len(key["attrs"]) == 1:
            by_name[key["attrs"][0]]["index"] = {}
        else:
            attrs.append({"name": key["name"], "type": "join",
                          "join_attrs": key["attrs"], "index": {}})
    return {"name": ds["name"],
            "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"monbench:{ds['name']}")),
            "attrs": attrs}


def convert_list(ds, list_attr):
    """Schema template for one list attribute (design doc Sec 6.5's SOS
    mapping): one schema per list attribute, columns = the list's own
    entry attributes (a record entry's tuple fields, or a single "value"
    column for a bare-scalar entry) plus component_id/job_id/seq/
    entry_index, joined back to the node schema's rows on (component_id,
    timestamp) via the 'comp_time' join attribute -- the same join key
    name and shape the node schema already uses for its own comp_time
    key, so a native query can use it the same way. 'timestamp' is
    carried on every instance (the node sample's own timestamp) so a
    list's rows can be ordered and joined without reading the node
    schema at all.
    """
    name = f"{ds['name']}_{list_attr['name']}"
    attrs = [
        {"name": "timestamp", "type": "TIMESTAMP", "index": {}},
        {"name": "component_id", "type": "UINT64", "index": {}},
        {"name": "job_id", "type": "UINT64"},
        {"name": "seq", "type": "UINT64"},
        {"name": "entry_index", "type": "UINT64"},
    ]
    entries = list_attr["entries"]
    if entries.get("type") == "record":
        attrs += [sos_attr(a) for a in entries.get("tuples", [])]
    else:
        # Bare-scalar entries have no field name of their own (the
        # metric_name sentinel is "" -- see valuegen.py's evaluate());
        # the schema column is just named "value".
        attrs.append(sos_attr({**entries, "name": "value"}))
    attrs.append({"name": "comp_time", "type": "join",
                  "join_attrs": ["component_id", "timestamp"], "index": {}})
    return {"name": name,
            "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"monbench:{name}")),
            "attrs": attrs}


def build_templates(ds):
    list_attrs = [a for a in ds.get("attributes", []) if a.get("type") == "list"]
    return {"node": convert(ds),
            "lists": {la["name"]: convert_list(ds, la) for la in list_attrs}}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    with open(sys.argv[1]) as f:
        tmpl = build_templates(json.load(f))
    with open(sys.argv[2], "w") as f:
        json.dump(tmpl, f, indent=2)
