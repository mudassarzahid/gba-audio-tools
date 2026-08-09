/* MP2K ("Sappy"/m4a) player: plays .pak files built by the extractor
 * (extractor/extractor_mp2k.c).
 *
 * Portable C99, no dependencies beyond libm. Semantics follow agbplay
 * (SequenceReader/MP2KChnPCM/MP2KChnPSG/MP2KTrack/ReverbEffect), the
 * reference MP2K implementation.
 *
 * Features: sequencer (incl. MEMACC, XCMD pseudo echo/XWAIT), PCM +
 * GameFreak DPCM + Camelot ADPCM + Golden Sun synth voices, fixed-rate
 * voices, PSG with hardware 4-bit envelopes / duty / wave quantization /
 * noise LFSR / SQ1 sweep (mono-strict polyphony), LFO/MOD (pitch, volume,
 * pan), MP2K reverb, rhythm-instrument pan/base key.
 *
 * The per-sample mixer is integer-only (32.32 phases, Q15 samples, int32
 * buses); floats survive only at event rate (pitch pow(), sweep), so the
 * engine also runs well on FPU-less embedded targets.
 *
 * Known limitations:
 *  - PSG is agbplay-style band-unlimited synthesis, not Gb_Apu; no BLEP
 *  - engine sound-mode values (PCM master volume, fixed rate, reverb
 *    type) are compile-time defaults tuned for Pokémon R/S/E
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
#ifndef GBA_AUDIO_MP2K_H
#define GBA_AUDIO_MP2K_H

#include <stdbool.h>
#include <stdint.h>

#include "gba_export.h"

#define MP2K_MAX_TRACKS 16
#define MP2K_MAX_CHANNELS 16
#define MP2K_CALL_STACK 3

typedef struct {
    const uint8_t *blob;   /* song data blob, offset-relocated (multi-song
                              paks point every song at one shared pool) */
    uint32_t blob_size;
    uint32_t hdr_off;      /* song header offset within blob */
    int sample_rate;       /* output rate, e.g. 32768 */
    /* Song-end policy for looping songs (GOTO). One-shot songs (FINE on all
     * tracks) always end on their own regardless of these. */
    int loop_count;        /* stop after this many full loops; 0 = never (jukebox) */
    int fade_ms;           /* fade-out length applied at the stop; 0 = hard cut */
} Mp2kConfig;

typedef struct Mp2kPlayer Mp2kPlayer;

/* Parse a .pak file. Returns number of songs, or -1 if not a valid pak.
 * For song i, fills cfg (blob/blob_size/hdr_off) from the pak index. */
GBA_API int mp2k_pak_songs(const uint8_t *pak, uint32_t pak_size);
GBA_API bool mp2k_pak_get(const uint8_t *pak, uint32_t pak_size, int song,
                  Mp2kConfig *cfg);
/* Original ROM songtable index of pak entry `song`, or -1. */
GBA_API int mp2k_pak_index(const uint8_t *pak, uint32_t pak_size, int song);

GBA_API Mp2kPlayer *mp2k_new(const Mp2kConfig *cfg);
GBA_API void mp2k_delete(Mp2kPlayer *p);

/* Render n interleaved stereo int16 samples. Returns false once the song
 * has ended: either FINE on all tracks with voices died out (one-shot), or
 * a looping song reached cfg.loop_count loops and its fade-out completed. */
GBA_API bool mp2k_render(Mp2kPlayer *p, int16_t *buf, int n);

GBA_API int mp2k_n_tracks(const Mp2kPlayer *p);

#endif
