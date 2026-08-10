# Webfoot sound-driver sequence format (Legacy of Goku II / Buu's Fury)

This document reverse-engineers the format from the Dragon Ball Z: The
Legacy of Goku II PAL cartridge dump (header `DRAGONBALL Z`, code `ALFP`,
Webfoot Technologies driver). No earlier sequence-level description of the
engine exists. Named offsets below are **LoG II PAL**; a US (`ALFE`) build
has the same structures at different addresses. In this repo,
`core/extractor/extractor_webfoot.c` locates every table by content
signature, with no per-game addresses, and `core/webfoot/` plays the
extracted data.

The format carries over unchanged to **Buu's Fury** (`DBZBUUSFURY`,
`BG3E`). The evidence: the same equal-tempered note table signature
(`0x200, 0x21E, 0x23F…`), the same slide-factor LUTs, and the same BGM
table structure — stride-20 entries that share one instrument-table
pointer and one flag-LUT pointer.

Buu's Fury specifics: BGM table at `0x3BB78C`, 53 songs, 79 instruments at
`0x36D18C`, note table at `0x7B6724`. All songs parse with zero desync.
The two games share several tracks.

## Games on this driver

Testing verifies signature detection, extraction, and rendering on six
Webfoot GBA titles:

| Game | Songs | Instruments |
|---|---|---|
| Dragon Ball Z: The Legacy of Goku II | 44 | 101 |
| Dragon Ball Z: Buu's Fury | 53 | 79 |
| Dragon Ball GT: Transformation | 25 | 44 |
| Hello Kitty: Happy Party Pals | 27 | 90 |
| My Little Pony: The Runaway Rainbow | 15 | 24 |
| Tonka: On the Job | 17 | 40 |

Testing validates playback accuracy against mGBA on the two Legacy of Goku
titles.

## How it was found

The in-game sound test (`Music Test: %d` string @`0x009978`) calls
`PlayBGM` with a raw song index. Tracing it: the index scales by 20
(`0x0837…` struct stride) to index the **BGM table @`0x4047AC`** (44
entries). The driver code lives at `0x1F600–0x22200` (Thumb); the mixer
streams 8-bit PCM to FIFO A/B via DMA1/2 timed by TM0 (`0x04000100`).
Per-tick sequencer: `0x20DBC` (row advance), `0x20CD0` (row parser),
`0x1FE86` (per-channel note/instrument apply), `0x1FF22` (resampling
mixer).

## BGM table: `0x4047AC`, 44 × 20 bytes

| off | type | meaning |
|-----|------|---------|
| +0  | u32  | pattern pointer table (array of u32 pattern ptrs) |
| +4  | u32  | order list (bytes; `0xFF` = end → loop to start) |
| +8  | u32  | instrument table (shared `0x37A524`, all songs) |
| +12 | u32  | flag LUT (shared `0x37A9E0`, 14 bytes) |
| +16 | u8   | tempo (≈BPM at 4 rows/beat) |
| +17 | u8   | speed (ticks per row) |
| +18 | u16  | pad |

Songs run 3–41 orders, up to ~37 patterns, **8 melodic channels** (one song
touches 13). Tempos 65–140, speed 3–6.

## Order list

Sequence of pattern indices, terminated by `0xFF`. On `0xFF` the driver
resets to index 0 (songs loop forever; song-end is the player's
silence/loop-count policy).

## Pattern

    u8  rowCount
    rows[rowCount]

Each **row** is a stream of channel events terminated by a `0x00` byte:

    event u8:  low nibble = channel+1 (1..15);  high nibble = flag-selector
      sel 0  -> next byte is a literal `flags` byte (also cached per channel)
      sel 1  -> reuse this channel's last literal `flags`
      sel ≥2 -> flags = flagLUT[sel-2]   (14-entry common-case table)

`flags` bits: the low nibble pulls bytes from the stream (in order), the
high nibble reuses per-channel stored state; the driver keeps last note,
volume, and effect per channel and the note-on path fires on
`flags & 0x11`:

    bit0 (0x01)  note byte follows (0xFF = note-off; else semitone, 12..111)
    bit1 (0x02)  instrument byte follows (0..100)
    bit2 (0x04)  volume byte follows (0..0x40)
    bit3 (0x08)  effect byte + param byte follow
    bit4 (0x10)  retrigger the channel's stored note (no byte); how drum
                 tracks avoid repeating the note byte; ~36% of all note-ons
    bit5 (0x20)  keep current instrument (skip instrument-record reload;
                 dirty mask 0x17 instead of 0x1F in the note-on handler)
    bit6 (0x40)  reapply the channel's stored volume (no byte); with bit2
                 forms mask 0x44 = mid-note volume update without retrigger
    bit7 (0x80)  repeat last effect with stored param (dispatch mask 0x88)

`flagLUT` for LoG II: `04 61 80 70 08 25 69 40 07 0f 34 2d e1 0c`. The
selector scheme is pure size compression: the same note/vol/fx
combinations recur, so the format indexes common flag bytes instead of
repeating them.

Validation: all 44 songs parse with **zero** desync.

- Every row's event stream terminates exactly on its `0x00`.
- Every pattern consumes to its declared rowCount.
- Every instrument index is below 101.
- Every note is below 120.

## Instrument table: `0x37A524`, 101 × 12 bytes

| off | type | meaning |
|-----|------|---------|
| +0  | u32  | sample pointer (ROM) |
| +4  | u8   | flags: bit0 = looped, bit1 = ping-pong (bidirectional) loop; the render wrapper `0x1FF22` reflects position at the loop points and flips step sign (direction state: voice+0x11 bit2) |
| +5  | u8   | pad |
| +6  | u16  | loop start (samples) |
| +8  | u16  | loop end (= sample length for one-shots) |
| +10 | u16  | base frequency (Hz; sample's natural pitch = note 60) |

Samples are **8-bit signed PCM** (GBA DirectSound): 26 one-shot, 66
forward-looped, 9 ping-pong. The ping-pong samples are the long, evolving
pads and swells. Forward-looping them by mistake injects a click of up to
~120/255 at every loop pass and audibly "restarts" the swell instead of
bouncing it.

## Pitch

Note→rate table `0x7D61AC` (u32 per semitone) is an exact equal-tempered
scale: note 48 = `0x2000`, note 60 = `0x4000` (octave ratio exactly 2.000,
semitone 1.05945). Playback frequency of note *n* on an instrument with
base *bf* is `bf · 2^((n−60)/12)`; the mixer scales this rate by the
instrument base at note-on (`0x1FEF8`). Two more u16 tables feed effects:
`0x7D670C` (portamento-down factors) and `0x7D648C` (portamento-up
factors).

## Effects

A jump table at `0x7D638C` dispatches per-note effect handlers (32
entries); every handler first runs the note-on path (`0x202D0`), then its
own logic. Verified semantics: the numbering is Webfoot's own, *not* the
XM letter mapping:

    fx1  (0x20360)  set speed: player+3 = param (ticks/row, next row).
                    Alternating values (5/3, 6/3) every 2 rows = swing;
                    also used for fermatas (song 19 intro: 16) and header
                    overrides (song 14: 6→7, song 20: 6→4)
    fx2  (0x20376)  position jump: forces pattern end, order position =
                    param. Backward jump = the song's loop point; order
                    entries after the jump row are unreachable
    fx4  (0x203F4)  volume slide on the voice's working volume (chan+0x48):
                    param X0 = up X/tick, 0Y = down Y/tick, XF = fine up X
                    once, FY = fine down Y once; continuous slides step on
                    the row's remaining ticks and rows must re-assert to
                    keep sliding; the next note resets volume from its
                    volume column
    fx5  (0x20496)  portamento down (0xFx/0xEx = fine/extra-fine one-shot;
                    LUT 0x7D670C)
    fx6  (0x20524)  portamento up (mirror of fx5; LUT 0x7D648C)
    fx7  (0x20634)  tone portamento: note sets TARGET period only, the
                    sample is NOT retriggered (param = slide rate,
                    0xFF ≈ instant = legato). The note's volume column
                    (bit2) or reapply-volume (bit6) still updates the
                    sounding voice; omitting that leaves a lead ringing at
                    full volume through quiet passages; it affects most
                    songs of both games.
    fx8  (0x206F0)  vibrato: param = speed<<4 | depth (depth*4 internally),
                    signed sine LUT 0x7D690C, phase (chan+0x51) reset on
                    note-on; modulates period via LUTs 0x7D648C/0x7D670C
    fx15 (0x20958)  sample offset: voice position = param*256
    fx17 (0x209B0)  retrigger every (param & 0xF) ticks
    fx20 (0x20A28)  set tempo: player tick period = 40000/param, the same
                    math PlayBGM applies to the header tempo byte. Header
                    tempos are often placeholders overridden by an fx20 on
                    the first row (e.g. song 6: header 125, real 82), and
                    songs ramp tempo mid-song (song 7 ends in a 60-step
                    ritardando 130→64)
    fx24 (0x20A92)  set pan (signed, ±0x40 → voice+0x13); mono L+R sum is
                    pan-independent
    fx27 (0x209FC)  note delay by param ticks (the note-on fires `param`
                    ticks late, the driver defers it via a per-channel
                    counter rather than triggering at dispatch)

**Timing note:** header tempo/speed are only defaults; fx1/fx20 override
them, often on the very first row, so a correct renderer must execute
effects, not just the header.

## Timing

The driver mixes at 2^24/1050 ≈ 15 978 Hz with `40000/tempo` samples per
sequencer tick. The C engine in this repo outputs 32 768 Hz mono and keeps
tick boundaries integer-exact (one tick = `spt·1050/512` output samples,
tracked with a /512 remainder accumulator, no drift).

## Validation

All songs of both games parse with zero desync (see Pattern above).

mGBA hardware emulation validated the renderer built from this
description. This oracle is independent of the reverse-engineering model:
a libmgba harness boots the ROM and triggers each song. LoG II songs match
the emulator at ≥0.99 chroma-cosine similarity; the densest worst-case
track (Buu's Fury song 34) matches at 0.976, with no isolatable channel
error. Durations also line up with the circulating gamerip recordings,
within ~2 s on single-pass tracks.

---

*This document: CC-BY-SA 4.0.*
