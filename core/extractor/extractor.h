#ifndef EXTRACTOR_H
#define EXTRACTOR_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct Extractor Extractor;

/* Returns a new extractor context given a ROM, or NULL if the ROM is not a
 * supported format (MP2K or Webfoot). */
Extractor* extractor_new(const uint8_t *rom, uint32_t rom_size, uint32_t mp2k_table_pos);
void extractor_free(Extractor *e);

#define EXTRACTOR_FMT_MP2K 0
#define EXTRACTOR_FMT_WEBFOOT 1

/* Returns the detected format (EXTRACTOR_FMT_MP2K or EXTRACTOR_FMT_WEBFOOT). */
int extractor_format(Extractor *e);

/* Returns the total number of songs found in the ROM. */
int extractor_song_count(Extractor *e);

/* Returns the instrument count (Webfoot; 0 where it doesn't apply). */
int extractor_inst_count(Extractor *e);

/* Per-song metadata (Webfoot; 0 for formats that don't provide it, since
 * MP2K songs are described by the scanner instead). idx is in [0, count). */
uint32_t extractor_song_duration_ms(Extractor *e, int idx);
int extractor_song_channels(Extractor *e, int idx);
int extractor_song_loops(Extractor *e, int idx);

/* Extracts the given song indices into a .pak (MP2K) or .wbf (Webfoot) buffer.
 * On success returns the buffer size and sets *out_buffer to the newly
 * allocated buffer (free() it). On failure returns 0 and *out_buffer = NULL. */
uint32_t extractor_build(Extractor *e, const int *song_indices, int num_songs, uint8_t **out_buffer);

#ifdef __cplusplus
}
#endif

#endif /* EXTRACTOR_H */
