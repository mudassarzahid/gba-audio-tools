"""Extraction test against a synthetic mini-ROM.

The fake ROM contains one real song: a sequence with TEMPO/VOICE/notes and
a GOTO loop, a voicegroup whose program 0 is a DirectSound instrument, and
a PCM sample with recognizable marker bytes. The test checks the .pak is
self-contained: all relocated pointers resolve inside the blob and the
sample bytes made it in.
"""

import struct

from gba_audio.native import PAK_MAGIC, build_pak
from gba_audio.scanner import AGB_MAP_ROM, find_songtable

ROM_SIZE = 0x8000
SONGTABLE = 0x1000
SONG_HDR = 0x2000
VOICEGROUP = 0x3000
SEQ = 0x4000
SAMPLE = 0x5000
SAMPLE_LEN = 0x40
MARKER = 0xA5


def _build_rom() -> bytes:
    rom = bytearray(ROM_SIZE)

    # songtable: 4 entries pointing at the same song (scanner needs >=4)
    for i in range(4):
        struct.pack_into("<IBBBB", rom, SONGTABLE + i * 8, AGB_MAP_ROM + SONG_HDR, 0, 0, 0, 0)
    struct.pack_into("<I", rom, 0x500, AGB_MAP_ROM + SONGTABLE)  # reference

    # song header: 1 track
    struct.pack_into("<BBBBI", rom, SONG_HDR, 1, 0, 0, 0x80, AGB_MAP_ROM + VOICEGROUP)
    struct.pack_into("<I", rom, SONG_HDR + 8, AGB_MAP_ROM + SEQ)

    # voicegroup prog 0: DirectSound sample, ADSR 255/0/255/0
    struct.pack_into("<BBBBII", rom, VOICEGROUP, 0x00, 60, 0, 0, AGB_MAP_ROM + SAMPLE, 0x00FF00FF)

    # sequence: TEMPO 150bpm, VOICE 0, N?? key vel, delay, running-status
    # note (bare key byte), delay, GOTO back to the note
    seq = bytes([0xBB, 75, 0xBD, 0x00, 0xE0, 60, 100, 0x84, 62, 0x84])
    rom[SEQ : SEQ + len(seq)] = seq
    goto_at = SEQ + len(seq)
    rom[goto_at] = 0xB2
    struct.pack_into("<I", rom, goto_at + 1, AGB_MAP_ROM + SEQ + 4)

    # sample: plain PCM, no loop, SAMPLE_LEN bytes of marker
    struct.pack_into("<IIII", rom, SAMPLE, 0, 0x1000, 8, SAMPLE_LEN)
    for i in range(SAMPLE_LEN):
        rom[SAMPLE + 16 + i] = MARKER
    return bytes(rom)


def test_pak_roundtrip(tmp_path):
    rom = _build_rom()
    table = find_songtable(rom)
    assert table is not None

    out = tmp_path / "test.pak"
    stats = build_pak(rom, table, [0], str(out))
    assert stats["written"] == [0] and not stats["errors"]

    d = out.read_bytes()
    assert d[:8] == PAK_MAGIC
    ver, n = struct.unpack_from("<HH", d, 8)
    assert (ver, n) == (0, 1)
    off, size, hdr, idx, ntr, _ = struct.unpack_from("<IIIHBB", d, 12)
    assert idx == 0 and ntr == 1
    blob = d[off : off + size]

    # header at hdr: track count, then relocated voicegroup/track offsets
    assert blob[hdr] == 1
    vg_off = struct.unpack_from("<I", blob, hdr + 4)[0]
    trk_off = struct.unpack_from("<I", blob, hdr + 8)[0]
    assert vg_off < size and trk_off < size

    # instrument 0 at vg_off: sample pointer relocated into the blob,
    # and the sample data (marker bytes) is really there
    smp_off = struct.unpack_from("<I", blob, vg_off + 4)[0]
    assert smp_off < size
    smp_len = struct.unpack_from("<I", blob, smp_off + 12)[0]
    assert smp_len == SAMPLE_LEN
    data = blob[smp_off + 16 : smp_off + 16 + SAMPLE_LEN]
    assert data == bytes([MARKER]) * SAMPLE_LEN

    # sequence: track offset points at our TEMPO command
    assert blob[trk_off] == 0xBB

    # GOTO target relocated: last 4 bytes of the goto point inside the blob
    goto_ptr = struct.unpack_from("<I", blob, trk_off + 10 + 1)[0]
    assert goto_ptr < size and blob[goto_ptr] == 0xE0


SEQ2 = 0x6000


def _build_rom_patt_into_visited() -> bytes:
    """Track that flows linearly through a pattern body, then CALLS that
    (now already-visited) pattern via PATT, and only AFTER the call has its
    loop GOTO. Regression: the tracer used to stop at the visited pattern
    and drop everything after the call (found via a real ROM's track 7)."""
    rom = bytearray(_build_rom())

    pat = SEQ2  # pattern body: note, delay, PEND
    pat_body = bytes([0xBD, 0x00, 0xE0, 60, 100, 0x84, 0xB4])
    rom[pat : pat + len(pat_body)] = pat_body

    call_at = pat + len(pat_body)  # falls straight through into the call
    rom[call_at] = 0xB3  # PATT -> already-visited pattern
    struct.pack_into("<I", rom, call_at + 1, AGB_MAP_ROM + pat)
    after = call_at + 5
    rom[after : after + 2] = bytes([0xBD, 0x5A])  # marker cmd after the call
    rom[after + 2] = 0xB2  # loop GOTO
    struct.pack_into("<I", rom, after + 3, AGB_MAP_ROM + pat)

    # point the song's track at the pattern body start
    struct.pack_into("<I", rom, SONG_HDR + 8, AGB_MAP_ROM + pat)
    return bytes(rom)


def test_tracer_resumes_after_call_to_visited_pattern(tmp_path):
    rom = _build_rom_patt_into_visited()
    table = find_songtable(rom)
    assert table is not None

    out = tmp_path / "patt.pak"
    stats = build_pak(rom, table, [0], str(out))
    assert stats["written"] == [0] and not stats["errors"]

    d = out.read_bytes()
    off, size, hdr, idx, ntr, _ = struct.unpack_from("<IIIHBB", d, 12)
    blob = d[off : off + size]
    trk_off = struct.unpack_from("<I", blob, hdr + 8)[0]

    # everything after the PATT call must be present and relocated:
    # marker VOICE cmd, then GOTO whose pointer resolves inside the blob
    call = trk_off + 7  # pattern body is 7 bytes
    assert blob[call] == 0xB3
    assert blob[call + 5 : call + 7] == bytes([0xBD, 0x5A])
    assert blob[call + 7] == 0xB2
    goto_ptr = struct.unpack_from("<I", blob, call + 8)[0]
    assert goto_ptr == trk_off  # loops back to the pattern body


def test_used_scan_catches_running_status_voice(tmp_path):
    """A VOICE selected via running status (bare arg byte while the last
    command is still 0xBD) must keep its voicegroup slot. The used[] scan
    only matched literal BD-xx byte pairs, so such slots were zeroed and
    the pak played that instrument silent (found via Emerald song 393,
    slot 24)."""
    rom = bytearray(_build_rom())
    # voicegroup slot 1: a second DirectSound instrument, same sample
    struct.pack_into(
        "<BBBBII", rom, VOICEGROUP + 12, 0x00, 60, 0, 0, AGB_MAP_ROM + SAMPLE, 0x00FF00FF
    )
    # VOICE 0, delay, bare '1' = running-status VOICE 1, note, delay, FINE
    seq = bytes([0xBD, 0x00, 0x84, 0x01, 0xE0, 60, 100, 0x84, 0xB1])
    rom[SEQ : SEQ + len(seq)] = bytes(len(seq))  # clear the old sequence tail
    rom[SEQ : SEQ + len(seq)] = seq
    rom[SEQ + len(seq) : SEQ + 20] = bytes(20 - len(seq))

    table = find_songtable(bytes(rom))
    assert table is not None
    out = tmp_path / "rs.pak"
    build_pak(bytes(rom), table, [0], str(out))

    d = out.read_bytes()
    off, size, hdr, idx, ntr, _ = struct.unpack_from("<IIIHBB", d, 12)
    blob = d[off : off + size]
    vg_off = struct.unpack_from("<I", blob, hdr + 4)[0]
    slot1 = blob[vg_off + 12 : vg_off + 24]
    assert any(slot1), "running-status VOICE slot was zeroed out of the pak"
    smp_off = struct.unpack_from("<I", slot1, 4)[0]
    assert smp_off < size and blob[smp_off + 16] == MARKER
