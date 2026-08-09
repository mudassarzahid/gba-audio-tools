/* webfoot engine core. See webfoot.h. Portable C99, integer-only,
 * no allocation. Mirrors the reference renderer the format was
 * reverse-engineered with: same sequencer, effects, loop modes and pitch
 * math, restructured from that offline voice-list into a real-time
 * per-tick streaming mixer.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
#include "webfoot.h"
#include <string.h>

#define NCH   WBF_CHANNELS
#define DECLK 48                 /* declick ramp, ~1.5 ms at 32768 Hz */
#define RAMP_STEP (32768 / DECLK + 1)
#define DEV_SHIFT 1              /* device output gain: acc >> 1. Calibrated
                                   across both games' full catalogs: the
                                   loudest song peaks at ~0.83 FS (no clipping
                                   anywhere), median RMS ~2700, in the same
                                   range as the GBS/SPC engines. wbf2wav
                                   re-normalizes offline so its A/B WAVs are
                                   unaffected; the settings volume trims
                                   further on-device. */

/* ---- little-endian image readers (image may be unaligned) -------------- */
static uint32_t rd32(const uint8_t *p) {
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 |
           (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }

typedef struct {
    uint8_t  active, looped, ping;
    const uint8_t *smp;
    uint32_t ls, le;
    uint64_t pos;            /* Q32 source-sample index */
    int64_t  inc;            /* Q32 effective step */
    int32_t  vol;            /* 0..64 */
    int32_t  ramp;           /* Q15 declick gain 0..32768 */
    int32_t  ramp_dir;       /* + rising, - falling, 0 steady */
} Voice;

struct WbfEngine {
    const uint8_t *base;
    const uint8_t *note_lut, *tup, *tdn, *vup, *vdn;
    const int8_t  *sine;
    const uint8_t *flag_lut, *songs, *insts;
    int n_songs, n_insts;

    int song, spt, speed, opos, order_len, row, rows, tick;
    const uint8_t *pat_tbl, *orders, *rowp;
    int pending_speed, pending_jump;
    int loops;
    int32_t tick_cd;         /* Q9 countdown to next tick */

    int      cur_ins[NCH], cur_vol[NCH], cur_note[NCH], work_vol[NCH];
    uint8_t  last_fl[NCH];
    int      last_fx[NCH], last_par[NCH];
    int      slide_mem[NCH], porta_rate[NCH];
    uint64_t base_step[NCH], tgt_step[NCH];
    int      vib_sp[NCH], vib_dp[NCH], vib_ph[NCH];
    int      eff_fx[NCH], eff_par[NCH];
    int      retrig_iv[NCH], retrig_ph[NCH];
    int      delay_ticks[NCH], delay_note[NCH];   /* fx27 note delay */

    Voice voice[NCH], tail[NCH];
};

unsigned wbf_engine_size(void) { return sizeof(struct WbfEngine); }
int wbf_song_count(const WbfEngine *w) { return w->n_songs; }
int wbf_loops(const WbfEngine *w) { return w->loops; }

/* pitch: fixed = (((bf*period)>>14)*0x1063 + 0x7FF) >> 12; step per
 * 32768-out sample (source samples) = fixed/33600. Returned Q32. */
static uint64_t note_step(WbfEngine *w, int bf, int note) {
    if (note < 0 || note >= 120) return 0;
    uint32_t period = rd32(w->note_lut + note * 4);
    uint64_t fixed = ((((uint64_t)bf * period) >> 14) * 0x1063u + 0x7FF) >> 12;
    return (fixed << 32) / 33600u;
}
/* step * lut / denom, all within uint64 (step<=~2^36, lut<=2^16) */
static uint64_t mul_lut(uint64_t step, unsigned v, unsigned denom) {
    return (step * v) / denom;
}
static const uint8_t *inst_rec(WbfEngine *w, int n) {
    return (n >= 0 && n < w->n_insts) ? w->insts + n * 12 : 0;
}

static void to_tail(WbfEngine *w, int ch) {
    Voice *v = &w->voice[ch];
    if (v->active && v->ramp > 0) {
        w->tail[ch] = *v;
        w->tail[ch].ramp_dir = -RAMP_STEP;
    }
}

static void voice_start(WbfEngine *w, int ch, int note, int fx, int par) {
    const uint8_t *ir = inst_rec(w, w->cur_ins[ch]);
    if (!ir) return;
    to_tail(w, ch);
    Voice *v = &w->voice[ch];
    int flags = ir[4];
    v->active = 1;
    v->smp = w->base + rd32(ir);
    v->ls = rd16(ir + 6);
    v->le = rd16(ir + 8);
    v->looped = flags & 1;
    v->ping = (flags & 2) != 0;
    w->base_step[ch] = note_step(w, rd16(ir + 10), note);
    w->tgt_step[ch] = 0;
    v->inc = (int64_t)w->base_step[ch];
    v->pos = (fx == 15 && par) ? ((uint64_t)(par * 256) << 32) : 0;
    w->work_vol[ch] = w->cur_vol[ch];
    v->vol = w->work_vol[ch];
    v->ramp = 0;
    v->ramp_dir = RAMP_STEP;
}

static void set_step(WbfEngine *w, int ch, uint64_t base) {
    w->base_step[ch] = base;
    if (w->voice[ch].active) w->voice[ch].inc = (int64_t)base;
}

static void parse_row(WbfEngine *w) {
    const uint8_t *s = w->rowp;
    for (int ch = 0; ch < NCH; ch++) { w->eff_fx[ch] = 0; w->eff_par[ch] = 0; }

    for (;;) {
        uint8_t b = *s++;
        if (b == 0) break;
        int ch = (b & 0xF) - 1, sel = b >> 4;
        uint8_t fl;
        if (sel == 0) { fl = *s++; if (ch >= 0 && ch < NCH) w->last_fl[ch] = fl; }
        else if (sel == 1) fl = (ch >= 0 && ch < NCH) ? w->last_fl[ch] : 0;
        else fl = w->flag_lut[sel - 2];

        int note = -2, fx = 0, par = 0, have_note = 0;
        if (fl & 1) { note = *s++; have_note = 1; }
        int insb = -1, volb = -1;
        if (fl & 2) insb = *s++;
        if (fl & 4) volb = *s++;
        if (fl & 8) { fx = *s++; par = *s++; }
        else if (fl & 0x80) { fx = 0; par = 0; }   /* filled below from memory */

        if (ch < 0 || ch >= NCH) continue;
        if (have_note) w->cur_note[ch] = note;
        if (insb >= 0) w->cur_ins[ch] = insb;
        if (volb >= 0) w->cur_vol[ch] = volb;
        if (fl & 8) { w->last_fx[ch] = fx; w->last_par[ch] = par; }
        else if (fl & 0x80) { fx = w->last_fx[ch]; par = w->last_par[ch]; }
        w->eff_fx[ch] = fx; w->eff_par[ch] = par;

        if (fx == 20 && par) w->spt = 40000 / par;
        else if (fx == 1 && par) w->pending_speed = par;
        else if (fx == 2) w->pending_jump = par;

        int n = w->cur_note[ch];
        if (fl & 0x11) {
            if (fx == 27 && par && n != 0xFF && n >= 0 && n < 120
                    && inst_rec(w, w->cur_ins[ch])) {
                /* fx27 note delay: fire the note-on `par` ticks later; the
                 * current voice keeps sounding until then */
                w->delay_ticks[ch] = par;
                w->delay_note[ch] = n;
            } else if (fx == 7 && n != 0xFF && n >= 0 && n < 120 && w->voice[ch].active) {
                /* tone portamento: glide toward the (stored or explicit) note
                 * without retriggering; a volume column (bit2) or reapply
                 * (bit6) still updates the sounding voice */
                const uint8_t *ir = inst_rec(w, w->cur_ins[ch]);
                if (ir) w->tgt_step[ch] = note_step(w, rd16(ir + 10), n);
                if (fl & 0x44) {
                    w->work_vol[ch] = w->cur_vol[ch];
                    w->voice[ch].vol = w->work_vol[ch];
                }
            } else if (n != 0xFF && n >= 0 && n < 120 && inst_rec(w, w->cur_ins[ch])) {
                w->delay_ticks[ch] = 0;   /* a real note-on cancels any pending delay */
                voice_start(w, ch, n, fx, par);
            } else {
                w->delay_ticks[ch] = 0;
                to_tail(w, ch);
                w->voice[ch].active = 0;
            }
        } else if (fl & 0x44) {
            w->work_vol[ch] = w->cur_vol[ch];
            if (w->voice[ch].active) w->voice[ch].vol = w->work_vol[ch];
        }

        if (fx == 8 && (fl & 0x11)) w->vib_ph[ch] = 0;
        if (fx == 17) {
            int iv = par & 0xF;
            if (iv && w->retrig_iv[ch] != iv) { w->retrig_iv[ch] = iv; w->retrig_ph[ch] = 0; }
        } else {
            w->retrig_iv[ch] = 0;
        }
    }
    w->rowp = s;   /* leave cursor at the next row */
}

static void apply_tick(WbfEngine *w, int ch) {
    int fx = w->eff_fx[ch], par = w->eff_par[ch], t = w->tick;
    Voice *v = &w->voice[ch];

    if (fx == 4) {
        int p = par ? par : w->slide_mem[ch];
        if (par) w->slide_mem[ch] = par;
        if (p) {
            int lo = p & 0xF, hi = p >> 4, d = 0, when = -1;
            if (lo == 0xF && hi)      { d = hi;  when = 0; }
            else if (hi == 0xF && lo) { d = -lo; when = 0; }
            else if (lo == 0 && hi)   { d = hi;  when = 1; }
            else if (hi == 0 && lo)   { d = -lo; when = 1; }
            if ((when == 0 && t == 0) || (when == 1 && t >= 1)) {
                int nv = w->work_vol[ch] + d;
                if (nv < 0) nv = 0;
                if (nv > 0x40) nv = 0x40;
                w->work_vol[ch] = nv;
                if (v->active) v->vol = nv;
            }
        }
    } else if ((fx == 5 || fx == 6) && v->active) {
        int p = par ? par : w->porta_rate[ch];
        if (par) w->porta_rate[ch] = par;
        int hi = p >> 4;
        /* the original driver also has an 0xEx "extra-fine" fx5 variant
         * (vdn LUT); no song in any of the six known driver games uses
         * it, so 0xEx is a no-op here */
        if (p && hi == 0xF && t == 0) {
            unsigned r = p & 0xF;
            set_step(w, ch, fx == 5 ? mul_lut(w->base_step[ch], rd16(w->tdn + r*2), 65536)
                                    : mul_lut(w->base_step[ch], rd16(w->tup + r*2), 16384));
        } else if (p && hi != 0xF && hi != 0xE && t >= 1) {
            set_step(w, ch, fx == 5 ? mul_lut(w->base_step[ch], rd16(w->tdn + p*2), 65536)
                                    : mul_lut(w->base_step[ch], rd16(w->tup + p*2), 16384));
        }
    } else if (fx == 7 && v->active && t >= 1) {
        if (par) w->porta_rate[ch] = par;
        int r = w->porta_rate[ch];
        uint64_t tg = w->tgt_step[ch], b = w->base_step[ch];
        if (tg && r) {
            if (b < tg) { b = mul_lut(b, rd16(w->tup + r*2), 16384); if (b > tg) b = tg; }
            else if (b > tg) { b = mul_lut(b, rd16(w->tdn + r*2), 65536); if (b < tg) b = tg; }
            set_step(w, ch, b);
        }
    } else if (fx == 8 && v->active) {
        if (par) {
            if (par >> 4) w->vib_sp[ch] = par >> 4;
            if (par & 0xF) w->vib_dp[ch] = (par & 0xF) << 2;
        }
        w->vib_ph[ch] = (w->vib_ph[ch] + w->vib_sp[ch]) & 0x3F;
        int m = (w->sine[w->vib_ph[ch]] * w->vib_dp[ch]) >> 6;
        uint64_t f = m >= 0 ? mul_lut(w->base_step[ch], rd16(w->vup + m*2), 32768)
                            : mul_lut(w->base_step[ch], rd16(w->vdn + (-m)*2), 65536);
        v->inc = (int64_t)f;
    }
}

static void load_pattern(WbfEngine *w) {
    int oi = w->orders[w->opos];
    const uint8_t *pat = w->base + rd32(w->pat_tbl + oi * 4);
    w->rows = pat[0];
    w->rowp = pat + 1;
}

static void next_row(WbfEngine *w) {
    int jump = w->pending_jump;
    w->pending_jump = -1;
    if (jump >= 0) {
        /* every fx2 in every known driver game jumps backward (the song's
         * loop point), so a jump is always a loop crossing */
        w->loops++;
        w->opos = jump; w->row = 0; load_pattern(w);
        return;
    }
    w->row++;
    if (w->row >= w->rows) {
        w->row = 0; w->opos++;
        if (w->opos >= w->order_len) { w->opos = 0; w->loops++; }
        load_pattern(w);
    }
    /* within a pattern rowp already points at the next row (parse_row) */
}

static void do_tick(WbfEngine *w) {
    /* fx27 note-delay countdown (before parse_row so a note set this tick
     * isn't decremented until the next one) */
    for (int ch = 0; ch < NCH; ch++) {
        if (w->delay_ticks[ch] > 0 && --w->delay_ticks[ch] == 0)
            voice_start(w, ch, w->delay_note[ch], 0, 0);
    }
    if (w->tick == 0) parse_row(w);
    for (int ch = 0; ch < NCH; ch++)
        if (w->voice[ch].active) w->voice[ch].inc = (int64_t)w->base_step[ch];
    for (int ch = 0; ch < NCH; ch++) apply_tick(w, ch);
    for (int ch = 0; ch < NCH; ch++) {
        if (w->eff_fx[ch] == 17 && w->retrig_iv[ch] && w->voice[ch].active) {
            if (++w->retrig_ph[ch] >= w->retrig_iv[ch]) {
                w->retrig_ph[ch] = 0;
                w->voice[ch].pos = 0;
                w->voice[ch].ramp = 0;
                w->voice[ch].ramp_dir = RAMP_STEP;
            }
        }
    }
    if (++w->tick >= w->speed) {
        w->tick = 0;
        if (w->pending_speed) { w->speed = w->pending_speed; w->pending_speed = 0; }
        next_row(w);
    }
}

static int32_t mix_voice(Voice *v) {
    if (!v->active) return 0;
    uint32_t idx = (uint32_t)(v->pos >> 32);
    if (idx >= v->le) {
        if (v->looped && v->le > v->ls) {
            uint32_t L = v->le - v->ls, rel = idx - v->ls;
            if (v->ping) {
                uint32_t ph = rel % (2 * L);
                idx = v->ls + (ph < L ? ph : (2 * L - ph));
                if (idx >= v->le) idx = v->le - 1;
            } else {
                idx = v->ls + rel % L;
            }
        } else {
            v->active = 0;
            return 0;
        }
    }
    int32_t s = (int32_t)v->smp[idx];        /* 8-bit signed */
    if (s >= 128) s -= 256;
    int32_t out = (s * v->vol * v->ramp) >> 15;
    v->pos += (uint64_t)v->inc;
    v->ramp += v->ramp_dir;
    if (v->ramp >= 32768) { v->ramp = 32768; v->ramp_dir = 0; }
    else if (v->ramp <= 0) { v->ramp = 0; if (v->ramp_dir < 0) v->active = 0; }
    return out;
}

static int16_t clip16(int32_t x) {
    return x > 32767 ? 32767 : (x < -32768 ? -32768 : (int16_t)x);
}

void wbf_render(WbfEngine *w, int16_t *out, int n) {
    for (int i = 0; i < n; i++) {
        while (w->tick_cd <= 0) { do_tick(w); w->tick_cd += w->spt * 1050; }
        int32_t acc = 0;
        for (int ch = 0; ch < NCH; ch++) {
            acc += mix_voice(&w->voice[ch]);
            acc += mix_voice(&w->tail[ch]);
        }
        out[i] = clip16(acc >> DEV_SHIFT);
        w->tick_cd -= 512;
    }
}

void wbf_start(WbfEngine *w, int song) {
    if (song < 0 || song >= w->n_songs) song = 0;
    w->song = song;
    const uint8_t *rec = w->songs + song * 16;
    w->orders = w->base + rd32(rec);
    w->pat_tbl = w->base + rd32(rec + 4);
    int tempo = rec[10]; w->speed = rec[11];
    w->spt = tempo ? 40000 / tempo : 300;

    w->order_len = 0;
    while (w->orders[w->order_len] != 0xFF) w->order_len++;

    w->opos = 0; w->row = 0; w->tick = 0; w->tick_cd = 0;
    w->pending_speed = 0; w->pending_jump = -1; w->loops = 0;

    for (int ch = 0; ch < NCH; ch++) {
        w->cur_ins[ch] = -1; w->cur_vol[ch] = 0x40; w->cur_note[ch] = -2;
        w->work_vol[ch] = 0x40; w->last_fl[ch] = 0;
        w->last_fx[ch] = 0; w->last_par[ch] = 0;
        w->slide_mem[ch] = 0; w->porta_rate[ch] = 0;
        w->base_step[ch] = 0; w->tgt_step[ch] = 0;
        w->vib_sp[ch] = 0; w->vib_dp[ch] = 0; w->vib_ph[ch] = 0;
        w->eff_fx[ch] = 0; w->eff_par[ch] = 0;
        w->retrig_iv[ch] = 0; w->retrig_ph[ch] = 0;
        w->delay_ticks[ch] = 0; w->delay_note[ch] = -2;
        memset(&w->voice[ch], 0, sizeof(Voice));
        memset(&w->tail[ch], 0, sizeof(Voice));
    }
    load_pattern(w);
}

WbfEngine *wbf_open(void *mem, const void *data, long size, const char **err) {
    const uint8_t *b = (const uint8_t *)data;
    if (size < 0x20 || memcmp(b, "WBF1", 4) != 0) {
        if (err) *err = "not a .wbf image";
        return 0;
    }
    long file_size = rd32(b + 0x18);
    if (file_size > size) { if (err) *err = "truncated .wbf"; return 0; }

    WbfEngine *w = (WbfEngine *)mem;
    memset(w, 0, sizeof(*w));
    w->base = b;
    w->n_songs = b[4];
    w->n_insts = b[5];
    uint32_t off_luts = rd32(b + 0x08), off_flag = rd32(b + 0x0C);
    uint32_t off_songs = rd32(b + 0x10), off_insts = rd32(b + 0x14);
    if (off_luts + 2080 > file_size || off_flag + 14 > file_size ||
        off_songs + (uint32_t)w->n_songs * 16 > file_size ||
        off_insts + (uint32_t)w->n_insts * 12 > file_size) {
        if (err) *err = "bad .wbf offsets";
        return 0;
    }
    w->note_lut = b + off_luts;
    w->tup = w->note_lut + 480;
    w->tdn = w->tup + 512;
    w->vup = w->tdn + 512;
    w->vdn = w->vup + 256;
    w->sine = (const int8_t *)(w->vdn + 256);
    w->flag_lut = b + off_flag;
    w->songs = b + off_songs;
    w->insts = b + off_insts;
    wbf_start(w, 0);
    if (err) *err = 0;
    return w;
}
