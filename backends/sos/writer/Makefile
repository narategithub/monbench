# Build the C writer against an installed SOS.
#   make SOS_PREFIX=/opt/ovis
# If the libraries are in lib64 or have other names, override SOS_LIBDIR or
# SOS_LIBS, e.g.  make SOS_LIBDIR=/opt/ovis/lib64
#
# sos_write.c no longer parses the dataset JSON or resolves attribute
# defaults itself -- that logic lives once in core/dataset.c (+
# core/valuegen.c), which this links against. See sos_write.c's header
# comment and design/value-generation.md.
SOS_PREFIX ?= /opt/ovis
SOS_INCDIR ?= $(SOS_PREFIX)/include
SOS_LIBDIR ?= $(SOS_PREFIX)/lib
SOS_LIBS   ?= -lsos -lods

CORE_DIR ?= ../../../core

CFLAGS  ?= -O2 -g -Wall
CFLAGS  += -I$(SOS_INCDIR) -I$(CORE_DIR)
LDFLAGS += -L$(SOS_LIBDIR) -Wl,-rpath,$(SOS_LIBDIR)
LDLIBS  += $(SOS_LIBS) -ljansson -luuid

sos_write: sos_write.c $(CORE_DIR)/dataset.c $(CORE_DIR)/valuegen.c \
           $(CORE_DIR)/dataset.h $(CORE_DIR)/valuegen.h
	$(CC) $(CFLAGS) -o $@ sos_write.c $(CORE_DIR)/dataset.c $(CORE_DIR)/valuegen.c \
		$(LDFLAGS) $(LDLIBS)

clean:
	rm -f sos_write

.PHONY: clean
