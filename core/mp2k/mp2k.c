/* MP2K player. See mp2k.h. Reference: agbplay (ipatix/agbplay).
 *
 * Sequencer + PCM/DPCM/ADPCM voices + PSG (agbplay CGB semantics) +
 * MP2K reverb + LFO/MOD + pseudo echo.
 *
 * The per-sample path is integer-only (32.32 phase accumulators, Q15
 * samples, 8-bit volume/envelope, int32 mix buses) so it runs well on an
 * FPU-less MCU. Floating point survives only at event rate (note-on /
 * pitch-change / 60 Hz sweep: pow() for pitch).
 * SPDX-License-Identifier: GPL-3.0-or-later */

#include "mp2k/mp2k.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

/* ---- engine configuration (m4aSoundMode values are per game; these are
 * the Pokémon R/S/E values, override with -D at build time) ---- */
#ifndef MP2K_PCM_MASTER_VOL          /* PCM master volume 0..15 */
#define MP2K_PCM_MASTER_VOL 12
#endif
#ifndef MP2K_FIXED_RATE              /* 'fixed frequency' voice rate, Hz */
#define MP2K_FIXED_RATE 13379
#endif
#ifndef MP2K_DMA_BUF_LEN             /* Nintendo PCM DMA buffer, samples */
#define MP2K_DMA_BUF_LEN 1584
#endif
#define MP2K_HEADROOM_Q16 22938      /* master scale 0.35 in Q16, ~10 voices */

/* ---- delay/note length table (agbplay SequenceReader delay/note LUTs) ---- */
static const uint8_t LEN_LUT[49] = {
    0,  1,  2,  3,  4,  5,  6,  7,  8,  9,  10, 11, 12, 13, 14, 15, 16,
    17, 18, 19, 20, 21, 22, 23, 24, 28, 30, 32, 36, 40, 42, 44, 48, 52,
    54, 56, 60, 64, 66, 68, 72, 76, 78, 80, 84, 88, 90, 92, 96,
};

enum { ENV_INIT, ENV_ATK, ENV_DEC, ENV_SUS, ENV_REL, ENV_ECHO, ENV_DIE, ENV_DEAD };
enum { V_PCM, V_SQ1, V_SQ2, V_WAVE, V_NOISE };
enum { P_PCM, P_DPCM, P_ADPCM, P_SYN_PWM, P_SYN_SAW, P_SYN_TRI };
enum { MODT_PITCH = 0, MODT_VOL = 1, MODT_PAN = 2 };
enum { PAN_LEFT = -1, PAN_CENTER = 0, PAN_RIGHT = 1 };

typedef struct {
    int state;              /* ENV_* */
    int kind;               /* V_* */
    int pcm_type;           /* P_* (V_PCM only) */
    bool stop;              /* note released */
    bool fast_release;      /* PSG: killed by polyphony/length */
    bool fixed;             /* PCM plays at MP2K_FIXED_RATE */
    int track;

    /* note */
    uint8_t key_track;      /* key as written in track data (EOT match) */
    uint8_t key_pitch;      /* key used for pitch (drum base key) */
    uint8_t vel;
    int length;             /* remaining ticks; 0 = TIE */
    int8_t rhythm_pan;
    uint8_t echo_vol, echo_len;
    uint8_t prio;

    /* instrument ADSR (raw; PSG masks its 4-bit ranges at note-on) */
    uint8_t att, dec, sus, rel;

    /* pitch. freq is event-rate state only; the sample loop uses the
     * 32.32 fixed-point accumulator. */
    float freq;
    uint64_t pos_fp;        /* PCM: sample index; PSG: waveform phase (32.32) */
    uint64_t step_fp;       /* per output sample (32.32) */

    /* PCM voice */
    uint32_t smp_off;       /* sample header offset in blob */
    uint32_t loop_pos, end_pos;
    bool loop_en;
    double midc;            /* mid-C rate, Hz */
    int env;                /* 0..255 */
    uint8_t lvol, rvol;     /* 0..255 */
    /* GameFreak DPCM block cache */
    int8_t dpcm_buf[64];
    uint32_t dpcm_block;
    /* Camelot ADPCM forward-decode state */
    uint32_t adp_pos;
    int16_t adp_level;
    uint8_t adp_shift;
    int16_t adp_prev, adp_cur;
    /* Golden Sun synth */
    uint32_t syn_pos;

    /* PSG voice */
    uint16_t psg_vol;       /* track vol (<<1, LFO applied), from SetVol */
    int16_t psg_pan;        /* -128..127, from SetVol */
    int env_level;          /* 0..15 */
    int env_peak, env_sustain;
    int frame_count;
    int pan_class;          /* PAN_* */
    uint16_t psg_len_count;
    bool psg_len_active;
    int duty;               /* SQ 0..3; NOISE 0=15-bit 1=7-bit */
    uint8_t sweep;
    bool sweep_en;
    float sweep_timer, sweep_coeff, sweep_conv;
    int sweep_start;        /* <0 = not yet initialised */
    uint32_t wave_off;
    int32_t wave_dc_q15;
    uint32_t lfsr, lfsr_mask;
    int16_t noise_q15;
} Chan;

typedef struct {
    bool enabled;
    uint32_t pos;
    int delay;
    uint8_t last_cmd;
    uint32_t ret[MP2K_CALL_STACK];
    int call_depth;
    int rept_count;

    int prog, vol, pan, bend, bendr, keyshift, tune, prio;
    uint8_t last_key, last_vel;

    /* LFO (agbplay MP2KTrack) */
    uint8_t mod, modt, lfos, lfodl, lfodl_count, lfo_phase;
    int8_t lfo_val;
    /* pseudo echo (XCMD XIECV/XIECL) */
    uint8_t echo_vol, echo_len;

    int loops;              /* times this track has taken its GOTO (loop count) */

    bool upd_vol, upd_pitch;
} Track;

struct Mp2kPlayer {
    Mp2kConfig cfg;
    Track trk[MP2K_MAX_TRACKS];
    int n_tracks;
    uint32_t bank;

    Chan chan[MP2K_MAX_CHANNELS];
    int bpm;
    int tick_acc;           /* += bpm each frame; tick while >= 150 */
    double frame_acc;       /* fractional samples per 60 Hz frame */
    bool ended;

    /* song-end policy (looping songs) */
    int loop_count;         /* stop after this many loops; 0 = never */
    long fade_samples;      /* fade length in stereo frames; 0 = hard cut */
    bool fading;            /* fade-out in progress */
    long fade_pos;          /* frames into the fade */

    uint8_t memacc[256];

    /* MP2K reverb (agbplay ReverbEffect, PCM voices only) */
    int32_t *rev_buf;       /* interleaved stereo ring, Q15 */
    uint32_t rev_frames;    /* ring length in stereo frames */
    uint32_t rev_pos, rev_pos2;
    int rev_level;          /* 0..127; intensity = level/128 */
};

/* ---- little helpers over the blob ---- */
static uint32_t rd32(const Mp2kPlayer *p, uint32_t off)
{
    if (off + 4 > p->cfg.blob_size) return 0;
    const uint8_t *b = p->cfg.blob + off;
    return (uint32_t)b[0] | (uint32_t)b[1] << 8 | (uint32_t)b[2] << 16 | (uint32_t)b[3] << 24;
}
static uint8_t rd8(const Mp2kPlayer *p, uint32_t off)
{
    return off < p->cfg.blob_size ? p->cfg.blob[off] : 0xB1; /* OOB reads FINE */
}
static int clampi(int v, int lo, int hi) { return v < lo ? lo : v > hi ? hi : v; }

/* ---- pak container ---- */
static const uint8_t PAK_MAGIC[8] = {'S','N','C','H','P','P','A','K'};

int mp2k_pak_songs(const uint8_t *pak, uint32_t n)
{
    if (n < 12 || memcmp(pak, PAK_MAGIC, 8) != 0) return -1;
    return pak[10] | pak[11] << 8;
}

int mp2k_pak_index(const uint8_t *pak, uint32_t n, int song)
{
    int count = mp2k_pak_songs(pak, n);
    if (count < 0 || song < 0 || song >= count) return -1;
    const uint8_t *e = pak + 12 + 16 * song;
    return e[12] | e[13] << 8;
}

bool mp2k_pak_get(const uint8_t *pak, uint32_t n, int song, Mp2kConfig *cfg)
{
    int count = mp2k_pak_songs(pak, n);
    if (count < 0 || song < 0 || song >= count) return false;
    const uint8_t *e = pak + 12 + 16 * song;
    uint32_t off  = (uint32_t)e[0] | e[1] << 8 | e[2] << 16 | (uint32_t)e[3] << 24;
    uint32_t size = (uint32_t)e[4] | e[5] << 8 | e[6] << 16 | (uint32_t)e[7] << 24;
    uint32_t hdr  = (uint32_t)e[8] | e[9] << 8 | e[10] << 16 | (uint32_t)e[11] << 24;
    if (off + size > n || hdr >= size) return false;
    cfg->blob = pak + off;
    cfg->blob_size = size;
    cfg->hdr_off = hdr;
    return true;
}

/* ---- lifecycle ---- */
Mp2kPlayer *mp2k_new(const Mp2kConfig *cfg)
{
    Mp2kPlayer *p = calloc(1, sizeof(*p));
    if (!p) return NULL;
    p->cfg = *cfg;
    uint32_t h = cfg->hdr_off;
    p->n_tracks = rd8(p, h);
    if (p->n_tracks == 0 || p->n_tracks > MP2K_MAX_TRACKS) { free(p); return NULL; }
    p->bank = rd32(p, h + 4);
    p->bpm = 150;
    p->loop_count = cfg->loop_count;
    p->fade_samples = cfg->fade_ms > 0
        ? (long)cfg->fade_ms * cfg->sample_rate / 1000 : 0;
    for (int i = 0; i < MP2K_MAX_CHANNELS; i++)
        p->chan[i].state = ENV_DEAD;
    for (int i = 0; i < p->n_tracks; i++) {
        Track *t = &p->trk[i];
        t->enabled = true;
        t->pos = rd32(p, h + 8 + 4 * (uint32_t)i);
        t->prog = 0xFF;             /* PROG_UNDEFINED */
        t->bendr = 2;
        t->lfos = 22;               /* MP2K default LFO speed */
    }

    /* reverb: song header byte 3, bit 7 = enable, low 7 bits = level.
     * Ring = numDmaBuffers engine frames (agbplay SoundMixer). */
    uint8_t rev = rd8(p, h + 3);
    uint32_t frame_len = (uint32_t)cfg->sample_rate / 60;
    uint32_t ndma = MP2K_DMA_BUF_LEN / (MP2K_FIXED_RATE / 60);
    if (ndma < 2) ndma = 2;
    p->rev_frames = frame_len * ndma;
    p->rev_buf = calloc(p->rev_frames * 2, sizeof(int32_t));
    if (!p->rev_buf) { free(p); return NULL; }
    p->rev_pos = 0;
    p->rev_pos2 = frame_len;
    p->rev_level = (rev & 0x80) ? (rev & 0x7F) : 0;
    return p;
}

void mp2k_delete(Mp2kPlayer *p)
{
    if (!p) return;
    free(p->rev_buf);
    free(p);
}
int mp2k_n_tracks(const Mp2kPlayer *p) { return p->n_tracks; }

/* ---- track pitch/vol/pan with LFO (agbplay MP2KTrack::Get*) ---- */
static int trk_pitch(const Track *t)
{
    int p = t->tune + t->bend * t->bendr + t->keyshift * 64;
    if (t->modt == MODT_PITCH) p += t->lfo_val * 4;
    return p;
}
static uint16_t trk_vol(const Track *t)
{
    int v = t->vol << 1;
    if (t->modt == MODT_VOL) v = (v * (t->lfo_val + 128)) >> 7;
    return (uint16_t)v;
}
static int16_t trk_pan(const Track *t)
{
    int p = t->pan << 1;
    if (t->modt == MODT_PAN) p += t->lfo_val;
    return (int16_t)p;
}
static void trk_reset_lfo(Track *t)
{
    t->lfo_val = 0;
    t->lfo_phase = 0;
    if (t->modt == MODT_PITCH) t->upd_pitch = true;
    else t->upd_vol = true;
}

/* ---- per-channel volume / pitch ---- */
static void chan_set_vol(Chan *c, uint16_t vol, int16_t pan)
{
    if (c->stop) return;
    if (c->kind == V_PCM) {
        int cpan = clampi(pan + c->rhythm_pan, -128, 128);
        if (cpan >= 126) cpan = 128;   /* keep full-right reachable */
        c->lvol = (uint8_t)clampi((c->vel * vol * (-cpan + 128)) >> 15, 0, 255);
        c->rvol = (uint8_t)clampi((c->vel * vol * (cpan + 128)) >> 15, 0, 255);
    } else {
        /* applied by the PSG envelope machinery (applyVol) */
        c->psg_vol = vol;
        c->psg_pan = (int16_t)clampi(pan, -128, 127);
    }
}

static float sq_timer2freq(float timer) { return 131072.0f / (2048.0f - timer); }
static float sq_freq2timer(float freq)
{
    float t = 131072.0f / freq;
    if (t > 2047.0f) t = 2047.0f;
    return 2048.0f - t;
}

static uint64_t step_fp_from(double freq, int sample_rate)
{
    return (uint64_t)(freq / (double)sample_rate * 4294967296.0);
}

static void chan_set_pitch(Mp2kPlayer *p, Chan *c, int pitch)
{
    switch (c->kind) {
    case V_PCM:
        if (c->stop && c->freq > 0.0f) return;   /* keep freq after release */
        c->freq = (float)(c->midc *
            pow(2.0, (double)(c->key_pitch - 60) / 12.0 + (double)pitch / 768.0));
        if (c->fixed && c->pcm_type <= P_ADPCM)
            c->step_fp = step_fp_from((double)MP2K_FIXED_RATE, p->cfg.sample_rate);
        else if (c->pcm_type >= P_SYN_PWM)
            c->step_fp = step_fp_from((double)c->freq / 64.0, p->cfg.sample_rate);
        else
            c->step_fp = step_fp_from((double)c->freq, p->cfg.sample_rate);
        break;
    case V_SQ1:
    case V_SQ2:
        if (c->stop && c->freq > 0.0f) return;
        c->freq = 3520.0f * powf(2.0f,
            (float)(c->key_pitch - 69) / 12.0f + (float)pitch / 768.0f);
        if (c->sweep_en && c->sweep_start < 0) {
            c->sweep_timer = sq_freq2timer(c->freq / 8.0f);
            int time = (c->sweep & 0x70) >> 4;
            c->sweep_start = time * 240;
        }
        if (!c->sweep_en)
            c->step_fp = step_fp_from((double)c->freq, p->cfg.sample_rate);
        break;
    case V_WAVE:
        c->freq = (440.0f * 16.0f) * powf(2.0f,
            (float)(c->key_pitch - 69) / 12.0f + (float)pitch / 768.0f);
        c->step_fp = step_fp_from((double)c->freq, p->cfg.sample_rate);
        break;
    case V_NOISE: {
        float fkey = (float)c->key_pitch + (float)pitch / 64.0f;
        float nf;
        if (fkey < 76.0f)      nf = 4096.0f * powf(8.0f, (fkey - 60.0f) / 12.0f);
        else if (fkey < 78.0f) nf = 65536.0f * powf(2.0f, (fkey - 76.0f) / 2.0f);
        else if (fkey < 80.0f) nf = 131072.0f * powf(2.0f, fkey - 78.0f);
        else                   nf = 524288.0f;
        if (nf < 4.5714f) nf = 4.5714f;
        c->freq = nf;
        c->step_fp = step_fp_from((double)nf, p->cfg.sample_rate);
        break;
    }
    }
}

static void chan_release(Chan *c) { if (c->state < ENV_DEAD) c->stop = true; }
static void chan_kill(Chan *c) { c->state = ENV_DEAD; }

/* ---- note on ---- */
static void note_on(Mp2kPlayer *p, int track, int key, int vel, int length)
{
    Track *t = &p->trk[track];
    if (t->prog > 127) return;

    uint32_t ins = p->bank + (uint32_t)t->prog * 12;
    uint8_t type = rd8(p, ins);
    int key_pitch = key;
    int8_t rhythm_pan = 0;

    if (type & 0x40) {                            /* keysplit */
        uint32_t sub = rd32(p, ins + 4), keymap = rd32(p, ins + 8);
        ins = sub + (uint32_t)rd8(p, keymap + (uint32_t)key) * 12;
        type = rd8(p, ins);
        if (type & 0xC0) return;
    } else if (type == 0x80) {                    /* rhythm/drum */
        uint32_t sub = rd32(p, ins + 4);
        ins = sub + (uint32_t)key * 12;
        type = rd8(p, ins);
        if (type & 0xC0) return;
        uint8_t ipan = rd8(p, ins + 3);
        if (ipan & 0x80) rhythm_pan = (int8_t)((ipan - 0xC0) * 2);
        key_pitch = rd8(p, ins + 1);              /* drum base key */
    }

    /* LFO restart on note (agbplay cmdPlayNote) */
    t->lfodl_count = t->lfodl;
    if (t->lfodl != 0) trk_reset_lfo(t);

    int cgb = type & 0x07;
    int kind;
    if (type == 0x00 || type == 0x08)      kind = V_PCM;
    else if (cgb == 1)                     kind = V_SQ1;
    else if (cgb == 2)                     kind = V_SQ2;
    else if (cgb == 3)                     kind = V_WAVE;
    else if (cgb == 4)                     kind = V_NOISE;
    else return;

    Chan *c = NULL;
    if (kind != V_PCM) {
        /* CGB mono-strict polyphony: one channel per PSG type */
        Chan *old = NULL;
        for (int i = 0; i < MP2K_MAX_CHANNELS; i++)
            if (p->chan[i].state != ENV_DEAD && p->chan[i].kind == kind) { old = &p->chan[i]; break; }
        if (old) {
            if (!old->stop) {
                if (old->prio > t->prio) return;
                if (old->prio == t->prio && old->track < track) return;
            }
            c = old;                              /* replace */
        }
    }
    if (!c) {
        for (int i = 0; i < MP2K_MAX_CHANNELS; i++)
            if (p->chan[i].state == ENV_DEAD) { c = &p->chan[i]; break; }
    }
    if (!c) {                                     /* steal quietest releasing */
        int best = 256;
        for (int i = 0; i < MP2K_MAX_CHANNELS; i++) {
            Chan *k = &p->chan[i];
            if (k->kind == V_PCM && k->stop && k->env < best) { best = k->env; c = k; }
        }
    }
    if (!c) return;
    memset(c, 0, sizeof(*c));

    c->kind = kind;
    c->att = rd8(p, ins + 8); c->dec = rd8(p, ins + 9);
    c->sus = rd8(p, ins + 10); c->rel = rd8(p, ins + 11);
    c->key_track = (uint8_t)key;
    c->key_pitch = (uint8_t)key_pitch;
    c->vel = (uint8_t)vel;
    c->length = length;
    c->rhythm_pan = rhythm_pan;
    c->echo_vol = t->echo_vol;
    c->echo_len = t->echo_len;
    c->prio = (uint8_t)t->prio;
    c->track = track;
    c->state = ENV_INIT;
    c->sweep_start = -1;

    if (kind == V_PCM) {
        c->smp_off = rd32(p, ins + 4);
        c->fixed = (type == 0x08);
        uint8_t mode = rd8(p, c->smp_off);
        c->loop_en = (rd8(p, c->smp_off + 3) & 0xC0) != 0;
        c->midc = (double)rd32(p, c->smp_off + 4) / 1024.0;
        c->loop_pos = rd32(p, c->smp_off + 8);
        c->end_pos  = rd32(p, c->smp_off + 12);
        c->dpcm_block = 0xFFFFFFFFu;

        if (c->loop_pos == 0 && c->end_pos == 0) {
            /* Golden Sun synth voice, selected by data byte 1 */
            uint8_t syn = rd8(p, c->smp_off + 16 + 1);
            c->pcm_type = syn == 0 ? P_SYN_PWM : syn == 1 ? P_SYN_SAW : P_SYN_TRI;
        } else if (c->end_pos >= 0x80000000u) {
            c->pcm_type = P_ADPCM;                /* Camelot 'negative' length */
            c->end_pos = (uint32_t)(-(int32_t)c->end_pos);
            c->loop_en = false;                   /* cannot loop */
        } else if (mode == 1) {
            c->pcm_type = P_DPCM;                 /* GameFreak compressed */
        } else if (mode == 0) {
            c->pcm_type = P_PCM;
        } else {
            c->state = ENV_DEAD;                  /* unknown sample mode */
            return;
        }
        if (c->loop_pos > c->end_pos) c->loop_pos = 0;      /* romhack fix */
        if (c->loop_pos == c->end_pos) c->loop_en = false;
    } else {
        /* PSG: 4-bit ADSR ranges */
        c->att &= 0x7; c->dec &= 0x7; c->sus &= 0xF; c->rel &= 0x7;
        uint8_t psg_len = rd8(p, ins + 2);
        if (psg_len > 0) {
            int inv = 64 - (psg_len & 0x3F);
            c->psg_len_count = (uint16_t)((inv * 60 + 128) / 256);
            c->psg_len_active = true;
        }
        uint32_t np = rd32(p, ins + 4);
        if (kind == V_SQ1 || kind == V_SQ2) {
            c->duty = (int)(np & 3);
            if (kind == V_SQ1) {
                c->sweep = rd8(p, ins + 3);
                c->sweep_en = c->sweep < 0x80 && (c->sweep & 0x7) != 0;
                if (c->sweep_en) {
                    int time = (c->sweep & 0x70) >> 4;
                    int shifts = c->sweep & 7;
                    float step_coeff = (c->sweep & 8)
                        ? (float)(128 - (128 >> shifts)) / 128.0f
                        : (float)(128 + (128 >> shifts)) / 128.0f;
                    if (time == 0) {
                        c->sweep_coeff = 1.0f;
                        c->sweep_en = false;
                    } else {
                        c->sweep_coeff = powf(step_coeff, (128.0f / (float)time) / 240.0f);
                    }
                    c->sweep_conv = (c->sweep & 8)
                        ? (float)((1 << shifts) - 1) : 2047.0f;
                }
            }
        } else if (kind == V_WAVE) {
            c->wave_off = np;
            int sum = 0;
            for (int i = 0; i < 16; i++) {
                uint8_t b = rd8(p, c->wave_off + (uint32_t)i);
                sum += (b >> 4) + (b & 0xF);
            }
            /* mean removal: -(sum/16)/32 in Q15 = -sum * 64 */
            c->wave_dc_q15 = -sum * 64;
        } else {
            c->duty = (int)(np & 1);
            if (c->duty == 0) { c->lfsr = 0x4000; c->lfsr_mask = 0x6000; }
            else              { c->lfsr = 0x40;   c->lfsr_mask = 0x60; }
            c->noise_q15 = -16384;
        }
    }

    t->upd_vol = true;
    t->upd_pitch = true;
}

/* ---- sequencer: one tick (24 per beat) ---- */
static void track_stop_all(Mp2kPlayer *p, int ti, bool kill)
{
    for (int i = 0; i < MP2K_MAX_CHANNELS; i++) {
        Chan *c = &p->chan[i];
        if (c->state != ENV_DEAD && c->track == ti) {
            if (kill) chan_kill(c); else chan_release(c);
        }
    }
}

static void track_fine(Mp2kPlayer *p, int ti)
{
    track_stop_all(p, ti, false);
    p->trk[ti].enabled = false;
}

static void cmd_memacc(Mp2kPlayer *p, Track *t)
{
    uint8_t op = rd8(p, t->pos++);
    uint8_t idx = rd8(p, t->pos++);
    uint8_t data = rd8(p, t->pos++);
    uint8_t *m = &p->memacc[idx];
    bool jump = false;

    switch (op) {
    case 0:  *m = data; return;
    case 1:  *m += data; return;
    case 2:  *m -= data; return;
    case 3:  *m = p->memacc[data]; return;
    case 4:  *m += p->memacc[data]; return;
    case 5:  *m -= p->memacc[data]; return;
    case 6:  jump = *m == data; break;
    case 7:  jump = *m != data; break;
    case 8:  jump = *m > data; break;
    case 9:  jump = *m >= data; break;
    case 10: jump = *m <= data; break;
    case 11: jump = *m < data; break;
    case 12: jump = *m == p->memacc[data]; break;
    case 13: jump = *m != p->memacc[data]; break;
    case 14: jump = *m > p->memacc[data]; break;
    case 15: jump = *m >= p->memacc[data]; break;
    case 16: jump = *m <= p->memacc[data]; break;
    case 17: jump = *m < p->memacc[data]; break;
    default: return;
    }
    if (jump) t->pos = rd32(p, t->pos);
    else t->pos += 4;
}

static void cmd_xcmd(Mp2kPlayer *p, int ti)
{
    Track *t = &p->trk[ti];
    uint8_t n = rd8(p, t->pos++);
    switch (n) {
    case 1:  t->pos += 4; break;                  /* XWAVE */
    case 2: case 4: case 5: case 6: case 7:
    case 10: case 11:
        t->pos++; break;                          /* 1-byte stubs */
    case 8:  t->echo_vol = rd8(p, t->pos++); break;   /* XIECV */
    case 9:  t->echo_len = rd8(p, t->pos++); break;   /* XIECL */
    case 12:                                       /* XWAIT */
        t->delay = rd8(p, t->pos) | rd8(p, t->pos + 1) << 8;
        t->pos += 2;
        break;
    case 13: t->pos += 4; break;                  /* XSOFF */
    default: track_fine(p, ti); break;
    }
}

static void track_tick(Mp2kPlayer *p, int ti)
{
    Track *t = &p->trk[ti];
    if (!t->enabled) return;

    /* count down running notes on this track */
    for (int i = 0; i < MP2K_MAX_CHANNELS; i++) {
        Chan *c = &p->chan[i];
        if (c->state != ENV_DEAD && c->track == ti && !c->stop && c->length > 0)
            if (--c->length == 0) chan_release(c);
    }

    while (t->delay == 0 && t->enabled) {
        uint8_t cmd = rd8(p, t->pos);

        if (cmd < 0x80) {
            cmd = t->last_cmd;
            if (cmd < 0x80) { track_fine(p, ti); return; }
        } else {
            t->pos++;
            if (cmd >= 0xBD) t->last_cmd = cmd;
        }

        if (cmd >= 0xCF) {                        /* note / TIE */
            int len = LEN_LUT[cmd - 0xCF];
            if (rd8(p, t->pos) < 0x80) {
                t->last_key = rd8(p, t->pos++);
                if (rd8(p, t->pos) < 0x80) {
                    t->last_vel = rd8(p, t->pos++);
                    if (rd8(p, t->pos) < 0x80) len += rd8(p, t->pos++);
                }
            }
            note_on(p, ti, t->last_key, t->last_vel, len);
        } else if (cmd <= 0xB0) {                 /* delay */
            t->delay = LEN_LUT[cmd - 0x80];
        } else switch (cmd) {
        case 0xB1: track_fine(p, ti); break;      /* FINE */
        case 0xB2: t->pos = rd32(p, t->pos); t->loops++; break;   /* GOTO (loop) */
        case 0xB3:
            if (t->call_depth >= MP2K_CALL_STACK) { track_fine(p, ti); break; }
            t->ret[t->call_depth++] = t->pos + 4;
            t->pos = rd32(p, t->pos);
            break;
        case 0xB4:
            if (t->call_depth > 0) t->pos = t->ret[--t->call_depth];
            break;
        case 0xB5: {
            uint8_t count = rd8(p, t->pos++);
            if (count == 0) { track_fine(p, ti); break; }
            if (++t->rept_count < count) t->pos = rd32(p, t->pos);
            else { t->rept_count = 0; t->pos += 4; }
            break;
        }
        case 0xB9: cmd_memacc(p, t); break;
        case 0xBA: t->prio = rd8(p, t->pos++); break;
        case 0xBB: p->bpm = rd8(p, t->pos++) * 2; break;
        case 0xBC: t->keyshift = (int8_t)rd8(p, t->pos++); t->upd_pitch = true; break;
        case 0xBD: t->prog = rd8(p, t->pos++); break;
        case 0xBE: t->vol = rd8(p, t->pos++); t->upd_vol = true; break;
        case 0xBF: t->pan = (int8_t)(rd8(p, t->pos++) - 0x40); t->upd_vol = true; break;
        case 0xC0: t->bend = (int8_t)(rd8(p, t->pos++) - 0x40); t->upd_pitch = true; break;
        case 0xC1: t->bendr = rd8(p, t->pos++); t->upd_pitch = true; break;
        case 0xC2:                                /* LFOS */
            t->lfos = rd8(p, t->pos++);
            if (t->lfos == 0) trk_reset_lfo(t);
            break;
        case 0xC3:                                /* LFODL */
            t->lfodl_count = t->lfodl = rd8(p, t->pos++);
            break;
        case 0xC4:                                /* MOD */
            t->mod = rd8(p, t->pos++);
            if (t->mod == 0) trk_reset_lfo(t);
            break;
        case 0xC5: {                              /* MODT */
            uint8_t modt = rd8(p, t->pos++);
            if (modt != t->modt) {
                t->modt = modt;
                t->upd_vol = true;
                t->upd_pitch = true;
            }
            break;
        }
        case 0xC8: t->tune = (int8_t)(rd8(p, t->pos++) - 0x40); t->upd_pitch = true; break;
        case 0xCD: cmd_xcmd(p, ti); break;
        case 0xCE: {                              /* EOT */
            uint8_t key = rd8(p, t->pos);
            if (key < 0x80) { t->pos++; t->last_key = key; }
            else key = t->last_key;
            for (int i = 0; i < MP2K_MAX_CHANNELS; i++) {
                Chan *c = &p->chan[i];
                if (c->state != ENV_DEAD && c->track == ti && !c->stop &&
                    c->key_track == key) {
                    chan_release(c);
                    break;
                }
            }
            break;
        }
        default: track_fine(p, ti); break;
        }
    }
    if (!t->enabled) return;
    t->delay--;

    /* LFO clocks once per tick (agbplay TrackMain) */
    if (t->lfos != 0 && t->mod != 0) {
        if (t->lfodl_count == 0) {
            t->lfo_phase = (uint8_t)(t->lfo_phase + t->lfos);
            int point;
            if ((int8_t)(t->lfo_phase - 64) >= 0) point = 128 - t->lfo_phase;
            else point = (int8_t)t->lfo_phase;
            point = (point * t->mod) >> 6;
            if (t->lfo_val != point) {
                t->lfo_val = (int8_t)point;
                if (t->modt == MODT_PITCH) t->upd_pitch = true;
                else t->upd_vol = true;
            }
        } else {
            t->lfodl_count--;
        }
    }
}

/* ---- PSG envelope (agbplay MP2KChnPSG, 4-bit, per 60 Hz frame) ---- */
static void psg_apply_vol(Chan *c)
{
    int vol = c->psg_vol, pan = c->psg_pan;
    int ml = ((127 - pan) * vol) >> 8;
    int mr = ((pan + 128) * vol) >> 8;
    int l = (((127 - c->rhythm_pan) * c->vel) * ml) >> 14;
    int r = (((c->rhythm_pan + 128) * c->vel) * mr) >> 14;

    if (r / 2 >= l)      c->pan_class = PAN_RIGHT;
    else if (l / 2 >= r) c->pan_class = PAN_LEFT;
    else                 c->pan_class = PAN_CENTER;

    c->env_peak = clampi((l + r) >> 4, 0, 15);
    c->env_sustain = clampi((c->env_peak * c->sus + 15) >> 4, 0, 15);
}

static int psg_echo_level(const Chan *c)
{
    return ((c->env_peak * c->echo_vol) + 0xFF) >> 8;
}

static void psg_env_frame(Chan *c)
{
    if (c->state == ENV_INIT) {
        if (c->stop) { c->state = ENV_DEAD; return; }
        psg_apply_vol(c);
        c->env_level = 0;
        c->frame_count = c->att;
        c->state = ENV_ATK;
        if (c->frame_count > 0) return;
        goto decay_start;
    }

    if (c->psg_len_active && c->psg_len_count > 0) {
        if (--c->psg_len_count == 0) { c->stop = true; c->fast_release = true; }
    }

    if (c->fast_release && c->state != ENV_DIE) {
        c->state = ENV_DIE;
        c->env_level = 0;
        return;
    }

    if (c->frame_count > 0) c->frame_count--;

    if (c->state == ENV_ECHO) {
        c->frame_count = 1;
        if (c->echo_len == 0 || --c->echo_len == 0) {
            c->state = ENV_DIE;
            c->env_level = 0;
        }
        return;
    }

    if (c->stop && c->state < ENV_REL) {
        c->state = ENV_REL;
        c->frame_count = c->rel;
        if (c->env_level == 0 || c->frame_count == 0) goto echo_start;
        return;
    }

    if (c->frame_count > 0) return;

    psg_apply_vol(c);

    switch (c->state) {
    case ENV_REL:
        if (--c->env_level <= 0) {
echo_start:
            c->frame_count = 1;
            c->env_level = psg_echo_level(c);
            if (c->env_level != 0 && c->echo_len != 0) {
                c->state = ENV_ECHO;
            } else {
                c->state = ENV_DIE;
                c->env_level = 0;
            }
        } else {
            c->frame_count = c->rel;
        }
        break;
    case ENV_SUS:
sustain_state:
        c->frame_count = 1;
        c->env_level = c->env_sustain;
        break;
    case ENV_DEC:
        c->env_level--;
        if (c->env_level <= c->env_sustain) {
sustain_start:
            if (c->sus == 0) { c->state = ENV_REL; goto echo_start; }
            c->state = ENV_SUS;
            c->env_level = c->env_sustain;
            goto sustain_state;
        }
        c->frame_count = c->dec;
        break;
    case ENV_ATK:
        c->env_level++;
        if (c->env_level >= c->env_peak) {
decay_start:
            c->state = ENV_DEC;
            c->frame_count = c->dec;
            if (c->env_peak == 0 || c->frame_count == 0 ||
                c->env_peak == c->env_sustain)
                goto sustain_start;
            c->env_level = c->env_peak;
        } else {
            c->frame_count = c->att;
        }
        break;
    case ENV_DIE:
        c->state = ENV_DEAD;
        break;
    }
}

/* ---- PCM envelope (agbplay MP2KChnPCM, 8-bit, per 60 Hz frame) ---- */
static void pcm_env_frame(Chan *c)
{
    if (c->state == ENV_INIT) {
        if (c->stop) { c->state = ENV_DEAD; return; }
        c->env = 0;
        c->state = ENV_ATK;
    }

    if (c->state == ENV_ECHO) {
        if (c->echo_len == 0 || --c->echo_len == 0) { c->state = ENV_DEAD; c->env = 0; }
        return;
    }

    if (c->stop) {
        c->env = (c->env * c->rel) >> 8;
        if (c->env <= c->echo_vol) {
            if (c->echo_vol == 0 || c->echo_len == 0) {
                c->state = ENV_DEAD;
                c->env = 0;
            } else {
                c->state = ENV_ECHO;
                c->env = c->echo_vol;
            }
        }
        return;
    }

    if (c->state == ENV_DEC) {
        c->env = (c->env * c->dec) >> 8;
        if (c->env <= c->sus) {
            c->env = c->sus;
            if (c->env == 0) {
                if (c->echo_vol != 0 && c->echo_len != 0) {
                    c->state = ENV_ECHO;
                    c->env = c->echo_vol;
                } else {
                    c->state = ENV_DEAD;
                }
            } else {
                c->state = ENV_SUS;
            }
        }
    } else if (c->state == ENV_ATK) {
        int e = c->env + c->att;
        if (e >= 255) { e = 255; c->state = ENV_DEC; }
        c->env = e;
    }
}

/* ---- per-frame channel updates that aren't the sequencer ---- */
static void chan_frame(Mp2kPlayer *p, Chan *c)
{
    if (c->kind == V_PCM) {
        pcm_env_frame(c);
        if (c->pcm_type == P_SYN_PWM && c->state != ENV_DEAD) {
            uint8_t duty_step = rd8(p, c->smp_off + 16 + 3);
            c->syn_pos += (uint32_t)duty_step << 24;
        }
        return;
    }
    psg_env_frame(c);
    if (c->state == ENV_DEAD) return;

    /* SQ1 hardware sweep, stepped at frame rate (agbplay steps at 240 Hz;
     * we apply the 240 Hz coefficient 4x per frame) */
    if ((c->kind == V_SQ1) && c->sweep_en && c->sweep_start >= 0) {
        for (int i = 0; i < 4; i++) {
            if (c->sweep_start == 0) {
                c->sweep_timer *= c->sweep_coeff;
                if (c->sweep & 8) {   /* descending: converge downwards */
                    if (c->sweep_timer < c->sweep_conv) c->sweep_timer = c->sweep_conv;
                } else {
                    if (c->sweep_timer > c->sweep_conv) c->sweep_timer = c->sweep_conv;
                }
            } else {
                c->sweep_start -= 128;
                if (c->sweep_start < 0) c->sweep_start = 0;
            }
        }
        c->step_fp = step_fp_from(8.0 * (double)sq_timer2freq(c->sweep_timer),
                                  p->cfg.sample_rate);
    }
}

/* ---- sample fetch (PCM voice family) ---- */
static const int8_t DPCM_DELTA[16] = {
    0, 1, 4, 9, 16, 25, 36, 49, -64, -49, -36, -25, -16, -9, -4, -1
};

static int8_t dpcm_fetch(Mp2kPlayer *p, Chan *c, uint32_t idx)
{
    uint32_t block = idx / 64;
    if (c->dpcm_block != block) {
        uint32_t base = c->smp_off + 16 + block * 0x21;
        int8_t acc = (int8_t)rd8(p, base);
        c->dpcm_buf[0] = acc;
        acc = (int8_t)(acc + DPCM_DELTA[rd8(p, base + 1) & 0xF]);
        c->dpcm_buf[1] = acc;
        for (uint32_t j = 2, h = 2; j < 64; j += 2, h++) {
            uint8_t b = rd8(p, base + h);
            acc = (int8_t)(acc + DPCM_DELTA[b >> 4]);
            c->dpcm_buf[j] = acc;
            acc = (int8_t)(acc + DPCM_DELTA[b & 0xF]);
            c->dpcm_buf[j + 1] = acc;
        }
        c->dpcm_block = block;
    }
    return c->dpcm_buf[idx % 64];
}

static void adpcm_decode_next(Mp2kPlayer *p, Chan *c)
{
    uint32_t pos = c->adp_pos;
    uint8_t data = rd8(p, c->smp_off + 16 + pos / 2);
    uint32_t nibble = (pos & 1)
        ? ((uint32_t)(data << 28) & 0xF0000000u)
        : ((uint32_t)(data << 24) & 0xF0000000u);
    if (c->adp_shift <= 63)
        c->adp_level = (int16_t)(c->adp_level +
            ((int32_t)nibble >> (c->adp_shift >> 1)));
    if (nibble & 0x80000000u) nibble = (uint32_t)(-(int32_t)nibble);
    c->adp_shift = (uint8_t)(c->adp_shift + 4);
    c->adp_shift = (uint8_t)(c->adp_shift - (nibble >> 28));
    c->adp_prev = c->adp_cur;
    c->adp_cur = c->adp_level;
    c->adp_pos = pos + 1;
}

/* ---- audio: one sample of one channel, into pcm or cgb bus (Q15) ---- */
static void chan_sample(Mp2kPlayer *p, Chan *c,
                        int32_t *pcm_l, int32_t *pcm_r,
                        int32_t *cgb_l, int32_t *cgb_r)
{
    if (c->kind == V_PCM) {
        int32_t v;                                /* Q15, nominal +-32768 */
        switch (c->pcm_type) {
        case P_SYN_PWM: {
            uint8_t base  = rd8(p, c->smp_off + 16 + 2);
            uint8_t depth = rd8(p, c->smp_off + 16 + 4);
            uint8_t init  = rd8(p, c->smp_off + 16 + 5);
            uint32_t it = ((uint32_t)init << 24) + c->syn_pos;
            it = (int32_t)it < 0 ? ~it >> 8 : it >> 8;
            it = it * depth + ((uint32_t)base << 24);
            uint32_t ph = (uint32_t)c->pos_fp;    /* phase 0..1 in Q32 */
            v = (ph < it ? 16384 : -16384) + 16384 - (int32_t)(it >> 17);
            c->pos_fp = (c->pos_fp + c->step_fp) & 0xFFFFFFFFu;
            break;
        }
        case P_SYN_SAW: {
            const uint32_t fix = 0x70;
            c->pos_fp = (c->pos_fp + c->step_fp) & 0xFFFFFFFFu;
            uint32_t ph = (uint32_t)c->pos_fp;
            uint32_t var1 = (ph >> 24) - fix;
            uint32_t var2 = (ph >> 16) << 17;
            uint32_t var3 = var1 - (var2 >> 27);
            c->syn_pos = var3 + (uint32_t)((int32_t)c->syn_pos >> 1);
            v = (int32_t)c->syn_pos << 7;         /* /256 in Q15 */
            break;
        }
        case P_SYN_TRI: {
            c->pos_fp = (c->pos_fp + c->step_fp) & 0xFFFFFFFFu;
            uint32_t p16 = (uint32_t)c->pos_fp >> 16;
            v = p16 < 32768 ? ((int32_t)(4 * p16) - 65536) >> 1
                            : (3 * 65536 - (int32_t)(4 * p16)) >> 1;
            break;
        }
        default: {                                /* PCM / DPCM / ADPCM */
            uint32_t len = c->end_pos;
            if (len == 0) { c->state = ENV_DEAD; return; }
            if ((uint32_t)(c->pos_fp >> 32) >= len) {
                if (c->loop_en && len > c->loop_pos) {
                    uint64_t range = (uint64_t)(len - c->loop_pos) << 32;
                    while ((uint32_t)(c->pos_fp >> 32) >= len)
                        c->pos_fp -= range;
                } else {
                    c->state = ENV_DEAD;
                    return;
                }
            }
            uint32_t i0 = (uint32_t)(c->pos_fp >> 32);
            uint32_t i1 = i0 + 1;
            if (i1 >= len) i1 = c->loop_en ? c->loop_pos : i0;
            int32_t frac8 = (int32_t)((uint32_t)(c->pos_fp >> 24) & 0xFF);
            int32_t s0, s1;
            if (c->pcm_type == P_PCM) {
                s0 = (int8_t)rd8(p, c->smp_off + 16 + i0);
                s1 = (int8_t)rd8(p, c->smp_off + 16 + i1);
            } else if (c->pcm_type == P_DPCM) {
                s0 = dpcm_fetch(p, c, i0);
                s1 = dpcm_fetch(p, c, i1);
            } else {                              /* ADPCM: forward-only */
                while (c->adp_pos < i0 + 2 && c->adp_pos < len)
                    adpcm_decode_next(p, c);
                if (c->adp_pos >= i0 + 2) { s0 = c->adp_prev; s1 = c->adp_cur; }
                else                      { s0 = c->adp_cur;  s1 = c->adp_cur; }
            }
            /* Linear, like Nintendo's real MP2K mixer ("triangular" per
             * agbplay docs). */
            v = (s0 << 8) + (s1 - s0) * frac8;    /* lerp, Q15 */
            c->pos_fp += c->step_fp;
            break;
        }
        }
        /* env(0..255) * lvol(0..255) -> Q16 gain */
        uint32_t gl = (uint32_t)c->env * c->lvol;
        uint32_t gr = (uint32_t)c->env * c->rvol;
        *pcm_l += (int32_t)(((int64_t)v * (int32_t)gl) >> 16);
        *pcm_r += (int32_t)(((int64_t)v * (int32_t)gr) >> 16);
        return;
    }

    /* PSG voices: DC-corrected waveforms, 4-bit level, hard pan */
    int32_t v;                                    /* Q15 */
    switch (c->kind) {
    case V_SQ1:
    case V_SQ2: {
        static const int duty_steps[4] = { 1, 2, 4, 6 };
        int steps = duty_steps[c->duty & 3];
        c->pos_fp += c->step_fp;                  /* step in pattern entries */
        uint32_t idx = (uint32_t)(c->pos_fp >> 32);
        if (idx >= 8) {
            c->pos_fp -= (uint64_t)(idx & ~7u) << 32;
            idx &= 7;
        }
        /* high = 1-d, low = -d, d = steps/8 (zero DC) */
        v = (int)idx < steps ? 32768 - steps * 4096 : -(steps * 4096);
        break;
    }
    case V_WAVE: {
        c->pos_fp += c->step_fp;                  /* steps in nibble units */
        uint32_t idx = (uint32_t)(c->pos_fp >> 32);
        if (idx >= 32) {
            c->pos_fp -= (uint64_t)(idx & ~31u) << 32;
            idx &= 31;
        }
        uint32_t idx1 = (idx + 1) & 31;
        uint8_t b0 = rd8(p, c->wave_off + idx / 2);
        uint8_t b1 = rd8(p, c->wave_off + idx1 / 2);
        int n0 = (idx & 1) ? (b0 & 0xF) : (b0 >> 4);
        int n1 = (idx1 & 1) ? (b1 & 0xF) : (b1 >> 4);
        int32_t frac8 = (int32_t)((uint32_t)(c->pos_fp >> 24) & 0xFF);
        /* linear interp between nibbles: tames the aliasing agbplay
         * removes with BLEP; most audible on high wave-channel keys */
        v = n0 * 2048 + (((n1 - n0) * 2048 * frac8) >> 8) + c->wave_dc_q15;
        break;
    }
    default: {                                    /* V_NOISE */
        c->pos_fp += c->step_fp;
        uint32_t clocks = (uint32_t)(c->pos_fp >> 32);
        c->pos_fp &= 0xFFFFFFFFu;
        while (clocks--) {
            if (c->lfsr & 1) {
                c->noise_q15 = 16384;
                c->lfsr >>= 1;
                c->lfsr ^= c->lfsr_mask;
            } else {
                c->noise_q15 = -16384;
                c->lfsr >>= 1;
            }
        }
        v = c->noise_q15;
        break;
    }
    }

    int lvl = c->env_level;                       /* amplitude = lvl/32 */
    if (c->kind == V_WAVE) {
        /* hardware wave volume quantization (agbplay accurateCh3Volume);
         * thresholds 1.5/5.5/9.5/13.5 scaled by 2 */
        int l2 = lvl * 2;
        if (l2 < 3)       lvl = 0;
        else if (l2 < 11) lvl = 4;
        else if (l2 < 19) lvl = 8;
        else if (l2 < 27) lvl = 12;
        else              lvl = 16;
    }
    int32_t out = (v * lvl) >> 5;
    if (c->pan_class != PAN_RIGHT) *cgb_l += out;
    if (c->pan_class != PAN_LEFT)  *cgb_r += out;
}

/* How many full loops the song has completed = the fewest loops taken by any
 * still-playing track (a track that hit FINE no longer constrains). This makes
 * a fast 1-bar drum loop wait for the slowest track, so one "loop" is one pass
 * of the whole arrangement. Returns 0 while any track is still on its first
 * pass. (An all-FINE song ends natively and never reaches here.) */
static int song_loops(const Mp2kPlayer *p)
{
    int m = -1;
    for (int i = 0; i < p->n_tracks; i++) {
        const Track *t = &p->trk[i];
        if (!t->enabled) continue;
        if (m < 0 || t->loops < m) m = t->loops;
    }
    return m < 0 ? 0 : m;
}

bool mp2k_render(Mp2kPlayer *p, int16_t *buf, int n)
{
    /* Engine frames tick at the real GBA frame rate, 2^24/280896 =
     * 59.7275 Hz, not 60 Hz. MP2K assumes 60 internally, so hardware
     * plays ~0.46% slower than nominal BPM; agbplay reproduces this via
     * its speed-correction factor and the A/B test against it caught
     * the mismatch (constant -4.5 ms/s drift). Reverb ring sizing stays
     * rate/60, matching agbplay's ReverbEffect (AGB_APPROX_FPS). */
    const double frame_len = (double)p->cfg.sample_rate * 280896.0 / 16777216.0;
    int out = 0;
    n /= 2; /* stereo frames */

    while (out < n) {
        if (p->ended) {                          /* fade done: silence the rest */
            memset(&buf[2 * out], 0, (size_t)(n - out) * 2 * sizeof(int16_t));
            break;
        }
        if (p->frame_acc <= 0.0) {
            /* one engine frame (59.7275 Hz) */
            p->tick_acc += p->bpm;
            while (p->tick_acc >= 150) {
                p->tick_acc -= 150;
                for (int i = 0; i < p->n_tracks; i++) track_tick(p, i);
            }
            /* song-end policy: begin fading once we've looped enough */
            if (p->loop_count > 0 && !p->fading
                && song_loops(p) >= p->loop_count)
                p->fading = true;
            /* apply pending volume/pitch (incl. LFO) to channels */
            for (int i = 0; i < p->n_tracks; i++) {
                Track *t = &p->trk[i];
                if (!t->upd_vol && !t->upd_pitch) continue;
                uint16_t vol = trk_vol(t);
                int16_t pan = trk_pan(t);
                int pitch = trk_pitch(t);
                for (int j = 0; j < MP2K_MAX_CHANNELS; j++) {
                    Chan *c = &p->chan[j];
                    if (c->state == ENV_DEAD || c->track != i) continue;
                    if (t->upd_vol) chan_set_vol(c, vol, pan);
                    if (t->upd_pitch) chan_set_pitch(p, c, pitch);
                }
                t->upd_vol = t->upd_pitch = false;
            }
            for (int i = 0; i < MP2K_MAX_CHANNELS; i++)
                if (p->chan[i].state != ENV_DEAD) chan_frame(p, &p->chan[i]);
            p->frame_acc += frame_len;
        }
        int todo = (int)p->frame_acc + 1;
        if (todo > n - out) todo = n - out;

        for (int s = 0; s < todo; s++) {
            if (p->ended) {                       /* fade finished mid-buffer */
                buf[2 * (out + s)] = buf[2 * (out + s) + 1] = 0;
                continue;
            }
            int32_t pl = 0, pr = 0, gl = 0, gr = 0;
            for (int i = 0; i < MP2K_MAX_CHANNELS; i++) {
                Chan *c = &p->chan[i];
                if (c->state == ENV_DEAD || c->state == ENV_INIT) continue;
                chan_sample(p, c, &pl, &pr, &gl, &gr);
            }
            pl = (pl * (MP2K_PCM_MASTER_VOL + 1)) >> 4;
            pr = (pr * (MP2K_PCM_MASTER_VOL + 1)) >> 4;

            /* MP2K reverb on the PCM bus (agbplay ReverbEffect).
             * rev = sum4 * (level/128) / 4 = sum4 * level >> 9 */
            int32_t *rb = p->rev_buf;
            int32_t rev = (int32_t)(((int64_t)(rb[2 * p->rev_pos] +
                                               rb[2 * p->rev_pos + 1] +
                                               rb[2 * p->rev_pos2] +
                                               rb[2 * p->rev_pos2 + 1]) *
                                     p->rev_level) >> 9);
            pl += rev;
            pr += rev;
            rb[2 * p->rev_pos] = pl;
            rb[2 * p->rev_pos + 1] = pr;
            if (++p->rev_pos >= p->rev_frames) p->rev_pos = 0;
            if (++p->rev_pos2 >= p->rev_frames) p->rev_pos2 = 0;

            int32_t li = (int32_t)(((int64_t)(pl + gl) * MP2K_HEADROOM_Q16) >> 16);
            int32_t ri = (int32_t)(((int64_t)(pr + gr) * MP2K_HEADROOM_Q16) >> 16);

            if (p->fading) {                      /* linear fade to silence */
                if (p->fade_samples <= 0 || p->fade_pos >= p->fade_samples) {
                    li = ri = 0;
                    p->ended = true;
                } else {
                    int64_t g = p->fade_samples - p->fade_pos;
                    li = (int32_t)((int64_t)li * g / p->fade_samples);
                    ri = (int32_t)((int64_t)ri * g / p->fade_samples);
                    p->fade_pos++;
                }
            }
            buf[2 * (out + s)] = (int16_t)(li > 32767 ? 32767 : li < -32768 ? -32768 : li);
            buf[2 * (out + s) + 1] = (int16_t)(ri > 32767 ? 32767 : ri < -32768 ? -32768 : ri);
        }
        out += todo;
        p->frame_acc -= todo;
    }

    /* ended? loop-count fade finished (above), or one-shot: all tracks
     * disabled and all channels dead. Never un-set a fade-completed end. */
    bool any_track = false, any_chan = false;
    for (int i = 0; i < p->n_tracks; i++) any_track |= p->trk[i].enabled;
    for (int i = 0; i < MP2K_MAX_CHANNELS; i++) any_chan |= p->chan[i].state != ENV_DEAD;
    p->ended = p->ended || (!any_track && !any_chan);
    return !p->ended;
}
