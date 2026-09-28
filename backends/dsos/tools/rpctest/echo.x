/*
 * Minimal ONC RPC program for testing RPC between two nodes without SOS.
 * The client sends a buffer and the server returns it unchanged.
 */
typedef opaque echo_buf<>;

program ECHOPROG {
	version ECHOVERS {
		echo_buf ECHO(echo_buf) = 1;
	} = 1;
} = 0x20000123;		/* 536871203, in the range reserved for local use */
