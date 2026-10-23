/*
 * dataset_cross_check.c -- C side of the Python/C dataset cross-check.
 * See dataset_cross_check.py's docstring.
 *
 * usage: dataset_cross_check DATASET_JSON T0 INTERVAL JOB_LEN KIND CID SEQ
 *
 *   KIND = EXP  -> one ds_expected() record, printed as its attributes'
 *                  formatted values (ds->attrs order) joined by '|'
 *   KIND = CNT  -> every ds_expected_lists() record, printed as
 *                  "list|index|value..." per record, records joined by
 *                  ';' (list declaration order then entry-index order,
 *                  matching dataset.py's expected_lists()) -- a
 *                  record-entries list prints one '|'-joined value per
 *                  tuple field (list->tuples order); a bare-scalar-
 *                  entries list prints its single value
 *
 * Formatting, matching dataset_cross_check.py's fmt_value() exactly:
 *   uintN/intN/timestamp -> decimal
 *   double                -> IEEE-754 bit pattern, 16 hex digits
 *   string                -> the raw decoded string
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include "dataset.h"

static uint64_t double_bits(double d)
{
	uint64_t bits;
	memcpy(&bits, &d, sizeof(bits));
	return bits;
}

/* Appends the formatted value for one attribute to buf/pos (a growing
 * fixed buffer), per the module header's formatting rules. */
static void append_value(char *buf, size_t *pos, size_t bufsize,
                          const ds_attr_t *attr, const ds_value_t *v)
{
	int n;
	switch (attr->family) {
	case DS_FAMILY_DOUBLE:
		n = snprintf(buf + *pos, bufsize - *pos, "%016" PRIx64, double_bits(v->d));
		break;
	case DS_FAMILY_STRING:
		n = snprintf(buf + *pos, bufsize - *pos, "%s", v->str);
		break;
	case DS_FAMILY_INT:
		n = snprintf(buf + *pos, bufsize - *pos, "%" PRId64, v->i64);
		break;
	default: /* UINT, TIMESTAMP */
		n = snprintf(buf + *pos, bufsize - *pos, "%" PRIu64, v->u64);
		break;
	}
	if (n > 0)
		*pos += (size_t)n;
}

int main(int argc, char **argv)
{
	dataset_t ds;
	char err[256];
	uint64_t t0, interval, job_len, cid, seq;
	const char *kind;
	char line[65536];
	size_t pos = 0;

	if (argc != 8) {
		fprintf(stderr, "usage: %s DATASET_JSON T0 INTERVAL JOB_LEN KIND CID SEQ\n",
		        argv[0]);
		return 2;
	}
	t0 = strtoull(argv[2], NULL, 10);
	interval = strtoull(argv[3], NULL, 10);
	job_len = strtoull(argv[4], NULL, 10);
	kind = argv[5];
	cid = strtoull(argv[6], NULL, 10);
	seq = strtoull(argv[7], NULL, 10);

	if (ds_load(&ds, argv[1], t0, interval, job_len, err, sizeof(err))) {
		fprintf(stderr, "ds_load failed: %s\n", err);
		return 2;
	}

	line[0] = '\0';

	if (strcmp(kind, "EXP") == 0) {
		ds_value_t values[DS_MAX_ATTRS];
		size_t i;

		ds_expected(&ds, cid, seq, values);
		for (i = 0; i < ds.n_attrs; i++) {
			if (i > 0)
				line[pos++] = '|';
			append_value(line, &pos, sizeof(line), &ds.attrs[i], &values[i]);
		}
	} else if (strcmp(kind, "CNT") == 0) {
		uint64_t job_id = ds_job_id(&ds, seq);
		/* static: each ds_list_rec_t is tens of KB (values[] is
		 * DS_MAX_ATTRS slots of the DS_MAX_STRING_LEN-sized
		 * ds_value_t union) -- an array of these on the stack
		 * easily exceeds the default stack size. */
		static ds_list_rec_t recs[256];
		int n = ds_expected_lists(&ds, cid, job_id, seq, recs,
		                           (int)(sizeof(recs) / sizeof(recs[0])));
		int r;

		if (n < 0) {
			fprintf(stderr, "ds_expected_lists: too many records\n");
			return 2;
		}
		for (r = 0; r < n; r++) {
			const ds_list_t *list = recs[r].list;
			int m;
			if (r > 0)
				line[pos++] = ';';
			m = snprintf(line + pos, sizeof(line) - pos, "%s|%" PRIu64,
			             list->name, recs[r].index);
			if (m > 0)
				pos += (size_t)m;
			line[pos++] = '|';
			if (list->is_record) {
				size_t ai;
				for (ai = 0; ai < list->n_tuples; ai++) {
					if (ai > 0)
						line[pos++] = '|';
					append_value(line, &pos, sizeof(line), &list->tuples[ai],
					             &recs[r].values[ai]);
				}
			} else {
				append_value(line, &pos, sizeof(line), &list->scalar_entry,
				             &recs[r].values[0]);
			}
		}
	} else {
		fprintf(stderr, "unknown KIND '%s'\n", kind);
		return 2;
	}

	line[pos] = '\0';
	printf("%s\n", line);
	ds_free(&ds);
	return 0;
}
