"""SF2 export tests.

A synthetic ROM exercises one voice of each shape - looped DirectSound
PCM, PSG square, a drum bank (base key + pan), and a keysplit - and the
output is decoded with the minimal SF2 reader below. As with the MIDI
tests, a .pak built by the C extractor must produce the byte-identical
SoundFont to the ROM it came from.
"""

import os
import struct

from gba_audio.midi import song_used_programs
from gba_audio.scanner import AGB_MAP_ROM
from gba_audio.sf2 import song_to_sf2

# ---- minimal SF2 reader -----------------------------------------------------


def parse_sf2(d: bytes):
    assert d[:4] == b"RIFF" and d[8:12] == b"sfbk"
    assert struct.unpack_from("<I", d, 4)[0] == len(d) - 8
    chunks = {}
    i = 12
    while i < len(d):
        assert d[i : i + 4] == b"LIST"
        size = struct.unpack_from("<I", d, i + 4)[0]
        form = d[i + 8 : i + 12]
        j = i + 12
        while j < i + 8 + size:
            tag = d[j : j + 4].decode()
            s = struct.unpack_from("<I", d, j + 4)[0]
            chunks[form.decode(), tag] = d[j + 8 : j + 8 + s]
            j += 8 + s + (s & 1)
        i += 8 + size + (size & 1)

    smpl = chunks["sdta", "smpl"]
    shdr = [
        struct.unpack_from("<20sIIIIIBbHH", chunks["pdta", "shdr"], k)
        for k in range(0, len(chunks["pdta", "shdr"]), 46)
    ]
    inst = [
        struct.unpack_from("<20sH", chunks["pdta", "inst"], k)
        for k in range(0, len(chunks["pdta", "inst"]), 22)
    ]
    ibag = [
        struct.unpack_from("<HH", chunks["pdta", "ibag"], k)
        for k in range(0, len(chunks["pdta", "ibag"]), 4)
    ]
    igen_raw = chunks["pdta", "igen"]
    phdr = [
        struct.unpack_from("<20sHHHIII", chunks["pdta", "phdr"], k)
        for k in range(0, len(chunks["pdta", "phdr"]), 38)
    ]

    def zone_gens(g0: int, g1: int) -> dict[int, int | tuple[int, int]]:
        gens: dict[int, int | tuple[int, int]] = {}
        for k in range(g0 * 4, g1 * 4, 4):
            gid = struct.unpack_from("<H", igen_raw, k)[0]
            if gid == 43:  # keyRange: byte pair
                lo, hi = igen_raw[k + 2], igen_raw[k + 3]
                gens[gid] = (lo, hi)
            else:
                gens[gid] = struct.unpack_from("<h", igen_raw, k + 2)[0]
        return gens

    instruments = []
    for n in range(len(inst) - 1):
        b0, b1 = inst[n][1], inst[n + 1][1]
        zones = [zone_gens(ibag[b][0], ibag[b + 1][0]) for b in range(b0, b1)]
        instruments.append((inst[n][0].rstrip(b"\0").decode(), zones))
    presets = [(p[0].rstrip(b"\0").decode(), p[1], p[2]) for p in phdr[:-1]]
    return smpl, shdr, instruments, presets


def sample16(smpl: bytes, start: int, n: int) -> list[int]:
    return list(struct.unpack_from(f"<{n}h", smpl, start * 2))


# ---- synthetic ROM ----------------------------------------------------------

SONGTABLE = 0x1000
SONG = 0x2000
SEQ = 0x2100
VOICEGROUP = 0x3000
DRUMTABLE = 0x3400
SPLITSUB = 0x3A00
KEYMAP = 0x3E00
SAMPLE = 0x5000
SAMPLE2 = 0x5800

SMP_RATE = 8363
SMP_LEN = 32
SMP_LOOP = 4
SMP2_LEN = 16
MARKER = 0xA5  # -91 as int8


def _adsr(a, d, s, r):
    return a | d << 8 | s << 16 | r << 24


def _build_rom() -> bytes:
    rom = bytearray(0x8000)
    for i in range(4):
        struct.pack_into("<IBBBB", rom, SONGTABLE + i * 8, AGB_MAP_ROM + SONG, 0, 0, 0, 0)
    struct.pack_into("<I", rom, 0x500, AGB_MAP_ROM + SONGTABLE)

    struct.pack_into("<BBBBI", rom, SONG, 1, 0, 0, 0x80, AGB_MAP_ROM + VOICEGROUP)
    struct.pack_into("<I", rom, SONG + 8, AGB_MAP_ROM + SEQ)
    # slot 3 is selected via RUNNING STATUS (bare arg byte after a delay,
    # last command still VOICE): regression for the extractor's used[] scan,
    # which used to see only literal BD-xx pairs and zeroed such slots
    seq = bytes(
        [0xBD, 0, 0xD0, 60, 100]
        + [0xBD, 1, 0xD0, 60, 100]
        + [0xBD, 2, 0xD0, 36, 100]
        + [0xBD, 1, 0x81, 3, 0xD0, 70, 100]
        + [0x84, 0xB1]
    )
    rom[SEQ : SEQ + len(seq)] = seq

    # slot 0: looped DirectSound PCM
    struct.pack_into(
        "<BBBBII", rom, VOICEGROUP, 0x00, 60, 0, 0, AGB_MAP_ROM + SAMPLE, _adsr(255, 200, 128, 100)
    )
    # slot 1: PSG square 2 with 50% duty
    struct.pack_into("<BBBBII", rom, VOICEGROUP + 12, 0x02, 60, 0, 0, 2, _adsr(2, 1, 10, 3))
    # slot 2: drum bank
    struct.pack_into("<BBBBII", rom, VOICEGROUP + 24, 0x80, 0, 0, 0, AGB_MAP_ROM + DRUMTABLE, 0)
    # slot 3: keysplit
    struct.pack_into(
        "<BBBBII",
        rom,
        VOICEGROUP + 36,
        0x40,
        0,
        0,
        0,
        AGB_MAP_ROM + SPLITSUB,
        AGB_MAP_ROM + KEYMAP,
    )

    # drum key 36: one-shot PCM, base key 48, pan byte 0xFF (hard right)
    struct.pack_into(
        "<BBBBII",
        rom,
        DRUMTABLE + 36 * 12,
        0x00,
        48,
        0,
        0xFF,
        AGB_MAP_ROM + SAMPLE2,
        _adsr(255, 0, 255, 0),
    )
    # keysplit sub-voices 0/1 -> the two samples; keymap: keys <60 -> 0, else 1
    struct.pack_into(
        "<BBBBII", rom, SPLITSUB, 0x00, 60, 0, 0, AGB_MAP_ROM + SAMPLE, _adsr(255, 0, 255, 0)
    )
    struct.pack_into(
        "<BBBBII", rom, SPLITSUB + 12, 0x00, 60, 0, 0, AGB_MAP_ROM + SAMPLE2, _adsr(255, 0, 255, 0)
    )
    for k in range(128):
        rom[KEYMAP + k] = 0 if k < 60 else 1

    # samples: SAMPLE = looped ramp, SAMPLE2 = one-shot marker bytes
    struct.pack_into("<IIII", rom, SAMPLE, 0xC0000000, SMP_RATE * 1024, SMP_LOOP, SMP_LEN)
    for i in range(SMP_LEN):
        rom[SAMPLE + 16 + i] = i
    struct.pack_into("<IIII", rom, SAMPLE2, 0, SMP_RATE * 1024, 0, SMP2_LEN)
    for i in range(SMP2_LEN):
        rom[SAMPLE2 + 16 + i] = MARKER
    return bytes(rom)


def _sf2() -> bytes:
    rom = _build_rom()
    progs = song_used_programs(rom, SONG)
    assert progs == {0, 1, 2, 3}
    out, skipped = song_to_sf2(rom, SONG, progs)
    assert skipped == []
    return out


# ---- tests -------------------------------------------------------------------


def test_presets_match_slots():
    _, _, instruments, presets = parse_sf2(_sf2())
    assert [(p[1], p[2]) for p in presets] == [(0, 0), (1, 0), (2, 0), (3, 0)]
    assert [name for name, _ in instruments] == ["prog000", "prog001", "prog002", "prog003"]


def test_pcm_sample_data_and_loop():
    smpl, shdr, instruments, _ = parse_sf2(_sf2())
    recs = {r[0].rstrip(b"\0").decode(): r for r in shdr[:-1]}
    ramp = next(
        r
        for n, r in recs.items()
        if n.startswith("smp") and r[5] == SMP_RATE and r[2] - r[1] == SMP_LEN
    )
    start, ls, le = ramp[1], ramp[3], ramp[4]
    assert (ls - start, le - start) == (SMP_LOOP, SMP_LEN)
    assert ramp[6] == 60  # root key
    assert sample16(smpl, start, SMP_LEN) == [i << 8 for i in range(SMP_LEN)]
    # marker sample: 8-bit signed -> 16-bit
    mark = next(r for r in shdr[:-1] if r[2] - r[1] == SMP2_LEN)
    assert sample16(smpl, mark[1], SMP2_LEN) == [(MARKER - 256) << 8] * SMP2_LEN


def test_square_voice():
    smpl, shdr, instruments, _ = parse_sf2(_sf2())
    sq = next(r for r in shdr[:-1] if r[0].startswith(b"sq_duty2"))
    assert (sq[5], sq[6]) == (28160, 69)  # 440 Hz fundamental at key 69
    data = sample16(smpl, sq[1], 64)
    assert data[:32] == [32768 - 4 * 4096] * 32 and data[32:] == [-4 * 4096] * 32


def test_drum_zone_fixed_pitch_and_pan():
    _, _, instruments, _ = parse_sf2(_sf2())
    zones = dict(instruments)["prog002"]
    assert len(zones) == 1
    z = zones[0]
    assert z[43] == (36, 36)  # keyRange
    assert z[56] == 0  # scaleTuning: pitch fixed
    assert z[51] == 48 - 60  # coarseTune: base key 48 on a root-60 sample
    assert z[17] == round((0xFF - 0xC0) * 2 * 500 / 128)  # pan hard right
    assert z[54] == 0  # one-shot


def test_keysplit_zones():
    _, shdr, instruments, _ = parse_sf2(_sf2())
    zones = dict(instruments)["prog003"]
    assert [z[43] for z in zones] == [(0, 59), (60, 127)]
    assert zones[0][53] != zones[1][53]  # different samples


def test_samples_are_deduped():
    _, shdr, _, _ = parse_sf2(_sf2())
    # slot 0 and keysplit sub 0 share SAMPLE; drum and sub 1 share SAMPLE2;
    # plus the square: 3 samples total
    assert len(shdr) - 1 == 3


def test_pak_converts_byte_identical_to_rom(tmp_path):
    from gba_audio.midi import pak_entry_count
    from gba_audio.native import build_pak
    from gba_audio.scanner import find_songtable

    rom = _build_rom()
    table = find_songtable(rom)
    assert table is not None
    out = tmp_path / "song.pak"
    build_pak(rom, table, [0], str(out))
    pak = out.read_bytes()
    assert pak_entry_count(pak) == 1

    off, size, hdr_off = struct.unpack_from("<III", pak, 12)
    pool = pak[off : off + size]
    progs = song_used_programs(pool, hdr_off, base=0)
    assert progs == {0, 1, 2, 3}
    from_pak, skipped = song_to_sf2(pool, hdr_off, progs, base=0)
    assert skipped == []
    assert from_pak == _sf2()


def test_cli_sf2(tmp_path):
    from gba_audio.cli import main

    rom_path = tmp_path / "game.gba"
    rom_path.write_bytes(_build_rom())
    out = tmp_path / "song0.sf2"
    assert main(["sf2", str(rom_path), "--song", "0", "-o", str(out)]) == 0
    d = out.read_bytes()
    assert d[:4] == b"RIFF" and d[8:12] == b"sfbk"


def test_cli_sf2_rejects_empty_wbf(tmp_path, capsys):
    from gba_audio.cli import main

    wbf = tmp_path / "songs.wbf"
    wbf.write_bytes(b"WBF1" + bytes(64))  # valid magic, no instrument table
    assert main(["sf2", str(wbf)]) == 1
    assert "no instruments" in capsys.readouterr().err


def test_wbf_sf2_rejects_bad_offsets():
    import pytest

    from gba_audio.sf2 import wbf_to_sf2

    wbf = bytearray(_fixture_wbf())
    struct.pack_into("<I", wbf, 0x14, len(wbf))  # instrument table past EOF
    with pytest.raises(ValueError, match="offsets"):
        wbf_to_sf2(bytes(wbf))


# ---- Webfoot ----------------------------------------------------------------

FIXTURE_WBF = os.path.join(os.path.dirname(__file__), "fixtures", "homebrew", "chiptune.wbf")


def _fixture_wbf() -> bytes:
    with open(FIXTURE_WBF, "rb") as f:
        return f.read()


def _wbf_inst_flags(wbf: bytes, slot: int) -> int:
    off = struct.unpack_from("<I", wbf, 0x14)[0]
    return wbf[off + slot * 12 + 4]


def test_wbf_sf2_shape_and_loop_modes():
    """The homebrew fixture has one looped and one one-shot instrument; each
    becomes a preset whose sampleModes reflects its loop flag."""
    from gba_audio.sf2 import wbf_to_sf2

    wbf = _fixture_wbf()
    sf2, _ = wbf_to_sf2(wbf)
    _, shdr, instruments, presets = parse_sf2(sf2)

    assert len(instruments) == 2 and len(presets) == 2
    for slot, (_, zones) in enumerate(instruments):
        looped = _wbf_inst_flags(wbf, slot) & 1
        assert zones[0][54] == (1 if looped else 0)  # sampleModes
        assert zones[0][43] == (0, 127)  # full keyrange
    # base frequency becomes the sample rate, played at root key 60
    assert all(s[5] == 16000 and s[6] == 60 for s in shdr[:2])


def test_wbf_sf2_pcm_is_8bit_promoted_and_padded():
    from gba_audio.sf2 import _SF2_MIN_SAMPLES, wbf_to_sf2

    smpl, shdr, _, _ = parse_sf2(wbf_to_sf2(_fixture_wbf())[0])
    for start, end in [(s[1], s[2]) for s in shdr[:2]]:
        assert end - start >= _SF2_MIN_SAMPLES
        vals = struct.unpack_from(f"<{end - start}h", smpl, start * 2)
        # 8-bit source promoted by <<8: every value is a multiple of 256
        assert all(v % 256 == 0 for v in vals)


def test_wbf_sf2_pingpong_is_unrolled():
    """SF2 has no bidirectional loop, so a ping-pong instrument must come out
    as a longer sample with a forward loop over the doubled span."""
    from gba_audio.sf2 import wbf_to_sf2

    wbf = bytearray(_fixture_wbf())
    off = struct.unpack_from("<I", wbf, 0x14)[0]
    plain = parse_sf2(wbf_to_sf2(bytes(wbf))[0])[1][0]
    wbf[off + 4] |= 0x02  # mark instrument 0 ping-pong
    bounced, notes = wbf_to_sf2(bytes(wbf))
    s = parse_sf2(bounced)[1][0]

    assert (s[2] - s[1]) > (plain[2] - plain[1])  # sample grew
    assert s[4] == s[2]  # loop end == sample end
    assert any("ping-pong" in n for n in notes)


def test_wbf_sf2_pad_keeps_loop_period():
    from gba_audio.sf2 import _SF2_MIN_SAMPLES, _pad_to_sf2_minimum

    pcm, loop = _pad_to_sf2_minimum([1, 2, 3, 4], (0, 4))
    assert len(pcm) >= _SF2_MIN_SAMPLES
    assert loop == (0, len(pcm))
    assert pcm[:8] == [1, 2, 3, 4, 1, 2, 3, 4]  # period repeated, not silence
    # a one-shot too short to loop is padded with silence instead
    pcm, loop = _pad_to_sf2_minimum([5, 6], None)
    assert loop is None and len(pcm) == _SF2_MIN_SAMPLES and pcm[2:] == [0] * 46


def test_cli_sf2_webfoot_from_image(tmp_path):
    from gba_audio.cli import main

    src = tmp_path / "songs.wbf"
    src.write_bytes(_fixture_wbf())
    out = tmp_path / "bank.sf2"
    assert main(["sf2", str(src), "-o", str(out)]) == 0
    d = out.read_bytes()
    assert d[:4] == b"RIFF" and d[8:12] == b"sfbk"


def test_psg_exclusive_class_and_decay_to_sustain():
    import math

    _, _, instruments, _ = parse_sf2(_sf2())
    z = dict(instruments)["prog001"][0]  # SQ2, ADSR 2,1,10,3
    # one hardware channel per PSG type: new notes must cut the old one
    assert z[57] == 2
    # DLS decay reading: time from peak to sustain. Engine: level
    # (15*10+15)>>4 = 10, so 5 steps of `dec`=1 frame at 60 Hz
    assert z[36] == round(1200 * math.log2(5 * 1 / 60))
