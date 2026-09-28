/*
 * sos_write -- write a deterministic data set into a local SOS
 * container using the SOS C API.
 *
 * Every record is a pure function of (component_id, seq), so any reader
 * can verify a record without knowing which node wrote it:
 *
 *   component_id = (node_index + 1) * 1000 + c        c in [0, components)
 *   timestamp    = t0 + seq * interval                seq in [0, samples)
 *   job_id       = 1 + seq / job_len
 *   mem_free     = component_id * 1000000 + seq
 *   load         = (component_id % 1000) + seq * 0.25
 *
 * This is the data set defined in core/dataset.py, which the query engine
 * verifies against; the Python writer (sos_write.py) produces the same.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <inttypes.h>
#include <string.h>
#include <strings.h>
#include <errno.h>
#include <getopt.h>
#include <time.h>
#include <uuid/uuid.h>
#include <jansson.h>
#include <sos/sos.h>

#define MAX_JOIN_ATTRS 16

struct opts {
	const char *path;
	const char *template_path;
	const char *backend;
	int node_index;
	uint64_t components;
	uint64_t samples;
	uint64_t t0;
	uint64_t interval;
	uint64_t job_len;
	uint64_t batch;
	double rate;		/* records per second, 0 = unthrottled */
};

static void usage(const char *prog)
{
	fprintf(stderr,
		"usage: %s --path PATH --template JSON --node-index N\n"
		"          [--components N] [--samples N] [--t0 SECS] [--interval SECS]\n"
		"          [--job-len N] [--batch N] [--rate RECS_PER_SEC]\n"
		"          [--storage mmos|lsos]\n", prog);
	exit(2);
}

static double now_s(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec / 1e9;
}

static const struct {
	const char *name;
	sos_type_t type;
} type_map[] = {
	{ "INT16", SOS_TYPE_INT16 },	 { "INT32", SOS_TYPE_INT32 },
	{ "INT64", SOS_TYPE_INT64 },	 { "UINT16", SOS_TYPE_UINT16 },
	{ "UINT32", SOS_TYPE_UINT32 },	 { "UINT64", SOS_TYPE_UINT64 },
	{ "FLOAT", SOS_TYPE_FLOAT },	 { "DOUBLE", SOS_TYPE_DOUBLE },
	{ "TIMESTAMP", SOS_TYPE_TIMESTAMP }, { "JOIN", SOS_TYPE_JOIN },
};

static int lookup_type(const char *name, sos_type_t *type)
{
	size_t i;
	for (i = 0; i < sizeof(type_map) / sizeof(type_map[0]); i++) {
		if (0 == strcasecmp(name, type_map[i].name)) {
			*type = type_map[i].type;
			return 0;
		}
	}
	return EINVAL;
}

/*
 * Build a schema from the same JSON template the Python Schema.from_template()
 * method accepts, so that both writers produce an identical schema.
 */
static sos_schema_t schema_from_template(const char *path)
{
	json_error_t jerr;
	json_t *tmpl, *attrs, *attr;
	size_t i;
	uuid_t uuid;
	sos_schema_t schema = NULL;
	int rc;

	tmpl = json_load_file(path, 0, &jerr);
	if (!tmpl) {
		fprintf(stderr, "error: %s:%d: %s\n", path, jerr.line, jerr.text);
		return NULL;
	}
	const char *name = json_string_value(json_object_get(tmpl, "name"));
	const char *uuid_str = json_string_value(json_object_get(tmpl, "uuid"));
	attrs = json_object_get(tmpl, "attrs");
	if (!name || !uuid_str || !json_is_array(attrs)) {
		fprintf(stderr, "error: template needs 'name', 'uuid' and 'attrs'\n");
		goto out;
	}
	if (uuid_parse(uuid_str, uuid)) {
		fprintf(stderr, "error: invalid uuid '%s'\n", uuid_str);
		goto out;
	}
	schema = sos_schema_create(name, uuid);
	if (!schema) {
		fprintf(stderr, "error %d: sos_schema_create\n", errno);
		goto out;
	}
	json_array_foreach(attrs, i, attr) {
		const char *aname = json_string_value(json_object_get(attr, "name"));
		const char *tname = json_string_value(json_object_get(attr, "type"));
		sos_type_t type;
		if (!aname || !tname || lookup_type(tname, &type)) {
			fprintf(stderr, "error: bad attribute %zu in template\n", i);
			goto err;
		}
		if (type == SOS_TYPE_JOIN) {
			char *join_list[MAX_JOIN_ATTRS];
			json_t *jattrs = json_object_get(attr, "join_attrs");
			size_t j, count = json_array_size(jattrs);
			if (!count || count > MAX_JOIN_ATTRS) {
				fprintf(stderr, "error: join '%s' needs 1..%d join_attrs\n",
					aname, MAX_JOIN_ATTRS);
				goto err;
			}
			for (j = 0; j < count; j++)
				join_list[j] = (char *)json_string_value(json_array_get(jattrs, j));
			rc = sos_schema_attr_add(schema, aname, type, count, join_list);
		} else {
			rc = sos_schema_attr_add(schema, aname, type, (size_t)0);
		}
		if (rc) {
			fprintf(stderr, "error %d adding attribute '%s'\n", rc, aname);
			goto err;
		}
		json_t *idx = json_object_get(attr, "index");
		if (!idx)
			continue;
		const char *idx_type = json_string_value(json_object_get(idx, "type"));
		const char *idx_key = json_string_value(json_object_get(idx, "key"));
		const char *idx_args = json_string_value(json_object_get(idx, "args"));
		rc = sos_schema_index_add(schema, aname);
		if (!rc)
			rc = sos_schema_index_modify(schema, aname,
						     idx_type ? idx_type : "BXTREE",
						     idx_key, idx_args);
		if (rc) {
			fprintf(stderr, "error %d indexing attribute '%s'\n", rc, aname);
			goto err;
		}
	}
	goto out;
err:
	sos_schema_free(schema);
	schema = NULL;
out:
	json_decref(tmpl);
	return schema;
}

static sos_schema_t get_schema(sos_t sos, const char *template_path)
{
	json_error_t jerr;
	json_t *tmpl = json_load_file(template_path, 0, &jerr);
	if (!tmpl) {
		fprintf(stderr, "error: %s:%d: %s\n", template_path, jerr.line, jerr.text);
		return NULL;
	}
	char *name = strdup(json_string_value(json_object_get(tmpl, "name")));
	json_decref(tmpl);

	sos_schema_t schema = sos_schema_by_name(sos, name);
	if (!schema) {
		sos_schema_t new_schema = schema_from_template(template_path);
		if (!new_schema)
			goto out;
		int rc = sos_schema_add(sos, new_schema);
		if (rc) {
			fprintf(stderr, "error %d adding schema '%s'\n", rc, name);
			goto out;
		}
		schema = sos_schema_by_name(sos, name);
	}
out:
	free(name);
	return schema;
}

static inline void set_u64(sos_obj_t obj, sos_attr_t attr, uint64_t v)
{
	struct sos_value_s v_;
	sos_value_t val = sos_value_init(&v_, obj, attr);
	val->data->prim.uint64_ = v;
	sos_value_put(val);
}

static inline void set_double(sos_obj_t obj, sos_attr_t attr, double v)
{
	struct sos_value_s v_;
	sos_value_t val = sos_value_init(&v_, obj, attr);
	val->data->prim.double_ = v;
	sos_value_put(val);
}

static inline void set_ts(sos_obj_t obj, sos_attr_t attr, uint64_t secs)
{
	struct sos_value_s v_;
	sos_value_t val = sos_value_init(&v_, obj, attr);
	val->data->prim.timestamp_.tv.tv_sec = (uint32_t)secs;
	val->data->prim.timestamp_.tv.tv_usec = 0;
	sos_value_put(val);
}

static sos_attr_t must_attr(sos_schema_t schema, const char *name)
{
	sos_attr_t attr = sos_schema_attr_by_name(schema, name);
	if (!attr) {
		fprintf(stderr, "error: schema has no attribute '%s'\n", name);
		exit(1);
	}
	return attr;
}

int main(int argc, char **argv)
{
	struct opts o = {
		.backend = "mmos", .node_index = -1, .components = 16,
		.samples = 5000, .t0 = 1700000000, .interval = 1,
		.job_len = 500, .batch = 10000, .rate = 0,
	};
	static struct option long_opts[] = {
		{ "path", required_argument, 0, 'p' },
		{ "template", required_argument, 0, 'T' },
		{ "node-index", required_argument, 0, 'n' },
		{ "components", required_argument, 0, 'c' },
		{ "samples", required_argument, 0, 's' },
		{ "t0", required_argument, 0, 't' },
		{ "interval", required_argument, 0, 'i' },
		{ "job-len", required_argument, 0, 'j' },
		{ "batch", required_argument, 0, 'b' },
		{ "rate", required_argument, 0, 'r' },
		{ "storage", required_argument, 0, 'B' },
		{ 0, 0, 0, 0 }
	};
	int opt;
	while ((opt = getopt_long(argc, argv, "", long_opts, NULL)) != -1) {
		switch (opt) {
		case 'p': o.path = optarg; break;
		case 'T': o.template_path = optarg; break;
		case 'n': o.node_index = atoi(optarg); break;
		case 'c': o.components = strtoull(optarg, NULL, 0); break;
		case 's': o.samples = strtoull(optarg, NULL, 0); break;
		case 't': o.t0 = strtoull(optarg, NULL, 0); break;
		case 'i': o.interval = strtoull(optarg, NULL, 0); break;
		case 'j': o.job_len = strtoull(optarg, NULL, 0); break;
		case 'b': o.batch = strtoull(optarg, NULL, 0); break;
		case 'r': o.rate = strtod(optarg, NULL); break;
		case 'B': o.backend = optarg; break;
		default: usage(argv[0]);
		}
	}
	if (!o.path || !o.template_path || o.node_index < 0 ||
	    !o.batch || !o.job_len || !o.interval)
		usage(argv[0]);

	sos_perm_t perm = SOS_PERM_CREAT | SOS_PERM_RW;
	perm |= (0 == strcasecmp(o.backend, "lsos")) ? SOS_BE_LSOS : SOS_BE_MMOS;

	double t_start = now_s();
	sos_t sos = sos_container_open(o.path, perm, 0660);
	if (!sos) {
		fprintf(stderr, "error %d opening container '%s'\n", errno, o.path);
		return 1;
	}
	sos_schema_t schema = get_schema(sos, o.template_path);
	if (!schema)
		return 1;
	sos_attr_t a_ts = must_attr(schema, "timestamp");
	sos_attr_t a_comp = must_attr(schema, "component_id");
	sos_attr_t a_job = must_attr(schema, "job_id");
	sos_attr_t a_seq = must_attr(schema, "seq");
	sos_attr_t a_mem = must_attr(schema, "mem_free");
	sos_attr_t a_load = must_attr(schema, "load");

	uint64_t base = (uint64_t)(o.node_index + 1) * 1000;
	uint64_t written = 0, in_batch = 0, errors = 0;
	uint64_t seq, c;
	int rc;
	double t_write = now_s();

	rc = sos_begin_x(sos);
	if (rc) {
		fprintf(stderr, "error %d beginning transaction\n", rc);
		return 1;
	}
	for (seq = 0; seq < o.samples; seq++) {
		for (c = 0; c < o.components; c++) {
			uint64_t comp = base + c;
			sos_obj_t obj = sos_obj_new(schema);
			if (!obj) {
				fprintf(stderr, "error %d allocating object\n", errno);
				return 1;
			}
			set_ts(obj, a_ts, o.t0 + seq * o.interval);
			set_u64(obj, a_comp, comp);
			set_u64(obj, a_job, 1 + seq / o.job_len);
			set_u64(obj, a_seq, seq);
			set_u64(obj, a_mem, comp * 1000000 + seq);
			set_double(obj, a_load, (double)(comp % 1000) + seq * 0.25);
			rc = sos_obj_index(obj);
			sos_obj_put(obj);
			if (rc) {
				errors++;
				if (errors <= 10)
					fprintf(stderr, "error %d indexing record %" PRIu64 "\n",
						rc, written);
			}
			written++;
			if (++in_batch == o.batch) {
				sos_container_commit(sos, SOS_COMMIT_ASYNC);
				sos_end_x(sos);
				in_batch = 0;
				sos_begin_x(sos);
			}
			if (o.rate > 0) {
				double due = t_write + written / o.rate;
				double wait = due - now_s();
				if (wait > 0) {
					struct timespec ts = {
						.tv_sec = (time_t)wait,
						.tv_nsec = (long)((wait - (time_t)wait) * 1e9)
					};
					nanosleep(&ts, NULL);
				}
			}
		}
	}
	sos_container_commit(sos, SOS_COMMIT_ASYNC);
	sos_end_x(sos);
	double t_indexed = now_s();
	sos_container_close(sos, SOS_COMMIT_SYNC);
	double t_end = now_s();

	/* One JSON line, parsed by bin/node.sh and bin/run.sh. */
	printf("{\"writer\": \"c\", \"container\": \"%s\", \"backend\": \"%s\", "
	       "\"node_index\": %d, \"records\": %" PRIu64 ", \"errors\": %" PRIu64 ", "
	       "\"batch\": %" PRIu64 ", \"rate\": %.0f, "
	       "\"open_s\": %.3f, \"write_s\": %.3f, \"close_sync_s\": %.3f, "
	       "\"total_s\": %.3f, \"records_per_s\": %.0f}\n",
	       o.path, o.backend, o.node_index, written, errors, o.batch, o.rate,
	       t_write - t_start, t_indexed - t_write, t_end - t_indexed,
	       t_end - t_start, written / (t_end - t_write));
	return errors ? 1 : 0;
}
