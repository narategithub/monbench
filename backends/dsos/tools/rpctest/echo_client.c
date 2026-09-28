/*
 * echo_client HOST [SIZE...]
 *
 * Sends buffers of each SIZE (bytes; suffix k or m allowed) to the echo
 * server on HOST over TCP and checks that each comes back unchanged.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "echo.h"

static size_t parse_size(const char *s)
{
	char *end;
	double v = strtod(s, &end);
	if (*end == 'k' || *end == 'K') v *= 1024;
	if (*end == 'm' || *end == 'M') v *= 1024 * 1024;
	return (size_t)v;
}

static double now_s(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec / 1e9;
}

int main(int argc, char **argv)
{
	static const char *defaults[] = { "0", "100", "4k", "64k", "1m", "16m" };
	const char **sizes = (const char **)&argv[2];
	int nsizes = argc - 2, i, failed = 0;

	if (argc < 2) {
		fprintf(stderr, "usage: %s HOST [SIZE...]\n", argv[0]);
		return 2;
	}
	if (nsizes == 0) {
		sizes = defaults;
		nsizes = sizeof(defaults) / sizeof(defaults[0]);
	}

	CLIENT *clnt = clnt_create(argv[1], ECHOPROG, ECHOVERS, "tcp");
	if (!clnt) {
		clnt_pcreateerror(argv[1]);
		return 1;
	}
	struct timeval timeout = { 60, 0 };
	clnt_control(clnt, CLSET_TIMEOUT, (char *)&timeout);
	printf("connected to %s over tcp\n", argv[1]);

	for (i = 0; i < nsizes; i++) {
		size_t n = parse_size(sizes[i]), j;
		echo_buf arg = { .echo_buf_len = n, .echo_buf_val = malloc(n ? n : 1) };
		for (j = 0; j < n; j++)
			arg.echo_buf_val[j] = (char)(j * 31 + 7);

		double t = now_s();
		echo_buf *res = echo_1(&arg, clnt);
		double dt = now_s() - t;

		if (!res) {
			printf("  %10zu bytes: FAIL  %s\n", n, clnt_sperror(clnt, "call"));
			failed = 1;
		} else if (res->echo_buf_len != n ||
			   (n && memcmp(res->echo_buf_val, arg.echo_buf_val, n))) {
			printf("  %10zu bytes: FAIL  reply differs (got %u bytes)\n",
			       n, res->echo_buf_len);
			failed = 1;
		} else {
			printf("  %10zu bytes: ok    %.3f ms\n", n, dt * 1e3);
		}
		if (res)
			xdr_free((xdrproc_t)xdr_echo_buf, (char *)res);
		free(arg.echo_buf_val);
	}
	clnt_destroy(clnt);
	return failed;
}
