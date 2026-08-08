"""MP2K -> MIDI export tests.

Songs are synthesized in memory (as in test_scanner/test_pak) and the SMF
output is decoded with the minimal parser below. Two independent cross
checks anchor the walk: the scanner's analyzer must agree on note count
and tick length, and a .pak built by the C extractor must convert to the
byte-identical MIDI as the ROM it came from (proving the walk follows the
relocated pointers exactly).
"""

import struct

from gba_audio.midi import (
    pak_entry_count,
    pak_entry_index,
    pak_song_to_midi,
    song_to_midi,
)
from gba_audio.scanner import AGB_MAP_ROM, SongEntry, analyze_song

# ---- minimal SMF reader ----------------------------------------------------


def _read_vlq(b: bytes, i: int) -> tuple[int, int]:
    v = 0
    while True:
        v = (v << 7) | (b[i] & 0x7F)
        i += 1
        if not b[i - 1] & 0x80:
            return v, i


def parse_smf(smf: bytes):
    """Return (division, tracks) where each track is a list of
    (tick, event) and event is ('on', ch, key, vel) / ('off', ch, key) /
    ('cc', ch, num, val) / ('prog', ch, n) / ('bend', ch, value14) /
    ('tempo', us) / ('marker', text) / ('eot',)."""
    assert smf[:4] == b"MThd"
    hlen, fmt, ntrks, division = struct.unpack_from(">IHHH", smf, 4)
    assert (hlen, fmt) == (6, 1)
    i = 14
    tracks = []
    for _ in range(ntrks):
        assert smf[i : i + 4] == b"MTrk"
        (tlen,) = struct.unpack_from(">I", smf, i + 4)
        j, end = i + 8, i + 8 + tlen
        tick = 0
        evs = []
        while j < end:
            delta, j = _read_vlq(smf, j)
            tick += delta
            st = smf[j]
            assert st >= 0x80, "writer never uses running status"
            if st == 0xFF:
                kind = smf[j + 1]
                ln, k = _read_vlq(smf, j + 2)
                payload = smf[k : k + ln]
                j = k + ln
                if kind == 0x51:
                    evs.append((tick, ("tempo", int.from_bytes(payload, "big"))))
                elif kind == 0x06:
                    evs.append((tick, ("marker", payload.decode())))
                elif kind == 0x2F:
                    evs.append((tick, ("eot",)))
                else:
                    evs.append((tick, ("meta", kind, payload)))
            else:
                op, ch = st & 0xF0, st & 0x0F
                if op == 0x90:
                    evs.append((tick, ("on", ch, smf[j + 1], smf[j + 2])))
                    j += 3
                elif op == 0x80:
                    evs.append((tick, ("off", ch, smf[j + 1])))
                    j += 3
                elif op == 0xB0:
                    evs.append((tick, ("cc", ch, smf[j + 1], smf[j + 2])))
                    j += 3
                elif op == 0xC0:
                    evs.append((tick, ("prog", ch, smf[j + 1])))
                    j += 2
                elif op == 0xE0:
                    evs.append((tick, ("bend", ch, smf[j + 1] | smf[j + 2] << 7)))
                    j += 3
                else:
                    raise AssertionError(f"unexpected status {st:#x}")
        assert evs[-1][1] == ("eot",)
        tracks.append(evs)
        i = end
    assert i == len(smf)
    return division, tracks


# ---- song builders ---------------------------------------------------------

_HDR = 0x100
_SEQ = 0x200


def _song(*track_seqs: bytes) -> bytes:
    """A buffer holding one song header whose tracks are laid out from _SEQ
    (0x100 apart, so GOTO/PATT pointers can target AGB_MAP_ROM+_SEQ+k)."""
    buf = bytearray(0x2000)
    buf[_HDR] = len(track_seqs)
    for i, seq in enumerate(track_seqs):
        start = _SEQ + i * 0x100
        struct.pack_into("<I", buf, _HDR + 8 + 4 * i, AGB_MAP_ROM + start)
        buf[start : start + len(seq)] = seq
    return bytes(buf)


def _events(seq: bytes, track: int = 0):
    _, tracks = parse_smf(song_to_midi(_song(seq), _HDR))
    return tracks[1 + track]


def _notes(evs):
    return [e for e in evs if e[1][0] in ("on", "off")]


# ---- structure -------------------------------------------------------------


def test_header_and_track_layout():
    smf = song_to_midi(_song(bytes([0xB1]), bytes([0xB1])), _HDR)
    division, tracks = parse_smf(smf)
    assert division == 24
    assert len(tracks) == 3  # conductor + 2 tracks


def test_default_tempo_when_song_sets_none():
    _, tracks = parse_smf(song_to_midi(_song(bytes([0xB1])), _HDR))
    assert tracks[0][0] == (0, ("tempo", 400_000))  # 150 bpm engine default


def test_tempo_commands_map_to_conductor():
    # TEMPO 75 -> 150 bpm at tick 0, then TEMPO 60 -> 120 bpm after 96 ticks
    seq = bytes([0xBB, 75, 0xB0, 0xBB, 60, 0xB1])
    _, tracks = parse_smf(song_to_midi(_song(seq), _HDR))
    tempos = [e for e in tracks[0] if e[1][0] == "tempo"]
    assert tempos == [(0, ("tempo", 400_000)), (96, ("tempo", 500_000))]


# ---- notes and timing ------------------------------------------------------


def test_note_pair_and_length():
    # 0xD5 = 6-tick note (LEN_LUT[6]), key 60 vel 100, after a 24-tick delay
    # (trailing delay: FINE releases running notes, as the engine does)
    evs = _events(bytes([0x98, 0xD5, 60, 100, 0x98, 0xB1]))
    assert _notes(evs) == [(24, ("on", 0, 60, 113)), (30, ("off", 0, 60))]


def test_note_length_add_byte():
    # third optional byte extends the base length: 6 + 3 = 9 ticks
    evs = _events(bytes([0xD5, 60, 100, 3, 0x8C, 0xB1]))
    assert _notes(evs) == [(0, ("on", 0, 60, 113)), (9, ("off", 0, 60))]


def test_running_status_reuses_key_and_velocity():
    # note, delay, bare key byte (running status), delay, FINE
    evs = _events(bytes([0xD0, 60, 100, 0x81, 62, 0x81, 0xB1]))
    assert _notes(evs) == [
        (0, ("on", 0, 60, 113)),
        (1, ("off", 0, 60)),
        (1, ("on", 0, 62, 113)),
        (2, ("off", 0, 62)),
    ]


def test_tie_released_by_eot():
    # TIE key 60, wait 96, EOT 60
    evs = _events(bytes([0xCF, 60, 100, 0xB0, 0xCE, 60, 0xB1]))
    assert _notes(evs) == [(0, ("on", 0, 60, 113)), (96, ("off", 0, 60))]


def test_tie_without_eot_closes_at_track_end():
    evs = _events(bytes([0xCF, 60, 100, 0xB0, 0xB1]))
    assert _notes(evs) == [(0, ("on", 0, 60, 113)), (96, ("off", 0, 60))]


def test_note_truncated_at_fine():
    # 96-tick note (0xFF) but FINE arrives after 4 ticks
    evs = _events(bytes([0xFF, 60, 100, 0x84, 0xB1]))
    assert _notes(evs) == [(0, ("on", 0, 60, 113)), (4, ("off", 0, 60))]


def test_same_key_retrigger_closes_previous_note():
    # two 96-tick notes on the same key, 4 ticks apart: first must close
    # when the second starts, and the off precedes the on at that tick
    evs = _events(bytes([0xFF, 60, 100, 0x84, 0xFF, 60, 100, 0xB1]))
    assert _notes(evs) == [
        (0, ("on", 0, 60, 113)),
        (4, ("off", 0, 60)),
        (4, ("on", 0, 60, 113)),
        (4, ("off", 0, 60)),  # second note then truncates at FINE
    ]


def test_keyshift_folds_into_note_keys():
    # KEYSH +12, then KEYSH -12 (0xF4 as signed)
    evs = _events(bytes([0xBC, 12, 0xD0, 60, 100, 0x81, 0xBC, 0xF4, 0xD0, 60, 100, 0xB1]))
    ons = [e for e in evs if e[1][0] == "on"]
    assert [e[1][2] for e in ons] == [72, 48]


# ---- controllers -----------------------------------------------------------


def test_voice_vol_pan_mod():
    seq = bytes([0xBD, 5, 0xBE, 100, 0xBF, 0x40, 0xC4, 33, 0xB1])
    evs = _events(seq)
    assert (0, ("prog", 0, 5)) in evs
    assert (0, ("cc", 0, 7, 113)) in evs
    assert (0, ("cc", 0, 10, 64)) in evs  # 0x40 = center
    assert (0, ("cc", 0, 1, 33)) in evs


def test_bend_emits_rpn_range_then_wheel():
    # BENDR 12 then BEND +32: wheel = 8192 + 32*12*128/12 = 12288
    evs = _events(bytes([0xC1, 12, 0xC0, 0x40 + 32, 0xB1]))
    ccs = [e[1] for e in evs if e[1][0] == "cc"]
    assert ccs[:3] == [("cc", 0, 101, 0), ("cc", 0, 100, 0), ("cc", 0, 6, 12)]
    assert [e[1] for e in evs if e[1][0] == "bend"][-1] == ("bend", 0, 12288)


def test_default_bend_range_is_two_semitones():
    evs = _events(bytes([0xC0, 0x40 + 16, 0xB1]))  # BEND +16, default BENDR 2
    assert ("cc", 0, 6, 2) in [e[1] for e in evs if e[1][0] == "cc"]
    assert [e[1] for e in evs if e[1][0] == "bend"] == [("bend", 0, 8192 + 16 * 128)]


def test_tune_shifts_the_wheel():
    # TUNE +32 = half a semitone; range 2 -> 32*128/2 = 2048 above center
    evs = _events(bytes([0xC8, 0x40 + 32, 0xB1]))
    assert [e[1] for e in evs if e[1][0] == "bend"] == [("bend", 0, 8192 + 2048)]


# ---- loops -----------------------------------------------------------------


def _looped_song() -> bytes:
    intro = bytes([0xBB, 75, 0xD0, 60, 100, 0xB0])
    body = bytes([0xD0, 62, 100, 0xB0])
    goto = bytes([0xB2]) + struct.pack("<I", AGB_MAP_ROM + _SEQ + len(intro))
    return intro + body + goto


def test_goto_becomes_loop_markers():
    evs = _events(_looped_song())
    marks = [e for e in evs if e[1][0] == "marker"]
    assert marks == [(96, ("marker", "loopStart")), (192, ("marker", "loopEnd"))]


def test_track_channels_skip_gm_percussion():
    smf = song_to_midi(_song(*[bytes([0xD0, 60, 100, 0xB1])] * 11), _HDR)
    _, tracks = parse_smf(smf)
    chans = [e[1][1] for t in tracks[1:] for e in t if e[1][0] == "on"]
    assert chans == [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11]  # no channel 9


# ---- cross-check against the scanner's independent walk ---------------------


def test_agrees_with_scanner_analysis():
    data = _song(_looped_song(), bytes([0xD5, 64, 90, 0xB0, 0xB0, 0xB1]))
    entry = analyze_song(data, SongEntry(0, _HDR, 0, 2, 0))
    _, tracks = parse_smf(song_to_midi(data, _HDR))
    ons = [e for t in tracks[1:] for e in t if e[1][0] == "on"]
    end = max(e[0] for t in tracks for e in t)
    assert len(ons) == entry.note_count
    assert end == entry.duration_ticks


# ---- .pak parity -----------------------------------------------------------


def test_pak_converts_byte_identical_to_rom(tmp_path):
    from test_pak import _build_rom

    from gba_audio.native import build_pak
    from gba_audio.scanner import find_songtable

    rom = _build_rom()
    table = find_songtable(rom)
    assert table is not None
    out = tmp_path / "song.pak"
    build_pak(rom, table, [0], str(out))
    pak = out.read_bytes()

    assert pak_entry_count(pak) == 1
    assert pak_entry_index(pak, 0) == 0
    assert pak_song_to_midi(pak, 0) == song_to_midi(rom, table.songs[0].song_pos)


# ---- CLI --------------------------------------------------------------------


def test_cli_midi_from_rom(tmp_path):
    from test_pak import _build_rom

    from gba_audio.cli import main

    rom_path = tmp_path / "game.gba"
    rom_path.write_bytes(_build_rom())
    out = tmp_path / "song0.mid"
    assert main(["midi", str(rom_path), "--song", "0", "-o", str(out)]) == 0
    division, tracks = parse_smf(out.read_bytes())
    assert division == 24 and len(tracks) == 2


def test_cli_midi_rejects_wbf(tmp_path, capsys):
    from gba_audio.cli import main

    wbf = tmp_path / "songs.wbf"
    wbf.write_bytes(b"WBF1" + bytes(64))
    assert main(["midi", str(wbf)]) == 1
    assert "MP2K" in capsys.readouterr().err


def test_volume_and_velocity_are_linearised():
    # GBA values are linear amplitude; GM synths square CC7/velocity, so
    # the export pre-warps with sqrt: 100 -> 113, 64 -> 90, extremes fixed
    evs = _events(bytes([0xBE, 64, 0xD0, 127, 127, 0x81, 0xD0, 60, 1, 0x81, 0xB1]))
    assert ("cc", 0, 7, 90) in [e[1] for e in evs]
    vels = [e[1][3] for e in evs if e[1][0] == "on"]
    assert vels == [127, 11]  # 127 stays put; 1 -> round(127*sqrt(1/127))
