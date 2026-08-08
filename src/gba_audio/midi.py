"""Convert MP2K sequences to Standard MIDI Files.

MP2K songs are compiled MIDI (Nintendo's mid2agb), so conversion back is a
sequence walk, no audio: each track is traversed with the sequencer's own
control flow (mirroring core/mp2k/mp2k.c track_tick, like the scanner's
analyzer) and commands map onto MIDI events — notes and TIE/EOT to
note-on/off, VOICE to program change, VOL/PAN/MOD to CC 7/10/1,
BEND/BENDR/TUNE to pitch wheel plus RPN 0 bend range, KEYSH folded into
note keys, TEMPO onto a conductor track. Output is SMF format 1 at
24 ticks per quarter note, mid2agb's own resolution (LEN_LUT's longest
note, 96 ticks, is one whole note).

The walk is one pass: a track ends at FINE or at its loop GOTO, closing
open notes there (the engine releases them the same way); the loop is
recorded as `loopStart`/`loopEnd` markers instead of being unrolled.
Tempo metas use the nominal BPM; real hardware ticks at 59.7275 Hz, not
60, so a render runs 0.45 % slower than the MIDI — every MP2K ripper
rounds this the same way.

Instrument numbers are voicegroup indices, not General MIDI programs, so
a GM synth will pick arbitrary sounds; the point of the export is notes
into a DAW. MIDI channels map 1:1 to tracks except that channel 9 (GM
percussion) is skipped when track count allows.

MP2K volumes and velocities are LINEAR amplitude, but GM/DLS/SF2 synths
apply a squared taper to CC 7 and note velocity, which would exaggerate
every balance difference between tracks. Exported values are therefore
pre-warped with a square root (127*sqrt(v/127)), so a standard synth
reproduces the engine's mix — the same correction as gba-mus-ripper's
"linearise volumes" option, always on here.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .scanner import (
    _LEN_LUT,
    _MAX_CALL_DEPTH,
    _MAX_STEPS,
    _MAX_TICKS,
    AGB_MAP_ROM,
    _xcmd_len,
)

TPQN = 24  # SMF division; MP2K's native tick = one 24th of a quarter note
_DEFAULT_BPM = 150  # engine start value (mp2k.c mp2k_new)

# .pak container fields read here in pure Python (same constants as
# native.py / the C core) so MIDI export never needs the C toolchain.
PAK_MAGIC = b"SNCHPPAK"

# same-tick event ordering: close notes before moving controls before
# opening notes, so back-to-back same-key notes never collapse
_OFF, _CTRL, _MARK, _ON = 0, 1, 2, 3


_OPEN = -1  # tick sentinel: unresolved TIE note-off, fixed at EOT or track end


@dataclass
class _Ev:
    tick: int
    prio: int
    data: bytes
    on_tick: int = 0  # note-offs only: when their note started (ordering aid)


def _meta(kind: int, payload: bytes) -> bytes:
    return bytes([0xFF, kind]) + _vlq(len(payload)) + payload


def _vlq(n: int) -> bytes:
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.insert(0, 0x80 | (n & 0x7F))
        n >>= 7
    return bytes(out)


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


# GBA linear amplitude -> value whose squared GM/DLS taper is that amplitude
_LIN = tuple(round(127 * (v / 127) ** 0.5) for v in range(128))


class _TrackWalk:
    """One track's pass over the bytecode, accumulating MIDI events.

    `base` is what a stored pointer must be lowered by to index `data`:
    AGB_MAP_ROM for a ROM, 0 for a .pak pool (the extractor rewrites
    pointers to pool offsets).
    """

    def __init__(self, data: bytes, base: int, ch: int, tempos: list[tuple[int, int]]):
        self.data = data
        self.base = base
        self.ch = ch
        self.tempos = tempos  # shared across tracks; TEMPO is global
        self.events: list[_Ev] = []
        self.ticks = 0
        self.keyshift = 0
        self.bend = 0
        self.bendr = 2  # engine default (mp2k.c mp2k_new)
        self.tune = 0
        self.rpn_bendr: int | None = None  # bend range last written as RPN 0
        self.open_note: dict[int, _Ev] = {}  # MIDI key -> pending note-off
        self.ties: dict[int, list[_Ev]] = {}  # written key -> open TIE offs
        self.progs: set[int] = set()  # VOICE numbers seen (sf2 export uses this)
        self.prog_keys: dict[int, set[int]] = {}  # prog -> written keys played
        self._cur_prog: int | None = None

    def u8(self, p: int) -> int:
        return self.data[p] if 0 <= p < len(self.data) else 0xB1  # OOB reads FINE

    def u16(self, p: int) -> int:
        return self.u8(p) | self.u8(p + 1) << 8

    def u32(self, p: int) -> int:
        return struct.unpack_from("<I", self.data, p)[0] if 0 <= p + 4 <= len(self.data) else 0

    def emit(self, prio: int, data: bytes) -> _Ev:
        ev = _Ev(self.ticks, prio, data)
        self.events.append(ev)
        return ev

    def cc(self, num: int, val: int) -> None:
        self.emit(_CTRL, bytes([0xB0 | self.ch, num, val]))

    def note_on(self, key: int, vel: int, length: int) -> None:
        if self._cur_prog is not None:
            self.prog_keys.setdefault(self._cur_prog, set()).add(key)
        mk = _clamp(key + self.keyshift, 0, 127)
        prev = self.open_note.get(mk)
        if prev is not None and (prev.tick == _OPEN or prev.tick > self.ticks):
            prev.tick = self.ticks  # same key retriggered: close the old note
        self.emit(_ON, bytes([0x90 | self.ch, mk, _LIN[_clamp(vel, 1, 127)]]))
        off = _Ev(
            self.ticks + length if length > 0 else _OPEN,
            _OFF,
            bytes([0x80 | self.ch, mk, 64]),
            on_tick=self.ticks,
        )
        self.events.append(off)
        self.open_note[mk] = off
        if length <= 0:  # TIE: off tick comes from EOT (matched on written key)
            self.ties.setdefault(key, []).append(off)

    def wheel(self) -> None:
        """BEND/BENDR/TUNE changed: (re)state RPN 0 and move the pitch wheel.
        Engine pitch offset is tune + bend*bendr in 1/64-semitone units
        (mp2k.c trk_pitch); the wheel spans ±bendr semitones."""
        if self.bendr != self.rpn_bendr:
            self.cc(101, 0)
            self.cc(100, 0)
            self.cc(6, _clamp(self.bendr, 0, 127))
            self.rpn_bendr = self.bendr
        rng = max(self.bendr, 1)
        val = _clamp(8192 + round((self.bend * self.bendr + self.tune) * 128 / rng), 0, 16383)
        self.emit(_CTRL, bytes([0xE0 | self.ch, val & 0x7F, val >> 7]))

    def run(self, start: int) -> int:
        """Walk the track; returns its end tick. Same command lengths and
        control flow as scanner._analyze_track, which mirrors the engine."""
        pos = start
        last_cmd = 0
        last_key = 0
        last_vel = 0
        call_stack: list[int] = []
        rept_count = 0
        tick_at: dict[int, int] = {}  # first tick each command position ran

        for _ in range(_MAX_STEPS):
            if self.ticks >= _MAX_TICKS:
                break
            tick_at.setdefault(pos, self.ticks)
            cmd = self.u8(pos)
            if cmd < 0x80:  # running status: reuse last command
                cmd = last_cmd
                if cmd < 0x80:
                    break  # data error -> engine plays FINE
            else:
                pos += 1
                if cmd >= 0xBD:
                    last_cmd = cmd

            if cmd >= 0xCF:  # TIE (0xCF) or note; optional key, vel, length add
                length = _LEN_LUT[cmd - 0xCF]
                if self.u8(pos) < 0x80:
                    last_key = self.u8(pos)
                    pos += 1
                    if self.u8(pos) < 0x80:
                        last_vel = self.u8(pos)
                        pos += 1
                        if self.u8(pos) < 0x80:
                            length += self.u8(pos)
                            pos += 1
                self.note_on(last_key, last_vel, length)
                continue
            if cmd <= 0xB0:  # delay
                self.ticks += _LEN_LUT[cmd - 0x80]
                continue
            if cmd == 0xB1:  # FINE
                break
            if cmd == 0xB2:  # GOTO: the loop point; mark it, end the pass
                target = tick_at.get(self.u32(pos) - self.base)
                if target is not None:
                    self.events.append(_Ev(target, _MARK, _meta(0x06, b"loopStart")))
                self.emit(_MARK, _meta(0x06, b"loopEnd"))
                break
            if cmd == 0xB3:  # PATT (call)
                if len(call_stack) >= _MAX_CALL_DEPTH:
                    break
                call_stack.append(pos + 4)
                pos = self.u32(pos) - self.base
                continue
            if cmd == 0xB4:  # PEND (return)
                if call_stack:
                    pos = call_stack.pop()
                continue
            if cmd == 0xB5:  # REPT count + ptr
                count = self.u8(pos)
                if count == 0:
                    break
                rept_count += 1
                if rept_count < count:
                    pos = self.u32(pos + 1) - self.base
                else:
                    rept_count = 0
                    pos += 5
                continue
            if cmd == 0xB9:  # MEMACC: fall through, never jump (as scanner)
                op = self.u8(pos)
                pos += 7 if 6 <= op <= 17 else 3
                continue
            if cmd == 0xBB:  # TEMPO (global, whichever track sets it)
                bpm = self.u8(pos) * 2
                if bpm > 0:
                    self.tempos.append((self.ticks, bpm))
                pos += 1
                continue
            if cmd == 0xBC:  # KEYSH: transposition, folded into note keys
                v = self.u8(pos)
                self.keyshift = v - 256 if v >= 128 else v
                pos += 1
                continue
            if cmd == 0xBD:  # VOICE
                prog = self.u8(pos)
                pos += 1
                self._cur_prog = prog if prog <= 127 else None
                if prog <= 127:
                    self.progs.add(prog)
                    self.emit(_CTRL, bytes([0xC0 | self.ch, prog]))
                continue
            if cmd == 0xBE:  # VOL (linear on GBA -> de-squared for GM synths)
                self.cc(7, _LIN[_clamp(self.u8(pos), 0, 127)])
                pos += 1
                continue
            if cmd == 0xBF:  # PAN: MP2K byte is 0..127 with 0x40 center, as CC10
                self.cc(10, _clamp(self.u8(pos), 0, 127))
                pos += 1
                continue
            if cmd == 0xC0:  # BEND
                self.bend = self.u8(pos) - 0x40
                pos += 1
                self.wheel()
                continue
            if cmd == 0xC1:  # BENDR
                self.bendr = self.u8(pos)
                pos += 1
                self.wheel()
                continue
            if cmd == 0xC4:  # MOD
                self.cc(1, _clamp(self.u8(pos), 0, 127))
                pos += 1
                continue
            if cmd == 0xC8:  # TUNE
                self.tune = self.u8(pos) - 0x40
                pos += 1
                self.wheel()
                continue
            if cmd in (0xBA, 0xC2, 0xC3, 0xC5):  # PRIO, LFOS, LFODL, MODT: no MIDI form
                pos += 1
                continue
            if cmd == 0xCD:  # XCMD
                sub = self.u8(pos)
                ln = _xcmd_len(sub)
                if ln < 0:
                    break  # engine plays FINE
                if sub == 12:  # XWAIT: an extra u16 delay
                    self.ticks += self.u16(pos + 1)
                pos += ln
                continue
            if cmd == 0xCE:  # EOT: release the oldest TIE on this key
                key = self.u8(pos)
                if key < 0x80:
                    pos += 1
                    last_key = key
                open_ties = self.ties.get(last_key)
                if open_ties:
                    open_ties.pop(0).tick = self.ticks
                continue
            break  # 0xB6-0xB8, 0xC6, 0xC7, 0xC9-0xCC -> FINE

        # Track over: FINE/GOTO release every running note (mp2k.c track_fine)
        for ev in self.events:
            if ev.tick == _OPEN or ev.tick > self.ticks:
                ev.tick = self.ticks
        return self.ticks


def _ev_key(e: _Ev) -> tuple[int, int]:
    # A note truncated to zero length still needs its off AFTER its on
    prio = _ON + 1 if e.prio == _OFF and e.tick == e.on_tick else e.prio
    return (e.tick, prio)


def _chunk(events: list[_Ev], end_tick: int) -> bytes:
    """Delta-encode sorted events plus end-of-track into an MTrk chunk."""
    out = bytearray()
    last = 0
    for ev in sorted(events, key=_ev_key):
        out += _vlq(ev.tick - last) + ev.data
        last = ev.tick
    out += _vlq(max(end_tick - last, 0)) + b"\xff\x2f\x00"
    return b"MTrk" + len(out).to_bytes(4, "big") + bytes(out)


def song_to_midi(data: bytes, hdr_off: int, *, base: int = AGB_MAP_ROM) -> bytes:
    """Convert the MP2K song whose header sits at `hdr_off` in `data` to a
    complete SMF byte string. `base` as in _TrackWalk: AGB_MAP_ROM when
    `data` is a ROM, 0 when it is a .pak pool."""
    if hdr_off + 8 > len(data):
        raise ValueError("song header out of range")
    n_tracks = data[hdr_off]
    if n_tracks == 0:
        raise ValueError("empty song (no tracks)")

    tempos: list[tuple[int, int]] = []
    chunks: list[bytes] = []
    end_max = 0
    skip_drum_ch = n_tracks <= 15  # keep melodic tracks off GM percussion
    for i in range(n_tracks):
        ch = min(i + 1 if skip_drum_ch and i >= 9 else i, 15)
        walk = _TrackWalk(data, base, ch, tempos)
        start = struct.unpack_from("<I", data, hdr_off + 8 + 4 * i)[0] - base
        end = walk.run(start)
        end_max = max(end_max, end)
        chunks.append(_chunk(walk.events, end))

    # conductor track: the merged tempo map (last write wins per tick)
    tempos.sort(key=lambda t: t[0])
    if not tempos or tempos[0][0] != 0:
        tempos.insert(0, (0, _DEFAULT_BPM))
    conductor = [
        _Ev(tick, _CTRL, _meta(0x51, round(60_000_000 / bpm).to_bytes(3, "big")))
        for n, (tick, bpm) in enumerate(tempos)
        if n + 1 == len(tempos) or tempos[n + 1][0] != tick
    ]

    header = b"MThd" + struct.pack(">IHHH", 6, 1, 1 + n_tracks, TPQN)
    return header + _chunk(conductor, end_max) + b"".join(chunks)


def song_used_programs(data: bytes, hdr_off: int, *, base: int = AGB_MAP_ROM) -> set[int]:
    """Program numbers a song's tracks actually VOICE-select (one walk).
    The SF2 export builds exactly these slots."""
    progs: set[int] = set()
    for i in range(data[hdr_off] if hdr_off < len(data) else 0):
        walk = _TrackWalk(data, base, 0, [])
        walk.run(struct.unpack_from("<I", data, hdr_off + 8 + 4 * i)[0] - base)
        progs |= walk.progs
    return progs


def song_program_keys(data: bytes, hdr_off: int, *, base: int = AGB_MAP_ROM) -> dict[int, set[int]]:
    """For each program a song plays notes on: the written keys it plays
    (pre-keyshift, what drum/keysplit banks index by)."""
    keys: dict[int, set[int]] = {}
    for i in range(data[hdr_off] if hdr_off < len(data) else 0):
        walk = _TrackWalk(data, base, 0, [])
        walk.run(struct.unpack_from("<I", data, hdr_off + 8 + 4 * i)[0] - base)
        for prog, ks in walk.prog_keys.items():
            keys.setdefault(prog, set()).update(ks)
    return keys


def pak_entry_count(pak: bytes) -> int:
    """Songs in a .pak (0 if the magic is wrong)."""
    if pak[:8] != PAK_MAGIC or len(pak) < 12:
        return 0
    return struct.unpack_from("<H", pak, 10)[0]


def pak_entry_index(pak: bytes, entry: int) -> int:
    """Original ROM songtable index of pak entry `entry`, or -1."""
    if not 0 <= entry < pak_entry_count(pak):
        return -1
    size, idx = struct.unpack_from("<I4xH", pak, 12 + entry * 16 + 4)
    return idx if size else -1


def pak_song_to_midi(pak: bytes, entry: int) -> bytes:
    """Convert one .pak entry to SMF bytes. Pool pointers are pool offsets
    (see extractor_mp2k.c mp2k_build), so the walk runs with base=0."""
    if not 0 <= entry < pak_entry_count(pak):
        raise ValueError(f"no song {entry} in pak")
    off, size, hdr_off = struct.unpack_from("<III", pak, 12 + entry * 16)
    if size == 0:
        raise ValueError(f"song {entry} was dropped at extraction")
    return song_to_midi(pak[off : off + size], hdr_off, base=0)
