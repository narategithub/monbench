/*
 * dataset.h -- shared, backend-neutral dataset layer: parses a dataset's
 * JSON definition and resolves each attribute's decode parameters, then
 * drives valuegen.c to compute values. C analog of dataset.py, built on
 * top of valuegen.c exactly as dataset.py is built on top of valuegen.py.
 *
 * Purpose: no backend writer should parse dataset JSON or resolve
 * per-attribute defaults itself (type dispatch, range/scale/offset/
 * length/alphabet defaults) -- that logic belongs here, once, so every
 * C writer (today: backends/sos/writer/sos_write.c) gets it by calling
 * this API, the same way every Python writer gets it by calling
 * dataset.py's Dataset class instead of reimplementing value rules.
 *
 * Mirrors dataset.py function-for-function:
 *   Dataset.load_definition + Dataset.__init__   -> ds_load
 *   Dataset.attr_type                             -> ds_attr_type
 *   Dataset.expected                              -> ds_expected
 *   Dataset.expected_lists                        -> ds_expected_lists
 *
 * Any change to dataset.py's attribute-resolution or expected()/
 * expected_lists() semantics must be mirrored here -- see
 * dataset_cross_check.py, which exercises both over the same dataset
 * JSON files and fails loudly on any divergence.
 */
#ifndef MONBENCH_DATASET_H
#define MONBENCH_DATASET_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DS_MAX_NAME        128
#define DS_MAX_ATTRS        64
#define DS_MAX_LISTS         8
#define DS_MAX_ALPHABET     64
/* Longest string value ds_expected()/ds_expected_lists() will decode
 * into a ds_value_t.str; must be >= any attribute's declared "length".
 * Not NUL-padded beyond the decoded length by this library's callers'
 * discretion -- ds_expected always NUL-terminates. */
#define DS_MAX_STRING_LEN  256

/* Attribute's decode family -- which vg_evaluate_.../vg_decode_... path
 * ds_eval_attr() dispatches to. Distinct from the attribute's exact
 * declared width (uint8 vs uint32, etc.), which callers needing it read
 * from attr->type_str (the dataset JSON's own "type" string, verbatim --
 * e.g. for building a backend's own schema, the way
 * backends/sos/make_schema.py already treats it). */
typedef enum {
	DS_FAMILY_TIMESTAMP,
	DS_FAMILY_UINT,
	DS_FAMILY_INT,
	DS_FAMILY_DOUBLE,
	DS_FAMILY_STRING,
} ds_family_t;

/* One resolved attribute: the dataset JSON's "type"/"range"/"scale"/...
 * already translated into the explicit parameters valuegen.c's
 * vg_evaluate_.../vg_decode_... functions take -- exactly mirroring
 * dataset.py/valuegen.py's attr dict + decode(). A list attribute's
 * bare-scalar entry type is also resolved into one of these, with name
 * left as "" (the metric_name sentinel -- see valuegen.py's evaluate()
 * and this header's ds_list_t comment). */
typedef struct {
	char name[DS_MAX_NAME];
	char type_str[32];		/* the JSON "type" string, verbatim */
	ds_family_t family;
	int is_identity;		/* timestamp/component_id/job_id/seq:
					 * dataset.py computes these directly,
					 * never through valuegen -- see
					 * IDENTITY_ATTRS in dataset.py. */
	uint64_t stride;		/* default 1 (every sample) */

	/* DS_FAMILY_UINT */
	uint64_t u_lo, u_hi;
	/* DS_FAMILY_INT */
	int64_t i_lo, i_hi;
	/* DS_FAMILY_DOUBLE: base_value decode range (always unsigned --
	 * design doc Sec 4.3 specifies "uintN_decode" for double's base;
	 * see ds_load()'s doc comment for why a signed "base" is rejected
	 * rather than silently mishandled), plus the scale/offset applied
	 * after. */
	uint64_t d_lo, d_hi;
	double scale, offset;
	/* DS_FAMILY_STRING */
	size_t length;
	char alphabet[DS_MAX_ALPHABET];
	size_t alphabet_len;
} ds_attr_t;

/* One list attribute (design doc Sec 6.1): a per-sample nested list with
 * a variable length, whose entries are either a flat record of named
 * tuple fields (is_record=1, tuples[0..n_tuples)) or a bare scalar
 * (is_record=0, scalar_entry -- a single resolved ds_attr_t with
 * name="", the metric_name sentinel valuegen.evaluate() expects for a
 * list entry with no sub-field name of its own).
 *
 * min_list_length/max_list_length are always both set by ds_load(), even
 * when the JSON gives only the "list_length" fixed-length shorthand
 * (min==max in that case) -- see ds_load()'s resolve_list() for the
 * legacy-field note. */
typedef struct {
	char name[DS_MAX_NAME];
	uint64_t min_list_length, max_list_length;
	uint64_t length_stride;
	int is_record;
	ds_attr_t tuples[DS_MAX_ATTRS];	/* is_record: named tuple fields */
	size_t n_tuples;
	ds_attr_t scalar_entry;			/* !is_record: the bare entry type */
} ds_list_t;

/* A loaded, fully-resolved dataset definition. Fixed-size (no heap
 * allocation) -- ds_free() exists only for API symmetry with dataset.py's
 * object lifetime and does nothing today. */
typedef struct {
	char name[DS_MAX_NAME];
	ds_attr_t attrs[DS_MAX_ATTRS];		/* JSON declaration order,
						 * identity attrs included --
						 * mirrors Dataset.attr_names;
						 * list attributes (below) are
						 * split out, not included here */
	size_t n_attrs;
	ds_list_t lists[DS_MAX_LISTS];
	size_t n_lists;

	/* Harness sampling parameters (dataset.py's Dataset.__init__
	 * arguments t0/interval/job_len; this library never reads
	 * environment variables itself -- the caller passes these, same
	 * as dataset.py's caller does). */
	uint64_t t0, interval, job_len;
} dataset_t;

/* One decoded value, typed by the attribute's ds_family_t. str is
 * always NUL-terminated by ds_expected()/ds_expected_lists(). */
typedef union {
	uint64_t u64;
	int64_t i64;
	double d;
	char str[DS_MAX_STRING_LEN];
} ds_value_t;

/*
 * Load and fully resolve a dataset definition from its JSON file (the
 * path given by e.g. the DATASET env var -- this library does not read
 * environment variables itself, mirroring dataset.py's
 * Dataset.load_definition()+Dataset.__init__() split: the caller reads
 * DATASET and passes the path, plus the harness's own t0/interval/
 * job_len, same arguments dataset.py's Dataset.__init__() takes besides
 * partitions/component_ids/samples, which this C layer has no need for
 * (those only drive Dataset.component_ids()/size(), not value
 * generation).
 *
 * On success, fills *ds and returns 0. On failure, returns -1 and, if
 * err is non-NULL, writes a human-readable message into err (an
 * err_len-byte caller-owned buffer).
 *
 * One resolution rule needs calling out because it is a deliberate
 * narrowing of dataset.py's behavior, not an oversight: a "double"
 * attribute's "base" must name an unsigned integer type. The design doc
 * (Sec 4.3) specifies the base decode as "uintN_decode", but
 * valuegen.py's default_range() happens to also accept the signed int
 * types (because its _RANGE_BY_TYPE table does), so {"type": "double",
 * "base": "int32"} currently succeeds in valuegen.py even though nothing
 * in the design or its tests exercises that path. valuegen.c's double
 * decode only takes an unsigned base range, so mirroring that
 * accidental Python permissiveness here would mean either silently
 * producing a different (wrong) value than valuegen.py's signed path,
 * or non-trivially extending valuegen.c for a case the design doc never
 * asked for. ds_load() instead rejects a signed "base" with a clear
 * error, matching the doc's own wording; if a dataset ever needs a
 * signed double base, valuegen.py/.c should gain that support
 * explicitly and in step, the same way every other decode rule here is
 * kept in lockstep.
 */
int ds_load(dataset_t *ds, const char *path, uint64_t t0, uint64_t interval,
            uint64_t job_len, char *err, size_t err_len);

/* No-op today (ds_load() allocates nothing on the heap); kept so a
 * caller that mirrors dataset.py's object lifetime (create/use/free)
 * doesn't need to change if that ever stops being true. */
void ds_free(dataset_t *ds);

/* Index of attribute name in ds->attrs (flat/node-level attributes
 * only), or -1 if no such attribute. Mirrors looking a name up in
 * Dataset.attr_names. */
int ds_attr_index(const dataset_t *ds, const char *name);

/* The dataset JSON's own "type" string for a flat attribute (e.g.
 * "uint32", "double"), verbatim -- mirrors Dataset.attr_type(). name
 * must be a valid flat attribute name; returns NULL if not. */
const char *ds_attr_type(const dataset_t *ds, const char *name);

/* job_id = 1 + seq / ds->job_len -- mirrors the job_id dataset.py's
 * Dataset.expected() computes inline. A caller needing both job_id and
 * a full ds_expected()/ds_expected_lists() call should take job_id from
 * this function (or read values[idx of "job_id"] back out of
 * ds_expected()'s output) rather than recomputing it a third way, for
 * the same reason dataset.py's docstring gives. */
uint64_t ds_job_id(const dataset_t *ds, uint64_t seq);

/* timestamp = t0 + seq * interval -- mirrors Dataset.expected()'s
 * "timestamp" value (as an integer; the caller converts to float/its
 * own timestamp representation as needed, same as dataset.py returning
 * float(...)). */
uint64_t ds_timestamp(const dataset_t *ds, uint64_t seq);

/*
 * Every flat (node-level) attribute's value for (comp, seq), written
 * into values[0..ds->n_attrs), in ds->attrs order -- mirrors
 * Dataset.expected(), which returns an equivalent dict keyed by name;
 * ds_attr_index()/the attrs array gives the name<->index mapping here.
 * Identity attributes (timestamp, component_id, job_id, seq) are
 * computed directly, not through valuegen, exactly as dataset.py does.
 * values must have at least ds->n_attrs elements.
 */
void ds_expected(const dataset_t *ds, uint64_t comp, uint64_t seq,
                  ds_value_t *values);

/* Deterministic, per-sample length for one list attribute (design doc
 * Sec 6.3) -- mirrors valuegen.list_length(), called through a resolved
 * ds_list_t instead of a raw JSON dict. */
uint64_t ds_list_length(const dataset_t *ds, const ds_list_t *list,
                         uint64_t comp, uint64_t job_id, uint64_t seq);

/* One list-attribute entry instance: which list, which index within it
 * this sample, and its value(s). For a record-entries list, values[0..
 * list->n_tuples) holds the tuple fields in list->tuples order. For a
 * bare-scalar-entries list, only values[0] is filled (the entry's own
 * value; list->n_tuples is 0, so a caller iterating tuples order
 * naturally sees nothing there and must check list->is_record first).
 *
 * Note for callers: values[] is DS_MAX_ATTRS slots of ds_value_t, each
 * sized by its largest member (str[DS_MAX_STRING_LEN]) regardless of
 * that attribute's actual type -- so sizeof(ds_list_rec_t) is several
 * KB. An array of these (e.g. one per entry instance, to pass to
 * ds_expected_lists()) adds up fast; prefer static or heap storage over
 * a stack array once you need more than a handful. */
typedef struct {
	const ds_list_t *list;
	uint64_t index;
	ds_value_t values[DS_MAX_ATTRS];
} ds_list_rec_t;

/*
 * Every list attribute's every entry instance for (comp, seq), across
 * every list attribute the dataset declares, declaration order then
 * entry-index order -- mirrors Dataset.expected_lists(). job_id must be
 * the same value ds_expected() derived (or ds_job_id()'s, which they
 * share), for the reason ds_job_id()'s doc comment gives.
 *
 * out_recs is a caller-supplied array of max_recs ds_list_rec_t.
 * Returns the number of records written, or -1 if max_recs was too
 * small to hold them all (nothing is written in that case).
 */
int ds_expected_lists(const dataset_t *ds, uint64_t comp,
                       uint64_t job_id, uint64_t seq,
                       ds_list_rec_t *out_recs, int max_recs);

#ifdef __cplusplus
}
#endif

#endif /* MONBENCH_DATASET_H */
