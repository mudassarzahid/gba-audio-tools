"""Render extracted songs to WAV.

Mirrors the desktop A/B rigs the engines were verified with: MP2K renders
interleaved stereo with the engine's own loop-count/fade song-end policy;
Webfoot renders mono, ends after a number of loop-point passes, and is
peak-normalized offline to 0.7 FS (the streaming engine can't look ahead).
"""

from __future__ import annotations

import array
import wave

from .native import (
    MP2K_SAMPLE_RATE,
    WBF_SAMPLE_RATE,
    Mp2kRenderer,
    WbfRenderer,
)

_CHUNK = 2048  # frames per render call


def _write_wav(path: str, pcm: bytes, channels: int, rate: int) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def render_pak_song(
    pak: bytes,
    song: int,
    out_path: str,
    *,
    loop_count: int = 2,
    fade_ms: int = 4000,
    max_seconds: int = 900,
) -> float:
    """Render one .pak entry to a stereo WAV; returns seconds written.

    A looping song plays `loop_count` full loops then fades over `fade_ms`
    (loop_count=0: loop until `max_seconds`); one-shot songs always end on
    their own. `max_seconds` is only the safety cap.
    """
    chunks: list[bytes] = []
    frames = 0
    cap = max_seconds * MP2K_SAMPLE_RATE
    with Mp2kRenderer(pak, song, loop_count=loop_count, fade_ms=fade_ms) as r:
        while frames < cap:
            chunks.append(r.render(_CHUNK))
            frames += _CHUNK
            if r.ended:
                break
    _write_wav(out_path, b"".join(chunks), 2, MP2K_SAMPLE_RATE)
    return frames / MP2K_SAMPLE_RATE


def render_wbf_song(
    wbf: bytes,
    song: int,
    out_path: str,
    *,
    loops: int = 1,
    max_seconds: int = 0,
    normalize: bool = True,
) -> float:
    """Render one .wbf song to a mono WAV; returns seconds written.

    The format loops forever, so a render ends after `loops` passes of the
    song's loop point, or at `max_seconds` if nonzero (whichever first;
    max_seconds=0 means the pass count decides, capped at 900 s)."""
    r = WbfRenderer(wbf, song)
    cap = (max_seconds or 900) * WBF_SAMPLE_RATE
    chunks: list[bytes] = []
    total = 0
    while total < cap:
        chunks.append(r.render(_CHUNK))
        total += _CHUNK
        if not max_seconds and r.loops >= loops:
            break

    samples = array.array("h")
    samples.frombytes(b"".join(chunks))
    if normalize and samples:
        peak = max(1, max(abs(s) for s in samples))
        gain = 0.7 * 32767.0 / peak
        for i, s in enumerate(samples):
            v = int(s * gain)
            samples[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    _write_wav(out_path, samples.tobytes(), 1, WBF_SAMPLE_RATE)
    return total / WBF_SAMPLE_RATE
