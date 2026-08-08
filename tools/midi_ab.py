#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=1.24"]
# ///
"""midi-ab — hearing-test A/B rig for the MP2K → MIDI export.

Renders one song of a ROM two ways and scores how alike they sound:

  engine  `gba-audio wav` — the verified MP2K C engine (the known-good side)
  midi    `gba-audio midi`, played through the macOS built-in General MIDI
          synth (tools/mid2wav.swift: DLSMusicDevice, rendered offline)

The score is chroma-cosine: per-frame 12-pitch-class similarity after a
full alignment sweep, robust to loudness/EQ/timbre — the method is
distilled from sunchip's tools/abcheck (abcompare.py), the same metric
that validated the engines against mGBA captures and gamerips.

Reading the number: the GM synth plays *wrong instruments by design*
(MIDI programs are the game's voicegroup indices, not GM patches), which
caps the score well below the >=0.95 same-engine gates. Calibration on
Pokemon Emerald: matched renders score about 0.93; two UNRELATED songs
score about 0.75-0.80 (tonal music shares pitch classes, so that is this
metric's floor, not zero). Treat >=0.90 as "same notes, same timing" —
then listen to the pair:

    afplay <out>/songNNN_engine.wav
    afplay <out>/songNNN_gm.wav

Usage: tools/midi_ab.py <rom> <song> [-o OUTDIR] [--open]
Needs ffmpeg on PATH; the GM render step needs macOS (swift + the system
DLS soundbank). Elsewhere the .mid is still written — render it with any
synth (fluidsynth, a DAW) and compare by ear.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SR = 16000  # analysis sample rate (as sunchip abcheck)
HOP = 0.25  # chroma frame (s)


def _run(cmd: list[str], cwd: str | None = None) -> None:
    # strip VIRTUAL_ENV: this script runs in uv's isolated script env, and
    # the nested `uv run gba-audio` would warn about the mismatch
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    res = subprocess.run(cmd, cwd=cwd, env=env)
    if res.returncode:
        raise SystemExit(res.returncode)


def decode(path: str) -> np.ndarray:
    """ffmpeg-decode any audio file to mono float64 @ SR."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"ffmpeg failed on {path}: {out.stderr.decode()[:200]}")
    return np.frombuffer(out.stdout, dtype=np.float32).astype(np.float64)


def chroma(x: np.ndarray) -> np.ndarray:
    """Per-HOP 12-pitch-class chroma, each frame L2-normalised (55Hz..4kHz)."""
    n = int(HOP * SR)
    m = len(x) // n
    if m < 1:
        return np.zeros((1, 12))
    frames = x[: m * n].reshape(m, n) * np.hanning(n)
    spec = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    out = np.zeros((m, 12))
    for b, fq in enumerate(freqs):
        if 55.0 <= fq <= 4000.0:
            out[:, int(round(12 * np.log2(fq / 440.0))) % 12] += spec[:, b]
    nrm = np.linalg.norm(out, axis=1, keepdims=True)
    nrm[nrm < 1e-9] = 1.0
    return out / nrm


def compare(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """(median, mean) per-frame cosine at the best full-sweep alignment."""
    ca, cb = chroma(a), chroma(b)
    na, nb = len(ca), len(cb)
    need = 0.5 * min(na, nb)
    best: tuple[float, np.ndarray] = (-2.0, np.zeros(1))
    for lag in range(-(na - 1), nb):  # lag = index in cb where ca[0] lands
        a0, b0 = max(0, -lag), max(0, lag)
        n = min(na - a0, nb - b0)
        if n < need:
            continue
        s = np.sum(ca[a0 : a0 + n] * cb[b0 : b0 + n], axis=1)
        if float(s.mean()) > best[0]:
            best = (float(s.mean()), s)
    return float(np.median(best[1])), best[0]


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="midi-ab",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    ap.add_argument("rom", help="GBA ROM (a game you legally own)")
    ap.add_argument("song", type=int, help="songtable index (see `gba-audio list`)")
    ap.add_argument("-o", "--out", help="output directory (default: system temp)")
    ap.add_argument("--open", action="store_true", help="reveal the output folder when done")
    ap.add_argument(
        "--sf2",
        action="store_true",
        help="also export the song's SoundFont and score the .mid played "
        "through the game's own instruments (the tighter A/B)",
    )
    args = ap.parse_args()

    rom = os.path.abspath(args.rom)
    out = args.out or os.path.join(tempfile.gettempdir(), "midi_ab")
    os.makedirs(out, exist_ok=True)
    eng = os.path.join(out, f"song{args.song:03d}_engine.wav")
    mid = os.path.join(out, f"song{args.song:03d}.mid")
    gm = os.path.join(out, f"song{args.song:03d}_gm.wav")
    sf2 = os.path.join(out, f"song{args.song:03d}.sf2")
    game = os.path.join(out, f"song{args.song:03d}_sf2.wav")

    song = str(args.song)
    _run(["uv", "run", "gba-audio", "wav", rom, "--song", song, "--loops", "1", "-o", eng], ROOT)
    _run(["uv", "run", "gba-audio", "midi", rom, "--song", song, "-o", mid], ROOT)
    if args.sf2:
        _run(["uv", "run", "gba-audio", "sf2", rom, "--song", song, "-o", sf2], ROOT)

    listen = [eng, gm]
    if sys.platform == "darwin" and shutil.which("swift"):
        swift = os.path.join(ROOT, "tools", "mid2wav.swift")
        ref = decode(eng)
        # normalize renders to the engine's peak so waveforms compare fairly
        peak = f"{max(float(np.abs(ref).max()), 0.05):.4f}"
        _run(["swift", swift, mid, gm, "-", peak])
        med, mean = compare(ref, decode(gm))
        print(f"\nchroma-cosine engine vs MIDI-through-GM:   {med:.3f} median | {mean:.3f} mean")
        if args.sf2:
            _run(["swift", swift, mid, game, sf2, peak])
            med, mean = compare(ref, decode(game))
            print(f"chroma-cosine engine vs MIDI+game-sf2:    {med:.3f} median | {mean:.3f} mean")
            listen.append(game)
        print("  (matched notes score ~0.90+ on GM, higher with the game sf2;")
        print("   unrelated songs ~0.75-0.80; see --help)")
    else:
        print("\nno macOS swift toolchain: synth render + score skipped; the .mid" )
        print("is written — render it with any synth and compare by ear.")

    print("\nlisten:")
    for f in listen:
        print(f"  afplay {f}")
    if args.open:
        subprocess.run(["open", out])
    return 0


if __name__ == "__main__":
    sys.exit(main())
