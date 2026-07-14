#!/usr/bin/env python3
"""Generate the homebrew Webfoot (.wbf) test fixture from scratch.

Builds a complete .wbf (the container the Webfoot extractor emits and
core/webfoot plays) with every byte synthesized here:

  - the driver lookup tables (equal-tempered note periods, fine pitch-slide
    up/down factors, vibrato factors, a 64-point sine) generated from their
    mathematical definitions,
  - two short 8-bit PCM instruments (a looped square lead, a one-shot
    percussive blip),
  - one song whose packed pattern plays a little two-channel phrase with a
    tempo set, a volume slide, and a vibrato, exercising the sequencer and
    the effect paths the real games use.

The output is committed as a repo fixture (see
tests/fixtures/homebrew/SOURCES.md). The .wbf layout matches what
core/extractor/extractor_webfoot.c emits.

Usage: python3 make_wbf_fixture.py [out.wbf]

SPDX-License-Identifier: GPL-3.0-or-later
"""

import math
import struct
import sys


def build_luts() -> bytes:
    b = bytearray()
    # u32 note[120]: period, note 60 == 0x4000, one octave == x2
    for n in range(120):
        b += struct.pack("<I", round(16384 * 2 ** ((n - 60) / 12)))
    # u16 tup[256]: fine pitch up, 2^(r/192) in 1.14 fixed (base 16384)
    for r in range(256):
        b += struct.pack("<H", min(65535, round(16384 * 2 ** (r / 192))))
    # u16 tdn[256]: fine pitch down, 2^(-r/192) in 0.16 fixed (base 65536)
    for r in range(256):
        b += struct.pack("<H", min(65535, round(65536 * 2 ** (-r / 192))))
    # u16 vup[128]: vibrato up factor, 2^(m/1536) base 32768
    for m in range(128):
        b += struct.pack("<H", min(65535, round(32768 * 2 ** (m / 1536))))
    # u16 vdn[128]: vibrato down factor, 2^(-m/1536) base 65536
    for m in range(128):
        b += struct.pack("<H", min(65535, round(65536 * 2 ** (-m / 1536))))
    # s8 sine[64]
    for i in range(64):
        b += struct.pack("<b", max(-127, min(127, round(127 * math.sin(2 * math.pi * i / 64)))))
    assert len(b) == 2080
    return bytes(b)


def square(n, hi=90, lo=-90, period=32):
    return bytes((hi if (i % period) < period // 2 else lo) & 0xFF for i in range(n))


def blip(n):
    out = bytearray()
    for i in range(n):
        env = max(0.0, 1.0 - i / n)
        out.append(round(100 * env * (1 if (i % 12) < 6 else -1)) & 0xFF)
    return bytes(out)


def build() -> bytes:
    luts = build_luts()
    flag_lut = bytes(
        [0x04, 0x61, 0x80, 0x70, 0x08, 0x25, 0x69, 0x40, 0x07, 0x0F, 0x34, 0x2D, 0xE1, 0x0C]
    )

    # instruments' PCM
    lead = square(512)  # looped
    perc = blip(300)  # one-shot

    blob = bytearray(0x20)  # header placeholder

    def align4():
        while len(blob) % 4:
            blob.append(0)

    off_luts = len(blob)
    blob += luts
    off_flag = len(blob)
    blob += flag_lut
    align4()

    lead_off = len(blob)
    blob += lead
    perc_off = len(blob)
    blob += perc
    align4()

    off_insts = len(blob)
    # {u32 off_sample, u8 flags, u8 0, u16 loop_start, u16 loop_end, u16 base_freq}
    blob += struct.pack("<IBBHHH", lead_off, 0x01, 0, 0, len(lead), 16000)
    blob += struct.pack("<IBBHHH", perc_off, 0x00, 0, 0, len(perc), 16000)
    n_insts = 2

    # --- one song: a 4-row pattern, two channels ---
    # event byte: (sel<<4)|(ch+1); sel 0 => literal flag byte follows
    # flag bits: 1 note, 2 instrument, 4 volume, 8 effect(2 bytes)
    def ev(ch, flag, *payload):
        return bytes([(0 << 4) | (ch + 1), flag]) + bytes(payload)

    NOTE, INS, VOL, FX = 1, 2, 4, 8
    rows = []
    # row 0: ch0 lead note 60 ins0 vol48 + set-tempo 150 (fx20); ch1 perc note 48 ins1 vol64
    rows.append(
        ev(0, NOTE | INS | VOL | FX, 60, 0, 48, 20, 150)
        + ev(1, NOTE | INS | VOL, 48, 1, 64)
        + b"\x00"
    )
    # row 1: ch0 note 64 + vibrato (fx8, speed4 depth3); ch1 perc note 48 (retrig via new note)
    rows.append(ev(0, NOTE | FX, 64, 8, 0x43) + ev(1, NOTE, 48) + b"\x00")
    # row 2: ch0 note 67 + volume slide down (fx4, 0x03); ch1 perc note 48
    rows.append(ev(0, NOTE | FX, 67, 4, 0x03) + ev(1, NOTE, 48) + b"\x00")
    # row 3: ch0 note 72; ch1 note-off (0xFF)
    rows.append(ev(0, NOTE, 72) + ev(1, NOTE, 0xFF) + b"\x00")

    pat = bytes([len(rows)]) + b"".join(rows)

    off_orders = len(blob)
    blob += bytes([0, 0xFF])  # order list: pattern 0, then loop
    align4()
    pat_off = len(blob)
    blob += pat
    align4()
    off_pat_tbl = len(blob)
    blob += struct.pack("<I", pat_off)

    off_songs = len(blob)
    # {u32 off_orders, u32 off_pat_tbl, u16 n_pats, u8 tempo, u8 speed, u32 0}
    blob += struct.pack("<IIHBBI", off_orders, off_pat_tbl, 1, 120, 6, 0)
    n_songs = 1

    struct.pack_into(
        "<4sBBHIIIIII",
        blob,
        0,
        b"WBF1",
        n_songs,
        n_insts,
        0,
        off_luts,
        off_flag,
        off_songs,
        off_insts,
        len(blob),
        0,
    )
    return bytes(blob)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "chiptune.wbf"
    data = build()
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out}: {len(data)} bytes")
