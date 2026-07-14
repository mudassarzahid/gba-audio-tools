/* webfoot: the Webfoot Technologies GBA sample-tracker engine (Legacy of
 * Goku II, Buu's Fury, DBGT: Transformation, and Webfoot's other GBA
 * titles), playing .wbf files produced by the extractor
 * (extractor/extractor_webfoot.c). Portable C99, no deps, no allocation
 * beyond one struct.
 *
 * The format and playback semantics were reverse-engineered from the
 * original driver (docs/webfoot-format.md) and validated against mGBA
 * hardware emulation. Sequencing is integer-exact to the original
 * hardware: the driver mixes at 2^24/1050 Hz with 40000/tempo samples per
 * tick, so at our 32768 Hz output one tick lasts exactly spt*1050/512
 * samples, tracked with a /512 remainder accumulator, no drift.
 *
 * Output is mono (the driver's pan effect never changes the L+R sum);
 * callers duplicate it for stereo. A song plays its order list and loops
 * at its fx2 loop point (or wraps at the order-list end); wbf_loops()
 * counts boundary crossings so the caller can apply the loop-count/fade
 * policy.
 *
 * The .wbf image is borrowed, not copied: it must outlive the engine.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
#ifndef GBA_AUDIO_WEBFOOT_H
#define GBA_AUDIO_WEBFOOT_H

#include <stdint.h>

#define WBF_SAMPLE_RATE 32768
#define WBF_CHANNELS 15

typedef struct WbfEngine WbfEngine;

/* Opaque engine, caller-allocated via wbf_engine_size() or static. */
unsigned wbf_engine_size(void);

/* Validate the image and bind it. Returns NULL + *err on failure. */
WbfEngine *wbf_open(void *mem, const void *data, long size, const char **err);

int  wbf_song_count(const WbfEngine *w);
void wbf_start(WbfEngine *w, int song);       /* select + reset */

/* Render n mono int16 samples at 32768 Hz. */
void wbf_render(WbfEngine *w, int16_t *out, int n);

/* Number of times playback crossed the song's loop point. */
int  wbf_loops(const WbfEngine *w);

#endif
