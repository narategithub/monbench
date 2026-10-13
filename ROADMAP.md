# Limitations and roadmap

This document tracks what the current version does not do, what we may do
about it, and why. Update it whenever an item is completed or revisited.
Operational notes for running the current version (such as permissions and
output formats) are in `README.md`, "Assumptions and limits".

**Status legend**

- **Suggested improvement** — a known limitation with a concrete path
  forward; not yet scheduled.
- **Deferred** — agreed to postpone, to stabilise this version.
- **Out of scope** — deliberately not covered by the suite.
- **Done** — completed; kept for the record of why.

Last updated: 2026-10-12

---

## Scale

* **Verification holds the whole data set in memory**
  * Current limitation:
    * `core/dataset.py` builds every expected record as a Python tuple before verifying.
    * Works for millions of records; fails on memory and time for billions.
  * Options:
    * Compute each query's expected answer as one `seq` range per component. Every supported condition narrows the components or a `seq` range, so cost grows with the number of components, not records.
    * For very large results, verify the exact row count plus a sample of rows instead of every row.
  * Status: **Suggested improvement.** About half a day. Best done before other backends depend on the current verifier.

* **Driver starts one `ssh` or `srun` per server**
  * Current limitation:
    * Thousands of servers means thousands of concurrent `ssh` connections or Slurm job steps from one host.
  * Options:
    * Slurm: one `srun` across all servers; each task finds its partition index from its host name.
    * ssh: cap the number of connections started at once (for example 64).
  * Status: **Suggested improvement.** A few hours.

* **Backend service starts one server at a time**
  * Current limitation:
    * `serve-write` and `serve-query` start the service on each server in turn and wait for each to be ready.
    * Takes a long time with many servers.
  * Options:
    * Start all servers in parallel and wait for all, as the write phase already does.
  * Status: **Suggested improvement.** About an hour.

* **One writer process per server**
  * Current limitation:
    * Each server writes its partition from a single process and thread.
    * Different writers (C, Python) run one after another. This is intentional, so their timings do not interfere.
  * Options:
    * Several writer processes per server, each writing part of the partition.
  * Status: **Deferred.** Not needed for query benchmarking; revisit for ingest benchmarking.

* **Scale not yet demonstrated**
  * Current limitation:
    * Tested on 2–3 nodes and up to 160,000 records. The node count itself is unrestricted (`SERVER_NODES`).
  * Options:
    * Run on a large cluster once the scale items above are addressed.
  * Status: **Deferred** until the scale items above are addressed.

---

## Measurement

* **Timings include building DataFrames**
  * Current limitation:
    * The `dsos` and `sos` adapters use the Python API, which returns DataFrames; conversion time is included.
    * Represents an analyst's experience, not the fastest a client could achieve.
  * Options:
    * A C query client for DSOS (and native clients for other backends) as an "engine tier".
  * Status: **Deferred.** Python is used for all backends in this version, which keeps the comparison even.

* **One client at a time**
  * Current limitation:
    * Each query runs alone, repeated `REPEAT` times. No concurrent clients.
  * Options:
    * Closed loop: N clients each sending the next query when the previous finishes (throughput).
    * Open loop: queries arrive at a set rate (latency under load).
  * Status: **Deferred** to the version that adds use-case workloads.

* **Few repetitions per query**
  * Current limitation:
    * Default `REPEAT=5`; the reported p95 is effectively the maximum.
  * Options:
    * Raise the default; report p95 only when enough runs exist.
  * Status: **Suggested improvement.** Small change.

* **No control of cache state**
  * Current limitation:
    * In `both` mode, timed runs follow an untimed verification run, so they measure warm-cache performance. In `query` mode, all runs are timed; the first may include cold-cache effects.
    * Dropping the page cache in Docker affects the whole host.
  * Options:
    * Optional cold-cache runs on bare metal (drop caches before each run).
  * Status: **Deferred.**

* **No server resource monitoring**
  * Current limitation:
    * CPU, memory and disk use on the servers during the benchmark are not recorded.
  * Options:
    * Sample `/proc` on each server during the check and query phases.
  * Status: **Deferred.**

---

## Workloads and query sets

* **No use-case mode**
  * Current limitation:
    * Only the automatic (neutral) query set is supported: each query runs in turn.
    * No query frequencies, mix weights, or real query logs.
  * Options:
    * A workload file with query strings, frequencies and scale, from configuration.
    * A converter from a site's query log into such a file.
  * Status: **Deferred** to version 2. The query set is already read from a file, so use-case files can be added as another workload source.

* **Query strings are written per backend**
  * Current limitation:
    * Each question needs a query string for each backend's language.
  * Options:
    * Generate backend strings from the neutral `match` description.
  * Status: **Deferred.** Manual strings are enough for the current backends.

* **Verifier's condition grammar still has gaps**
  * Current limitation:
    * `match.where` supports `attribute op number` and, as of the
      value-generation refactor (`design/value-generation.md` Sec 6.5),
      two more condition forms generic over any declared component kind:
      `kind[i].attr op value` / `kind[*].attr op value` (indexed and
      existential) and `count(kind) op n` (cardinality) — see
      `core/bench.py`'s `parse_match()`/`record_matches()` and
      `queries/curated_metrics.json` for worked examples.
    * Still missing: `or`, aggregates, and grouping. Such queries run
      unverified (INFO) or with `"verify": false`.
  * Options:
    * Extend the condition grammar further; add expected results for aggregates.
  * Status: **Deferred** (for `or`/aggregates/grouping). The component-kind
    forms above are **Done**.

* **No aggregation, downsampling or derivative queries**
  * Current limitation:
    * Grafana-style questions (averages per time bin, rates of change, latest value per component) are not in the default set.
    * DSOS binning syntax is unknown; the `group_by` syntax is a guess.
    * DSOS returns partial group rows per server, which the client must combine.
  * Options:
    * Add these families once the syntax is confirmed from the DSOS query parser source.
    * Define each by its result; count client-side work in the backend's time.
  * Status: **Deferred.**

---

## Data sets

* **One data shape authored so far**
  * Current limitation:
    * Only the default monitoring shape (`dataset/default.json`) and the
      component-kind worked example (`dataset/curated_metrics_like.json`)
      exist.
    * Previously this item also covered the value rules being hardcoded
      Python/C code; that part is **done** — the value-generation
      refactor (`design/value-generation.md`) made every value rule
      (including component-kind generation) a pure function of a
      dataset's own JSON declaration, computed by the shared
      `core/valuegen.py`/`.c`. Defining a new dataset, flat or nested,
      needs no change to `core/dataset.py`, `core/valuegen.py`, or any
      backend's writer code (see `EXTENDING.md`, "Defining a custom
      dataset").
  * Options:
    * Additional shapes (wide, narrow, other entity models) are now just
      more `dataset/<n>.json` files; no code-level option remains open
      here.
  * Status: **Done** (value-rule hardcoding). Writing more example
    datasets is not tracked as a limitation any more, just something to
    do as the need arises.

* **Per-CPU and per-interface records**
  * Background:
    * The default data set's attribute names and units are modeled on the
      `curated_metrics` LDMS sampler plugin, which exposes `/proc/meminfo`,
      `/proc/loadavg` and the scalar `/proc/stat` counters as flat metrics —
      these are all in the default data set (see `README.md`, "The data
      set"). `curated_metrics` also exposes per-CPU-core `/proc/stat` fields
      and per-network-interface `/proc/net/dev` fields, as nested LDMS
      *records*: a variable-length list of per-core or per-interface values
      attached to each sample, not flat scalars.
  * Resolution:
    * Rather than the "wide columns" option previously recommended here,
      this was implemented as **true nested records** — generic
      **component kinds** (`design/value-generation.md` Sec 6): a dataset
      declares zero or more component kinds, each a flat record with a
      variable, per-sample instance count, one level of nesting below the
      node. `dataset/curated_metrics_like.json`'s `cpu` and `netdev` kinds
      are the worked example, modeled directly on `curated_metrics`'s own
      per-core/per-interface records.
    * Generation (`core/valuegen.py`/`.c`) and verification
      (`core/bench.py`'s `kind[i].attr`/`kind[*].attr`/`count(kind)`
      condition grammar) are implemented and backend-neutral. Backend
      schema mapping and native `kind[i]`/`kind[*]` queries are
      implemented for `influxdb`, `sos` and `dsos` (see each backend's
      README.md, "Component-kind data model mapping", and
      `queries/curated_metrics.json`); `count(kind)` is native only on
      `influxdb` (see `STATUS.md`).
    * Not yet done: smoke-testing any of this against real SOS/DSOS/
      InfluxDB servers (`STATUS.md`'s "Suggested next step"), and the
      "true nested records within a component" extension (`component →
      sub-component[]`) that `design/value-generation.md` Sec 6.6
      explicitly leaves out of scope.
  * Status: **Done** (generation, verification, and backend mapping for
    one level of component nesting). The two-level-nesting
    (sub-component) extension remains **Out of scope** (design doc
    Sec 6.6); real-server smoke-testing is tracked in `STATUS.md`.

* **No support for existing data from other sources**
  * Current limitation:
    * Verification and query parameters need a metadata file written by the suite's own writers.
    * Data written by earlier versions of the suite has no metadata; the engine falls back to current settings.
  * Options:
    * A hand-written metadata template for existing databases; verification off for such data.
  * Status: **Deferred.**

---

## Backends

* **Backend status**
  * Current state:
    * `sos` writes local SOS containers (C and Python writers) and can query
      them directly on disk (Python adapter).
    * `dsos` writes through `dsosd` (Python writer) and queries through `dsosd`
      (Python API and `dsosql`).
    * `influxdb` writes and queries InfluxDB v3 Core (Python client and HTTP).
    * A run picks a write backend and a query backend; tested pairs are
      sos→dsos, sos→sos, dsos→dsos, and influxdb→influxdb.
  * Options / next:
    * Elasticsearch: distributes queries across shards, closer to DSOS in design.
  * Status: Elasticsearch is a **suggested improvement.**

* **Unequal topologies between backends**
  * Current limitation:
    * DSOS spreads data and each query over several servers.
    * Open-source InfluxDB 3 Core is single-node; clustering needs the commercial Enterprise edition, and adds query throughput rather than per-query speed.
    * The `sos` direct query adapter provides a single-node comparison point
      for SOS/DSOS, but the topologies still differ when comparing against
      InfluxDB.
  * Options:
    * Also run DSOS on one server, for a like-for-like comparison and a scaling curve.
    * Report topology and total resources with every result.
  * Status: **Suggested improvement.**

* **Different result semantics between backends**
  * Current limitation:
    * DSOS applies `limit` per server; the verifier accepts both per-server and whole-result counts.
    * DSOS returns partial groups per server.
  * Options:
    * Define each question's result exactly; adapters or the client combine partial results, and that time counts.
  * Status: **Deferred** to when aggregate queries are added.

* **No multi-config run**
  * Current limitation:
    * Running several writer→query pairs (sos→dsos, dsos→dsos,
      influxdb→influxdb) requires separate `bin/run.sh -c <conf>` invocations.
  * Options:
    * A wrapper script or a `CONFIGS` list in a top-level config that runs
      each pair in sequence and collects the results.
    * A combined report comparing results across pairs.
  * Status: **Suggested improvement.**

---

## Scope

* **Writing and querying at the same time**
  * Current limitation:
    * The `live` test checks correctness while data is written; it is not a benchmark.
  * Options:
    * A mixed read/write benchmark with concurrent writers and query clients.
  * Status: **Out of scope** for this version; too much work for its value now.

---

## Naming

* **Suite renamed from `sosbench` to `monbench`**
  * Reason:
    * `sosbench` suggested an SOS-only tool; SOS is now one plugin among equals.
    * `hpcmonbench` was considered, but claims more HPC scale than the suite has demonstrated so far.
    * The default data set is now named `monitoring`, separate from the suite's name, since other data shapes may be added.
  * Note:
    * Name checks: not on PyPI. On GitHub, `Wilfred2002/MonBench` is a personal repository in an unrelated field (a Monad blockchain app template, no description or stars). GitHub names are per owner, so it does not block `ovis-hpc/monbench`.
    * Similar names exist: `benchmon` (an HPC resource monitor from the SKA project) and `moonbench` (a MoonBit benchmarking toolkit).
  * Status: **Done** in this version.

---

## Upstream SOS issues found

Issues in SOS found while building the suite. Not suite work, but tracked here
so they are reported.

* **`SqlQuery.next()` drops one row per full batch** (`Sos.pyx`)
  * Confirmed by measurement; fix confirmed by rerun.
  * To do: report upstream with the patch.

* **`dsosd` crashes on queries in one development branch**
  * `query_select_1_svc()` dereferences `query->ast->iter_attr_e` when the parser set none.
  * To do: report to the branch maintainers with the backtrace.

* **Found by reading the code; not yet confirmed by testing:**
  * `dsosql import` ignores the result of `dsos_obj_create()`.
  * Server-side ingest errors never reach the caller.
  * Client request threads appear to poll without sleeping (2 ns instead of 2 s timeout).
  * Large transactions may overflow the stack through recursive XDR encoding.
  * Grouped and binned queries may skip one object at each 65,536-object batch boundary.
