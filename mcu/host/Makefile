# The microcontroller front end on a desktop, for testing it: see port.c.
#
#   make -C mcu/host        # after `make -C bench`; -> mcu/host/cpcpub-mcu-host
#
# Links the measurement objects bench/ already built, so the code under test
# is the code the desktop binary runs, compiled the same way.

CC     ?= cc
CFLAGS ?= -O3 -pipe -std=c2x -Wall -Wextra -Wshadow -Wconversion -Wdouble-promotion -Wformat=2 -fno-strict-aliasing

BENCH  := ../../bench/src
SHARED := $(BENCH)/measure.o $(BENCH)/report.o $(BENCH)/sha256.o \
          $(addprefix $(BENCH)/kernels-,$(addsuffix .o,scalar-nofma vector-nofma scalar-fma vector-fma))

all: cpcpub-mcu-host

cpcpub-mcu-host: frontend.o port.o $(SHARED)
	$(CC) $(CFLAGS) -o $@ frontend.o port.o $(SHARED) -lm -pthread

frontend.o: ../frontend.c ../port.h $(BENCH)/measure.h $(BENCH)/report.h $(BENCH)/kernels.h
	$(CC) $(CFLAGS) -I$(BENCH) -I.. -DBUILD_CC='"$(CC)"' -DBUILD_FLAGS='"$(CFLAGS)"' -c -o $@ $<

port.o: port.c ../port.h $(BENCH)/measure.h
	$(CC) $(CFLAGS) -I$(BENCH) -I.. -c -o $@ $<

$(SHARED):
	@echo "build bench/ first: make -C bench" >&2; exit 1

clean:
	rm -f cpcpub-mcu-host frontend.o port.o

.PHONY: all clean
