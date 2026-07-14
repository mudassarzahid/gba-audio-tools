#ifndef EXTRACTOR_INTERNAL_H
#define EXTRACTOR_INTERNAL_H

#include "extractor.h"

struct Extractor {
    const uint8_t *rom;
    uint32_t rom_size;
    int format;
    uint32_t table_pos;
    int inst_count;          /* instruments in the ROM (0 if N/A) */
    void *priv;
    void (*free_fn)(Extractor *e);
    int (*song_count_fn)(Extractor *e);
    void (*song_info_fn)(Extractor *e, int idx, uint32_t *dur_ms, int *channels, int *loops);
    uint32_t (*build_fn)(Extractor *e, const int *song_indices, int num_songs, uint8_t **out_buffer);
};

static inline uint32_t rd32(const uint8_t *b, uint32_t off) {
    return (uint32_t)b[off] | ((uint32_t)b[off+1] << 8) | ((uint32_t)b[off+2] << 16) | ((uint32_t)b[off+3] << 24);
}

static inline uint16_t rd16(const uint8_t *b, uint32_t off) {
    return (uint16_t)b[off] | ((uint16_t)b[off+1] << 8);
}

static inline int find_sig(const uint8_t *rom, uint32_t size, const uint8_t *sig, uint32_t sig_len) {
    if (size < sig_len) return -1;
    int first = -1;
    for (uint32_t i = 0; i <= size - sig_len; i++) {
        bool match = true;
        for (uint32_t j = 0; j < sig_len; j++) {
            if (rom[i + j] != sig[j]) { match = false; break; }
        }
        if (match) {
            if (first >= 0) return -1; /* not unique */
            first = i;
        }
    }
    return first;
}

#endif
