"""Scanner test against a synthetic mini-ROM.

We build a fake ROM in memory with a valid MP2K songtable (correct entry
layout, song headers, voicegroup and track pointers, and a literal-pool
reference to the table) and check the scanner finds exactly it.
"""

import struct

import pytest

from gba_audio.scanner import (
    AGB_MAP_ROM,
    SongEntry,
    analyze_song,
    classify,
    find_songtable,
)

ROM_SIZE = 0x4000


def _build_fake_rom() -> bytes:
    rom = bytearray(ROM_SIZE)

    songtable_pos = 0x1000
    n_songs = 5
    song_headers_pos = 0x2000
    voicegroup_pos = 0x3000
    track_pos = 0x3100

    for i in range(n_songs):
        song_pos = song_headers_pos + i * 0x40
        # songtable entry: song ptr, player, 0, player, 0
        struct.pack_into("<IBBBB", rom, songtable_pos + i * 8, AGB_MAP_ROM + song_pos, 0, 0, 0, 0)
        # song header: nTracks=2, nBlocks=0, prio=0, rev=0x80, voicegroup ptr
        struct.pack_into("<BBBBI", rom, song_pos, 2, 0, 0, 0x80, AGB_MAP_ROM + voicegroup_pos)
        # two track pointers
        struct.pack_into(
            "<II", rom, song_pos + 8, AGB_MAP_ROM + track_pos, AGB_MAP_ROM + track_pos + 0x20
        )

    # literal-pool reference to the songtable (as in m4aSongNumStart)
    struct.pack_into("<I", rom, 0x500, AGB_MAP_ROM + songtable_pos)
    return bytes(rom)


def test_finds_songtable():
    table = find_songtable(_build_fake_rom())
    assert table is not None
    assert table.pos == 0x1000
    assert len(table.songs) == 5
    assert all(s.n_tracks == 2 for s in table.songs)
    assert table.songs[0].voicegroup_pos == 0x3000


def test_rejects_unreferenced_table():
    rom = bytearray(_build_fake_rom())
    struct.pack_into("<I", rom, 0x500, 0)  # remove the reference
    assert find_songtable(bytes(rom)) is None


# ---- track-event analyzer -------------------------------------------------

_SEQ = 0x200  # where the single track's bytecode lives in the mini buffer


def _mk_song(track: bytes) -> tuple[bytes, SongEntry]:
    """A minimal buffer with one song header whose single track is `track`,
    placed at _SEQ (so GOTO/PATT pointers can target AGB_MAP_ROM + _SEQ + k)."""
    buf = bytearray(0x2000)
    hdr = 0x100
    buf[hdr] = 1  # nTracks = 1
    struct.pack_into("<I", buf, hdr + 8, AGB_MAP_ROM + _SEQ)  # track pointer
    buf[_SEQ : _SEQ + len(track)] = track
    entry = SongEntry(0, hdr, 0, 1, 0)
    return bytes(buf), entry


def _note(key=60, vel=100):  # note command 0xD5 + key + velocity
    return bytes([0xD5, key, vel])


def test_analyzer_music_loops_long_dense():
    seq = bytes([0xBB, 75])  # TEMPO -> 150 bpm
    loop_start = _SEQ + len(seq)
    for _ in range(16):  # 16 notes, 96 ticks apart
        seq += _note() + bytes([0xB0])  # 0xB0 = 96-tick delay
    seq += bytes([0xB2]) + struct.pack("<I", AGB_MAP_ROM + loop_start)  # GOTO
    data, entry = _mk_song(seq)

    analyze_song(data, entry)
    assert entry.loops is True
    assert entry.note_count == 16
    assert entry.duration_ticks == 16 * 96
    assert entry.duration_sec == pytest.approx(16 * 96 / (16777216 / 280896), rel=1e-6)
    assert entry.kind == "music"


def test_analyzer_sfx_short_oneshot():
    seq = _note() + bytes([0x82, 0xB1])  # note, 2-tick delay, FINE
    data, entry = _mk_song(seq)

    analyze_song(data, entry)
    assert entry.loops is False
    assert entry.note_count == 1
    assert entry.duration_ticks == 2
    assert entry.kind == "sfx"


def test_analyzer_jingle_medium_oneshot():
    seq = b""
    for _ in range(8):  # 8 notes, 48 ticks apart
        seq += _note() + bytes([0xA0])  # 0xA0 = 48-tick delay
    seq += bytes([0xB1])  # FINE (no loop)
    data, entry = _mk_song(seq)

    analyze_song(data, entry)
    assert entry.loops is False
    assert entry.duration_ticks == 8 * 48  # ~6.4 s
    assert 3 <= entry.duration_sec < 10
    assert entry.kind == "jingle"


def test_analyzer_tempo_scales_duration():
    # identical tick count, half the tempo -> twice the seconds
    body = (_note() + bytes([0xB0])) * 4 + bytes([0xB1])
    slow, e_slow = _mk_song(bytes([0xBB, 40]) + body)  # 80 bpm
    fast, e_fast = _mk_song(bytes([0xBB, 80]) + body)  # 160 bpm
    analyze_song(slow, e_slow)
    analyze_song(fast, e_fast)
    assert e_slow.duration_ticks == e_fast.duration_ticks
    assert e_slow.duration_sec == pytest.approx(2 * e_fast.duration_sec, rel=1e-6)


def test_analyzer_handles_garbage_track():
    # a track of zero bytes: running status with no last command -> FINE
    data, entry = _mk_song(bytes(0))
    analyze_song(data, entry)
    assert entry.duration_ticks == 0 and entry.note_count == 0


def test_classify_boundaries():
    assert classify(True, 30, 200, 10)[0] == "music"  # looping arrangement
    assert classify(True, 1, 2, 1)[0] == "jingle"  # tiny loop
    assert classify(False, 0.1, 1, 1)[0] == "sfx"  # blip
    assert classify(False, 6, 8, 2)[0] == "jingle"  # fanfare
    assert classify(False, 40, 300, 8)[0] == "music"  # long non-looping
    assert classify(True, 4, 0, 1)[0] == "jingle"  # loops but plays nothing
    # looping music always outranks a longer sound effect
    music = classify(True, 20, 100, 4)[1]
    sfx = classify(False, 2, 4, 10)[1]
    assert music > sfx
