/*
 * valuegen_cross_check.c -- C side of the Python/C valuegen cross-check.
 *
 * Reads a vectors file (written by valuegen_cross_check.py) where each
 * line is one test case:
 *
 *   UINT|dataset|metric|list_name|node_id|entry_index|job_id|seq|stride|lo|hi
 *   INT |dataset|metric|list_name|node_id|entry_index|job_id|seq|stride|lo|hi
 *   STR |dataset|metric|list_name|node_id|entry_index|job_id|seq|stride|length|alphabet
 *   DBL |dataset|metric|list_name|node_id|entry_index|job_id|seq|stride|base_lo|base_hi|scale|offset
 *   CNT |dataset|list_name|node_id|job_id|seq|length_stride|lo|hi
 *   DFLT|type
 *
 * and prints one result line per test case, in the same order, for
 * valuegen_cross_check.py to diff against valuegen.py's own computation:
 *
 *   UINT/CNT -> decimal uint64_t
 *   INT      -> decimal int64_t
 *   STR      -> the decoded string
 *   DBL      -> the IEEE-754 bit pattern, as 16 hex digits (exact compare,
 *               independent of text-to-float formatting differences)
 *   DFLT     -> "<is_uint>|<is_int>|<lo>|<hi>", lo/hi "NA" when neither
 *               vg_is_uint_type() nor vg_is_int_type() is true for *type*
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include "valuegen.h"

#define MAXLINE 4096
#define MAXFIELDS 16

/*
 * Split on '|', preserving empty fields (list_name="" for a node-level
 * attribute is a real, meaningful empty field) -- unlike strtok(), which
 * collapses consecutive delimiters and would silently misalign every
 * field after an empty one.
 */
static int split(char *line, char *fields[], int max)
{
	int n = 0;
	char *p = line;
	char *nl = strchr(line, '\n');
	if (nl)
		*nl = '\0';
	fields[n++] = p;
	while (n < max) {
		char *bar = strchr(p, '|');
		if (!bar)
			break;
		*bar = '\0';
		p = bar + 1;
		fields[n++] = p;
	}
	return n;
}

static uint64_t double_bits(double d)
{
	uint64_t bits;
	memcpy(&bits, &d, sizeof(bits));
	return bits;
}

int main(int argc, char **argv)
{
	FILE *f;
	char line[MAXLINE];

	if (argc != 2) {
		fprintf(stderr, "usage: %s VECTORS_FILE\n", argv[0]);
		return 2;
	}
	f = fopen(argv[1], "r");
	if (!f) {
		fprintf(stderr, "error opening %s\n", argv[1]);
		return 2;
	}

	while (fgets(line, sizeof(line), f)) {
		char *fields[MAXFIELDS];
		int n = split(line, fields, MAXFIELDS);
		if (n == 0)
			continue;
		const char *kind = fields[0];

		if (strcmp(kind, "UINT") == 0 || strcmp(kind, "INT") == 0) {
			const char *dataset = fields[1], *metric = fields[2], *list_name = fields[3];
			uint64_t node_id = strtoull(fields[4], NULL, 10);
			uint64_t entry_index = strtoull(fields[5], NULL, 10);
			uint64_t job_id = strtoull(fields[6], NULL, 10);
			uint64_t seq = strtoull(fields[7], NULL, 10);
			uint64_t stride = strtoull(fields[8], NULL, 10);
			if (strcmp(kind, "UINT") == 0) {
				uint64_t lo = strtoull(fields[9], NULL, 10);
				uint64_t hi = strtoull(fields[10], NULL, 10);
				uint64_t v = vg_evaluate_uint(dataset, metric, list_name, node_id,
				                               entry_index, job_id, seq, stride, lo, hi);
				printf("%" PRIu64 "\n", v);
			} else {
				int64_t lo = strtoll(fields[9], NULL, 10);
				int64_t hi = strtoll(fields[10], NULL, 10);
				int64_t v = vg_evaluate_int(dataset, metric, list_name, node_id,
				                             entry_index, job_id, seq, stride, lo, hi);
				printf("%" PRId64 "\n", v);
			}
		} else if (strcmp(kind, "STR") == 0) {
			const char *dataset = fields[1], *metric = fields[2], *list_name = fields[3];
			uint64_t node_id = strtoull(fields[4], NULL, 10);
			uint64_t entry_index = strtoull(fields[5], NULL, 10);
			uint64_t job_id = strtoull(fields[6], NULL, 10);
			uint64_t seq = strtoull(fields[7], NULL, 10);
			uint64_t stride = strtoull(fields[8], NULL, 10);
			size_t length = (size_t)strtoull(fields[9], NULL, 10);
			const char *alphabet = fields[10];
			char out[256];
			vg_evaluate_string(dataset, metric, list_name, node_id, entry_index,
			                    job_id, seq, stride, length, alphabet,
			                    strlen(alphabet), out);
			fwrite(out, 1, length, stdout);
			fputc('\n', stdout);
		} else if (strcmp(kind, "DBL") == 0) {
			const char *dataset = fields[1], *metric = fields[2], *list_name = fields[3];
			uint64_t node_id = strtoull(fields[4], NULL, 10);
			uint64_t entry_index = strtoull(fields[5], NULL, 10);
			uint64_t job_id = strtoull(fields[6], NULL, 10);
			uint64_t seq = strtoull(fields[7], NULL, 10);
			uint64_t stride = strtoull(fields[8], NULL, 10);
			uint64_t base_lo = strtoull(fields[9], NULL, 10);
			uint64_t base_hi = strtoull(fields[10], NULL, 10);
			double scale = strtod(fields[11], NULL);
			double offset = strtod(fields[12], NULL);
			double v = vg_evaluate_double(dataset, metric, list_name, node_id,
			                               entry_index, job_id, seq, stride,
			                               base_lo, base_hi, scale, offset);
			printf("%016" PRIx64 "\n", double_bits(v));
		} else if (strcmp(kind, "CNT") == 0) {
			const char *dataset = fields[1], *list_name = fields[2];
			uint64_t node_id = strtoull(fields[3], NULL, 10);
			uint64_t job_id = strtoull(fields[4], NULL, 10);
			uint64_t seq = strtoull(fields[5], NULL, 10);
			uint64_t length_stride = strtoull(fields[6], NULL, 10);
			uint64_t lo = strtoull(fields[7], NULL, 10);
			uint64_t hi = strtoull(fields[8], NULL, 10);
			uint64_t v = vg_list_length(dataset, list_name, node_id, job_id,
			                             seq, length_stride, lo, hi);
			printf("%" PRIu64 "\n", v);
		} else if (strcmp(kind, "DFLT") == 0) {
			const char *type = fields[1];
			int is_u = vg_is_uint_type(type);
			int is_i = vg_is_int_type(type);
			if (is_u) {
				uint64_t lo, hi;
				vg_default_range_uint(type, &lo, &hi);
				printf("%d|%d|%" PRIu64 "|%" PRIu64 "\n", is_u, is_i, lo, hi);
			} else if (is_i) {
				int64_t lo, hi;
				vg_default_range_int(type, &lo, &hi);
				printf("%d|%d|%" PRId64 "|%" PRId64 "\n", is_u, is_i, lo, hi);
			} else {
				printf("%d|%d|NA|NA\n", is_u, is_i);
			}
		} else {
			fprintf(stderr, "unknown test kind '%s'\n", kind);
			return 2;
		}
	}
	fclose(f);
	return 0;
}
