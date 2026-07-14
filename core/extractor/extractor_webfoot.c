#include "extractor_internal.h"
#include <stdlib.h>
#include <string.h>

static const uint8_t SIG_NOTE[] = { 0x00, 0x02, 0x00, 0x00, 0x1E, 0x02, 0x00, 0x00, 0x3F, 0x02, 0x00, 0x00 };
static const uint8_t SIG_TUP[] = { 0x00, 0x40, 0x3B, 0x40, 0x77, 0x40 };
static const uint8_t SIG_TDN[] = { 0xFF, 0xFF, 0x14, 0xFF, 0x29, 0xFE };

typedef struct {
    uint32_t note_rate;
    uint32_t tup;
    uint32_t tdn;
    uint32_t bgm_table;
    int n_songs;
    uint32_t inst_table;
    uint32_t flag_lut;
    int n_insts;
} WbfCtx;

static int pattern_len(const uint8_t *rom, uint32_t pp, uint32_t flag_lut) {
    uint8_t rows = rom[pp];
    uint32_t s = pp + 1;
    uint8_t last_flag[16] = {0};
    for (int r = 0; r < rows; r++) {
        while (true) {
            uint8_t b = rom[s++];
            if (b == 0) break;
            uint8_t ch = (b & 0xF) - 1;
            uint8_t sel = b >> 4;
            uint8_t fl;
            if (sel == 0) {
                fl = rom[s++];
                if (ch < 16) last_flag[ch] = fl;
            } else if (sel == 1) {
                fl = ch < 16 ? last_flag[ch] : 0;
            } else {
                fl = rom[flag_lut + sel - 2];
            }
            if (fl & 1) s++;
            if (fl & 2) s++;
            if (fl & 4) s++;
            if (fl & 8) s += 2;
        }
    }
    return s - pp;
}

static uint32_t ro(uint32_t p) { return p - 0x08000000; }

/* Per-song metadata for the converter UI: single-pass duration (ms), number
 * of channels that ever play a note, and whether the song loops (ends on a
 * backward fx2 jump). Timing mirrors the verified renderer: samples/tick =
 * 40000/tempo at the driver's 2^24/1050 Hz clock, following fx1 (speed),
 * fx20 (tempo) and fx2 (jump) exactly. */
static void wbf_song_info(Extractor *e, int idx, uint32_t *dur_ms,
                          int *channels, int *loops) {
    *dur_ms = 0; *channels = 0; *loops = 0;
    WbfCtx *w = (WbfCtx *)e->priv;
    if (idx < 0 || idx >= w->n_songs) return;
    const uint8_t *rom = e->rom;

    uint32_t se = w->bgm_table + (uint32_t)idx * 20;
    uint32_t pat_tbl = ro(rd32(rom, se));
    uint32_t order = ro(rd32(rom, se + 4));
    int tempo = rom[se + 16], speed = rom[se + 17];
    int spt = tempo ? 40000 / tempo : 300;

    int order_len = 0;
    while (rom[order + order_len] != 0xFF && order_len < 512) order_len++;

    uint8_t last_flag[16] = {0}, last_fx[16] = {0}, last_par[16] = {0};
    int cur_note[16];
    for (int i = 0; i < 16; i++) cur_note[i] = -2;
    uint16_t chan_mask = 0;
    uint64_t total = 0;
    int opos = 0, looped = 0, guard = 0;

    while (opos < order_len && ++guard < 100000) {
        uint8_t oi = rom[order + opos];
        uint32_t pp = ro(rd32(rom, pat_tbl + (uint32_t)oi * 4));
        int rows = rom[pp];
        uint32_t s = pp + 1;
        int pending_jump = -1, pending_speed = 0, row = 0;
        while (row < rows) {
            while (1) {
                uint8_t b = rom[s++];
                if (b == 0) break;
                int ch = (b & 0xF) - 1, sel = b >> 4;
                uint8_t fl;
                if (sel == 0) { fl = rom[s++]; if (ch >= 0 && ch < 16) last_flag[ch] = fl; }
                else if (sel == 1) fl = (ch >= 0 && ch < 16) ? last_flag[ch] : 0;
                else fl = rom[w->flag_lut + sel - 2];
                int fx = 0, par = 0;
                if (fl & 1) { int note = rom[s++]; if (ch >= 0 && ch < 16) cur_note[ch] = note; }
                if (fl & 2) s++;
                if (fl & 4) s++;
                if (fl & 8) { fx = rom[s]; par = rom[s + 1]; s += 2;
                              if (ch >= 0 && ch < 16) { last_fx[ch] = fx; last_par[ch] = par; } }
                else if (fl & 0x80) { if (ch >= 0 && ch < 16) { fx = last_fx[ch]; par = last_par[ch]; } }
                if (fx == 20 && par) spt = 40000 / par;
                else if (fx == 1 && par) pending_speed = par;
                else if (fx == 2) pending_jump = par;
                if ((fl & 0x11) && ch >= 0 && ch < 16) {
                    int n = cur_note[ch];
                    if (n >= 0 && n != 0xFF) chan_mask |= (uint16_t)(1u << ch);
                }
            }
            total += (uint64_t)spt * speed;
            row++;
            if (pending_speed) { speed = pending_speed; pending_speed = 0; }
            if (pending_jump >= 0) break;
        }
        if (pending_jump >= 0) {
            /* every fx2 in every known driver game jumps backward (the
             * song's loop point), so a jump ends the first pass */
            looped = 1;
            break;
        }
        opos++;
    }

    int cc = 0;
    for (int i = 0; i < 16; i++) if (chan_mask & (1u << i)) cc++;
    *dur_ms = (uint32_t)(total * 1050000ULL / 16777216ULL);
    *channels = cc;
    *loops = looped;
}

static uint32_t wbf_build(Extractor *e, const int *song_indices, int num_songs, uint8_t **out_buffer) {
    WbfCtx *w = (WbfCtx *)e->priv;

    uint32_t cap = 128 * 1024;
    uint8_t *blob = malloc(cap);
    if (!blob) return 0;
    uint32_t size = 0x20;

    /* pad to a 4-byte boundary */
    #define ALIGN4() do { while (size % 4) { blob[size++] = 0; } } while(0)
    #define APPEND(data, len) do { \
        if (size + (len) > cap) { cap = (size + (len)) * 2; blob = realloc(blob, cap); } \
        memcpy(blob + size, (data), (len)); size += (len); \
    } while(0)

    uint32_t off_luts = size;
    APPEND(e->rom + w->note_rate, 480);
    APPEND(e->rom + w->tup, 512);
    APPEND(e->rom + w->tdn, 512);
    APPEND(e->rom + w->tup - 0x80, 256);
    APPEND(e->rom + w->tdn - 0x80, 256);
    APPEND(e->rom + w->tdn + 0x200, 64);

    uint32_t off_flag_lut = size;
    APPEND(e->rom + w->flag_lut, 14);
    ALIGN4();

    /* Samples: one per instrument, in table order (no instrument shares a
     * sample pointer in any known driver game, so there is nothing to dedup) */
    uint32_t *smp_offs = calloc(w->n_insts, sizeof(uint32_t));
    for (int i = 0; i < w->n_insts; i++) {
        uint32_t o = w->inst_table + i * 12;
        uint32_t p = ro(rd32(e->rom, o));
        uint16_t le = rd16(e->rom, o + 8);
        smp_offs[i] = size;
        APPEND(e->rom + p, le);
    }
    ALIGN4();

    uint32_t off_insts = size;
    for (int i = 0; i < w->n_insts; i++) {
        uint32_t o = w->inst_table + i * 12;
        uint32_t ptr = smp_offs[i];
        uint8_t flags = e->rom[o + 4];
        uint16_t loop_start = rd16(e->rom, o + 6);
        uint16_t loop_end = rd16(e->rom, o + 8);
        uint16_t freq = rd16(e->rom, o + 10);
        uint8_t rec[12];
        rec[0] = ptr; rec[1] = ptr>>8; rec[2] = ptr>>16; rec[3] = ptr>>24;
        rec[4] = flags; rec[5] = 0;
        rec[6] = loop_start; rec[7] = loop_start>>8;
        rec[8] = loop_end; rec[9] = loop_end>>8;
        rec[10] = freq; rec[11] = freq>>8;
        APPEND(rec, 12);
    }

    uint32_t *song_recs = malloc(num_songs * 16);

    for (int i = 0; i < num_songs; i++) {
        int s = song_indices[i];
        uint32_t eo = w->bgm_table + s * 20;
        uint32_t pat_tbl = ro(rd32(e->rom, eo));
        uint32_t orders = ro(rd32(e->rom, eo + 4));
        uint8_t tempo = e->rom[eo + 16];
        uint8_t speed = e->rom[eo + 17];

        uint32_t oend = orders;
        uint8_t max_pat = 0;
        while (e->rom[oend] != 0xFF) {
            if (e->rom[oend] > max_pat) max_pat = e->rom[oend];
            oend++;
        }
        int n_pats = max_pat + 1;
        uint32_t off_orders = size;
        APPEND(e->rom + orders, oend - orders + 1);
        ALIGN4();

        uint32_t *pat_offs = malloc(n_pats * 4);
        for (int pi = 0; pi < n_pats; pi++) {
            uint32_t pp = ro(rd32(e->rom, pat_tbl + pi * 4));
            int plen = pattern_len(e->rom, pp, w->flag_lut);
            pat_offs[pi] = size;
            APPEND(e->rom + pp, plen);
        }
        ALIGN4();
        uint32_t off_pat_tbl = size;
        APPEND(pat_offs, n_pats * 4);
        free(pat_offs);

        uint8_t *rec = (uint8_t*)&song_recs[i * 4];
        rec[0] = off_orders; rec[1] = off_orders>>8; rec[2] = off_orders>>16; rec[3] = off_orders>>24;
        rec[4] = off_pat_tbl; rec[5] = off_pat_tbl>>8; rec[6] = off_pat_tbl>>16; rec[7] = off_pat_tbl>>24;
        rec[8] = n_pats; rec[9] = n_pats>>8;
        rec[10] = tempo; rec[11] = speed;
        rec[12] = 0; rec[13] = 0; rec[14] = 0; rec[15] = 0;
    }

    uint32_t off_songs = size;
    APPEND(song_recs, num_songs * 16);
    free(song_recs);
    free(smp_offs);

    blob[0] = 'W'; blob[1] = 'B'; blob[2] = 'F'; blob[3] = '1';
    blob[4] = num_songs; blob[5] = w->n_insts; blob[6] = 0; blob[7] = 0;
    blob[8] = off_luts; blob[9] = off_luts>>8; blob[10] = off_luts>>16; blob[11] = off_luts>>24;
    blob[12] = off_flag_lut; blob[13] = off_flag_lut>>8; blob[14] = off_flag_lut>>16; blob[15] = off_flag_lut>>24;
    blob[16] = off_songs; blob[17] = off_songs>>8; blob[18] = off_songs>>16; blob[19] = off_songs>>24;
    blob[20] = off_insts; blob[21] = off_insts>>8; blob[22] = off_insts>>16; blob[23] = off_insts>>24;
    blob[24] = size; blob[25] = size>>8; blob[26] = size>>16; blob[27] = size>>24;
    blob[28] = 0; blob[29] = 0; blob[30] = 0; blob[31] = 0;

    *out_buffer = blob;
    return size;
}

static void wbf_free(Extractor *e) {
    if (e->priv) free(e->priv);
    free(e);
}

static int wbf_song_count(Extractor *e) {
    WbfCtx *w = (WbfCtx *)e->priv;
    return w->n_songs;
}

Extractor* webfoot_detect(const uint8_t *rom, uint32_t rom_size) {
    if (rom_size < 0xB3 || rom[0xB2] != 0x96) return NULL;
    int note = find_sig(rom, rom_size, SIG_NOTE, sizeof(SIG_NOTE));
    if (note < 0) return NULL;
    int tup = find_sig(rom, rom_size, SIG_TUP, sizeof(SIG_TUP));
    if (tup < 0) return NULL;
    int tdn = find_sig(rom, rom_size, SIG_TDN, sizeof(SIG_TDN));
    if (tdn < 0) return NULL;

    int bgm = -1;
    for (uint32_t o = 0; o < rom_size - 20 * 8; o += 4) {
        bool ok = true;
        for (int j = 0; j < 4; j++) {
            uint32_t p = rd32(rom, o + j*4);
            if (p < 0x08000000 || p >= 0x08000000 + rom_size) { ok = false; break; }
        }
        if (!ok) continue;
        if (rom[o + 16] < 40 || rom[o + 16] > 200 || rom[o + 17] < 1 || rom[o + 17] > 15) continue;
        if (rom[o + 18] || rom[o + 19]) continue;

        int cnt = 1;
        while (1) {
            uint32_t eo = o + cnt * 20;
            bool ok2 = true;
            for (int j = 0; j < 4; j++) {
                uint32_t p = rd32(rom, eo + j*4);
                if (p < 0x08000000 || p >= 0x08000000 + rom_size) { ok2 = false; break; }
            }
            if (!ok2 || rd32(rom, eo + 8) != rd32(rom, o + 8) || rd32(rom, eo + 12) != rd32(rom, o + 12)) break;
            cnt++;
        }
        if (cnt >= 8) { bgm = o; break; } /* first match wins */
    }

    if (bgm < 0) return NULL;

    uint32_t inst = rd32(rom, bgm + 8) - 0x08000000;
    uint32_t lut = rd32(rom, bgm + 12) - 0x08000000;
    if (lut <= inst || (lut - inst) % 12 != 0) return NULL;

    WbfCtx *w = calloc(1, sizeof(WbfCtx));
    w->note_rate = note;
    w->tup = tup;
    w->tdn = tdn;
    w->bgm_table = bgm;
    w->n_songs = 0;

    int cnt = 0;
    while (1) {
        uint32_t eo = bgm + cnt * 20;
        bool ok2 = true;
        for (int j = 0; j < 4; j++) {
            uint32_t p = rd32(rom, eo + j*4);
            if (p < 0x08000000 || p >= 0x08000000 + rom_size) { ok2 = false; break; }
        }
        if (!ok2 || rd32(rom, eo + 8) - 0x08000000 != inst || rd32(rom, eo + 12) - 0x08000000 != lut) break;
        cnt++;
    }

    w->n_songs = cnt;
    w->inst_table = inst;
    w->flag_lut = lut;
    w->n_insts = (lut - inst) / 12;

    Extractor *e = calloc(1, sizeof(Extractor));
    e->rom = rom;
    e->rom_size = rom_size;
    e->format = EXTRACTOR_FMT_WEBFOOT;
    e->inst_count = w->n_insts;
    e->priv = w;
    e->free_fn = wbf_free;
    e->song_count_fn = wbf_song_count;
    e->song_info_fn = wbf_song_info;
    e->build_fn = wbf_build;
    return e;
}
