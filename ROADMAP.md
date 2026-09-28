# Limitations and roadmap

This document tracks what the current version does not do, what we plan to do
about it, and why. Update it whenever an item is decided, deferred, or done.
Operational notes for running the current version (such as permissions and
output formats) are in `README.md`, "Assumptions and limits".

**Status legend**

- **Decided** — agreed; to be done in the next version.
- **Proposed** — recommended; awaiting a decision.
- **Deferred** — agreed to postpone, to stabilise the first version.
- **Out of scope** — deliberately not covered by the suite.
- **Done** — completed; kept for the record of why.

Last updated: 2026-09-28

---

## Scale

* **Verification holds the whole data set in memory**
  * Current limitation:
    * `core/dataset.py` builds every expected record as a Python tuple before verifying.
    * Works for millions of records; fails on memory and time for billions.
  * Options:
    * Compute each query's expected answer as one `seq` range per component. Every supported condition narrows the components or a `seq` range, so cost grows with the number of components, not records.
    * For very large results, verify the exact row count plus a sample of rows instead of every row.
  * Decision: **Proposed.** About half a day. Best done before other backends depend on the current verifier.

* **Driver starts one `ssh` or `srun` per server**
  * Current limitation:
    * Thousands of servers means thousands of concurrent `ssh` connections or Slurm job steps from one host.
  * Options:
    * Slurm: one `srun` across all servers; each task finds its partition index from its host name.
    * ssh: cap the number of connections started at once (for example 64).
  * Decision: **Proposed.** A few hours.

* **Backend service starts one server at a time**
  * Current limitation:
    * `serve` starts the service on each server in turn and waits for each to be ready.
    * Takes a long time with many servers.
  * Options:
    * Start all servers in parallel and wait for all, as the write phase already does.
  * Decision: **Proposed.** About an hour.

* **One writer process per server**
  * Current limitation:
    * Each server writes its partition from a single process and thread.
    * Different writers (C, Python) run one after another. This is intentional, so their timings do not interfere.
  * Options:
    * Several writer processes per server, each writing part of the partition.
  * Decision: **Deferred.** Not needed for query benchmarking; revisit for ingest benchmarking.

* **Scale not yet demonstrated**
  * Current limitation:
    * Tested on 2–3 nodes and up to 160,000 records. The node count itself is unrestricted (`SERVER_NODES`).
  * Options:
    * Run on a large cluster once the three proposed scale items are done.
  * Decision: **Deferred** until the scale items above are addressed.

---

## Measurement

* **Timings include building DataFrames**
  * Current limitation:
    * The `dsos` adapter uses the Python API, which returns DataFrames; conversion time is included.
    * Represents an analyst's experience, not the fastest a client could achieve.
  * Options:
    * A C query client for DSOS (and native clients for other backends) as an "engine tier".
  * Decision: **Deferred.** Python is used for all backends in the first version, which keeps the comparison even.

* **One client at a time**
  * Current limitation:
    * Each query runs alone, repeated `REPEAT` times. No concurrent clients.
  * Options:
    * Closed loop: N clients each sending the next query when the previous finishes (throughput).
    * Open loop: queries arrive at a set rate (latency under load).
  * Decision: **Deferred** to the version that adds use-case workloads.

* **Few repetitions per query**
  * Current limitation:
    * Default `REPEAT=5`; the reported p95 is effectively the maximum.
  * Options:
    * Raise the default; report p95 only when enough runs exist.
  * Decision: **Proposed.** Small change.

* **No control of cache state**
  * Current limitation:
    * All timed runs follow an untimed run, so they measure warm-cache performance only.
    * Dropping the page cache in Docker affects the whole host.
  * Options:
    * Optional cold-cache runs on bare metal (drop caches before each run).
  * Decision: **Deferred.**

* **No server resource monitoring**
  * Current limitation:
    * CPU, memory and disk use on the servers during queries are not recorded.
  * Options:
    * Sample `/proc` on each server during the check phase.
  * Decision: **Deferred.**

---

## Workloads and query sets

* **No use-case mode**
  * Current limitation:
    * Only the automatic (neutral) query set is supported: each query runs in turn.
    * No query frequencies, mix weights, or real query logs.
  * Options:
    * A workload file with query strings, frequencies and scale, from configuration.
    * A converter from a site's query log into such a file.
  * Decision: **Deferred** to version 2. The query set is already read from a file, so use-case files can be added as another workload source.

* **Query strings are written per backend**
  * Current limitation:
    * Each question needs a query string for each backend's language.
  * Options:
    * Generate backend strings from the neutral `match` description.
  * Decision: **Deferred.** Manual strings are enough for two backends.

* **Verifier understands only simple conditions**
  * Current limitation:
    * `match.where` supports `attribute op number` joined by `and`.
    * No `or`, no aggregates, no grouping; such queries run unverified (INFO).
  * Options:
    * Extend the condition grammar; add expected results for aggregates.
  * Decision: **Deferred.**

* **No aggregation, downsampling or derivative queries**
  * Current limitation:
    * Grafana-style questions (averages per time bin, rates of change, latest value per component) are not in the default set.
    * DSOS binning syntax is unknown; the `group_by` syntax is a guess.
    * DSOS returns partial group rows per server, which the client must combine.
  * Options:
    * Add these families once the syntax is confirmed from the DSOS query parser source.
    * Define each by its result; count client-side work in the backend's time.
  * Decision: **Deferred.**

---

## Data sets

* **One data shape**
  * Current limitation:
    * Only the default monitoring shape exists.
    * `dataset/default.json` lists attributes and keys, but the value rules are code in `core/dataset.py` and the writers.
  * Options:
    * Additional shapes (wide, narrow, other entity models), each with its own value rules.
  * Decision: **Deferred.**

* **No support for existing data from other sources**
  * Current limitation:
    * Verification and query parameters need a metadata file written by the suite's own writers.
    * Data written by earlier versions of the suite has no metadata; the engine falls back to current settings.
  * Options:
    * A hand-written metadata template for existing databases; verification off for such data.
  * Decision: **Deferred.**

---

## Backends

* **Backends: sos (write), dsos (query), influxdb (write+query) planned**
  * Current state:
    * `sos` writes local SOS containers (C and Python writers); no query adapter yet.
    * `dsos` queries SOS containers across servers through dsosd; no writer yet.
    * A run picks a write backend and a query backend (`WRITE_BACKEND`, `QUERY_BACKEND`); the primary case is write with `sos`, query with `dsos`.
    * `influxdb` (v3) is not yet implemented.
  * Options / next:
    * InfluxDB v3 plugin: write and query, with `INFLUX_INTERFACE` (python/http) and `INFLUX_QUERY_LANG` (sql/influxql).
    * `dsos` writer: the DSOS Python API can write (`DsosContainer.insert_df`, object-based, not SQL); add it so DSOS write can be benchmarked too.
    * `sos` query adapter: query one node's container directly, for a single-node comparison point.
    * Elasticsearch: distributes queries across shards, closer to DSOS in design.
  * Decision: **Decided:** InfluxDB v3 is the next backend. `dsos` writer and `sos` query are **Deferred** (sos covers writing, dsos covers querying for now).

* **Unequal topologies between backends**
  * Current limitation:
    * DSOS spreads data and each query over several servers.
    * Open-source InfluxDB 3 Core is single-node; clustering needs the commercial Enterprise edition, and adds query throughput rather than per-query speed.
  * Options:
    * Also run DSOS on one server, for a like-for-like comparison and a scaling curve.
    * Report topology and total resources with every result.
  * Decision: **Proposed.**

* **Different result semantics between backends**
  * Current limitation:
    * DSOS applies `limit` per server; the verifier accepts both per-server and whole-result counts.
    * DSOS returns partial groups per server.
  * Options:
    * Define each question's result exactly; adapters or the client combine partial results, and that time counts.
  * Decision: **Deferred** to when aggregate queries are added.

---

## Scope

* **Writing and querying at the same time**
  * Current limitation:
    * The `live` test checks correctness while data is written; it is not a benchmark.
  * Options:
    * A mixed read/write benchmark with concurrent writers and query clients.
  * Decision: **Out of scope** for the first version; too much work for its value now.

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
  * Decision: **Done** in this version.

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
