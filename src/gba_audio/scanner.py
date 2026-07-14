"""Locate the MP2K (Sappy) songtable in a GBA ROM and describe each song.

Locating: Python port of agbplay's MP2KScanner (FindSongTable path, non-GSF).
Reference: src/agbplay/MP2KScanner.cpp.

Describing: a lightweight, audio-free pass over each song's track bytecode
(mirroring core/mp2k/mp2k.c track_tick) recovers signals the raw table
doesn't expose (whether a song loops, its length, and how many notes it
plays), so callers can tell background music from sound effects and jingles
instead of guessing from track count alone.

MP2K songtable layout: one 8-byte entry per song:
    u32 song pointer (0x08xxxxxx, into ROM)
    u8  player index, u8 zero, u8 player index (again), u8 zero
Song header at the pointed-to position:
    u8 nTracks, u8 nBlocks(=0), u8 priority, u8 reverb,
    u32 voicegroup pointer, then nTracks u32 track pointers.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

AGB_MAP_ROM = 0x08000000
SEARCH_START = 0x200
MIN_SONG_NUM = 4  # lowest seen in the wild is 7 (Tetris Worlds)

# Engine frame rate: 2^24 / 280896 Hz (real GBA, not a round 60). The
# sequencer ticks whenever a per-frame accumulator (+= bpm each frame) crosses
# 150, so ticks/sec = FPS * bpm / 150. See mp2k.c mp2k_render.
_FPS = 16777216.0 / 280896.0
_DEFAULT_BPM = 150

# delay / note-length lookup (mp2k.c LEN_LUT, agbplay SequenceReader LUTs):
# a delay command 0x80+i or a note command 0xCF+i lasts LEN_LUT[i] ticks.
_LEN_LUT = (
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    23,
    24,
    28,
    30,
    32,
    36,
    40,
    42,
    44,
    48,
    52,
    54,
    56,
    60,
    64,
    66,
    68,
    72,
    76,
    78,
    80,
    84,
    88,
    90,
    92,
    96,
)

_MAX_STEPS = 100_000  # per-track command cap (guards against malformed loops)
_MAX_TICKS = 300_000  # per-track tick cap (~80 min at 60 tps)
_MAX_CALL_DEPTH = 16  # MP2K PATT call-stack depth


@dataclass
class SongEntry:
    index: int
    song_pos: int  # ROM file offset of song header
    player: int
    n_tracks: int
    voicegroup_pos: int | None  # None for empty songs
    # Filled in by analyze_song (0/False for empty songs or on parse failure):
    loops: bool = False  # a track ends in GOTO (background music) vs FINE
    duration_ticks: int = 0  # length of the longest track's first pass
    duration_sec: float = 0.0  # duration_ticks converted at the song's tempo
    note_count: int = 0  # total note-on events across all tracks
    kind: str = "empty"  # "music" | "jingle" | "sfx" | "empty"
    score: float = 0.0  # relevance for default sort (higher = more song-like)


@dataclass
class SongTable:
    pos: int  # ROM file offset of the table
    songs: list[SongEntry]


class Rom:
    def __init__(self, data: bytes):
        self.data = data

    def u8(self, pos: int) -> int:
        return self.data[pos]

    def u32(self, pos: int) -> int:
        return struct.unpack_from("<I", self.data, pos)[0]

    def valid_pointer(self, ptr: int) -> bool:
        off = ptr - AGB_MAP_ROM
        return 0 <= off < len(self.data) - 1

    def ptr_to_pos(self, pos: int) -> int:
        return self.u32(pos) - AGB_MAP_ROM


def _valid_songtable_entry(rom: Rom, pos: int) -> bool:
    if pos + 8 > len(rom.data):
        return False
    if not rom.valid_pointer(rom.u32(pos)):
        return False
    p1, z1, p2, z2 = rom.data[pos + 4 : pos + 8]
    if z1 != 0 or z2 != 0 or p1 != p2:
        return False
    song_pos = rom.ptr_to_pos(pos)
    if song_pos + 8 > len(rom.data):
        return False
    n_tracks, n_blocks, prio, rev = rom.data[song_pos : song_pos + 4]
    if (n_tracks | n_blocks | prio | rev) == 0:
        return True  # 'empty' song, allowed
    if not rom.valid_pointer(rom.u32(song_pos + 4)):
        return False  # voicegroup pointer
    for i in range(n_tracks):
        tp = song_pos + 8 + i * 4
        if tp + 4 > len(rom.data) or not rom.valid_pointer(rom.u32(tp)):
            return False
    return True


def _pointer_cache(rom: Rom) -> set[int]:
    cache = set()
    for i in range(SEARCH_START, len(rom.data) - 3, 4):
        ptr = rom.u32(i)
        if rom.valid_pointer(ptr) and ptr % 4 == 0:
            cache.add(ptr)
    return cache


# ---- per-song bytecode analysis -------------------------------------------


def _analyze_track(data: bytes, start: int) -> tuple[int, int, bool, int | None]:
    """Fast-forward one track's sequence, no audio. Returns
    (ticks, note_count, loops, first_tempo_bpm).

    Follows the same command lengths and control flow as the real sequencer
    (mp2k.c track_tick): delays accumulate ticks, notes count note-ons, PATT/
    PEND/REPT are followed, a GOTO is the loop point (stop, loops=True), FINE
    or any unknown command ends the track. MEMACC conditionals fall through
    (jump not taken). The result is a deterministic length estimate, not a
    full simulation.
    """
    n = len(data)

    def u8(p: int) -> int:
        return data[p] if 0 <= p < n else 0xB1  # OOB reads as FINE

    def u32(p: int) -> int:
        return struct.unpack_from("<I", data, p)[0] if 0 <= p + 4 <= n else 0

    pos = start
    last = 0
    ticks = 0
    notes = 0
    loops = False
    tempo: int | None = None
    call_stack: list[int] = []
    rept_count = 0

    for _ in range(_MAX_STEPS):
        if ticks >= _MAX_TICKS:
            break
        cmd = u8(pos)
        if cmd < 0x80:  # running status: reuse last command
            cmd = last
            if cmd < 0x80:
                break  # data error -> engine plays FINE
        else:
            pos += 1
            if cmd >= 0xBD:
                last = cmd  # only >=0xBD commands are repeatable

        if cmd >= 0xCF:  # TIE (0xCF) or note (0xD0..0xFF)
            if u8(pos) < 0x80:  # optional key, velocity, length add
                pos += 1
                if u8(pos) < 0x80:
                    pos += 1
                    if u8(pos) < 0x80:
                        pos += 1
            if cmd >= 0xD0:
                notes += 1
            continue
        if cmd <= 0xB0:  # delay 0x80..0xB0
            ticks += _LEN_LUT[cmd - 0x80]
            continue
        if cmd == 0xB1:  # FINE
            break
        if cmd == 0xB2:  # GOTO -> this is the loop point
            loops = True
            break
        if cmd == 0xB3:  # PATT (call)
            if len(call_stack) >= _MAX_CALL_DEPTH:
                break
            call_stack.append(pos + 4)
            pos = u32(pos) - AGB_MAP_ROM
            continue
        if cmd == 0xB4:  # PEND (return)
            if call_stack:
                pos = call_stack.pop()
            continue
        if cmd == 0xB5:  # REPT count + ptr
            count = u8(pos)
            if count == 0:
                break
            rept_count += 1
            if rept_count < count:
                pos = u32(pos + 1) - AGB_MAP_ROM
            else:
                rept_count = 0
                pos += 5
            continue
        if cmd == 0xB9:  # MEMACC op,idx,data (+ptr if cond)
            op = u8(pos)
            pos += 7 if 6 <= op <= 17 else 3  # fall through; never jump
            continue
        if cmd == 0xBB:  # TEMPO (bpm = byte * 2)
            if tempo is None:
                tempo = u8(pos) * 2
            pos += 1
            continue
        if 0xBA <= cmd <= 0xC8 and cmd not in (0xC6, 0xC7):
            pos += 1  # remaining 1-argument commands
            continue
        if cmd == 0xCD:  # XCMD
            ln = _xcmd_len(u8(pos))
            if ln < 0:
                break  # engine plays FINE
            pos += ln
            continue
        if cmd == 0xCE:  # EOT (optional key byte)
            if u8(pos) < 0x80:
                pos += 1
            continue
        break  # 0xB6-0xB8, 0xC6, 0xC7, 0xC9-0xCC -> FINE

    return ticks, notes, loops, tempo


def _xcmd_len(sub: int) -> int:
    """Bytes an XCMD (0xCD) occupies from its sub-type byte on (agbplay
    cmdPlayXCmd). -1 = the engine treats it as FINE."""
    if sub in (0, 3) or sub > 13:
        return -1
    if sub in (1, 13):  # XWAVE, XSOFF: 4-byte arg
        return 5
    if sub == 12:  # XWAIT: u16 arg
        return 3
    return 2  # 1-byte-arg commands


def _ticks_to_sec(ticks: int, bpm: int) -> float:
    return ticks * 150.0 / (_FPS * bpm) if bpm else 0.0


def classify(loops: bool, duration_sec: float, note_count: int, n_tracks: int) -> tuple[str, float]:
    """Map raw signals to a coarse kind and a relevance score for sorting."""
    if n_tracks == 0:
        return "empty", 0.0

    if note_count == 0:  # nothing actually plays -> not music
        kind = "sfx" if duration_sec < 3 else "jingle"
    elif loops:
        kind = "music" if (note_count >= 12 or duration_sec >= 4) else "jingle"
    elif duration_sec < 3 and note_count < 16:
        kind = "sfx"
    elif duration_sec < 10:
        kind = "jingle"
    else:
        kind = "music"

    score = (
        (60.0 if loops else 0.0)
        + min(note_count, 500) * 0.10
        + min(duration_sec, 120.0) * 0.40
        + min(n_tracks, 16) * 0.50
    )
    if kind == "sfx":
        score -= 40.0
    return kind, score


def analyze_song(data: bytes, entry: SongEntry) -> SongEntry:
    """Fill entry.loops / duration_* / note_count / kind / score in place.
    Robust: any parse error leaves an entry classified as it was (kind stays
    'empty' -> effectively unranked) rather than aborting the whole scan."""
    if entry.n_tracks == 0:
        return entry
    try:
        max_ticks = 0
        total_notes = 0
        any_loop = False
        bpm: int | None = None
        for i in range(entry.n_tracks):
            tp = entry.song_pos + 8 + i * 4
            start = struct.unpack_from("<I", data, tp)[0] - AGB_MAP_ROM
            ticks, notes, loops, tempo = _analyze_track(data, start)
            max_ticks = max(max_ticks, ticks)
            total_notes += notes
            any_loop = any_loop or loops
            if bpm is None and tempo is not None:
                bpm = tempo
        bpm = bpm or _DEFAULT_BPM
        entry.loops = any_loop
        entry.duration_ticks = max_ticks
        entry.duration_sec = _ticks_to_sec(max_ticks, bpm)
        entry.note_count = total_notes
        entry.kind, entry.score = classify(
            any_loop, entry.duration_sec, total_notes, entry.n_tracks
        )
    except (IndexError, struct.error):
        pass
    return entry


def find_songtable(data: bytes, *, analyze: bool = True) -> SongTable | None:
    """Return the first valid, referenced MP2K songtable, or None.

    When ``analyze`` is true (default) each non-empty song is also described
    (loops / duration / note_count / kind / score) via analyze_song."""
    rom = Rom(data)
    refs = _pointer_cache(rom)

    i = SEARCH_START
    while i < len(data) - 3:
        # candidate: MIN_SONG_NUM consecutive valid entries
        ok = True
        for j in range(MIN_SONG_NUM):
            if not _valid_songtable_entry(rom, i + j * 8):
                i += j * 8 + 4
                ok = False
                break
        if not ok:
            continue
        # table must be referenced somewhere in the ROM (literal pools of
        # the m4a functions); this kills false positives
        if (i + AGB_MAP_ROM) not in refs:
            i += 4
            continue

        songs: list[SongEntry] = []
        idx = 0
        while _valid_songtable_entry(rom, i + idx * 8):
            pos = i + idx * 8
            song_pos = rom.ptr_to_pos(pos)
            n_tracks = rom.u8(song_pos)
            vg = rom.ptr_to_pos(song_pos + 4) if n_tracks else None
            entry = SongEntry(idx, song_pos, rom.u8(pos + 4), n_tracks, vg)
            if analyze:
                analyze_song(data, entry)
            songs.append(entry)
            idx += 1
        return SongTable(pos=i, songs=songs)

    return None
