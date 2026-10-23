/*
 * valuegen.c -- generic, hash-based value generation and verification.
 * See valuegen.h and design/value-generation.md.
 */
#include "valuegen.h"
#include <string.h>

#define FNV_OFFSET ((uint64_t)0xcbf29ce484222325ULL)
#define FNV_PRIME  ((uint64_t)0x100000001b3ULL)

uint64_t vg_fnv1a64(const uint8_t *data, size_t len)
{
	uint64_t h = FNV_OFFSET;
	size_t i;
	for (i = 0; i < len; i++) {
		h ^= data[i];
		h *= FNV_PRIME;		/* uint64_t multiply wraps mod 2^64, same as Python's & MASK64 */
	}
	return h;
}

void vg_le64(uint64_t x, uint8_t out[8])
{
	int i;
	for (i = 0; i < 8; i++)
		out[i] = (uint8_t)(x >> (8 * i));
}

uint64_t vg_digest(const uint8_t *key, size_t keylen, uint64_t i)
{
	uint8_t buf[VG_KEY_BUF_MAX + 8];
	if (keylen > VG_KEY_BUF_MAX)
		keylen = VG_KEY_BUF_MAX;	/* defensive; callers keep keys within bound */
	memcpy(buf, key, keylen);
	vg_le64(i, buf + keylen);
	return vg_fnv1a64(buf, keylen + 8);
}

/* Appends name (NUL-terminated, <= VG_MAX_NAME bytes) and a 0x00
 * separator to buf at *pos; returns 0 on success, -1 if name is too
 * long. */
static int append_name(uint8_t *buf, size_t *pos, const char *name)
{
	size_t len = strlen(name);
	if (len > VG_MAX_NAME)
		return -1;
	memcpy(buf + *pos, name, len);
	*pos += len;
	buf[(*pos)++] = 0x00;
	return 0;
}

size_t vg_key_bytes(uint8_t *buf,
                     const char *dataset_name, const char *metric_name,
                     const char *list_name,
                     uint64_t node_id, uint64_t entry_index,
                     uint64_t job_id, uint64_t seq, uint64_t stride)
{
	size_t pos = 0;
	uint64_t bucket = stride ? seq / stride : seq;

	if (append_name(buf, &pos, dataset_name) ||
	    append_name(buf, &pos, metric_name) ||
	    append_name(buf, &pos, list_name))
		return 0;

	vg_le64(node_id, buf + pos);     pos += 8;
	vg_le64(entry_index, buf + pos); pos += 8;
	vg_le64(job_id, buf + pos);      pos += 8;
	vg_le64(bucket, buf + pos);      pos += 8;
	return pos;
}

size_t vg_length_key_bytes(uint8_t *buf,
                            const char *dataset_name, const char *list_name,
                            uint64_t node_id, uint64_t job_id, uint64_t seq,
                            uint64_t length_stride)
{
	size_t pos = 0;
	uint64_t bucket = length_stride ? seq / length_stride : seq;

	if (append_name(buf, &pos, dataset_name) ||
	    append_name(buf, &pos, "__length__") ||
	    append_name(buf, &pos, list_name))
		return 0;

	vg_le64(node_id, buf + pos); pos += 8;
	vg_le64(job_id, buf + pos);  pos += 8;
	vg_le64(bucket, buf + pos);  pos += 8;
	return pos;
}

uint64_t vg_decode_uint_range(uint64_t d0, uint64_t lo, uint64_t hi)
{
	uint64_t span = hi - lo + 1;	/* wraps to 0 when lo=0, hi=UINT64_MAX */
	if (span == 0)
		return lo + d0;		/* full 64-bit range: d0 already uniform over it */
	return lo + (d0 % span);
}

int64_t vg_decode_int_range(uint64_t d0, int64_t lo, int64_t hi)
{
	uint64_t span = (uint64_t)(hi - lo) + 1;	/* wraps to 0 for the full int64 range */
	if (span == 0)
		return (int64_t)((uint64_t)lo + d0);	/* full range: d0 already uniform over it */
	return lo + (int64_t)(d0 % span);
}

void vg_decode_string(const uint8_t *key, size_t keylen,
                       size_t length, const char *alphabet, size_t alphabet_len,
                       char *out)
{
	size_t filled = 0, i = 0;
	while (filled < length) {
		uint64_t d = vg_digest(key, keylen, i);
		uint8_t block[8];
		size_t b;
		vg_le64(d, block);
		for (b = 0; b < 8 && filled < length; b++)
			out[filled++] = alphabet[block[b] % alphabet_len];
		i++;
	}
}

double vg_decode_double(uint64_t d0, uint64_t lo, uint64_t hi,
                         double scale, double offset)
{
	uint64_t base_value = vg_decode_uint_range(d0, lo, hi);
	return (double)base_value / scale + offset;
}

uint64_t vg_evaluate_uint(const char *dataset_name, const char *metric_name,
                           const char *list_name, uint64_t node_id,
                           uint64_t entry_index, uint64_t job_id,
                           uint64_t seq, uint64_t stride,
                           uint64_t range_lo, uint64_t range_hi)
{
	uint8_t key[VG_KEY_BUF_MAX];
	size_t keylen = vg_key_bytes(key, dataset_name, metric_name, list_name,
	                             node_id, entry_index, job_id, seq, stride);
	uint64_t d0 = vg_digest(key, keylen, 0);
	return vg_decode_uint_range(d0, range_lo, range_hi);
}

int64_t vg_evaluate_int(const char *dataset_name, const char *metric_name,
                          const char *list_name, uint64_t node_id,
                          uint64_t entry_index, uint64_t job_id,
                          uint64_t seq, uint64_t stride,
                          int64_t range_lo, int64_t range_hi)
{
	uint8_t key[VG_KEY_BUF_MAX];
	size_t keylen = vg_key_bytes(key, dataset_name, metric_name, list_name,
	                             node_id, entry_index, job_id, seq, stride);
	uint64_t d0 = vg_digest(key, keylen, 0);
	return vg_decode_int_range(d0, range_lo, range_hi);
}

double vg_evaluate_double(const char *dataset_name, const char *metric_name,
                           const char *list_name, uint64_t node_id,
                           uint64_t entry_index, uint64_t job_id,
                           uint64_t seq, uint64_t stride,
                           uint64_t base_lo, uint64_t base_hi,
                           double scale, double offset)
{
	uint8_t key[VG_KEY_BUF_MAX];
	size_t keylen = vg_key_bytes(key, dataset_name, metric_name, list_name,
	                             node_id, entry_index, job_id, seq, stride);
	uint64_t d0 = vg_digest(key, keylen, 0);
	return vg_decode_double(d0, base_lo, base_hi, scale, offset);
}

void vg_evaluate_string(const char *dataset_name, const char *metric_name,
                         const char *list_name, uint64_t node_id,
                         uint64_t entry_index, uint64_t job_id,
                         uint64_t seq, uint64_t stride,
                         size_t length, const char *alphabet, size_t alphabet_len,
                         char *out)
{
	uint8_t key[VG_KEY_BUF_MAX];
	size_t keylen = vg_key_bytes(key, dataset_name, metric_name, list_name,
	                             node_id, entry_index, job_id, seq, stride);
	vg_decode_string(key, keylen, length, alphabet, alphabet_len, out);
}

/* ----------------------------------------------------------------- defaults
 *
 * Mirrors valuegen.py's _RANGE_BY_TYPE table exactly. Table-driven so the
 * ranges can't drift out of sync between the uint/int lookup functions.
 */

struct uint_range { const char *type; uint64_t lo, hi; };
struct int_range  { const char *type; int64_t lo, hi; };

static const struct uint_range UINT_RANGES[] = {
	{ "uint8",  0, 255 },
	{ "uint16", 0, 65535 },
	{ "uint32", 0, 4294967295ULL },
	{ "uint64", 0, UINT64_MAX },
};
#define N_UINT_RANGES (sizeof(UINT_RANGES) / sizeof(UINT_RANGES[0]))

static const struct int_range INT_RANGES[] = {
	{ "int8",  -128, 127 },
	{ "int16", -32768, 32767 },
	{ "int32", -2147483648LL, 2147483647LL },
	{ "int64", INT64_MIN, INT64_MAX },
};
#define N_INT_RANGES (sizeof(INT_RANGES) / sizeof(INT_RANGES[0]))

int vg_default_range_uint(const char *type, uint64_t *lo, uint64_t *hi)
{
	size_t i;
	for (i = 0; i < N_UINT_RANGES; i++) {
		if (strcmp(type, UINT_RANGES[i].type) == 0) {
			*lo = UINT_RANGES[i].lo;
			*hi = UINT_RANGES[i].hi;
			return 0;
		}
	}
	return -1;
}

int vg_default_range_int(const char *type, int64_t *lo, int64_t *hi)
{
	size_t i;
	for (i = 0; i < N_INT_RANGES; i++) {
		if (strcmp(type, INT_RANGES[i].type) == 0) {
			*lo = INT_RANGES[i].lo;
			*hi = INT_RANGES[i].hi;
			return 0;
		}
	}
	return -1;
}

int vg_is_uint_type(const char *type)
{
	size_t i;
	for (i = 0; i < N_UINT_RANGES; i++)
		if (strcmp(type, UINT_RANGES[i].type) == 0)
			return 1;
	return 0;
}

int vg_is_int_type(const char *type)
{
	size_t i;
	for (i = 0; i < N_INT_RANGES; i++)
		if (strcmp(type, INT_RANGES[i].type) == 0)
			return 1;
	return 0;
}

uint64_t vg_list_length(const char *dataset_name, const char *list_name,
                         uint64_t node_id, uint64_t job_id, uint64_t seq,
                         uint64_t length_stride, uint64_t lo, uint64_t hi)
{
	uint8_t key[VG_KEY_BUF_MAX];
	size_t keylen = vg_length_key_bytes(key, dataset_name, list_name,
	                                    node_id, job_id, seq, length_stride);
	uint64_t h = vg_fnv1a64(key, keylen);
	uint64_t span = hi - lo + 1;
	if (span == 0)
		return lo + h;
	return lo + (h % span);
}
