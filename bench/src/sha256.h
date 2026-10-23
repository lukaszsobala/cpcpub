// SHA-256 of a file, or of a block of memory, for identifying the binary a
// result came from.
#ifndef CPCPUB_SHA256_H
#define CPCPUB_SHA256_H

#include <stddef.h>
#include <stdint.h>

// Hashes `path` and writes 64 lowercase hex digits plus a NUL into `out_hex`.
// Returns 0 on success, -1 if the file could not be read (out_hex is then an
// empty string). No allocation, no state kept between calls.
int sha256_file(const char *path, char out_hex[65]);

// The same over `n` bytes at `p`: a microcontroller's firmware, which is not a
// file but sits in flash, readable where it is mapped.
void sha256_mem(const void *p, size_t n, char out_hex[65]);

// The same a piece at a time, for data read in chunks: init, update as often
// as there is more, final for the digest. The context is the caller's.
typedef struct {
    uint32_t h[8];
    uint64_t bytes;             // total fed in, for the length block
    unsigned char buf[64];
    size_t used;                // bytes sitting in buf
} sha256_ctx;

void sha256_init(sha256_ctx *c);
void sha256_update(sha256_ctx *c, const void *p, size_t n);
void sha256_final(sha256_ctx *c, char out_hex[65]);

#endif
