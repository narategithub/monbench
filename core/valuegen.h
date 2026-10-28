/*
 * valuegen.h -- generic, hash-based value generation and verification.
 *
 * Bit-identical C port of valuegen.py. See design/value-generation.md for
 * the design (key construction Sec 4.1, hash Sec 4.2, decode rules Sec
 * 4.3, list-attribute generation Sec 6.3) and valuegen.py's docstring
 * for the Python API this mirrors function-for-function.
 *
 * Any change to the hash, key construction, or decode rules must be
 * mirrored in valuegen.py -- valuegen_cross_check exercises both
 * implementations over the same inputs and must stay passing.
 */
#ifndef MONBENCH_VALUEGEN_H
#define MONBENCH_VALUEGEN_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Longest dataset/metric/list name this implementation accepts. The key
 * buffer is sized from these at compile time; see VG_KEY_BUF_MAX. */
#define VG_MAX_NAME 128
#define VG_KEY_BUF_MAX (3 * (VG_MAX_NAME + 1) + 4 * 8)

/* ----------------------------------------------------------------- hashing */

uint64_t vg_fnv1a64(const uint8_t *data, size_t len);

/* Writes 8 bytes, little-endian, to out. */
void vg_le64(uint64_t x, uint8_t out[8]);

/* digest(K, i) = fnv1a64(K || le64(i)) */
uint64_t vg_digest(const uint8_t *key, size_t keylen, uint64_t i);

/* ----------------------------------------------------------------- key construction */

/*
 * Build K (design doc Sec 4.1) into buf (must be >= VG_KEY_BUF_MAX bytes).
 * list_name may be "" (sentinel, node-level attribute); metric_name may
 * be "" (sentinel, a bare-scalar list entry). stride=1 means the value
 * changes every sample. Returns the key length, or 0 if a name exceeds
 * VG_MAX_NAME.
 */
size_t vg_key_bytes(uint8_t *buf,
                     const char *dataset_name, const char *metric_name,
                     const char *list_name,
                     uint64_t node_id, uint64_t entry_index,
                     uint64_t job_id, uint64_t seq, uint64_t stride);

/*
 * Key for a list attribute's per-sample length (design doc Sec 6.3), using
 * the reserved metric name "__length__". Returns the key length, or 0 if a
 * name exceeds VG_MAX_NAME.
 */
size_t vg_length_key_bytes(uint8_t *buf,
                            const char *dataset_name, const char *list_name,
                            uint64_t node_id, uint64_t job_id, uint64_t seq,
                            uint64_t length_stride);

/* ----------------------------------------------------------------- decoding */

/* value = lo + (d0 mod (hi - lo + 1)), unsigned arithmetic; handles the
 * full-uint64-range case (lo=0, hi=UINT64_MAX) without overflow. */
uint64_t vg_decode_uint_range(uint64_t d0, uint64_t lo, uint64_t hi);

/* Same form, for a signed range (e.g. int8 default [-128, 127]). */
int64_t vg_decode_int_range(uint64_t d0, int64_t lo, int64_t hi);

/*
 * String decode (design doc Sec 4.3): fills out[0..length) from alphabet
 * (alphabet_len symbols), chaining digest blocks. out is NOT
 * NUL-terminated by this call unless the caller sizes it that way; pass
 * length = desired string length, out buffer >= length bytes.
 */
void vg_decode_string(const uint8_t *key, size_t keylen,
                       size_t length, const char *alphabet, size_t alphabet_len,
                       char *out);

/*
 * double decode (design doc Sec 4.3, "option b"): base_value is an
 * unsigned integer decode over [lo, hi], then value = base_value / scale
 * + offset.
 */
double vg_decode_double(uint64_t d0, uint64_t lo, uint64_t hi,
                         double scale, double offset);

/* ----------------------------------------------------------------- evaluator */

/*
 * Evaluate one unsigned-integer-typed attribute (uint8/16/32/64); the
 * caller passes that type's range (its own default_range or an
 * attribute-level override). vg_evaluate_int below is the signed form.
 */
uint64_t vg_evaluate_uint(const char *dataset_name, const char *metric_name,
                           const char *list_name, uint64_t node_id,
                           uint64_t entry_index, uint64_t job_id,
                           uint64_t seq, uint64_t stride,
                           uint64_t range_lo, uint64_t range_hi);

int64_t vg_evaluate_int(const char *dataset_name, const char *metric_name,
                          const char *list_name, uint64_t node_id,
                          uint64_t entry_index, uint64_t job_id,
                          uint64_t seq, uint64_t stride,
                          int64_t range_lo, int64_t range_hi);

double vg_evaluate_double(const char *dataset_name, const char *metric_name,
                           const char *list_name, uint64_t node_id,
                           uint64_t entry_index, uint64_t job_id,
                           uint64_t seq, uint64_t stride,
                           uint64_t base_lo, uint64_t base_hi,
                           double scale, double offset);

/* out must be >= length bytes; not NUL-terminated by this call. */
void vg_evaluate_string(const char *dataset_name, const char *metric_name,
                         const char *list_name, uint64_t node_id,
                         uint64_t entry_index, uint64_t job_id,
                         uint64_t seq, uint64_t stride,
                         size_t length, const char *alphabet, size_t alphabet_len,
                         char *out);

/*
 * Deterministic, per-sample length for one list attribute (design doc
 * Sec 6.3). lo == hi gives a fixed length.
 */
uint64_t vg_list_length(const char *dataset_name, const char *list_name,
                         uint64_t node_id, uint64_t job_id, uint64_t seq,
                         uint64_t length_stride, uint64_t lo, uint64_t hi);

/* A very large length_stride that effectively fixes a list's length for
 * any realistic run length -- the default when a dataset's JSON omits it
 * (design doc Sec 7, item 2). Mirrors valuegen.py's DEFAULT_LENGTH_STRIDE. */
#define VG_DEFAULT_LENGTH_STRIDE ((uint64_t)1000000000000000ULL)

/* ----------------------------------------------------------------- defaults
 *
 * These mirror valuegen.py's _RANGE_BY_TYPE / default_range() and the
 * DEFAULT_* constants exactly (design doc Sec 4.3's type table). They
 * exist so that any C code resolving a dataset JSON attribute's decode
 * parameters -- today that's core/dataset.c -- applies the identical
 * default-resolution rules as valuegen.py's decode(), rather than each
 * reimplementing this table. valuegen.c itself never calls these; they
 * are for a caller translating a JSON attribute declaration into the
 * explicit lo/hi/scale/offset/length/alphabet the vg_evaluate_* /
 * vg_decode_* functions above take.
 */

/* Fills lo/hi for a known unsigned type name ("uint8".."uint64").
 * Returns 0 on success, -1 if type is not an unsigned type. */
int vg_default_range_uint(const char *type, uint64_t *lo, uint64_t *hi);

/* Fills lo/hi for a known signed type name ("int8".."int64").
 * Returns 0 on success, -1 if type is not a signed type. */
int vg_default_range_int(const char *type, int64_t *lo, int64_t *hi);

/* Returns 1 if type is one of the unsigned integer types, else 0. */
int vg_is_uint_type(const char *type);
/* Returns 1 if type is one of the signed integer types, else 0. */
int vg_is_int_type(const char *type);

/* double's own default base range (Sec 4.3's table entry, used only when
 * an attribute declares neither "base" nor "range" -- NOT the same as
 * vg_default_range_uint("uint64", ...), which is far too wide). */
#define VG_DEFAULT_DOUBLE_RANGE_LO ((uint64_t)0)
#define VG_DEFAULT_DOUBLE_RANGE_HI ((uint64_t)9999)
#define VG_DEFAULT_DOUBLE_SCALE  (100.0)
#define VG_DEFAULT_DOUBLE_OFFSET (0.0)

#define VG_DEFAULT_STRING_LENGTH 8
#define VG_DEFAULT_STRING_ALPHABET "abcdefghijklmnopqrstuvwxyz0123456789"

#ifdef __cplusplus
}
#endif

#endif /* MONBENCH_VALUEGEN_H */
