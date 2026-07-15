"""WAV rendering end-to-end: the MP2K path renders a .pak extracted from
test_pak's synthetic ROM; the Webfoot path renders the generated homebrew
fixture (tests/fixtures/homebrew/chiptune.wbf)."""

import os
import struct
import wave

import pytest
from test_pak import SEQ, _build_rom

from gba_audio.cli import main as cli_main
from gba_audio.native import (
    MP2K_SAMPLE_RATE,
    WBF_SAMPLE_RATE,
    extract_songs,
    pak_songs,
)
from gba_audio.scanner import AGB_MAP_ROM, find_songtable
from gba_audio.wav import render_pak_song, render_wbf_song

FIXTURE_WBF = os.path.join(os.path.dirname(__file__), "fixtures", "homebrew", "chiptune.wbf")


def _audible_rom() -> bytes:
    """test_pak's synthetic ROM with the sequence swapped for one that sets
    track volume (MP2K tracks start at volume 0, so a song without a VOL
    command is correct-but-silent): TEMPO, VOL, VOICE, a note, a
    running-status note, GOTO back to the first note."""
    rom = bytearray(_build_rom())
    seq = bytes([0xBB, 75, 0xBE, 127, 0xBD, 0x00, 0xE0, 60, 100, 0x84, 62, 0x84])
    rom[SEQ : SEQ + len(seq)] = seq
    goto_at = SEQ + len(seq)
    rom[goto_at] = 0xB2
    struct.pack_into("<I", rom, goto_at + 1, AGB_MAP_ROM + SEQ + 6)
    return bytes(rom)


def _read_wav(path):
    with wave.open(path, "rb") as w:
        return (w.getnchannels(), w.getframerate(), w.readframes(w.getnframes()))


def test_render_pak_song(tmp_path):
    rom = _audible_rom()
    table = find_songtable(rom)
    assert table is not None
    pak, _ = extract_songs(rom, [0], table.pos)
    assert pak_songs(pak) == 1

    out = tmp_path / "song.wav"
    sec = render_pak_song(pak, 0, str(out), loop_count=1, fade_ms=200)
    assert sec > 0.1

    channels, rate, frames = _read_wav(str(out))
    assert (channels, rate) == (2, MP2K_SAMPLE_RATE)
    assert any(frames)  # the held note is audible, not silence


def test_render_wbf_song(tmp_path):
    with open(FIXTURE_WBF, "rb") as f:
        wbf = f.read()

    out = tmp_path / "song.wav"
    res = render_wbf_song(wbf, 0, str(out), loops=1)
    assert res.seconds > 0.1
    # one pass ends at the loop point, so the render is that song length
    assert res.song_sec == pytest.approx(res.seconds, abs=0.1)

    channels, rate, frames = _read_wav(str(out))
    assert (channels, rate) == (1, WBF_SAMPLE_RATE)
    # normalized to 0.7 FS: peak within a couple of percent of 22937
    peak = max(
        abs(int.from_bytes(frames[i : i + 2], "little", signed=True))
        for i in range(0, len(frames), 2)
    )
    assert 0.65 * 32767 < peak <= 0.72 * 32767


def test_cli_wav_from_wbf(tmp_path):
    out = tmp_path / "out.wav"
    assert cli_main(["wav", FIXTURE_WBF, "--song", "0", "-o", str(out)]) == 0
    channels, rate, frames = _read_wav(str(out))
    assert channels == 1 and any(frames)


def test_cli_list_and_extract_synthetic_rom(tmp_path, capsys):
    rom_path = tmp_path / "fake.gba"
    rom_path.write_bytes(_build_rom())

    assert cli_main(["list", str(rom_path)]) == 0
    assert "MP2K songtable" in capsys.readouterr().out

    pak_path = tmp_path / "fake.pak"
    assert cli_main(["extract", str(rom_path), "--songs", "0", "-o", str(pak_path)]) == 0
    assert pak_songs(pak_path.read_bytes()) == 1
