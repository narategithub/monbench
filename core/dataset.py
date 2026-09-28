#!/usr/bin/env python3
"""dataset.py -- the benchmark data set, independent of any backend.

The data set is split into partitions, one per server node (SERVER_NODES). Partition p holds
components (p + 1) * 1000 + c for c in [0, components_per_partition), each
with one record per sample. Every record is a pure function of
(component_id, seq):

  timestamp = t0 + seq * interval
  job_id    = 1 + seq // job_len
  mem_free  = component_id * 1000000 + seq
  load      = (component_id % 1000) + seq * 0.25

Every writer, in any backend, must produce exactly these values; the query
engine verifies answers against them.

After a write, the harness records what was written in a metadata file,
<RESULTS_DIR>/meta/<target>.json. The query engine reads it so that it
verifies against the data actually written, and so that it can refuse to
query a target written for a different backend.

Command line (used by bin/node.sh):
  dataset.py meta TARGET WRITER MODE    write the metadata file for TARGET
"""
import datetime
import json
import os
import sys

# The attributes the value rules define, in record-tuple order.
ATTRS = ("timestamp", "component_id", "job_id", "seq", "mem_free", "load")
META_FORMAT = 1


class Dataset:
    def __init__(self, definition, partitions, components, samples, t0,
                 interval, job_len):
        names = tuple(a["name"] for a in definition.get("attributes", []))
        if set(names) != set(ATTRS):
            raise ValueError(f"data set definition attributes {names} do not match "
                             f"the value rules in dataset.py {ATTRS}")
        self.definition = definition
        self.partitions = partitions
        self.components_per_partition = components
        self.samples = samples
        self.t0 = t0
        self.interval = interval
        self.job_len = job_len
        self._records = None

    # ------------------------------------------------------------ construction

    @staticmethod
    def load_definition(path):
        with open(path) as f:
            return json.load(f)

    @classmethod
    def from_env(cls, live=False):
        """Parameters from the settings exported by bin/node.sh."""
        e = os.environ
        partitions = len(e["SERVER_NODES"].split())
        return cls(cls.load_definition(e["DATASET"]), partitions,
                   int(e["COMPONENTS_PER_NODE"]),
                   int(e["LIVE_SAMPLES"] if live else e["SAMPLES"]),
                   int(e["T0"]), int(e["INTERVAL"]), int(e["JOB_LEN"]))

    @classmethod
    def from_meta(cls, meta):
        p = meta["params"]
        return cls(meta["dataset"], p["partitions"], p["components_per_partition"],
                   p["samples"], p["t0"], p["interval"], p["job_len"])

    def params(self):
        return {"partitions": self.partitions,
                "components_per_partition": self.components_per_partition,
                "samples": self.samples, "t0": self.t0,
                "interval": self.interval, "job_len": self.job_len}

    # ------------------------------------------------------------ contents

    @property
    def name(self):
        return self.definition["name"]

    def size(self):
        return self.partitions * self.components_per_partition * self.samples

    def components(self, partition=None):
        parts = range(self.partitions) if partition is None else [partition]
        return [(p + 1) * 1000 + c for p in parts
                for c in range(self.components_per_partition)]

    @staticmethod
    def partition_of(component_id):
        return component_id // 1000 - 1

    def expected(self, comp, seq):
        return {"timestamp": float(self.t0 + seq * self.interval),
                "job_id": 1 + seq // self.job_len,
                "mem_free": comp * 1000000 + seq,
                "load": float(comp % 1000) + seq * 0.25}

    def records(self):
        """Every record, as tuples in ATTRS order (built once, then cached)."""
        if self._records is None:
            self._records = []
            for comp in self.components():
                for seq in range(self.samples):
                    v = self.expected(comp, seq)
                    self._records.append((v["timestamp"], comp, v["job_id"], seq,
                                          v["mem_free"], v["load"]))
        return self._records

    def placeholders(self, limit):
        """Values available to query sets as {name}."""
        def ts(seq):
            return self.t0 + seq * self.interval
        s, c, last = self.samples, self.components_per_partition, self.partitions
        mid = s // 2
        return {
            "schema": self.name,
            "limit": limit,
            "t_start": ts(0), "t_end": ts(s - 1),
            "t_q1": ts(s // 4), "t_mid": ts(mid), "t_q3": ts(3 * s // 4),
            "t_mid_1pct": ts(mid + max(1, s // 100)),
            "comp_s0": 1000, "comp_s0_last": 1000 + c - 1,
            "comp_slast": last * 1000, "comp_last": last * 1000 + c - 1,
            "job_first": 1, "job_mid": 1 + mid // self.job_len,
            "job_last": 1 + (s - 1) // self.job_len,
        }

# ---------------------------------------------------------------- metadata


def meta_path(target):
    return os.path.join(os.environ["RESULTS_DIR"], "meta", f"{target}.json")


def write_meta(target, writer, mode):
    ds = Dataset.from_env(live=(mode == "live"))
    meta = {
        "format": META_FORMAT,
        "backend": os.environ["BACKEND"],
        "target": target,
        "writer": writer,
        "write_nodes": os.environ["SERVER_NODES"].split(),
        "dataset": ds.definition,
        "params": ds.params(),
        "records": ds.size(),
        "written_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    path = meta_path(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"metadata: {path}")


def read_meta(target):
    path = meta_path(target)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        meta = json.load(f)
    if meta.get("format") != META_FORMAT:
        raise ValueError(f"{path}: unsupported metadata format {meta.get('format')}")
    return meta


def main(argv):
    if len(argv) == 4 and argv[0] == "meta":
        write_meta(*argv[1:])
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
