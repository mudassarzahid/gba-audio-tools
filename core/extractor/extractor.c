#include "extractor_internal.h"
#include <stdlib.h>
#include <string.h>

/* Forward declarations for format extractors */
Extractor* mp2k_detect(const uint8_t *rom, uint32_t rom_size, uint32_t table_pos);
Extractor* webfoot_detect(const uint8_t *rom, uint32_t rom_size);

Extractor* extractor_new(const uint8_t *rom, uint32_t rom_size, uint32_t mp2k_table_pos) {
    Extractor *e = webfoot_detect(rom, rom_size);
    if (e) return e;
    e = mp2k_detect(rom, rom_size, mp2k_table_pos);
    if (e) return e;
    return NULL;
}

void extractor_free(Extractor *e) {
    if (e && e->free_fn) e->free_fn(e);
}

int extractor_format(Extractor *e) {
    return e ? e->format : -1;
}

int extractor_song_count(Extractor *e) {
    return e && e->song_count_fn ? e->song_count_fn(e) : 0;
}

int extractor_inst_count(Extractor *e) {
    return e ? e->inst_count : 0;
}

static void song_info(Extractor *e, int idx, uint32_t *d, int *c, int *l) {
    uint32_t dd = 0; int cc = 0, ll = 0;
    if (e && e->song_info_fn) e->song_info_fn(e, idx, &dd, &cc, &ll);
    if (d) *d = dd;
    if (c) *c = cc;
    if (l) *l = ll;
}

uint32_t extractor_song_duration_ms(Extractor *e, int idx) {
    uint32_t d; int c, l; song_info(e, idx, &d, &c, &l); return d;
}
int extractor_song_channels(Extractor *e, int idx) {
    uint32_t d; int c, l; song_info(e, idx, &d, &c, &l); return c;
}
int extractor_song_loops(Extractor *e, int idx) {
    uint32_t d; int c, l; song_info(e, idx, &d, &c, &l); return l;
}

uint32_t extractor_build(Extractor *e, const int *song_indices, int num_songs, uint8_t **out_buffer) {
    if (!e || !e->build_fn || !out_buffer) return 0;
    return e->build_fn(e, song_indices, num_songs, out_buffer);
}
