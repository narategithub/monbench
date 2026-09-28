# Build the C writer against an installed SOS.
#   make SOS_PREFIX=/opt/ovis
# If the libraries are in lib64 or have other names, override SOS_LIBDIR or
# SOS_LIBS, e.g.  make SOS_LIBDIR=/opt/ovis/lib64
SOS_PREFIX ?= /opt/ovis
SOS_INCDIR ?= $(SOS_PREFIX)/include
SOS_LIBDIR ?= $(SOS_PREFIX)/lib
SOS_LIBS   ?= -lsos -lods

CFLAGS  ?= -O2 -g -Wall
CFLAGS  += -I$(SOS_INCDIR)
LDFLAGS += -L$(SOS_LIBDIR) -Wl,-rpath,$(SOS_LIBDIR)
LDLIBS  += $(SOS_LIBS) -ljansson -luuid

sos_write: sos_write.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS) $(LDLIBS)

clean:
	rm -f sos_write

.PHONY: clean
