# monbench dataset value-generation design

Status: **implemented, terminology revised.** The generation/verification
design (hashing, decode rules, list-attribute generation, verification
model, backend mapping) was implemented against an earlier vocabulary that
overloaded **component** for two unrelated things: the monitored entity
(`component_id`, a reserved LDMS name) and a per-sample nested list (a CPU
or network-interface record). This revision removes the second meaning
entirely — there is no "component kind" any more, only **list
attributes**, identified the same way every other attribute is identified:
by `name`. `component_id` keeps its name throughout (§7.4) because it is
reserved by LDMS; the ambiguity was always the second, suite-invented
usage, not that one. See `STATUS.md` for the implementation's
stage-by-stage history under the old vocabulary and `EXTENDING.md` for how
to define a new dataset against the model described here.

---

## 1. Motivation

The previous design hardcoded:

- A fixed `ATTRS` tuple and a literal formula per attribute in
  `core/dataset.py`'s `Dataset.expected()`.
- A hand-translated C re-implementation of those same formulas in
  `backends/sos/writer/sos_write.c`, because C cannot import a Python
  module. Any writer that is not Python-based inherited this problem.

Consequences that motivated the original value-generation design:

- Defining a new dataset meant editing `core/dataset.py` (there was no
  dispatch between multiple value-rule sets) *and* hand-updating every
  non-Python writer — not just writing a new `dataset/<n>.json`.
- Hand-picking formula coefficients (multipliers, moduli, scales) per
  attribute was not realistic to ask of an admin defining their own
  dataset.

A second motivation, addressed by this revision specifically: the first
version of this design introduced its own **component kind** concept for
nested per-sample lists (a CPU or network-interface record), alongside a
`kind` field distinct from `name`. That was redundant — a list attribute
already has a `name`, the same as every other attribute — and it
collided, in casual reading, with the suite's pre-existing, LDMS-reserved
`component_id`. This revision removes the redundant concept: a list is
just an attribute whose `type` is `"list"`, identified by its own `name`.
Nothing about generation, verification, or backend mapping needed to
change in substance — only the vocabulary and the field names built on
it.

**Generic by design.** Everything below — the hash construction, decode
rules, nested-attribute generation algorithm, the verification model, and
the C and Python implementations — is driven entirely by a dataset's own
JSON declaration (names, types, nesting). None of it is hardcoded to any
one real sampler's schema. LDMS's `curated_metrics` plugin is used
throughout as a *motivating example* of what a realistic monitoring
dataset looks like (§6.4) and as a check that the type system covers what
a real sampler produces (§6.5), not as the specification the generator is
built to match. §6.6 states the one structural limitation this generality
accepts — and, in this revision, that limitation is enforced by the
grammar itself rather than left as a documented promise (see §6.6).

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **node** | A compute node — what `component_id` means in code, data and every other doc. Reserved as `component_id` rather than renamed, because that name is fixed by LDMS convention (§7.4). |
| **list attribute** | An attribute whose value is a variable-length, per-sample list (§6). Identified by its own `name`, exactly like any other attribute — there is no separate "kind" concept. |
| **job** | A job running on the system |
| **job rank** | A rank (e.g. an MPI rank) within a job. **Name reserved, support deferred** — no current metric needs it |

Hierarchy implemented by this design: **server** (writer) → **node**,
with **job** as an orthogonal dimension that cuts across nodes. A node's
sample may include list attributes (§6) — e.g. one entry per CPU or
network interface — but these are not a separate tier in this vocabulary;
they are ordinary attributes whose `type` happens to be `"list"`.

This vocabulary is also now consistent with the shell/config layer:
`SERVER_NODES`/`COMPONENTS_PER_NODE`/`QUERY_NODE` used "node" to mean the
*physical writer machine* — the opposite of this table's "node" — which
is why those settings are renamed to `SERVER_HOSTS`,
`COMPONENT_IDS_PER_SERVER`, and `QUERY_HOST` (§7.4). "Server" now means
the writer everywhere, matching this table's hierarchy line exactly.

## 3. Requirements

- A new dataset must be definable as pure data (a new `dataset/<n>.json`),
  with no changes to `core/dataset.py` or any backend's writer code. (Met:
  see `EXTENDING.md`, "Defining a custom dataset".)
- Admins must be able to write a dataset definition by specifying, per
  attribute: **name, type, and (optionally) a range/length** — no
  per-attribute tuning of coefficients.
- One shared, deterministic evaluator computes every value; it must be
  trivially portable to non-Python writers (same algorithm, implementable
  identically in C). (Met: `core/valuegen.py` / `core/valuegen.c`,
  cross-checked bit-exact by `core/valuegen_cross_check.py`.)
- The evaluator must not require a parser or an expression language (no
  AST) — ruled out explicitly during design discussion.
- Preserve three properties the original hand-written formulas had,
  without hand-tuning:
  - **Selectivity** — a numeric condition (e.g. `free < 50`) should match a
    realistic, non-contiguous fraction of rows.
  - **Distinguishability** — different attributes must not be trivially
    inter-derivable, so column-swap, truncation, or type-coercion bugs are
    still caught by verification.
  - **Compression resistance** — values must not be trivially compressible
    (monotonic, repeating), or write/query timings would measure a
    backend's compressor rather than the backend itself (see §5.3).
- Support a `string` type, including a **list of strings** (§6.1) —
  strings are a scalar type like any other, so a list whose entries are
  strings needs no special case.
- Support attributes whose value is constant, or changes at a coarser
  grain than every sample (not every metric needs to vary every `seq`).
- Support an arbitrary number of **list attributes** per node, each with
  a **variable, per-sample length** (not a fixed dataset-wide constant),
  whose entries are either a flat record of scalar attributes or a bare
  scalar — one level of list nesting below the node, generically, for any
  dataset declaration (§6). A list attribute's entries cannot themselves
  be (or contain) a list — not merely a documented rule, but something the
  entry-type grammar makes inexpressible (§6.6).
- The verifier (`core/bench.py`) must support conditions on list-entry
  attributes (indexed and existential), list lengths, and string equality
  — fully designed in §6.5. (Met: `parse_match()`/`record_matches()` in
  `core/bench.py`.)
- No backward-compatibility constraint: the suite has not been released
  publicly, so the current default dataset's exact computed values, and
  its JSON shape, were free to change under this scheme and again under
  this revision.

## 4. Value-generation function

### 4.1 Key construction

For a given attribute, build a byte string from its identifying
coordinates. Unused coordinates (e.g. list name/entry index for a
node-level attribute, or the tuple-field name for a bare-scalar list
entry) are fixed to an empty-string / `0` sentinel:

```
K = dataset_name || 0x00 || metric_name || 0x00 || list_name || 0x00
  || le64(node_id) || le64(entry_index)
  || le64(job_id)  || le64(floor(seq / stride))
```

- `le64(x)`: 8-byte little-endian encoding of a 64-bit unsigned integer.
- `list_name`: the declaring list attribute's own `name` (e.g. `"cpu"`,
  `"netdev"`), empty string for a node-level (non-list) attribute.
  Included so that two list attributes can reuse the same tuple-field
  name (e.g. both `"cpu"` and `"gpu"` having a `util` field) without
  colliding. See §6.
- `metric_name`: the attribute's own name for a flat or record-tuple
  attribute; the empty-string sentinel for a list whose entries are a
  bare scalar (there is no sub-field name in that case — the entry *is*
  the value). This is what makes different attributes of the same record
  statistically independent of each other (see §5.2).
- `entry_index`: 0-based position within the list (see §6.3); `0` when
  not applicable.
- `stride` (default 1): lets an attribute be "sticky." `stride = s` means
  the value only changes every `s` samples. A very large stride (or an
  explicit `"constant": true` flag) makes the value depend only on
  identity, never on `seq`.

This key shape is generic: it has exactly one list-entry slot, and a
dataset may declare any number of sibling list attributes (§6), each
using this same `K` with its own `list_name`. It is not specific to any
fixed set of lists like `cpu`/`netdev` — those are simply the instance
used as a worked example in §6.4.

### 4.2 Hash function: FNV-1a, 64-bit

Chosen for being simple, fast, and trivially implementable identically in
Python and C (plain integer ops, no library dependency). Not a
cryptographic hash — none of this needs cryptographic properties, only
good mixing and speed, since the suite hashes on the write-performance
critical path.

```
FNV_OFFSET = 0xcbf29ce484222325
FNV_PRIME  = 0x100000001b3
MASK64     = 2^64 - 1

fnv1a64(b):                      # b: byte string
    h = FNV_OFFSET
    for byte in b:
        h = h XOR byte
        h = (h * FNV_PRIME) AND MASK64
    return h
```

For attributes needing more than 64 bits of output (e.g. long strings),
chain blocks with a counter:

```
digest(K, i) = fnv1a64(K || le64(i)),   i = 0, 1, 2, ...
```

### 4.3 Decoding a digest into a typed value

Let `D0 = digest(K, 0)`.

```
uintN:  value = lo + (D0 mod (hi - lo + 1))
intN:   value = lo + (D0 mod (hi - lo + 1))     # same form, signed default range
```

**`double` — scaled integer.** `double` is not a separate primitive; it is
a display transform over an integer decode:

```
double: base_value = uintN_decode(D0, attr.base or uint64, attr.range or default_range(base))
        value = base_value / attr.scale + attr.offset
                 # scale default 100.0, offset default 0.0
```

Default range table (admin may override any field per attribute):

| Type | Default range / shape |
|---|---|
| `uint8` | `[0, 255]` |
| `uint16` | `[0, 65535]` |
| `uint32` | `[0, 2^32 - 1]` |
| `uint64` | `[0, 2^64 - 1]` |
| `int8` | `[-128, 127]` |
| `int16` | `[-32768, 32767]` |
| `int32` | `[-2^31, 2^31 - 1]` |
| `int64` | `[-2^63, 2^63 - 1]` |
| `string` | length 8, alphabet `a-z0-9` |
| `double` | base `uint64` range `[0, 9999]`, `scale = 100.0`, `offset = 0.0` → value in `[0.00, 99.99]` |

**String decode** (length `L`, alphabet `Σ`, `|Σ|` symbols):

```
string(K, L, Σ):
    chars = []
    i = 0
    while len(chars) < L:
        bytes = le64(digest(K, i))        # 8 bytes
        for byte in bytes:
            if len(chars) == L: break
            chars.append(Σ[byte mod |Σ|])
        i = i + 1
    return join(chars)
```

A `string`-typed list entry (§3) uses exactly this decode — nothing
list-specific about it; `list_name`/`entry_index` just feed into `K` the
same way they do for any other list-entry attribute.

### 4.4 Full evaluator

```
evaluate(attr, dataset_name, node_id, list_name, entry_index,
         job_id, seq):
    K = key_bytes(dataset_name, attr.name, list_name,
                   node_id, entry_index, job_id, seq, attr.stride or 1)
    return decode(attr, K)
```

This same function is called for every flat attribute, every record-tuple
field, and every bare scalar list entry a dataset declares — nothing
about it is specific to a particular list.

## 5. Rationale: why these properties, without hand-tuning

### 5.1 Selectivity
`D0` is effectively uniform over its declared range for a given
`(metric_name, ...)`, so a numeric condition like `free < 50` matches a
realistic, non-contiguous fraction of rows — exercising a backend's filter
rather than letting it exploit a trivial pattern (e.g. one contiguous run).

### 5.2 Distinguishability
Because `metric_name` (and, for list-entry attributes, `list_name`) is
part of `K`, two attributes evaluated at the same `(node_id, seq, ...)`
get statistically unrelated digests — there is no algebraic relationship
between them. A column-swap, truncation, or type-coercion bug is still
caught by verification, because nothing about one attribute's value
predicts another's.

### 5.3 Compression resistance
Several backends (InfluxDB especially, SOS's on-disk structures to a
lesser extent) compress columns at write time — delta encoding,
run-length encoding, dictionary encoding. Trivially regular synthetic
values (monotonic counters, repeated values, values strongly correlated
with row position) let a compressor collapse them almost for free: writes
become artificially cheap, storage artificially small, and scans
artificially fast, none of which reflects how the backend performs on real
`curated_metrics` data, which varies unpredictably. Every timing the suite
reports (`write_s`, `records_per_s`, query latencies) is downstream of how
many real bytes had to move through I/O, so this bias affects every
measurement even though the suite doesn't report a compression ratio
directly. A hash-based value has no structure for a compressor to exploit,
so cross-backend comparisons stay about the engines, not about how
compressible the test data happens to be.

## 6. List attributes

A dataset's list attributes are declared and generated generically: the
schema, key construction, and algorithm below support *any* set of list
attributes with *any* entry shape — a dataset with no lists, one list, or
ten; entries with 2 fields or 20, or no fields at all (a bare scalar).
`curated_metrics` (§6.4) is one concrete instance plugged into this
generic machinery, used because it is real evidence of what a monitoring
dataset's nested structure and data types look like — it is not what the
schema or algorithm are tailored to.

### 6.1 Dataset declaration

There is a single `attributes` array; a list attribute is simply one
whose `type` is `"list"`. Its entries are either a flat record (scalar
tuple fields) or a bare scalar — including `string` (§3). Length is
variable per sample, bounded by a declared min/max (or fixed via a
`list_length` shorthand), and these length properties — `min_list_length`,
`max_list_length`, `list_length`, `length_stride` — are properties of the
*list*, siblings of `entries`, not of any one entry field:

```json
{
  "name": "monitoring",
  "attributes": [
    { "name": "timestamp",    "type": "timestamp" },
    { "name": "component_id", "type": "uint64" },
    { "name": "job_id",       "type": "uint64" },
    { "name": "seq",          "type": "uint64" },

    { "name": "cpu",
      "type": "list",
      "min_list_length": 2,
      "max_list_length": 2,
      "length_stride": 1000000000,
      "entries": {
        "type": "record",
        "tuples": [
          { "name": "cpu_name", "type": "string" },
          { "name": "idle",     "type": "double" },
          { "name": "sys",      "type": "double" },
          { "name": "user",     "type": "double" },
          { "name": "iowait",   "type": "double" }
        ]
      }
    },

    { "name": "netdev",
      "type": "list",
      "min_list_length": 1,
      "max_list_length": 2,
      "entries": {
        "type": "record",
        "tuples": [
          { "name": "iface_name", "type": "string" },
          { "name": "rx_bytes",   "type": "uint64" },
          { "name": "tx_bytes",   "type": "uint64" }
        ]
      }
    },

    { "name": "recent_latencies_us",
      "type": "list",
      "list_length": 5,
      "entries": { "type": "uint32", "range": [0, 5000] }
    }
  ]
}
```

- `min_list_length` / `max_list_length`: the length for a given
  `(node_id, job_id, seq-bucket)` is hash-derived (§6.3) and falls in
  `[min_list_length, max_list_length]`, inclusive.
- `list_length`: shorthand for `min_list_length == max_list_length ==
  list_length` — how an admin gets a fixed length, if that is all they
  need. Omitting all three defaults to a fixed length of 1 (§7, item 2).
- `length_stride` (default: a very large constant, i.e. effectively fixed
  for the run): like an attribute's own `stride` (§4.1), controls how
  often the *length* is allowed to change — independent of any entry
  field's own `stride`, which controls how often that field's *value*
  changes. A real interface list, for instance, rarely changes length
  within one run, while an interface's own counters still change every
  sample.
- `entries`: either `{"type": "record", "tuples": [...]}` (each tuple
  exactly like a flat attribute, minus list-level fields — a tuple's own
  `type` cannot be `"list"`; see §6.6) or a bare scalar type spec
  (`{"type": "uint32", "range": [...]}`, `{"type": "string", ...}`, etc.).

The third example above (`recent_latencies_us`) is a list of scalars: no
record wrapper, no sub-field name — the list's own `name` and each
entry's index are all the identity a value needs.

See `dataset/curated_metrics_like.json` for the real worked example this
schema produces (also declaring `rx_packets`/`tx_packets` on `netdev`).

### 6.2 Identifiers

`list_name` is the declaring attribute's own `name`, carried into `K` as
in §4.1. `entry_index` is a plain 0-based position within that list, up
to (length − 1) for the current sample — not hashed itself, just the loop
variable in §6.3.

### 6.3 Generation algorithm

```
list_length(dataset_name, list_attr, node_id, job_id, seq):
    K_n = dataset_name || 0x00 || "__length__" || 0x00 || list_attr.name || 0x00
        || le64(node_id) || le64(job_id)
        || le64(floor(seq / list_attr.length_stride))
    span = list_attr.max_list_length - list_attr.min_list_length + 1
    return list_attr.min_list_length + (fnv1a64(K_n) mod span)

generate_node_sample(dataset, node_id, job_id, seq):
    sample = {}
    for attr in dataset.attributes:
        if attr.type != "list":
            sample[attr.name] = evaluate(attr, dataset.name, node_id,
                                           "", 0, job_id, seq)
            continue
        n = list_length(dataset.name, attr, node_id, job_id, seq)
        sample[attr.name] = []
        for i in range(n):
            if attr.entries.type == "record":
                entry = {}
                for field in attr.entries.tuples:
                    entry[field.name] = evaluate(field, dataset.name, node_id,
                                                  attr.name, i, job_id, seq)
            else:
                entry = evaluate(attr.entries, dataset.name, node_id,
                                  attr.name, i, job_id, seq)
            sample[attr.name].append(entry)
    return sample
```

Deterministic and order-independent: every `(node_id, job_id, seq)`
produces the same length and the same attribute values regardless of
write order, exactly like the flat case — including the length itself,
which a verifier can recompute rather than having to read back and count
entries.

Nothing in this algorithm names `cpu` or `netdev`, or treats list
attributes as a separate category from flat ones beyond the single
`attr.type == "list"` branch; a dataset with one list, five lists, or
none runs through the same loop.

### 6.4 Resulting shape (curated_metrics as a worked instance)

Plugging the §6.1 declaration (modeled on `curated_metrics`'s `cpu` and
`netdev` record types) into §6.3, one sample looks like:

```json
{
  "timestamp": ..., "job_id": ..., "seq": ...,
  "cpu": [
    { "cpu_name": "cpu0", "idle": 71.22, "sys": 8.91, "user": 12.08, "iowait": 1.03 },
    { "cpu_name": "cpu1", "idle": 65.90, "sys": 7.44, "user": 18.41, "iowait": 2.10 }
  ],
  "netdev": [
    { "iface_name": "eth0", "rx_bytes": ..., "tx_bytes": ... }
  ]
}
```

This is one instance of the generic shape — the node has two list
attributes (`cpu`, `netdev`), each a list of records, each with its own
independently variable length. A different dataset declaration (say, a
GPU list with power/temperature tuples, or a bare scalar list, or no
lists at all) produces the same shape of result through the same
algorithm.

### 6.5 Verification model and backend mapping

**Verification is recomputation, not storage.** For any attribute
occurrence identified by its coordinates

```
C = (dataset_name, node_id, list_name, entry_index, job_id, seq)
```

define

```
expected(attr, C)                          = evaluate(attr, C)           # §4.4
expected_length(list_attr, node_id, job_id, seq) = list_length(list_attr, ...)  # §6.3
```

No expected-value table is ever stored or shipped alongside a dataset —
the verifier holds only the dataset declaration (the same JSON used to
generate) and recomputes both functions directly.

**Condition grammar**, generic over any declared list attribute:

```
condition    := node_cond | list_cond | length_cond
node_cond    := attr cmp value
list_cond    := name '[' idx ']' ('.' attr)? cmp value
idx          := INT | '*'
length_cond  := 'count(' name ')' cmp INT
cmp          := '==' | '!=' | '<' | '<=' | '>' | '>='
value        := NUMBER | STRING_LITERAL
```

`.attr` is required when `name`'s `entries.type == "record"`, and
forbidden when `entries` is a bare scalar — the entry *is* the value in
that case. Semantics:

```
match(node_cond, C)                 = (evaluate(attr, C) cmp value)
match(name[i].attr cmp v, C)        = i < expected_length(name, C) AND evaluate(attr, C_i) cmp v
match(name[i] cmp v, C)             = i < expected_length(name, C) AND evaluate(entries, C_i) cmp v
match(name[*].attr cmp v, C)        = ∃ i ∈ [0, expected_length(name, C)) : evaluate(attr, C_i) cmp v
match(name[*] cmp v, C)             = ∃ i ∈ [0, expected_length(name, C)) : evaluate(entries, C_i) cmp v
match(count(name) cmp n, C)         = expected_length(name, C) cmp n
```

`STRING_LITERAL` needs no new machinery beyond `==`/`!=`: `evaluate`
already produces a string for `string`-typed attributes (§4.3), so string
equality is just that decoded value compared to a literal — the same
comparison form as a number, and this applies identically whether the
string is a flat attribute, a record-tuple field, or a bare scalar list
entry.

**Backend schema mapping**, generic per declared list attribute:

| Level | SOS/DSOS | InfluxDB |
|---|---|---|
| node | existing node schema, keyed `(component_id, job_id, seq)` | existing measurement, tags `component_id, job_id` |
| each list attribute | one schema per list, columns = the list's entry fields (or one value column for a bare-scalar list) + `component_id, job_id, seq, entry_index`; join index on `(component_id, job_id, seq)` | one measurement per list, tags `component_id, job_id, entry_index`, fields = the list's entry fields (or one value field) |

This is the same shape the node table already uses — one schema/
measurement per declared list attribute rather than hardcoded ones for
`cpu`/`netdev`. Translating the grammar into native queries:

- `name[i].attr cmp v` / `name[i] cmp v` → `WHERE entry_index = i AND
  attr cmp v` (or `WHERE entry_index = i AND value cmp v` for a
  bare-scalar list) on that list's table/measurement, joined to the node
  row by `(component_id, job_id, seq)`.
- `name[*]...` → `WHERE attr cmp v` (or `WHERE value cmp v`) on that
  list's table — no `EXISTS` subquery needed; a matching row's existence
  *is* the existential, since row cardinality alone encodes it.
- `count(name) cmp n` → either `SELECT COUNT(*) ... WHERE component_id=?
  AND job_id=? AND seq=?` against the backend, or, since
  `expected_length` is a pure recomputation, the verifier can check this
  one without touching the backend at all.

**Implementation status, per backend** (see `STATUS.md` for the full
record and `backends/<name>/README.md` for each backend's own mapping
details): InfluxDB, SOS and DSOS all write list-attribute entries and
answer `name[i]...`/`name[*]...` as native queries (SOS/DSOS via a plain
select on the per-list schema plus `Adapter.postprocess()`-based dedup for
the existential form, since the SOS query language has no
JOIN/DISTINCT). `count(name)` is native only on InfluxDB; SOS/DSOS have
no `GROUP BY`/`HAVING`-style count filter, so that condition form reports
SKIP under those backends.

**Type coverage check against curated_metrics.** Every field type
`curated_metrics.c` actually uses is already covered by §4.3's type
table: flat scalar counters (`free`, `MemTotal`, ...) → `uint64`/`int64`;
`cpu`/`netdev` name fields (`cpu_name`, `iface_name`) → `string`;
percentage-style fields (`idle`, `sys`, `user`, `iowait`) → `double`
(scaled-integer decode); byte/packet counters (`rx_bytes`, `tx_packets`,
...) → `uint64`. Nothing new was needed in the type system to cover a
real sampler's output.

### 6.6 Scope limitation: one level of list nesting only

This design supports exactly **one level** of list nesting below a node:
`node → list[]`, where each entry is a flat record of scalar attributes
or a bare scalar. It does **not** support an entry itself containing a
nested list.

This is enforced structurally, not just documented: a record entry's
tuple fields are declared with the same attribute grammar as a flat
attribute, and that grammar's `type` excludes `"list"` for a tuple field.
A dataset JSON that tried to declare `cpu`'s `idle` tuple as `"type":
"list"` is rejected by the loader, not merely discouraged by this
section.

This is a deliberate boundary, not an oversight:

- It matches how LDMS itself represents this data. Reading
  `curated_metrics.c`'s `create_metric_set()` confirms each record type
  (`cpu`, `netdev`) is built from scalar fields only
  (`ldms_record_create` + `ldms_record_metric_add`, never a nested
  record), and is attached to the schema as one variable-length list
  (`ldms_schema_metric_list_add`). Going to a list-of-lists would need a
  shape that is not something `curated_metrics` — or, as far as this
  design has checked, LDMS's record/list API — produces.
- It keeps the key construction (§4.1), the generation algorithm (§6.3),
  the verifier grammar, and the backend schema mapping (§6.5) all one
  level deep, with no third hash-key dimension and no third level of
  indexing to design, implement, and keep consistent across backends.

**Consequence, accepted as a limitation:** the suite cannot benchmark a
dataset whose true shape is two-level-nested. If a future dataset
genuinely needs that, it is out of scope for this design and would need a
separate extension — one that would have to introduce a new grammar
construct, since today's grammar makes the shape inexpressible rather
than merely unimplemented.

## 7. Resolved questions

1. **Default string length/alphabet** — 8 characters, alphabet `a-z0-9`
   (`valuegen.py`'s `DEFAULT_STRING_LENGTH`/`DEFAULT_STRING_ALPHABET`,
   mirrored in `valuegen.h` as `VG_DEFAULT_STRING_LENGTH`/
   `VG_DEFAULT_STRING_ALPHABET`).
2. **Default `min_list_length`/`max_list_length`/`length_stride`** when a
   dataset's JSON omits them — `list_length()` falls back to a fixed
   length (`list_length`, default 1, i.e. `min == max`) and a very large
   `length_stride` (`DEFAULT_LENGTH_STRIDE = 10**15` in Python,
   `VG_DEFAULT_LENGTH_STRIDE` in C), so a dataset that doesn't care about
   variable lengths gets a fixed-length list with no extra configuration.
   This supersedes the earlier, undocumented `count_per_node` shorthand
   that `dataset.c`'s loader silently accepted without this doc ever
   specifying it — `list_length` is the one documented spelling going
   forward.
3. **Job rank** — name reserved in the vocabulary (§2); no implementation
   planned. (The earlier **sub-component** reservation no longer applies:
   there is no "component" tier any more for a sub-unit of it to attach
   to. A list attribute whose entries themselves need to be lists remains
   out of scope per §6.6, should it ever come up again.)
4. **Vocabulary rename — executed, this revision.** An earlier version of
   this document used `node_id` in its own prose while the implementation
   used `component_id` everywhere, and declined to reconcile them because
   `component_id` is a reserved LDMS metric name. That part of the
   decision stands: `component_id` keeps its name. What's changed is the
   *other* overload this document used to carry — a "component kind"
   concept for nested lists, with its own `kind` field distinct from
   `name` — which this revision removes outright, in both the JSON schema
   and the Python/C implementations (`component_kind`/`component_index`/
   `component_count()` → `list_name`/`entry_index`/`list_length()`,
   `Dataset.components()` → `Dataset.component_ids()`, etc.). Separately,
   the shell/config layer's own node-vs-server ambiguity
   (`SERVER_NODES`/`COMPONENTS_PER_NODE`/`QUERY_NODE`, which used "node"
   for the physical writer — the opposite of this document's "node") is
   resolved by renaming those settings to `SERVER_HOSTS`,
   `COMPONENT_IDS_PER_SERVER`, and `QUERY_HOST`, bringing the config layer
   in line with the vocabulary this document always specified.

## 8. Status

Generation (§4, §6.1–§6.4), verification (§6.5), and the C port are all
implemented, driven by a dataset's own JSON declaration rather than
hardcoded to `curated_metrics`, covering every data type (scalar lists
included) and the one-level nested shape `curated_metrics` actually uses,
with the two-level-nesting limitation enforced by the grammar (§6.6). The
vocabulary cleanup this revision makes (§2, §7 item 4) touches
`core/dataset.py`/`.h`/`.c`, `core/valuegen.py`/`.h`/`.c`,
`core/bench.py`, every `dataset/*.json` and `queries/*.json` file, each
backend's README and writer, and the shell/config layer
(`bench*.conf*`, `bin/run.sh`, `bin/node.sh`,
`slurm/monbench.sbatch`). See `STATUS.md` for the generation/verification
implementation's stage-by-stage history and what remains to be
smoke-tested against real SOS/DSOS/InfluxDB servers; the vocabulary
cleanup itself is tracked as a fresh pass over those same files, not a
re-verification of the underlying algorithm, which is unchanged in
substance.
