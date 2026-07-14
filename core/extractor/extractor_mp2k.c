#include "extractor_internal.h"
#include <stdlib.h>
#include <string.h>

typedef struct {
    uint32_t start;
    uint32_t size;
} Chunk;

static int chunk_cmp(const void *a, const void *b) {
    const Chunk *ca = (const Chunk *)a;
    const Chunk *cb = (const Chunk *)b;
    if (ca->start < cb->start) return -1;
    if (ca->start > cb->start) return 1;
    return 0;
}

typedef struct {
    uint32_t site;
    uint32_t target;
    bool from_full;   /* recorded while fully walking a sub-bank */
} PtrSite;

typedef struct {
    Extractor *e;

    Chunk *chunks;
    int n_chunks;
    int cap_chunks;

    PtrSite *ptr_sites;
    int n_ptr_sites;
    int cap_ptr_sites;

    uint32_t *zero_entries;
    int n_zero_entries;
    int cap_zero_entries;

    /* byte ranges of fully-walked sub-banks (drum/keysplit) that must never be
     * zeroed by the main voicegroup's used[] optimization -- sub-banks overlap
     * the main voicegroup's address space, so an "unused" main-voice slot can
     * actually be a live drum/keysplit sub-voice. */
    Chunk *keep_ranges;
    int n_keep_ranges;
    int cap_keep_ranges;

    uint32_t *visited_banks;   /* fully-walked sub-banks (chunks already added) */
    int n_visited_banks;
    int cap_visited_banks;

    bool in_full;              /* currently inside a full sub-bank walk */
    bool error;
} Walk;

static void walk_init(Walk *w, Extractor *e) {
    memset(w, 0, sizeof(Walk));
    w->e = e;
    w->cap_chunks = 256;
    w->chunks = malloc(sizeof(Chunk) * w->cap_chunks);
    w->cap_ptr_sites = 256;
    w->ptr_sites = malloc(sizeof(PtrSite) * w->cap_ptr_sites);
    w->cap_zero_entries = 64;
    w->zero_entries = malloc(sizeof(uint32_t) * w->cap_zero_entries);
    w->cap_keep_ranges = 16;
    w->keep_ranges = malloc(sizeof(Chunk) * w->cap_keep_ranges);
    w->cap_visited_banks = 16;
    w->visited_banks = malloc(sizeof(uint32_t) * w->cap_visited_banks);
}

static void walk_free(Walk *w) {
    free(w->chunks);
    free(w->ptr_sites);
    free(w->zero_entries);
    free(w->keep_ranges);
    free(w->visited_banks);
}

static void add_chunk(Walk *w, uint32_t start, uint32_t size) {
    if (size == 0) return;
    if (w->n_chunks >= w->cap_chunks) {
        w->cap_chunks *= 2;
        w->chunks = realloc(w->chunks, sizeof(Chunk) * w->cap_chunks);
    }
    w->chunks[w->n_chunks].start = start;
    w->chunks[w->n_chunks].size = size;
    w->n_chunks++;
}

static void add_ptr_site(Walk *w, uint32_t site, uint32_t target) {
    if (w->n_ptr_sites >= w->cap_ptr_sites) {
        w->cap_ptr_sites *= 2;
        w->ptr_sites = realloc(w->ptr_sites, sizeof(PtrSite) * w->cap_ptr_sites);
    }
    w->ptr_sites[w->n_ptr_sites].site = site;
    w->ptr_sites[w->n_ptr_sites].target = target;
    w->ptr_sites[w->n_ptr_sites].from_full = w->in_full;
    w->n_ptr_sites++;
}

static void add_zero_entry(Walk *w, uint32_t pos) {
    if (w->n_zero_entries >= w->cap_zero_entries) {
        w->cap_zero_entries *= 2;
        w->zero_entries = realloc(w->zero_entries, sizeof(uint32_t) * w->cap_zero_entries);
    }
    w->zero_entries[w->n_zero_entries++] = pos;
}

static void add_keep_range(Walk *w, uint32_t start, uint32_t size) {
    if (size == 0) return;
    if (w->n_keep_ranges >= w->cap_keep_ranges) {
        w->cap_keep_ranges *= 2;
        w->keep_ranges = realloc(w->keep_ranges, sizeof(Chunk) * w->cap_keep_ranges);
    }
    w->keep_ranges[w->n_keep_ranges].start = start;
    w->keep_ranges[w->n_keep_ranges].size = size;
    w->n_keep_ranges++;
}

/* Check `pos` against the keep ranges [first, first+count), a per-view
 * slice, so each voicegroup view is filtered only by its own sub-banks. */
static bool in_keep_range(Walk *w, int first, int count, uint32_t pos) {
    for (int i = first; i < first + count; i++)
        if (pos >= w->keep_ranges[i].start &&
            pos < w->keep_ranges[i].start + w->keep_ranges[i].size)
            return true;
    return false;
}

static bool bank_visited(Walk *w, uint32_t bank) {
    for (int i = 0; i < w->n_visited_banks; i++) {
        if (w->visited_banks[i] == bank) return true;
    }
    if (w->n_visited_banks >= w->cap_visited_banks) {
        w->cap_visited_banks *= 2;
        w->visited_banks = realloc(w->visited_banks, sizeof(uint32_t) * w->cap_visited_banks);
    }
    w->visited_banks[w->n_visited_banks++] = bank;
    return false;
}

static uint32_t ptr(Walk *w, uint32_t p) {
    if (p + 3 >= w->e->rom_size) { w->error = true; return 0; }
    uint32_t v = rd32(w->e->rom, p);
    uint32_t t = v - 0x08000000;
    if (t >= w->e->rom_size) { w->error = true; return 0; }
    add_ptr_site(w, p, t);
    return t;
}

static int xcmd_len(uint8_t sub) {
    if (sub == 0 || sub == 3 || sub > 13) return -1;
    if (sub == 1 || sub == 13) return 5;
    if (sub == 12) return 3;
    return 2;
}

static void trace_track(Walk *w, uint32_t start) {
    uint32_t todo[64];
    uint8_t todo_last[64];
    int todo_n = 1;
    todo[0] = start;
    todo_last[0] = 0;

    uint8_t *visited = calloc(1, w->e->rom_size);
    uint32_t stack[16];

    while (todo_n > 0) {
        todo_n--;
        uint32_t pos = todo[todo_n];
        uint8_t last = todo_last[todo_n];
        uint32_t run_start = pos;
        int stack_n = 0;

        while (1) {
            if (pos >= w->e->rom_size) { w->error = true; break; }
            if (visited[pos]) {
                if (stack_n > 0) {
                    add_chunk(w, run_start, pos > run_start ? pos - run_start : 1);
                    stack_n--;
                    pos = stack[stack_n];
                    run_start = pos;
                    continue;
                }
                break;
            }
            visited[pos] = 1;
            uint8_t cmd = w->e->rom[pos];
            if (cmd < 0x80) {
                if (last >= 0xCF) {
                    pos++;
                    for (int j = 0; j < 2; j++) {
                        if (pos < w->e->rom_size && w->e->rom[pos] < 0x80) { visited[pos++] = 1; } else break;
                    }
                } else if (last == 0xCE) {
                    pos++;
                } else if (last == 0xCD) {
                    int n = xcmd_len(w->e->rom[pos]);
                    if (n < 0) { pos++; break; }
                    for (int k = 1; k < n; k++) visited[pos + k] = 1;
                    pos += n;
                } else if (last >= 0xBD && last <= 0xC8) {
                    pos++;
                } else {
                    pos++; break;
                }
                continue;
            }
            pos++;
            if (cmd >= 0xBD) last = cmd;
            if (cmd <= 0xB0) continue;
            if (cmd == 0xB1) break;
            if (cmd == 0xB2) {
                add_chunk(w, run_start, pos + 4 - run_start);
                pos = run_start = ptr(w, pos);
                continue;
            }
            if (cmd == 0xB3) {
                add_chunk(w, run_start, pos + 4 - run_start);
                uint32_t target = ptr(w, pos);
                if (stack_n < 16) stack[stack_n++] = pos + 4;
                pos = run_start = target;
                continue;
            }
            if (cmd == 0xB4) {
                add_chunk(w, run_start, pos - run_start);
                if (stack_n == 0) continue;
                stack_n--;
                pos = run_start = stack[stack_n];
                continue;
            }
            if (cmd == 0xB5) {
                uint32_t target = ptr(w, pos + 1);
                if (todo_n < 64) { todo[todo_n] = target; todo_last[todo_n] = last; todo_n++; }
                pos += 5;
                continue;
            }
            if (cmd == 0xB9) {
                uint8_t op = w->e->rom[pos];
                visited[pos] = 1; visited[pos+1] = 1; visited[pos+2] = 1;
                if (op >= 6 && op <= 17) {
                    uint32_t target = ptr(w, pos + 3);
                    if (todo_n < 64) { todo[todo_n] = target; todo_last[todo_n] = last; todo_n++; }
                    visited[pos+3] = 1; visited[pos+4] = 1; visited[pos+5] = 1; visited[pos+6] = 1;
                    pos += 7;
                } else pos += 3;
                continue;
            }
            if (cmd >= 0xBA && cmd <= 0xC8 && cmd != 0xC6 && cmd != 0xC7) {
                visited[pos++] = 1; continue;
            }
            if (cmd == 0xCD) {
                int n = xcmd_len(w->e->rom[pos]);
                if (n < 0) break;
                for (int k = 0; k < n; k++) visited[pos + k] = 1;
                pos += n;
                continue;
            }
            if (cmd == 0xCE || cmd >= 0xCF) {
                int count = cmd >= 0xCF ? 3 : 1;
                for (int j = 0; j < count; j++) {
                    if (pos < w->e->rom_size && w->e->rom[pos] < 0x80) visited[pos++] = 1; else break;
                }
                continue;
            }
            break;
        }
        add_chunk(w, run_start, pos > run_start ? pos - run_start : 1);
    }
    free(visited);
}

static uint32_t sample_size(Walk *w, uint32_t pos) {
    if (pos + 16 > w->e->rom_size) return 16;
    uint32_t loop_pos = rd32(w->e->rom, pos + 8);
    uint32_t end = rd32(w->e->rom, pos + 12);
    if (loop_pos == 0 && end == 0) return 16 + 8;
    if (end >= 0x80000000u) return 16 + (uint32_t)((0x100000000ull - end) / 2);
    if (w->e->rom[pos] & 1) return 16 + (end + 63) / 64 * 0x21;
    return 16 + end;
}

/* Walk one voicegroup. A full walk (drum/keysplit sub-bank) is engine-visible
 * raw data: its chunk goes to the shared pool once, and its keep range is
 * recorded unconditionally so every view walking it can filter its zero
 * entries. Sub-banks overlap main voicegroups' address space, so an "unused"
 * main-voice slot can actually be a live drum/keysplit sub-voice. A partial
 * walk (main voicegroup, used[] pruning) adds no chunk for the bank itself:
 * the caller materializes a private per-view copy instead. */
static void walk_bank(Walk *w, uint32_t bank, int depth, bool full, uint8_t *used) {
    int n = 128;
    if (bank + n * 12 > w->e->rom_size) n = (w->e->rom_size > bank) ? (w->e->rom_size - bank) / 12 : 0;
    if (full) {
        add_keep_range(w, bank, n * 12);
        if (bank_visited(w, bank)) return;
        add_chunk(w, bank, n * 12);
    }
    bool outer_full = w->in_full;
    if (full) w->in_full = true;
    for (int i = 0; i < n; i++) {
        uint32_t pos = bank + i * 12;
        if (!full && used && !used[i]) { add_zero_entry(w, pos); continue; }
        uint8_t t = w->e->rom[pos];
        bool ok = true;
        if (t & 0x40) {
            if (depth > 0) ok = false;
            else {
                uint32_t sub = ptr(w, pos + 4);
                uint32_t keymap = ptr(w, pos + 8);
                add_chunk(w, keymap, 128);
                walk_bank(w, sub, depth + 1, true, NULL);
            }
        } else if (t == 0x80) {
            if (depth > 0) ok = false;
            else {
                uint32_t sub = ptr(w, pos + 4);
                walk_bank(w, sub, depth + 1, true, NULL);
            }
        } else if ((t & 0x07) == 3) {
            uint32_t wave = ptr(w, pos + 4);
            add_chunk(w, wave, 16);
        } else if ((t & 0x07) == 1 || (t & 0x07) == 2 || (t & 0x07) == 4) {
            /* no pointers */
        } else if (t == 0x00 || t == 0x08) {
            uint32_t smp = ptr(w, pos + 4);
            uint32_t size = sample_size(w, smp);
            if (smp + size > w->e->rom_size) ok = false;
            else add_chunk(w, smp, size);
        } else {
            ok = false;
        }
        if (!ok || w->error) {
            w->error = false;
            add_zero_entry(w, pos);
        }
    }
    w->in_full = outer_full;
}

/* ROM position -> pool offset. Chunks must be sorted and disjoint (i.e. only
 * call this after the qsort+merge pass). */
static uint32_t to_blob(uint32_t *chunk_boff, Walk *w, uint32_t rom_pos) {
    int lo = 0, hi = w->n_chunks - 1;
    while (lo <= hi) {
        int mid = lo + (hi - lo) / 2;
        const Chunk *c = &w->chunks[mid];
        if (rom_pos < c->start) hi = mid - 1;
        else if (rom_pos >= c->start + c->size) lo = mid + 1;
        else return chunk_boff[mid] + (rom_pos - c->start);
    }
    w->error = true;
    return 0;
}

static int bank_entries(const Extractor *e, uint32_t bank) {
    int n = 128;
    if (bank + (uint32_t)n * 12 > e->rom_size)
        n = (e->rom_size > bank) ? (int)((e->rom_size - bank) / 12) : 0;
    return n;
}

/* A private per-view copy of a main voicegroup: the bank's 12-byte slots
 * with unused (and invalid) ones zeroed, byte-identical to what a standalone
 * single-song extraction produces. Songs with identical (bank, used) share
 * one view. The z/k/s pairs are this view's slices of the Walk's global
 * zero-entry / keep-range / ptr-site lists. */
typedef struct {
    uint32_t bank;
    uint8_t used[128];
    int n_entries;
    uint32_t priv_off;   /* pool offset of the materialized copy */
    int z0, nz;
    int k0, nk;
    int s0, ns;
} BankView;

typedef struct {
    uint32_t pos;        /* song header ROM offset */
    int idx;             /* original songtable index */
    uint8_t n_tracks;
    int view;            /* index into views[]; -1 = dropped song */
} SongRef;

/* A pak stores ONE shared chunk pool: all songs' headers, track data,
 * samples, keymaps and drum/keysplit sub-banks are walked into a single
 * global chunk list, so ROM ranges shared between songs (sample data above
 * all) are stored once. Main voicegroups are the exception: their bytes
 * depend on the song (slots the song doesn't use are zeroed, which playback
 * relies on), so each distinct (voicegroup, used-set) view gets a private
 * copy and the song header's voicegroup pointer targets it. Every 16-byte
 * index entry points at the same pool (off = pool start, size = pool size)
 * with a per-song header offset. That is exactly the blob/hdr_off contract
 * mp2k_pak_get exposes, so the container layout is unchanged and any engine
 * build reads it. */
static uint32_t mp2k_build(Extractor *e, const int *song_indices, int num_songs, uint8_t **out_buffer) {
    int total_songs = e->song_count_fn(e);

    SongRef *songs = calloc(num_songs > 0 ? num_songs : 1, sizeof(SongRef));
    if (!songs) return 0;

    Walk w;
    walk_init(&w, e);

    BankView *views = NULL;
    int n_views = 0, cap_views = 0;

    /* Pass 1: trace every song's header + tracks; find or create its view. */
    for (int si = 0; si < num_songs; si++) {
        songs[si].view = -1;
        int idx = song_indices[si];
        if (idx < 0 || idx >= total_songs) continue;
        uint32_t song_pos = rd32(e->rom, e->table_pos + idx * 8) - 0x08000000;
        if (song_pos >= e->rom_size || e->rom_size - song_pos < 8) continue;
        uint8_t n_tracks = e->rom[song_pos];
        if (e->rom_size - song_pos < 8u + 4u * n_tracks) continue;

        int c0 = w.n_chunks, p0 = w.n_ptr_sites;
        w.error = false;
        add_chunk(&w, song_pos, 8 + 4 * n_tracks);
        uint32_t bank = ptr(&w, song_pos + 4);
        for (int i = 0; i < n_tracks; i++) trace_track(&w, ptr(&w, song_pos + 8 + 4 * i));
        if (w.error) {             /* bad pointer somewhere: drop this song */
            w.n_chunks = c0;
            w.n_ptr_sites = p0;
            w.error = false;
            continue;
        }

        uint8_t used[128] = {0};
        used[0] = 1;
        for (int i = c0; i < w.n_chunks; i++) {
            for (uint32_t p = w.chunks[i].start; p < w.chunks[i].start + w.chunks[i].size - 1; p++) {
                if (e->rom[p] == 0xBD && e->rom[p+1] < 0x80) { used[e->rom[p+1]] = 1; p++; }
            }
        }

        int v;
        for (v = 0; v < n_views; v++)
            if (views[v].bank == bank && memcmp(views[v].used, used, 128) == 0) break;
        if (v == n_views) {
            if (n_views >= cap_views) {
                cap_views = cap_views ? cap_views * 2 : 8;
                views = realloc(views, sizeof(BankView) * cap_views);
            }
            BankView *bv = &views[n_views++];
            bv->bank = bank;
            memcpy(bv->used, used, 128);
            bv->n_entries = bank_entries(e, bank);
            bv->z0 = w.n_zero_entries;
            bv->k0 = w.n_keep_ranges;
            bv->s0 = w.n_ptr_sites;
            walk_bank(&w, bank, 0, false, bv->used);
            bv->nz = w.n_zero_entries - bv->z0;
            bv->nk = w.n_keep_ranges - bv->k0;
            bv->ns = w.n_ptr_sites - bv->s0;
            w.error = false;
        }

        songs[si].pos = song_pos;
        songs[si].idx = idx;
        songs[si].n_tracks = n_tracks;
        songs[si].view = v;
    }

    qsort(w.chunks, w.n_chunks, sizeof(Chunk), chunk_cmp);
    int merged = 0;
    for (int i = 0; i < w.n_chunks; i++) {
        if (merged > 0 && w.chunks[i].start <= w.chunks[merged-1].start + w.chunks[merged-1].size) {
            uint32_t end1 = w.chunks[merged-1].start + w.chunks[merged-1].size;
            uint32_t end2 = w.chunks[i].start + w.chunks[i].size;
            w.chunks[merged-1].size = (end2 > end1 ? end2 : end1) - w.chunks[merged-1].start;
        } else {
            w.chunks[merged++] = w.chunks[i];
        }
    }
    w.n_chunks = merged;

    uint32_t *chunk_boff = malloc(sizeof(uint32_t) * (w.n_chunks > 0 ? w.n_chunks : 1));
    uint32_t pool_size = 0;
    for (int i = 0; i < w.n_chunks; i++) {
        chunk_boff[i] = pool_size;
        pool_size = (pool_size + w.chunks[i].size + 3) & ~3u;
    }
    for (int v = 0; v < n_views; v++) {
        views[v].priv_off = pool_size;
        pool_size = (pool_size + (uint32_t)views[v].n_entries * 12 + 3) & ~3u;
    }

    uint32_t pool_off = (12 + 16 * (uint32_t)num_songs + 3) & ~3u;
    uint8_t *out = calloc(1, pool_off + pool_size); /* zero header, entries, padding */
    if (!out || !chunk_boff) {
        free(out); free(chunk_boff); free(views); free(songs); walk_free(&w);
        return 0;
    }
    out[0] = 'S'; out[1] = 'N'; out[2] = 'C'; out[3] = 'H';
    out[4] = 'P'; out[5] = 'P'; out[6] = 'A'; out[7] = 'K';
    out[10] = num_songs; out[11] = num_songs >> 8;

    /* Shared pool: raw chunk copies, then every resolvable pointer site. */
    uint8_t *pool = out + pool_off;
    for (int i = 0; i < w.n_chunks; i++)
        memcpy(pool + chunk_boff[i], e->rom + w.chunks[i].start, w.chunks[i].size);
    for (int i = 0; i < w.n_ptr_sites; i++) {
        w.error = false;
        uint32_t ps = to_blob(chunk_boff, &w, w.ptr_sites[i].site);
        uint32_t pt = to_blob(chunk_boff, &w, w.ptr_sites[i].target);
        if (!w.error) {
            pool[ps] = pt; pool[ps+1] = pt>>8; pool[ps+2] = pt>>16; pool[ps+3] = pt>>24;
        }
    }

    /* Materialize the voicegroup views: raw copy, per-view zeroing, then
     * pointer relocation, the same order a standalone extraction applies.
     * Sites from other views' partial walks are skipped (their slots are
     * pruned here); sites recorded inside sub-banks apply to every view
     * they overlap. */
    for (int v = 0; v < n_views; v++) {
        BankView *bv = &views[v];
        uint8_t *pc = pool + bv->priv_off;
        uint32_t bsize = (uint32_t)bv->n_entries * 12;
        memcpy(pc, e->rom + bv->bank, bsize);
        for (int j = bv->z0; j < bv->z0 + bv->nz; j++) {
            uint32_t pos = w.zero_entries[j];
            if (in_keep_range(&w, bv->k0, bv->nk, pos)) continue;  /* live sub-voice */
            if (pos < bv->bank || pos - bv->bank >= bsize) continue;
            memset(pc + (pos - bv->bank), 0, 12);
        }
        for (int j = 0; j < w.n_ptr_sites; j++) {
            const PtrSite *s = &w.ptr_sites[j];
            bool own = j >= bv->s0 && j < bv->s0 + bv->ns;
            if (!own && !s->from_full) continue;
            if (s->site < bv->bank || s->site - bv->bank >= bsize) continue;
            w.error = false;
            uint32_t pt = to_blob(chunk_boff, &w, s->target);
            if (w.error) continue;
            uint8_t *at = pc + (s->site - bv->bank);
            at[0] = pt; at[1] = pt>>8; at[2] = pt>>16; at[3] = pt>>24;
        }
    }

    /* Index: every entry shares the pool; a dropped song's entry stays
     * all-zero, which mp2k_pak_get rejects. Each song header's voicegroup
     * pointer is re-targeted at the song's private view. */
    for (int si = 0; si < num_songs; si++) {
        if (songs[si].view < 0) continue;
        w.error = false;
        uint32_t hdr_off = to_blob(chunk_boff, &w, songs[si].pos);
        if (w.error) continue;
        uint32_t vg = views[songs[si].view].priv_off;
        pool[hdr_off+4] = vg; pool[hdr_off+5] = vg>>8;
        pool[hdr_off+6] = vg>>16; pool[hdr_off+7] = vg>>24;
        uint8_t *hdr = out + 12 + si * 16;
        hdr[0] = pool_off; hdr[1] = pool_off>>8; hdr[2] = pool_off>>16; hdr[3] = pool_off>>24;
        hdr[4] = pool_size; hdr[5] = pool_size>>8; hdr[6] = pool_size>>16; hdr[7] = pool_size>>24;
        hdr[8] = hdr_off; hdr[9] = hdr_off>>8; hdr[10] = hdr_off>>16; hdr[11] = hdr_off>>24;
        hdr[12] = songs[si].idx; hdr[13] = songs[si].idx >> 8;
        hdr[14] = songs[si].n_tracks;
    }

    free(chunk_boff);
    free(views);
    free(songs);
    walk_free(&w);
    *out_buffer = out;
    return pool_off + pool_size;
}

static int mp2k_song_count(Extractor *e) {
    uint32_t ptr = e->table_pos;
    if (ptr >= e->rom_size) return 0;
    int count = 0;
    while (ptr < e->rom_size) {
        uint32_t s = rd32(e->rom, ptr);
        if (s < 0x08000000 || s >= 0x08000000 + e->rom_size) break;
        count++; ptr += 8;
    }
    return count;
}

Extractor* mp2k_detect(const uint8_t *rom, uint32_t rom_size, uint32_t table_pos) {
    if (table_pos == 0) {
        if (rom_size < 0x1A4) return NULL;
        uint32_t sft = rd32(rom, 0x1A0);
        if (sft < 0x08000000 || sft >= 0x08000000 + rom_size) return NULL;
        table_pos = sft - 0x08000000;
        uint32_t s = rd32(rom, table_pos);
        if (s < 0x08000000 || s >= 0x08000000 + rom_size) return NULL;
    }

    Extractor *e = calloc(1, sizeof(Extractor));
    e->rom = rom;
    e->rom_size = rom_size;
    e->table_pos = table_pos;
    e->format = EXTRACTOR_FMT_MP2K;
    e->free_fn = NULL;
    e->song_count_fn = mp2k_song_count;
    e->build_fn = mp2k_build;
    return e;
}
