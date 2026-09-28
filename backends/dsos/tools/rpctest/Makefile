# Build a minimal ONC RPC echo server and client (needs rpcgen and libtirpc).
#   Ubuntu: sudo apt install rpcsvc-proto libtirpc-dev
CFLAGS += -O2 -Wall -I/usr/include/tirpc
LDLIBS += -ltirpc
GEN = echo.h echo_clnt.c echo_svc.c echo_xdr.c

all: echo_server echo_client

$(GEN): echo.x
	rpcgen echo.x

echo_server: echo_svc.c echo_server.c echo_xdr.c echo.h
	$(CC) $(CFLAGS) -o $@ echo_svc.c echo_server.c echo_xdr.c $(LDLIBS)

echo_client: echo_client.c echo_clnt.c echo_xdr.c echo.h
	$(CC) $(CFLAGS) -o $@ echo_client.c echo_clnt.c echo_xdr.c $(LDLIBS)

clean:
	rm -f echo_server echo_client $(GEN)

.PHONY: all clean
