#!/usr/bin/env python3
"""bench.py -- benchmark and verify queries against one backend.

Runs on the query node (normally through 'bin/node.sh check' or
'bin/node.sh query'). Settings come from the environment exported by
node.sh from bench.conf and the backend's <name>_backend.conf.

The engine is backend-neutral. It loads the query adapter from the backend
plugin (backends/<BACKEND>/query/<BACKEND>_query.py), connects once, then for each query in
the query set runs one or both of the following steps:

  check  Run the query once, untimed, and verify the full result against the
         data set (core/dataset.py).
  query  Run the query REPEAT times, timing each run from the start of the
         query until the last row has been fetched.

The data set parameters come from the target's metadata file, written when
the target was written. If it names a different backend, the run stops.

'--live' polls a target that is still being written and checks that every
answer is internally consistent and that the row count never decreases.
"""
import argparse
import importlib.util
import json
import os
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapter import BackendError                      # noqa: E402
from dataset import ATTRS, Dataset, read_meta         # noqa: E402

# ----------------------------------------------------------------- settings


def env(name, cast=str, default=None):
    value = os.environ.get(name, default)
    if value is None:
        sys.exit(f"bench.py: {name} is not set; run through bin/node.sh")
    return cast(value)


BENCH_DIR = env("BENCH_DIR")
BACKEND = env("BACKEND")               # the query backend (set by run.sh)
QUERY_BACKEND = os.environ.get("QUERY_BACKEND", BACKEND)
WRITE_BACKEND = os.environ.get("WRITE_BACKEND", BACKEND)
# Backends whose data this query backend can read (from its <name>_backend.conf).
PLUGIN_READS = os.environ.get("PLUGIN_READS", "").split()
SERVERS = env("SERVER_NODES").split()
RUN_DIR = env("RUN_DIR")
RESULTS_DIR = env("RESULTS_DIR")
LIMIT_N = env("LIMIT_N", int)
LIVE_POLL = env("LIVE_POLL", float)
REPEAT = env("REPEAT", int, "5")
QUERY_ADAPTER = os.environ.get("QUERY_ADAPTER", "")
QUERY_FILE = os.environ.get("QUERY_FILE") or os.path.join(BENCH_DIR, "queries", "default.json")

DS = None          # the Dataset being checked; set in main()


def load_plugin():
    """Load backends/<BACKEND>/query/<BACKEND>_query.py and return (module, adapter class)."""
    path = os.path.join(BENCH_DIR, "backends", BACKEND, "query", f"{BACKEND}_query.py")
    if not os.path.exists(path):
        sys.exit(f"bench.py: backend '{BACKEND}' has no query adapter ({path})")
    spec = importlib.util.spec_from_file_location(f"backend_{BACKEND}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    name = QUERY_ADAPTER or module.DEFAULT_ADAPTER
    if name not in module.ADAPTERS:
        sys.exit(f"bench.py: backend '{BACKEND}' has no adapter '{name}'; "
                 f"available: {', '.join(sorted(module.ADAPTERS))}")
    return module.ADAPTERS[name]


def make_adapter(cls, target):
    return cls({"target": target, "schema": DS.name, "servers": SERVERS,
                "run_dir": RUN_DIR, "env": os.environ})

# ----------------------------------------------------------------- row checks


def parse_ts(value):
    """Return a timestamp in seconds as a float, or None if not recognised.

    Accepts numbers, 'sec.usec' or '(sec, usec)' text, and datetime-like
    objects such as pandas Timestamp or numpy datetime64 (taken as UTC).
    """
    if hasattr(value, "timestamp"):                      # pandas / datetime
        return value.timestamp()
    if type(value).__name__ == "datetime64":             # numpy
        return value.astype("datetime64[us]").astype("int64") / 1e6
    try:
        return float(value)
    except (TypeError, ValueError):
        m = re.match(r"\(?\s*(\d+)\s*[,.]\s*(\d+)\s*\)?$", str(value).strip())
        if m:
            return int(m.group(1)) + int(m.group(2)) / 1e6
    return None


def validate_rows(rows):
    """Check each row against the data set.

    Only the columns present are checked, so queries that select a subset of
    columns can be verified. Returns (problems, keys, warnings); keys lists
    (component_id, seq) for each row.
    """
    problems, keys = [], []
    ts_unparsed = 0
    for n, r in enumerate(rows):
        try:
            comp, seq = int(r["component_id"]), int(r["seq"])
        except (KeyError, ValueError, TypeError):
            problems.append(f"row {n}: missing component_id/seq: {r}")
            continue
        keys.append((comp, seq))
        exp = DS.expected(comp, seq)
        try:
            if "job_id" in r and int(r["job_id"]) != exp["job_id"]:
                problems.append(f"row {n}: job_id {r['job_id']} != {exp['job_id']}")
            if "mem_free" in r and int(r["mem_free"]) != exp["mem_free"]:
                problems.append(f"row {n}: mem_free {r['mem_free']} != {exp['mem_free']}")
            if "load" in r and abs(float(r["load"]) - exp["load"]) > 1e-6:
                problems.append(f"row {n}: load {r['load']} != {exp['load']}")
        except (ValueError, TypeError) as e:
            problems.append(f"row {n}: cannot parse value ({e}): {r}")
        if "timestamp" in r:
            ts = parse_ts(r["timestamp"])
            if ts is None:
                ts_unparsed += 1
            elif abs(ts - exp["timestamp"]) > 1e-3:
                problems.append(f"row {n}: timestamp {r['timestamp']} != {exp['timestamp']}")
    warnings = []
    if ts_unparsed:
        warnings.append(f"{ts_unparsed} timestamps in an unrecognised format (not validated)")
    return problems, keys, warnings


def is_sorted(values):
    return all(a <= b for a, b in zip(values, values[1:]))

# ----------------------------------------------------------------- query sets
# A query set is a JSON file. Each query has a backend-neutral description of
# the question ("match"), used for verification, and one query string per
# backend or dialect ("query"). Strings may use {placeholders}. See
# queries/default.json and README.md.

OPS = {"==": lambda a, b: a == b, "!=": lambda a, b: a != b,
       "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
       ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}
COND_RE = re.compile(r"^\(?\s*(\w+)\s*(==|!=|<=|>=|<|>)\s*(-?[\d.]+)\s*\)?$")


def fill(value, values, where):
    """Apply placeholders to a string (or to the strings in a dict or list)."""
    if isinstance(value, str):
        try:
            return value.format(**values)
        except KeyError as e:
            sys.exit(f"bench.py: {where} uses unknown placeholder {e}; "
                     f"available: {', '.join(sorted(values))}")
    if isinstance(value, dict):
        return {k: fill(v, values, where) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, values, where) for v in value]
    return value


def parse_match(match):
    """Turn a 'match' description into conditions the verifier can apply.

    Returns (spec, reason); reason is set when the description cannot be used.
    """
    spec = {"conds": [], "order_by": match.get("order_by") or [],
            "limit": int(match["limit"]) if match.get("limit") not in (None, "") else None}
    where = (match.get("where") or "").strip()
    if where:
        for term in re.split(r"\s+and\s+", where, flags=re.IGNORECASE):
            c = COND_RE.match(term.strip())
            if not c or c.group(1) not in ATTRS:
                return None, f"match condition '{term.strip()}' not understood"
            spec["conds"].append((ATTRS.index(c.group(1)), OPS[c.group(2)],
                                  float(c.group(3))))
    unknown = [a for a in spec["order_by"] if a not in ATTRS]
    if unknown:
        return None, f"match order_by names unknown attributes {unknown}"
    return spec, None


def load_query_set(path, adapter):
    with open(path) as f:
        doc = json.load(f)
    queries = doc.get("queries")
    if not isinstance(queries, list) or not queries:
        sys.exit(f"bench.py: {path} has no 'queries' list")
    values = DS.placeholders(LIMIT_N)
    out = []
    for n, q in enumerate(queries):
        where = f"query {n + 1} ('{q.get('name', '')}') in {path}"
        text = q.get("query")
        if isinstance(text, dict):                       # per backend or dialect
            text = text.get(adapter.name, text.get(adapter.dialect))
        if text is None and "query" not in q:
            sys.exit(f"bench.py: {where} has no 'query'")
        match = q.get("match")
        spec, reason = (parse_match(fill(match, values, where)) if match
                        else (None, "no 'match' description"))
        if not q.get("verify", True):
            spec, reason = None, "verification off for this query"
        out.append({"name": q.get("name", f"query {n + 1}"),
                    "type": q.get("type", "-"),
                    "query": fill(text, values, where) if text else None,
                    "spec": spec, "unverified_reason": reason})
    return out

# ----------------------------------------------------------------- verification


def verify(spec, columns, rows):
    """Compare a full result with the records the question should return."""
    expected = [r for r in DS.records() if all(op(r[i], v) for i, op, v in spec["conds"])]
    have_key = "component_id" in columns and "seq" in columns
    problems, notes = [], []
    got = want = None
    if have_key:
        vp, keys, warn = validate_rows(rows)
        problems += vp[:3]
        notes += warn
        want = {(r[1], r[3]) for r in expected}
        got = set(keys)
        if len(keys) != len(got):
            problems.append(f"{len(keys) - len(got)} duplicate rows")
        if got - want:
            problems.append(f"{len(got - want)} rows that do not match the question")
    else:
        notes.append("rows not identified (component_id and seq not selected); "
                     "count checked only")
    n = len(rows)
    if spec["limit"] is None:
        if n != len(expected):
            problems.append(f"{n} rows, expected {len(expected)}")
        elif have_key and got != want:
            problems.append("returned rows differ from the expected rows")
    else:
        # How a limit is applied differs between backends: to the whole
        # result, or on each server to its own partition. Both are accepted.
        per_server = sum(min(spec["limit"],
                             sum(1 for r in expected if DS.partition_of(r[1]) == p))
                         for p in range(DS.partitions))
        overall = min(spec["limit"], len(expected))
        if n == overall:
            notes.append("limit applied to the whole result")
        elif n == per_server:
            notes.append("limit applied per server")
        else:
            problems.append(f"{n} rows; expected {overall} (whole result) "
                            f"or {per_server} (per server)")
    order = spec["order_by"]
    if order and all(a in columns for a in order):
        try:
            keyvals = [tuple(parse_ts(r[a]) if a == "timestamp" else float(r[a])
                             for a in order) for r in rows]
            if not is_sorted(keyvals):
                problems.append(f"rows not ordered by {', '.join(order)}")
        except (TypeError, ValueError):
            notes.append("order not checked (unparsed values)")
    elif order:
        notes.append("order not checked (ordering columns not selected)")
    return ("FAIL" if problems else "PASS"), "; ".join(problems[:4] or notes)

# ----------------------------------------------------------------- engine


class Tee:
    """Write every print() to stdout and to a log file at the same time."""
    def __init__(self, path):
        self._file = open(path, "w", buffering=1)
        self._stdout = sys.stdout
        sys.stdout = self

    def write(self, text):
        self._stdout.write(text)
        self._file.write(text)

    def flush(self):
        self._stdout.flush()
        self._file.flush()

    def close(self):
        sys.stdout = self._stdout
        self._file.close()


class Report:
    def __init__(self, target, adapter):
        self.target = target
        self.adapter = adapter
        self.items = []
        self.width = 40

    def add(self, label, status, detail, **fields):
        item = {"check": label, "status": status, "detail": detail}
        item.update(fields)
        self.items.append(item)
        timing = ""
        if "median_s" in fields:
            timing = f"{fields['median_s'] * 1e3:10.1f} {fields['p95_s'] * 1e3:10.1f}"
        rows = fields.get("rows", "")
        print(f"  {status:5} {label:{self.width}} {rows:>8} {timing:21}  {detail}")

    def failed(self):
        return any(i["status"] == "FAIL" for i in self.items)


def collect(adapter, text):
    """Run a query and return (columns, rows) for the whole result."""
    columns, rows = [], []
    for batch in adapter.query(text):
        cols, recs = adapter.records(batch)
        columns = columns or cols
        rows.extend(recs)
    return columns, rows


def timed_run(adapter, text):
    """Time one run: from the start of the query until the last row is fetched."""
    begin = time.perf_counter()
    n = 0
    for batch in adapter.query(text):
        n += adapter.count(batch)
    return time.perf_counter() - begin, n


def percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


def run_query_set(rep, adapter, queries):
    rep.width = max(len(f"[{q['type']}] {q['name']}") for q in queries)
    print(f"  {'':5} {'query':{rep.width}} {'rows':>8} {'median ms':>10} {'p95 ms':>10}  detail")
    for q in queries:
        label = f"[{q['type']}] {q['name']}"
        if not q["query"]:
            rep.add(label, "SKIP", f"no query string for adapter '{adapter.name}'")
            continue
        # 1. Untimed run: fetch and verify the full result.
        try:
            columns, rows = collect(adapter, q["query"])
        except Exception as e:
            rep.add(label, "FAIL", f"{type(e).__name__}: {e}", query=q["query"])
            continue
        if q["spec"]:
            status, detail = verify(q["spec"], columns, rows)
        else:
            status, detail = "INFO", q["unverified_reason"]
            if q["type"] == "aggregate" and columns and rows:
                groups = {}
                for r in rows:
                    groups[r.get(columns[0])] = groups.get(r.get(columns[0]), 0) + 1
                detail += f"; {len(groups)} groups, rows per group {sorted(set(groups.values()))}"
        # 2. Timed runs.
        times = []
        try:
            for _ in range(REPEAT):
                t, n = timed_run(adapter, q["query"])
                times.append(t)
                if n != len(rows):
                    status, detail = "FAIL", f"row count changed between runs ({len(rows)} then {n})"
        except Exception as e:
            status, detail = "FAIL", f"timed run failed: {type(e).__name__}: {e}"
        fields = {"query": q["query"], "rows": len(rows), "times_s": [round(t, 6) for t in times]}
        if times:
            med = statistics.median(times)
            fields.update(median_s=med, p95_s=percentile(times, 95), min_s=min(times),
                          rows_per_s=round(len(rows) / med) if med > 0 else None)
        rep.add(label, status, detail, **fields)


def load_dataset(target, live):
    """Data set to verify against: from the target's metadata if present."""
    global DS
    meta = None if live else read_meta(target)
    if meta:
        wrote = meta["backend"]
        if PLUGIN_READS and wrote not in PLUGIN_READS:
            sys.exit(f"bench.py: target '{target}' was written by backend "
                     f"'{wrote}', which query backend '{QUERY_BACKEND}' cannot "
                     f"read (it reads: {', '.join(PLUGIN_READS)})")
        DS = Dataset.from_meta(meta)
        return f"written by {wrote} ({meta['writer']} writer) at {meta['written_at']}"
    DS = Dataset.from_env(live=live)
    return "no metadata file; using the current settings" if not live else "live settings"


def load_ingest_results(target):
    """Read the write result files for TARGET and return structured data."""
    import glob
    pattern = os.path.join(RESULTS_DIR, f"write-{target}-*.json")
    files = sorted(glob.glob(pattern))
    nodes = []
    for f in files:
        host = os.path.basename(f).replace(f"write-{target}-", "").replace(".json", "")
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            nodes.append({"host": host, "error": f"could not read {f}"})
            continue
        nodes.append({
            "host": host,
            "backend": WRITE_BACKEND,
            "writer": d.get("writer", "-"),
            "storage": d.get("backend", "-"),
            "records": d.get("records"),
            "write_s": d.get("write_s"),
            "sync_s": d.get("close_sync_s"),
            "records_per_s": d.get("records_per_s"),
        })
    ok = [n for n in nodes if "error" not in n and isinstance(n.get("records"), (int, float))]
    summary = {}
    if ok:
        summary = {
            "total_records": sum(n["records"] for n in ok),
            "max_write_s": max(n["write_s"] for n in ok if n.get("write_s")),
            "max_sync_s": max((n["sync_s"] for n in ok if n.get("sync_s")), default=0),
        }
        elapsed = summary["max_write_s"] + summary["max_sync_s"]
        if elapsed > 0:
            summary["aggregate_records_per_s"] = round(summary["total_records"] / elapsed)
    return {"nodes": nodes, "summary": summary}


def print_ingest_summary(ingest):
    """Print the ingest results as a table."""
    nodes = ingest.get("nodes", [])
    if not nodes:
        print("  (no ingest result files found)")
        return
    w = max(len(n.get("host", "")) for n in nodes)
    print(f"  {'host':{w}}  {'writer':6}  {'backend':8}  {'storage':9}"
          f"  {'records':>10}  {'write_s':>8}  {'sync_s':>7}  {'records/s':>10}")
    for n in nodes:
        host = n.get("host", "?")
        if "error" in n:
            print(f"  {host:{w}}  {n['error']}")
            continue
        def fmt(key, f):
            v = n.get(key, "-")
            return f.format(v) if isinstance(v, (int, float)) else f"{v:>10}"
        print(f"  {host:{w}}  {n.get('writer','-'):6}  {n.get('backend','-'):8}  {n.get('storage','-'):9}"
              f"  {fmt('records', '{:>10d}')}  {fmt('write_s', '{:>8.2f}')}  {fmt('sync_s', '{:>7.2f}')}"
              f"  {fmt('records_per_s', '{:>10d}')}")
    sm = ingest.get("summary", {})
    if sm:
        print(f"  {'total/max':{w}}  {'':6}  {'':8}  {'':9}"
              f"  {sm.get('total_records',0):>10d}  {sm['max_write_s']:.2f}"
              f"  {sm['max_sync_s']:.2f}  {sm.get('aggregate_records_per_s','-'):>10}")


def run_checks(target, query_file):
    cls = load_plugin()
    source = load_dataset(target, live=False)
    adapter = make_adapter(cls, target)
    print(f"Write backend '{WRITE_BACKEND}' -> query backend '{QUERY_BACKEND}' "
          f"(adapter '{adapter.name}'), target '{target}' on {', '.join(SERVERS)}")
    print(f"Data set '{DS.name}': {DS.size()} records in {DS.partitions} partitions; {source}")
    ingest = load_ingest_results(target)
    print("\nIngest performance:")
    print_ingest_summary(ingest)
    try:
        adapter.connect()
    except BackendError as e:
        sys.exit(f"bench.py: backend '{BACKEND}' check failed: {e}")
    queries = load_query_set(query_file, adapter)
    print(f"\nQuery benchmark  [adapter: {adapter.name}]")
    print(f"  {query_file} — {len(queries)} queries, "
          f"1 verified run + {REPEAT} timed runs each")
    rep = Report(target, adapter.name)
    rep.ingest = ingest
    try:
        run_query_set(rep, adapter, queries)
    finally:
        adapter.close()
    return rep


def run_live(target):
    """Poll a target that is being written; each answer must be valid."""
    cls = load_plugin()
    load_dataset(target, live=True)
    rep = Report(target, cls.name)
    final = DS.size()
    # The poll uses the query set's first query of type "all", so that the
    # live test needs no backend-specific query of its own.
    text = next((q["query"] for q in load_query_set(QUERY_FILE, cls)
                 if q["type"] == "all" and q["query"]), None)
    if not text:
        sys.exit(f"bench.py: {QUERY_FILE} has no query of type 'all' for adapter '{cls.name}'")
    print(f"Polling '{target}' every {LIVE_POLL}s until {final} rows")
    counts, invalid, errors = [], 0, 0
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        adapter = make_adapter(cls, target)
        try:
            adapter.connect()
            _, rows = collect(adapter, text)
        except Exception as e:  # e.g. the target does not exist yet
            errors += 1
            print(f"    poll: error ({e})")
        else:
            vp, keys, _ = validate_rows(rows)
            if vp or len(keys) != len(set(keys)):
                invalid += 1
            counts.append(len(rows))
            print(f"    poll: {len(rows)} rows, {'valid' if not vp else vp[0]}")
            if len(rows) >= final:
                break
        finally:
            adapter.close()
        time.sleep(LIVE_POLL)
    monotonic = is_sorted(counts)
    complete = bool(counts) and counts[-1] == final
    shown = counts if len(counts) <= 6 else counts[:3] + ["..."] + counts[-3:]
    rep.add("rows never decrease", "PASS" if monotonic else "FAIL",
            "counts " + ", ".join(map(str, shown)))
    rep.add("every answer valid", "PASS" if invalid == 0 else "FAIL",
            f"{invalid} of {len(counts)} answers had invalid or duplicate rows")
    rep.add("final count reached", "PASS" if complete else "FAIL",
            f"{counts[-1] if counts else 0}/{final} rows")
    rep.add("query errors while writing", "INFO",
            f"{errors} polls returned an error (e.g. target not yet created)")
    return rep


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", required=True,
                   help="name of the written data (container, database, ...)")
    p.add_argument("--live", action="store_true")
    p.add_argument("--queries", default=QUERY_FILE,
                   help="query set file (default: QUERY_FILE, or queries/default.json)")
    a = p.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    suffix = "-live" if a.live else f"-{QUERY_ADAPTER or 'default'}"
    log_path = os.path.join(RESULTS_DIR, f"benchmark-{a.target}{suffix}.log")
    tee = Tee(log_path)
    try:
        rep = run_live(a.target) if a.live else run_checks(a.target, a.queries)
    finally:
        tee.close()

    # Build a generic benchmark result: same structure regardless of backend.
    query_results = []
    for item in rep.items:
        entry = {
            "name": item["check"],
            "status": item["status"],
            "rows": item.get("rows"),
            "verified": item["status"] in ("PASS", "FAIL"),
        }
        # Timing fields (present when timed runs succeeded)
        for key in ("median_s", "p95_s", "min_s", "rows_per_s", "times_s"):
            if key in item:
                entry[key] = item[key]
        if item.get("query"):
            entry["query"] = item["query"]
        if item["status"] == "FAIL":
            entry["detail"] = item.get("detail", "")
        query_results.append(entry)

    result = {
        "target": a.target,
        "dataset": DS.params(),
        "servers": SERVERS,
        "ingest": {
            "backend": WRITE_BACKEND,
            "nodes": getattr(rep, "ingest", {}).get("nodes", []),
            "summary": getattr(rep, "ingest", {}).get("summary", {}),
        },
        "query": {
            "backend": QUERY_BACKEND,
            "adapter": rep.adapter,
            "query_file": None if a.live else a.queries,
            "repeat": REPEAT,
            "results": query_results,
        },
        "all_passed": not rep.failed(),
    }

    out = os.path.join(RESULTS_DIR, f"benchmark-{a.target}{suffix}.json")
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  results: {out}")
    print(f"  log:     {log_path}")
    return 1 if rep.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
