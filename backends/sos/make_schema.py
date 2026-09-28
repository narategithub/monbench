#!/usr/bin/env python3
"""make_schema.py DATASET OUT -- write the SOS schema template for a data set.

Converts the backend-neutral data set definition into the template format
that Sos.Schema.from_template() and the C writer read. Each key becomes an
index: a single-attribute key indexes that attribute, a multi-attribute key
becomes an indexed JOIN attribute with the key's name. The schema UUID is
derived from the data set name, so every writer creates an identical schema.
"""
import json
import sys
import uuid


def convert(ds):
    attrs = [{"name": a["name"], "type": a["type"]} for a in ds["attributes"]]
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


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    with open(sys.argv[1]) as f:
        tmpl = convert(json.load(f))
    with open(sys.argv[2], "w") as f:
        json.dump(tmpl, f, indent=2)
