"""Build a SoundFont 2 (.sf2) from an MP2K voicegroup.

Companion to gba_audio.midi: the MIDI export writes program numbers that
are the game's voicegroup slot indices, and this module builds the bank
those numbers index — preset (bank 0, program n) holds slot n's actual
instrument, so `song.mid` + `song.sf2` in a DAW or softsynth plays the
export with the game's own sounds. Pure Python, ROM or .pak input (same
pointer bases as midi.py).

The mapping mirrors core/mp2k/mp2k.c note_on/chan_set_pitch/chan_sample:

- DirectSound (type 0x00/0x08): signed 8-bit PCM, rate = header
  word/1024 Hz, root key 60; 0x08 plays at the fixed 13379 Hz rate
  (scale tuning 0). GameFreak DPCM (mode 1) and Camelot ADPCM
  ('negative' length) are decoded to PCM here — SF2 stores raw samples.
  Golden Sun synth voices become single-cycle waves (PWM approximated
  as a 50% square, its sweep dropped).
- PSG squares: one cycle at the engine's duty table (12.5/25/50/75 %),
  root key 69 = 440 Hz. PSG wave: the 32-nibble wavetable as one cycle,
  DC-removed, root key 69 = 220 Hz. PSG noise: one full LFSR period
  (15-bit or 7-bit) clocked at 4096 Hz for key 60, scale tuning 300
  cents/key (the engine's 8^(k/12) region below key 76; the flattening
  above is not representable in SF2).
- Keysplit (0x40): one zone per keymap run; drum (0x80): one zone per
  key, pitch fixed at the sub-entry's base key, its pan byte as SF2 pan.
- ADSR: the 60 Hz GBA envelopes converted to SF2 timecents/centibels
  (PCM: 8-bit with multiplicative decay/release; PSG: 4-bit stepped).
  Decay is written as time-from-peak-to-sustain, the DLS reading that
  Apple's synth (and most others) uses, not the SF2 spec's 100 dB ramp.
  The PSG hardware length counter and echo (XIECV/XIECL) are dropped.
- PSG voices carry an exclusive class per hardware channel (SQ1/SQ2/
  WAVE/NOISE): the GBA has one of each, every note replaces the last,
  and without the cut rendered squares stack into a sustained blur.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

from .scanner import AGB_MAP_ROM

FIXED_RATE = 13379  # 'fixed frequency' voice rate (mp2k.c MP2K_FIXED_RATE)

_DPCM_DELTA = (0, 1, 4, 9, 16, 25, 36, 49, -64, -49, -36, -25, -16, -9, -4, -1)
_DUTY_STEPS = (1, 2, 4, 6)  # of 8 pattern entries high (mp2k.c duty_steps)


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def _timecents(sec: float) -> int:
    if sec <= 0.001:
        return -12000
    return _clamp(round(1200 * math.log2(sec)), -12000, 8000)


def _sustain_cb(frac: float) -> int:
    """SF2 sustain = attenuation from peak in centibels."""
    if frac <= 0.0:
        return 1440
    return _clamp(round(-200 * math.log10(frac)), 0, 1440)


def _exp_decay_sec(factor: int) -> float:
    """Seconds for an env multiplied by factor/256 per 60 Hz frame to fall
    100 dB (release: the full-ramp reading synths apply from note-off)."""
    if factor <= 0:
        return 0.0
    if factor >= 256:
        return 100.0
    db_per_frame = 20 * math.log10(factor / 256.0)
    return -100.0 / db_per_frame / 60.0


def _decay_to_sustain_sec(dec: int, sus: int) -> float:
    """DLS-style decay: seconds from peak to the sustain level at the
    engine's rate (env *= dec/256 per 60 Hz frame, from 255 down to sus)."""
    if dec <= 0 or sus >= 255:
        return 0.0
    if dec >= 256:
        return 100.0
    frames = math.log(max(sus, 1) / 255.0) / math.log(dec / 256.0)
    return frames / 60.0


# SF2 generator ids used here
_G_START_LOOP_COARSE = 45
_G_PAN = 17
_G_ATTACK = 34
_G_HOLD = 35
_G_DECAY = 36
_G_SUSTAIN = 37
_G_RELEASE = 38
_G_INSTRUMENT = 41
_G_KEYRANGE = 43
_G_COARSE_TUNE = 51
_G_FINE_TUNE = 52
_G_SAMPLE_ID = 53
_G_SAMPLE_MODES = 54
_G_SCALE_TUNING = 56
_G_EXCLUSIVE = 57
_G_ROOT_KEY = 58


@dataclass
class _Sample:
    name: str
    pcm: list[int]  # int16
    rate: int
    root: int
    correction: int = 0  # cents
    loop: tuple[int, int] | None = None  # [start, end) in samples


@dataclass
class _Zone:
    keylo: int
    keyhi: int
    sample: int  # index into samples
    gens: list[tuple[int, int]] = field(default_factory=list)  # (gen, value)


@dataclass
class _Inst:
    slot: int
    zones: list[_Zone]


class Mp2kSoundFont:
    """Extracts one voicegroup into SF2 samples/instruments/presets.

    `base` as in midi.py: AGB_MAP_ROM for a ROM, 0 for a .pak pool (the
    extractor rewrites pointers; a pak bank is the song's private view).
    """

    def __init__(self, data: bytes, bank_off: int, base: int = AGB_MAP_ROM):
        self.data = data
        self.bank = bank_off
        self.base = base
        self.samples: list[_Sample] = []
        self._sample_ids: dict[tuple, int] = {}
        self.insts: list[_Inst] = []
        self.skipped: list[str] = []  # human-readable notes on dropped voices

    # ---- little helpers --------------------------------------------------

    def u8(self, p: int) -> int:
        return self.data[p] if 0 <= p < len(self.data) else 0

    def u32(self, p: int) -> int:
        if p >= 0 and p + 4 <= len(self.data):
            return struct.unpack_from("<I", self.data, p)[0]
        return 0

    def ptr(self, p: int) -> int:
        return self.u32(p) - self.base

    def _in_range(self, off: int, need: int = 1) -> bool:
        return off >= 0 and off + need <= len(self.data)

    # ---- sample builders (all deduped through _intern) -------------------

    def _intern(self, key: tuple, make) -> int:
        idx = self._sample_ids.get(key)
        if idx is None:
            idx = len(self.samples)
            self._sample_ids[key] = idx
            self.samples.append(make())
        return idx

    def _pcm_sample(self, smp_off: int, fixed: bool) -> tuple[int, bool] | None:
        """DirectSound sample header at smp_off -> (sample index, looped).
        Handles plain PCM8, GameFreak DPCM and Camelot ADPCM; synth voices
        (loop==end==0) are built by _synth_sample instead. A `fixed` (type
        0x08) voice plays at FIXED_RATE whatever the header says, so it
        gets its own interned copy."""
        if not self._in_range(smp_off, 16):
            return None
        mode = self.u8(smp_off)
        loop_en = (self.u8(smp_off + 3) & 0xC0) != 0
        midc = self.u32(smp_off + 4) / 1024.0
        loop_pos = self.u32(smp_off + 8)
        end_pos = self.u32(smp_off + 12)
        if midc <= 0:
            return None

        adpcm = end_pos >= 0x80000000
        if adpcm:
            end_pos = (-end_pos) & 0xFFFFFFFF
            loop_en = False
        if end_pos == 0 or end_pos > 0x1000000:
            return None
        if loop_pos > end_pos:
            loop_pos = 0  # mp2k.c romhack fix
        if loop_pos == end_pos:
            loop_en = False
        # the data must really be there (the extractor's sample_size rule);
        # without this, junk entries reachable only through garbage keymap
        # bytes parse as truncated "samples" on a ROM but not on a .pak
        data_len = (
            ((end_pos + 63) // 64) * 0x21
            if mode == 1 and not adpcm
            else (end_pos + 1) // 2
            if adpcm
            else end_pos
        )
        if not self._in_range(smp_off + 16, data_len):
            return None

        def make() -> _Sample:
            n = end_pos
            if adpcm:
                pcm = self._decode_adpcm(smp_off + 16, n)
            elif mode == 1:
                pcm = self._decode_dpcm(smp_off + 16, n)
            else:
                d = self.data[smp_off + 16 : smp_off + 16 + n]
                pcm = [(b - 256 if b >= 128 else b) << 8 for b in d]
            rate = FIXED_RATE if fixed else max(400, round(midc))
            corr = 0 if fixed else _clamp(round(1200 * math.log2(midc / rate)), -99, 99)
            return _Sample(
                name=f"smp{len(self.samples):03d}",
                pcm=pcm,
                rate=rate,
                root=60,
                correction=corr,
                loop=(loop_pos, end_pos) if loop_en else None,
            )

        return self._intern(("pcm", smp_off, fixed), make), loop_en

    def _decode_dpcm(self, off: int, n: int) -> list[int]:
        """GameFreak DPCM: 0x21-byte blocks of 64 samples (mp2k.c dpcm_fetch)."""
        out: list[int] = []
        for block in range((n + 63) // 64):
            base = off + block * 0x21
            acc = self.u8(base)
            acc = acc - 256 if acc >= 128 else acc
            out.append(acc)
            acc = ((acc + _DPCM_DELTA[self.u8(base + 1) & 0xF] + 128) & 0xFF) - 128
            out.append(acc)
            for h in range(2, 33):
                b = self.u8(base + h)
                acc = ((acc + _DPCM_DELTA[b >> 4] + 128) & 0xFF) - 128
                out.append(acc)
                acc = ((acc + _DPCM_DELTA[b & 0xF] + 128) & 0xFF) - 128
                out.append(acc)
        return [v << 8 for v in out[:n]]

    def _decode_adpcm(self, off: int, n: int) -> list[int]:
        """Camelot ADPCM, forward-only (mp2k.c adpcm_decode_next)."""
        level = 0
        shift = 0
        out: list[int] = []
        for pos in range(n):
            data = self.u8(off + pos // 2)
            nibble = ((data << 28) if pos & 1 else (data << 24)) & 0xF0000000
            if shift <= 63:
                signed = nibble - (1 << 32) if nibble & 0x80000000 else nibble
                level += signed >> (shift >> 1)
                level = ((level + 0x8000) & 0xFFFF) - 0x8000  # int16 wrap
            if nibble & 0x80000000:
                nibble = (-nibble) & 0xFFFFFFFF
            shift = (shift + 4 - (nibble >> 28)) & 0xFF
            out.append(_clamp(level << 8, -32768, 32767))
        return out

    def _synth_sample(self, smp_off: int) -> int:
        """Golden Sun synth voice (loop==end==0): single-cycle stand-in.
        Waveform selected by data byte 1: 0 PWM (as 50% square), 1 saw,
        2 triangle. Root 60 at midc/64 Hz (mp2k.c chan_set_pitch)."""
        syn = self.u8(smp_off + 16 + 1)
        midc = self.u32(smp_off + 4) / 1024.0

        def make() -> _Sample:
            n = 64
            if syn == 1:  # saw
                pcm = [round(32767 * (1 - 2 * i / n)) for i in range(n)]
            elif syn == 2:  # triangle (mp2k.c P_SYN_TRI shape)
                pcm = [
                    (4 * (i * 65536 // n) - 65536) // 2
                    if i < n // 2
                    else (3 * 65536 - 4 * (i * 65536 // n)) // 2
                    for i in range(n)
                ]
            else:  # PWM: 50% square, sweep dropped
                pcm = [16384 if i < n // 2 else -16384 for i in range(n)]
            f0 = max(1.0, midc / 64.0)
            return _Sample(
                name=f"syn{len(self.samples):03d}",
                pcm=pcm,
                rate=max(400, round(f0 * n)),
                root=60,
                loop=(0, n),
            )

        return self._intern(("syn", smp_off, syn), make)

    def _square_sample(self, duty: int) -> int:
        def make() -> _Sample:
            steps = _DUTY_STEPS[duty & 3]
            hi = min(32767, 32768 - steps * 4096)
            lo = -steps * 4096
            pcm = [hi if i < steps * 8 else lo for i in range(64)]
            # 64-sample cycle at 28160 Hz = 440 Hz fundamental (root 69)
            return _Sample(f"sq_duty{duty}", pcm, 28160, 69, loop=(0, 64))

        return self._intern(("sq", duty & 3), make)

    def _wave_sample(self, wave_off: int) -> int:
        def make() -> _Sample:
            nib = []
            for i in range(16):
                b = self.u8(wave_off + i)
                nib += [b >> 4, b & 0xF]
            dc = -sum(nib) * 64
            cycle = [_clamp(v * 2048 + dc, -32768, 32767) for v in nib]
            pcm = cycle * 4  # 128 samples at 28160 Hz = 220 Hz (root 69)
            return _Sample(f"wav{len(self.samples):03d}", pcm, 28160, 69, loop=(0, 128))

        return self._intern(("wave", self.data[wave_off : wave_off + 16]), make)

    def _noise_sample(self, short: int) -> int:
        def make() -> _Sample:
            lfsr, mask = (0x40, 0x60) if short else (0x4000, 0x6000)
            pcm = []
            for _ in range(127 if short else 32767):
                if lfsr & 1:
                    pcm.append(16384)
                    lfsr = (lfsr >> 1) ^ mask
                else:
                    pcm.append(-16384)
                    lfsr >>= 1
            # LFSR clocked at 4096 Hz for key 60 (mp2k.c noise curve)
            return _Sample(f"noise{short}", pcm, 4096, 60, loop=(0, len(pcm)))

        return self._intern(("noise", short & 1), make)

    # ---- envelopes --------------------------------------------------------

    def _env_gens(self, ins: int, psg: bool) -> dict[int, int]:
        att, dec, sus, rel = (self.u8(ins + 8 + i) for i in range(4))
        if psg:
            att &= 0x7
            dec &= 0x7
            sus &= 0xF
            rel &= 0x7
            level = min(15, (15 * sus + 15) >> 4)  # mp2k.c psg_apply_vol
            steps = 15 - level  # decay walks -1 level every `dec` frames
            return {
                _G_ATTACK: _timecents(att * 15 / 60),
                _G_SUSTAIN: _sustain_cb(level / 15.0),
                _G_DECAY: _timecents(steps * dec / 60) if dec and steps else -12000,
                _G_RELEASE: _timecents(max(level, 1) * rel / 60) if rel else -12000,
            }
        return {
            _G_ATTACK: _timecents(255 / att / 60) if att else -12000,
            _G_SUSTAIN: _sustain_cb(sus / 256.0),
            _G_DECAY: _timecents(_decay_to_sustain_sec(dec, sus)),
            _G_RELEASE: _timecents(_exp_decay_sec(rel)),
        }

    # ---- voices -----------------------------------------------------------

    def _voice_zone(self, ins: int, keylo: int, keyhi: int, *, drum: bool = False) -> _Zone | None:
        """One voicegroup entry (already keysplit/drum-resolved) -> zone.
        drum: fix pitch at the entry's base key and honor its pan byte."""
        if not self._in_range(ins, 12) or not any(self.data[ins : ins + 12]):
            return None  # all-zero = unused slot (what the extractor emits);
            # must be tested explicitly: in a pak, base 0 makes a null
            # sample pointer alias real pool data instead of falling OOB
        vtype = self.u8(ins)
        if vtype & 0xC0:
            return None  # nested split/drum: invalid, engine drops it too
        cgb = vtype & 0x07

        gens: dict[int, int] = {}  # dict: a later value overrides, no duplicates
        noise = fixed = False
        if vtype in (0x00, 0x08):  # DirectSound
            fixed = vtype == 0x08
            smp_off = self.ptr(ins + 4)
            if not self._in_range(smp_off, 16):
                return None
            if self.u32(smp_off + 8) == 0 and self.u32(smp_off + 12) == 0:
                sid = self._synth_sample(smp_off)
                looped = True
            else:
                got = self._pcm_sample(smp_off, fixed)
                if got is None:
                    return None
                sid, looped = got
            if fixed:  # plays at FIXED_RATE whatever the key
                gens[_G_SCALE_TUNING] = 0
            psg = False
        elif cgb == 1 or cgb == 2:  # squares
            sid = self._square_sample(self.u32(ins + 4) & 3)
            looped, psg = True, True
        elif cgb == 3:  # programmable wave
            wave_off = self.ptr(ins + 4)
            if not self._in_range(wave_off, 16):
                return None
            sid = self._wave_sample(wave_off)
            looped, psg = True, True
        elif cgb == 4:  # noise
            sid = self._noise_sample(self.u32(ins + 4) & 1)
            looped, psg, noise = True, True, True
            gens[_G_SCALE_TUNING] = 300  # engine curve: 8^(k/12) below key 76
        else:
            return None
        if psg:  # one hardware channel per PSG type: a new note cuts the old
            gens[_G_EXCLUSIVE] = cgb

        if drum:
            base_key = self.u8(ins + 1)
            gens[_G_SCALE_TUNING] = 0
            if not fixed:  # fixed voices ignore the key entirely
                # noise pitch moves 3 semitones per key in the engine curve
                shift = (3 if noise else 1) * (base_key - self.samples[sid].root)
                gens[_G_COARSE_TUNE] = _clamp(shift, -120, 120)
            ipan = self.u8(ins + 3)
            if ipan & 0x80:
                rhythm_pan = (ipan - 0xC0) * 2  # mp2k.c note_on
                gens[_G_PAN] = _clamp(round(rhythm_pan * 500 / 128), -500, 500)
        gens.update(self._env_gens(ins, psg))
        gens[_G_SAMPLE_MODES] = 1 if looped else 0
        return _Zone(keylo, keyhi, sid, list(gens.items()))

    def add_instrument(self, slot: int) -> bool:
        """Voicegroup slot -> SF2 instrument (resolving keysplit/drum)."""
        ins = self.bank + slot * 12
        if not self._in_range(ins, 12):
            return False
        vtype = self.u8(ins)
        zones: list[_Zone] = []

        if vtype & 0x40 and vtype != 0x80:  # keysplit
            sub = self.ptr(ins + 4)
            keymap = self.ptr(ins + 8)
            if not self._in_range(keymap, 128):
                return False
            lo = 0
            while lo < 128:
                hi = lo
                sv = self.u8(keymap + lo)
                while hi + 1 < 128 and self.u8(keymap + hi + 1) == sv:
                    hi += 1
                # sub-banks hold at most 128 entries (extractor walk_bank);
                # larger keymap bytes are aliased garbage no song plays
                if sv < 128:
                    z = self._voice_zone(sub + sv * 12, lo, hi)
                    if z:
                        zones.append(z)
                lo = hi + 1
        elif vtype == 0x80:  # rhythm/drum: sub-entry per key
            sub = self.ptr(ins + 4)
            for key in range(128):
                sub_ins = sub + key * 12
                if not self._in_range(sub_ins, 12):
                    break
                z = self._voice_zone(sub_ins, key, key, drum=True)
                if z:
                    zones.append(z)
        else:
            z = self._voice_zone(ins, 0, 127)
            if z:
                zones.append(z)

        if not zones:
            self.skipped.append(f"slot {slot}: type 0x{vtype:02X} has no usable voice")
            return False
        self.insts.append(_Inst(slot, zones))
        return True

    # ---- SF2 serialization -------------------------------------------------

    def tobytes(self, name: str = "gba-audio") -> bytes:
        if not self.insts:
            raise ValueError("no instruments to write")
        smpl = bytearray()
        shdr = bytearray()
        starts: list[tuple[int, int]] = []
        for s in self.samples:
            start = len(smpl) // 2
            smpl += struct.pack(f"<{len(s.pcm)}h", *(_clamp(v, -32768, 32767) for v in s.pcm))
            smpl += bytes(46 * 2)  # required guard points
            starts.append((start, start + len(s.pcm)))
        for s, (start, end) in zip(self.samples, starts, strict=True):
            ls, le = (start + s.loop[0], start + s.loop[1]) if s.loop else (start, end)
            shdr += struct.pack(
                "<20sIIIIIBbHH",
                s.name.encode()[:20],
                start,
                end,
                ls,
                le,
                s.rate,
                s.root,
                s.correction,
                0,
                1,  # mono sample
            )
        shdr += struct.pack("<20sIIIIIBbHH", b"EOS", 0, 0, 0, 0, 0, 0, 0, 0, 0)

        # instruments: one global-less zone list; generator order per spec
        inst = bytearray()
        ibag = bytearray()
        igen = bytearray()
        phdr = bytearray()
        pbag = bytearray()
        pgen = bytearray()
        for i, ins in enumerate(self.insts):
            inst += struct.pack("<20sH", f"prog{ins.slot:03d}".encode(), len(ibag) // 4)
            for z in ins.zones:
                ibag += struct.pack("<HH", len(igen) // 4, 0)
                igen += struct.pack("<HBB", _G_KEYRANGE, z.keylo, z.keyhi)
                for gen, val in z.gens:
                    igen += struct.pack("<Hh", gen, val)
                igen += struct.pack("<HH", _G_SAMPLE_ID, z.sample)
            phdr += struct.pack(
                "<20sHHHIII", f"prog{ins.slot:03d}".encode(), ins.slot, 0, len(pbag) // 4, 0, 0, 0
            )
            pbag += struct.pack("<HH", len(pgen) // 4, 0)
            pgen += struct.pack("<HH", _G_INSTRUMENT, i)
        inst += struct.pack("<20sH", b"EOI", len(ibag) // 4)
        ibag += struct.pack("<HH", len(igen) // 4, 0)
        igen += struct.pack("<Hh", 0, 0)  # terminal
        phdr += struct.pack("<20sHHHIII", b"EOP", 0, 0, len(pbag) // 4, 0, 0, 0)
        pbag += struct.pack("<HH", len(pgen) // 4, 0)
        pgen += struct.pack("<HH", 0, 0)
        pmod = struct.pack("<HHhHH", 0, 0, 0, 0, 0)  # terminal only
        imod = struct.pack("<HHhHH", 0, 0, 0, 0, 0)

        def chunk(tag: bytes, payload: bytes) -> bytes:
            return tag + struct.pack("<I", len(payload)) + payload + (b"\0" * (len(payload) & 1))

        info = b"".join(
            [
                chunk(b"ifil", struct.pack("<HH", 2, 1)),
                chunk(b"isng", b"EMU8000\0"),
                chunk(b"INAM", name.encode()[:255] + b"\0"),
                chunk(b"ISFT", b"gba-audio-tools\0"),
            ]
        )
        pdta = b"".join(
            [
                chunk(b"phdr", bytes(phdr)),
                chunk(b"pbag", bytes(pbag)),
                chunk(b"pmod", pmod),
                chunk(b"pgen", bytes(pgen)),
                chunk(b"inst", bytes(inst)),
                chunk(b"ibag", bytes(ibag)),
                chunk(b"imod", imod),
                chunk(b"igen", bytes(igen)),
                chunk(b"shdr", bytes(shdr)),
            ]
        )
        body = b"".join(
            [
                b"sfbk",
                b"LIST" + struct.pack("<I", 4 + len(info)) + b"INFO" + info,
                b"LIST" + struct.pack("<I", 4 + len(chunk(b"smpl", bytes(smpl)))),
                b"sdta" + chunk(b"smpl", bytes(smpl)),
                b"LIST" + struct.pack("<I", 4 + len(pdta)) + b"pdta" + pdta,
            ]
        )
        return b"RIFF" + struct.pack("<I", len(body)) + body


def song_to_sf2(
    data: bytes, hdr_off: int, progs: set[int], *, base: int = AGB_MAP_ROM, name: str = "gba-audio"
) -> tuple[bytes, list[str]]:
    """Build the SF2 for the song whose header sits at hdr_off, covering the
    given program numbers (the VOICE commands the song actually uses).
    Returns (sf2 bytes, list of skipped-voice notes)."""
    bank = struct.unpack_from("<I", data, hdr_off + 4)[0] - base
    sf = Mp2kSoundFont(data, bank, base)
    for slot in sorted(p for p in progs if 0 <= p <= 127):
        sf.add_instrument(slot)
    return sf.tobytes(name), sf.skipped
