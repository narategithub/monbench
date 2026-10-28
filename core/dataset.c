/*
 * dataset.c -- see dataset.h for the design and the API this mirrors
 * from dataset.py.
 */
#include "dataset.h"
#include "valuegen.h"
#include <jansson.h>
#include <string.h>
#include <stdio.h>

/* IDENTITY_ATTRS from dataset.py -- their values come from the
 * harness's own sampling schedule, never from valuegen. */
static int is_identity_name(const char *name)
{
	return strcmp(name, "timestamp") == 0 ||
	       strcmp(name, "component_id") == 0 ||
	       strcmp(name, "job_id") == 0 ||
	       strcmp(name, "seq") == 0;
}

/* json_integer_value()/json_number_value() both accept an int or a real
 * node without the caller needing to check which; dataset JSON writes
 * range bounds as plain integers, which is all we resolve here. */
static int json_int_pair(json_t *arr, int64_t *lo, int64_t *hi)
{
	json_t *a, *b;
	if (!json_is_array(arr) || json_array_size(arr) != 2)
		return -1;
	a = json_array_get(arr, 0);
	b = json_array_get(arr, 1);
	*lo = (int64_t)json_integer_value(a);
	*hi = (int64_t)json_integer_value(b);
	return 0;
}

/*
 * Resolve one attribute JSON object into *out, exactly mirroring
 * valuegen.py's decode()'s per-type default/override handling (and, for
 * "double", the three-way base/range branch documented there and in
 * dataset.h's ds_load() comment). Used for flat (top-level or record-
 * tuple) attributes, which always have a "name"; for a bare-scalar list
 * entry, which has no "name" of its own, pass require_name=0 and out->
 * name is left as "" (the metric_name sentinel -- see valuegen.py's
 * evaluate()).
 */
static int resolve_attr(json_t *aj, ds_attr_t *out, char *err, size_t errlen,
                         int require_name)
{
	const char *name = json_string_value(json_object_get(aj, "name"));
	const char *type_str = json_string_value(json_object_get(aj, "type"));
	json_t *stride_j, *range_j;

	if (!type_str || (require_name && !name)) {
		snprintf(err, errlen, "attribute missing 'name' or 'type'");
		return -1;
	}
	if (name && strlen(name) >= DS_MAX_NAME) {
		snprintf(err, errlen, "attribute name '%s' too long", name);
		return -1;
	}
	if (strlen(type_str) >= sizeof(out->type_str)) {
		snprintf(err, errlen, "attribute '%s': type name too long",
		         name ? name : "(entries)");
		return -1;
	}

	memset(out, 0, sizeof(*out));
	if (name)
		strcpy(out->name, name);	/* else "" -- the metric_name sentinel */
	strcpy(out->type_str, type_str);
	out->is_identity = name && is_identity_name(name);

	stride_j = json_object_get(aj, "stride");
	out->stride = stride_j ? (uint64_t)json_integer_value(stride_j) : 1;

	if (strcmp(type_str, "timestamp") == 0) {
		out->family = DS_FAMILY_TIMESTAMP;
		return 0;
	}

	if (out->is_identity) {
		/* component_id/job_id/seq: dataset.py's Dataset.expected()
		 * computes these directly from (comp, seq, job_len), never
		 * through valuegen.evaluate() -- so no decode parameters
		 * are resolved here. family is reported as DS_FAMILY_UINT
		 * since all three are declared uint64 in every dataset
		 * JSON seen so far; ds_expected() never dispatches on it
		 * for an identity attribute regardless. */
		out->family = DS_FAMILY_UINT;
		return 0;
	}

	if (vg_is_uint_type(type_str)) {
		uint64_t lo, hi;
		vg_default_range_uint(type_str, &lo, &hi);
		range_j = json_object_get(aj, "range");
		if (range_j) {
			int64_t slo, shi;
			if (json_int_pair(range_j, &slo, &shi)) {
				snprintf(err, errlen,
				         "attribute '%s': 'range' must be a 2-element array",
				         name ? name : "(entries)");
				return -1;
			}
			lo = (uint64_t)slo;
			hi = (uint64_t)shi;
		}
		out->family = DS_FAMILY_UINT;
		out->u_lo = lo;
		out->u_hi = hi;
		return 0;
	}

	if (vg_is_int_type(type_str)) {
		int64_t lo, hi;
		vg_default_range_int(type_str, &lo, &hi);
		range_j = json_object_get(aj, "range");
		if (range_j) {
			if (json_int_pair(range_j, &lo, &hi)) {
				snprintf(err, errlen,
				         "attribute '%s': 'range' must be a 2-element array",
				         name ? name : "(entries)");
				return -1;
			}
		}
		out->family = DS_FAMILY_INT;
		out->i_lo = lo;
		out->i_hi = hi;
		return 0;
	}

	if (strcmp(type_str, "string") == 0) {
		json_t *len_j = json_object_get(aj, "length");
		json_t *alpha_j = json_object_get(aj, "alphabet");
		const char *alpha = alpha_j ? json_string_value(alpha_j)
		                             : VG_DEFAULT_STRING_ALPHABET;

		out->family = DS_FAMILY_STRING;
		out->length = len_j ? (size_t)json_integer_value(len_j)
		                     : VG_DEFAULT_STRING_LENGTH;
		if (!alpha || strlen(alpha) == 0) {
			snprintf(err, errlen, "attribute '%s': empty 'alphabet'",
			         name ? name : "(entries)");
			return -1;
		}
		if (strlen(alpha) >= sizeof(out->alphabet)) {
			snprintf(err, errlen, "attribute '%s': 'alphabet' too long",
			         name ? name : "(entries)");
			return -1;
		}
		if (out->length >= DS_MAX_STRING_LEN) {
			snprintf(err, errlen, "attribute '%s': 'length' %zu exceeds %d",
			         name ? name : "(entries)", out->length,
			         DS_MAX_STRING_LEN - 1);
			return -1;
		}
		strcpy(out->alphabet, alpha);
		out->alphabet_len = strlen(alpha);
		return 0;
	}

	if (strcmp(type_str, "double") == 0) {
		json_t *base_j = json_object_get(aj, "base");
		json_t *scale_j = json_object_get(aj, "scale");
		json_t *offset_j = json_object_get(aj, "offset");
		uint64_t lo, hi;

		range_j = json_object_get(aj, "range");
		if (range_j) {
			int64_t slo, shi;
			if (json_int_pair(range_j, &slo, &shi)) {
				snprintf(err, errlen,
				         "attribute '%s': 'range' must be a 2-element array",
				         name ? name : "(entries)");
				return -1;
			}
			lo = (uint64_t)slo;
			hi = (uint64_t)shi;
		} else if (!base_j) {
			/* Fully unconfigured double: design doc Sec 4.3's
			 * table entry, not default_range(base) -- see
			 * valuegen.py's decode(). */
			lo = VG_DEFAULT_DOUBLE_RANGE_LO;
			hi = VG_DEFAULT_DOUBLE_RANGE_HI;
		} else {
			const char *base = json_string_value(base_j);
			/* design doc Sec 4.3: double's base decode is
			 * "uintN_decode" -- see dataset.h's ds_load() doc
			 * comment for why a signed base is rejected here
			 * rather than mirroring valuegen.py's wider (and
			 * here, un-mirrorable) default_range(). */
			if (!base || !vg_is_uint_type(base) ||
			    vg_default_range_uint(base, &lo, &hi)) {
				snprintf(err, errlen,
				         "attribute '%s': double 'base' must be an "
				         "unsigned integer type, got '%s'",
				         name ? name : "(entries)", base ? base : "(missing)");
				return -1;
			}
		}
		out->family = DS_FAMILY_DOUBLE;
		out->d_lo = lo;
		out->d_hi = hi;
		out->scale = scale_j ? json_number_value(scale_j) : VG_DEFAULT_DOUBLE_SCALE;
		out->offset = offset_j ? json_number_value(offset_j) : VG_DEFAULT_DOUBLE_OFFSET;
		return 0;
	}

	snprintf(err, errlen, "attribute '%s': unknown type '%s'",
	         name ? name : "(entries)", type_str);
	return -1;
}

/* Resolves a JSON array of named attributes (always require_name=1:
 * top-level flat attributes, or a list attribute's record "tuples")
 * into out_attrs[0..*n_out), each via resolve_attr(). */
static int resolve_attr_list(json_t *arr, ds_attr_t *out_attrs, size_t max,
                              size_t *n_out, char *err, size_t errlen)
{
	size_t i, n;

	if (!json_is_array(arr)) {
		snprintf(err, errlen, "'tuples' must be an array");
		return -1;
	}
	n = json_array_size(arr);
	if (n > max) {
		snprintf(err, errlen, "too many attributes (%zu, max %zu)", n, max);
		return -1;
	}
	for (i = 0; i < n; i++) {
		if (resolve_attr(json_array_get(arr, i), &out_attrs[i], err, errlen, 1))
			return -1;
	}
	*n_out = n;
	return 0;
}

/*
 * Resolve one list-attribute JSON object (design doc Sec 6.1/6.2): its
 * name, min_list_length/max_list_length (or the "list_length" fixed-
 * length shorthand -- a legacy field, formerly undocumented as
 * count_per_node, now named and documented as list_length), optional
 * length_stride, and its "entries" -- either {"type":"record","tuples":
 * [...]} or a bare scalar type spec.
 */
static int resolve_list(json_t *lj, ds_list_t *out, char *err, size_t errlen)
{
	const char *name = json_string_value(json_object_get(lj, "name"));
	json_t *min_j = json_object_get(lj, "min_list_length");
	json_t *max_j = json_object_get(lj, "max_list_length");
	json_t *fixed_j = json_object_get(lj, "list_length");
	json_t *stride_j = json_object_get(lj, "length_stride");
	json_t *entries_j = json_object_get(lj, "entries");
	const char *entries_type;

	if (!name || strlen(name) >= DS_MAX_NAME) {
		snprintf(err, errlen, "list attribute missing or invalid 'name'");
		return -1;
	}
	memset(out, 0, sizeof(*out));
	strcpy(out->name, name);

	if (min_j && max_j) {
		out->min_list_length = (uint64_t)json_integer_value(min_j);
		out->max_list_length = (uint64_t)json_integer_value(max_j);
	} else {
		/* "list_length": a fixed length, default 1 -- mirrors
		 * valuegen.list_length()'s fallback. */
		uint64_t fixed = fixed_j ? (uint64_t)json_integer_value(fixed_j) : 1;
		out->min_list_length = out->max_list_length = fixed;
	}
	out->length_stride = stride_j ? (uint64_t)json_integer_value(stride_j)
	                               : VG_DEFAULT_LENGTH_STRIDE;

	if (!json_is_object(entries_j)) {
		snprintf(err, errlen, "list attribute '%s': missing or invalid 'entries'",
		         name);
		return -1;
	}
	entries_type = json_string_value(json_object_get(entries_j, "type"));
	if (entries_type && strcmp(entries_type, "record") == 0) {
		json_t *tuples_j = json_object_get(entries_j, "tuples");
		out->is_record = 1;
		if (resolve_attr_list(tuples_j, out->tuples, DS_MAX_ATTRS,
		                       &out->n_tuples, err, errlen))
			return -1;
	} else {
		out->is_record = 0;
		if (resolve_attr(entries_j, &out->scalar_entry, err, errlen, 0))
			return -1;
	}
	return 0;
}

int ds_load(dataset_t *ds, const char *path, uint64_t t0, uint64_t interval,
            uint64_t job_len, char *err, size_t err_len)
{
	char local_err[256];
	json_error_t jerr;
	json_t *root, *name_j, *attrs_j;
	size_t i, n;
	int rc = 0;

	if (!err) {
		err = local_err;
		err_len = sizeof(local_err);
	}

	memset(ds, 0, sizeof(*ds));
	ds->t0 = t0;
	ds->interval = interval;
	ds->job_len = job_len;

	root = json_load_file(path, 0, &jerr);
	if (!root) {
		snprintf(err, err_len, "%s:%d: %s", path, jerr.line, jerr.text);
		return -1;
	}

	name_j = json_object_get(root, "name");
	if (!json_is_string(name_j) || strlen(json_string_value(name_j)) >= DS_MAX_NAME) {
		snprintf(err, err_len, "%s: missing or invalid 'name'", path);
		rc = -1;
		goto out;
	}
	strcpy(ds->name, json_string_value(name_j));

	attrs_j = json_object_get(root, "attributes");
	if (!json_is_array(attrs_j)) {
		snprintf(err, err_len, "%s: 'attributes' must be an array", path);
		rc = -1;
		goto out;
	}
	n = json_array_size(attrs_j);
	for (i = 0; i < n; i++) {
		json_t *aj = json_array_get(attrs_j, i);
		const char *type_str = json_string_value(json_object_get(aj, "type"));

		if (type_str && strcmp(type_str, "list") == 0) {
			if (ds->n_lists >= DS_MAX_LISTS) {
				snprintf(err, err_len, "%s: too many list attributes (max %d)",
				         path, DS_MAX_LISTS);
				rc = -1;
				goto out;
			}
			if (resolve_list(aj, &ds->lists[ds->n_lists], err, err_len)) {
				rc = -1;
				goto out;
			}
			ds->n_lists++;
		} else {
			if (ds->n_attrs >= DS_MAX_ATTRS) {
				snprintf(err, err_len, "%s: too many attributes (max %d)",
				         path, DS_MAX_ATTRS);
				rc = -1;
				goto out;
			}
			if (resolve_attr(aj, &ds->attrs[ds->n_attrs], err, err_len, 1)) {
				rc = -1;
				goto out;
			}
			ds->n_attrs++;
		}
	}

	{
		static const char *identity[] = {
			"timestamp", "component_id", "job_id", "seq"
		};
		size_t k;
		for (k = 0; k < sizeof(identity) / sizeof(identity[0]); k++) {
			if (ds_attr_index(ds, identity[k]) < 0) {
				snprintf(err, err_len,
				         "%s: missing required identity attribute '%s'",
				         path, identity[k]);
				rc = -1;
				goto out;
			}
		}
	}

out:
	json_decref(root);
	return rc;
}

void ds_free(dataset_t *ds)
{
	(void)ds;	/* nothing to free: ds_load() allocates no heap memory */
}

int ds_attr_index(const dataset_t *ds, const char *name)
{
	size_t i;
	for (i = 0; i < ds->n_attrs; i++)
		if (strcmp(ds->attrs[i].name, name) == 0)
			return (int)i;
	return -1;
}

const char *ds_attr_type(const dataset_t *ds, const char *name)
{
	int i = ds_attr_index(ds, name);
	return i < 0 ? NULL : ds->attrs[i].type_str;
}

uint64_t ds_job_id(const dataset_t *ds, uint64_t seq)
{
	return 1 + seq / ds->job_len;
}

uint64_t ds_timestamp(const dataset_t *ds, uint64_t seq)
{
	return ds->t0 + seq * ds->interval;
}

/*
 * Evaluate one non-identity attribute's value for the given coordinates,
 * dispatching on its resolved family -- mirrors valuegen.py's decode(),
 * called through valuegen.evaluate(). list_name="" / entry_index=0 are
 * the node-level sentinel, same as dataset.py's expected() passing them
 * to valuegen.evaluate(). attr->name is passed through as the
 * metric_name (vg_evaluate_*'s own metric_name parameter); for a
 * bare-scalar list entry, attr->name is "" (resolve_attr() left it
 * that way), which is exactly the metric_name sentinel valuegen.py's
 * evaluate(metric_name="") uses for the same case.
 */
static void eval_attr(const dataset_t *ds, const ds_attr_t *attr, uint64_t comp,
                       const char *list_name, uint64_t entry_index,
                       uint64_t job_id, uint64_t seq, ds_value_t *out)
{
	switch (attr->family) {
	case DS_FAMILY_UINT:
		out->u64 = vg_evaluate_uint(ds->name, attr->name, list_name,
		                             comp, entry_index, job_id, seq,
		                             attr->stride, attr->u_lo, attr->u_hi);
		break;
	case DS_FAMILY_INT:
		out->i64 = vg_evaluate_int(ds->name, attr->name, list_name,
		                            comp, entry_index, job_id, seq,
		                            attr->stride, attr->i_lo, attr->i_hi);
		break;
	case DS_FAMILY_DOUBLE:
		out->d = vg_evaluate_double(ds->name, attr->name, list_name,
		                             comp, entry_index, job_id, seq,
		                             attr->stride, attr->d_lo, attr->d_hi,
		                             attr->scale, attr->offset);
		break;
	case DS_FAMILY_STRING: {
		char buf[DS_MAX_STRING_LEN];
		vg_evaluate_string(ds->name, attr->name, list_name, comp,
		                    entry_index, job_id, seq, attr->stride,
		                    attr->length, attr->alphabet, attr->alphabet_len,
		                    buf);
		memcpy(out->str, buf, attr->length);
		out->str[attr->length] = '\0';
		break;
	}
	case DS_FAMILY_TIMESTAMP:
		/* Only reachable for a non-identity attribute explicitly
		 * typed "timestamp", which no current dataset declares;
		 * dataset.py has no decode rule for this either (it is
		 * reserved for the identity "timestamp" attribute, handled
		 * directly in ds_expected() below, never through here). */
		out->u64 = ds_timestamp(ds, seq);
		break;
	}
}

void ds_expected(const dataset_t *ds, uint64_t comp, uint64_t seq,
                  ds_value_t *values)
{
	uint64_t job_id = ds_job_id(ds, seq);
	size_t i;

	for (i = 0; i < ds->n_attrs; i++) {
		const ds_attr_t *attr = &ds->attrs[i];

		if (attr->is_identity) {
			if (strcmp(attr->name, "timestamp") == 0)
				values[i].u64 = ds_timestamp(ds, seq);
			else if (strcmp(attr->name, "component_id") == 0)
				values[i].u64 = comp;
			else if (strcmp(attr->name, "job_id") == 0)
				values[i].u64 = job_id;
			else /* "seq" */
				values[i].u64 = seq;
			continue;
		}
		eval_attr(ds, attr, comp, "", 0, job_id, seq, &values[i]);
	}
}

uint64_t ds_list_length(const dataset_t *ds, const ds_list_t *list,
                         uint64_t comp, uint64_t job_id, uint64_t seq)
{
	return vg_list_length(ds->name, list->name, comp, job_id, seq,
	                       list->length_stride, list->min_list_length,
	                       list->max_list_length);
}

int ds_expected_lists(const dataset_t *ds, uint64_t comp,
                       uint64_t job_id, uint64_t seq,
                       ds_list_rec_t *out_recs, int max_recs)
{
	int n_out = 0;
	size_t li;

	for (li = 0; li < ds->n_lists; li++) {
		const ds_list_t *list = &ds->lists[li];
		uint64_t n = ds_list_length(ds, list, comp, job_id, seq);
		uint64_t ei;

		for (ei = 0; ei < n; ei++) {
			ds_list_rec_t *rec;

			if (n_out >= max_recs)
				return -1;
			rec = &out_recs[n_out++];
			rec->list = list;
			rec->index = ei;
			if (list->is_record) {
				size_t ai;
				for (ai = 0; ai < list->n_tuples; ai++)
					eval_attr(ds, &list->tuples[ai], comp, list->name,
					          ei, job_id, seq, &rec->values[ai]);
			} else {
				eval_attr(ds, &list->scalar_entry, comp, list->name,
				          ei, job_id, seq, &rec->values[0]);
			}
		}
	}
	return n_out;
}
