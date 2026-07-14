"""ctypes bindings to the C core (core/): extractor + both renderers.

One shared library carries everything: format detection and extraction
(core/extractor), the MP2K player (core/mp2k) and the Webfoot engine
(core/webfoot). A pip install builds it as the extension module
``gba_audio._native`` (see setup.py), never imported, only dlopen'd. In a
git checkout without a built extension, it is compiled from core/ with cc
on first use.
"""

from __future__ import annotations

import ctypes
import glob
import os
import subprocess
import sys
import tempfile
from typing import NamedTuple

PAK_MAGIC = b"SNCHPPAK"

FMT_MP2K = 0
FMT_WEBFOOT = 1

MP2K_SAMPLE_RATE = 32768  # rate both engines are tuned for
WBF_SAMPLE_RATE = 32768  # fixed in core/webfoot/webfoot.h

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_CORE_DIR = os.path.abspath(os.path.join(_PKG_DIR, "..", "..", "core"))

_C_SOURCES = (
    "extractor/extractor.c",
    "extractor/extractor_mp2k.c",
    "extractor/extractor_webfoot.c",
    "mp2k/mp2k.c",
    "webfoot/webfoot.c",
)


def _find_built_extension() -> str | None:
    for pattern in ("_native*.so", "_native*.pyd", "_native*.dylib"):
        hits = glob.glob(os.path.join(_PKG_DIR, pattern))
        if hits:
            return hits[0]
    return None


def _compile_from_source() -> str:
    """Dev fallback (git checkout, no built wheel): cc the core into a
    shared lib next to the package, or into a temp dir if that's not
    writable."""
    if not os.path.isdir(_CORE_DIR):
        raise OSError(
            "gba_audio._native is not built and the C sources (core/) are "
            "not present. Install from PyPI/a wheel, or work in a full "
            "git checkout."
        )
    out_dir = (
        _PKG_DIR if os.access(_PKG_DIR, os.W_OK) else tempfile.mkdtemp(prefix="gba_audio_native_")
    )
    out = os.path.join(out_dir, "_native_cc.so")
    if not os.path.exists(out) or any(
        os.path.getmtime(os.path.join(_CORE_DIR, s)) > os.path.getmtime(out) for s in _C_SOURCES
    ):
        cmd = [
            "cc",
            "-shared",
            "-fPIC",
            "-O2",
            "-I" + _CORE_DIR,
            *(os.path.join(_CORE_DIR, s) for s in _C_SOURCES),
            "-o",
            out,
        ]
        if os.name == "posix":
            cmd.append("-lm")
        try:
            subprocess.run(cmd, check=True)
        except Exception as e:  # pragma: no cover - toolchain-dependent
            print(f"Failed to compile the C core: {e}", file=sys.stderr)
            raise
    return out


def _load_lib() -> ctypes.CDLL:
    path = _find_built_extension() or _compile_from_source()
    return ctypes.CDLL(path)


lib = _load_lib()

# ---- extractor (core/extractor/extractor.h) --------------------------------

lib.extractor_new.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32]
lib.extractor_new.restype = ctypes.c_void_p
lib.extractor_free.argtypes = [ctypes.c_void_p]
lib.extractor_format.argtypes = [ctypes.c_void_p]
lib.extractor_format.restype = ctypes.c_int
lib.extractor_song_count.argtypes = [ctypes.c_void_p]
lib.extractor_song_count.restype = ctypes.c_int
lib.extractor_inst_count.argtypes = [ctypes.c_void_p]
lib.extractor_inst_count.restype = ctypes.c_int
lib.extractor_song_duration_ms.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.extractor_song_duration_ms.restype = ctypes.c_uint32
lib.extractor_song_channels.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.extractor_song_channels.restype = ctypes.c_int
lib.extractor_song_loops.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.extractor_song_loops.restype = ctypes.c_int
lib.extractor_build.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_int,
    ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),
]
lib.extractor_build.restype = ctypes.c_uint32

# free() for the buffer extractor_build hands back (it malloc'd it)
_libc = ctypes.CDLL(None)
_libc.free.argtypes = [ctypes.c_void_p]

# ---- MP2K player (core/mp2k/mp2k.h) ----------------------------------------


class Mp2kConfig(ctypes.Structure):
    _fields_ = [
        ("blob", ctypes.c_void_p),
        ("blob_size", ctypes.c_uint32),
        ("hdr_off", ctypes.c_uint32),
        ("sample_rate", ctypes.c_int),
        ("loop_count", ctypes.c_int),
        ("fade_ms", ctypes.c_int),
    ]


lib.mp2k_pak_songs.argtypes = [ctypes.c_char_p, ctypes.c_uint32]
lib.mp2k_pak_songs.restype = ctypes.c_int
lib.mp2k_pak_get.argtypes = [
    ctypes.c_char_p,
    ctypes.c_uint32,
    ctypes.c_int,
    ctypes.POINTER(Mp2kConfig),
]
lib.mp2k_pak_get.restype = ctypes.c_bool
lib.mp2k_pak_index.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_int]
lib.mp2k_pak_index.restype = ctypes.c_int
lib.mp2k_new.argtypes = [ctypes.POINTER(Mp2kConfig)]
lib.mp2k_new.restype = ctypes.c_void_p
lib.mp2k_delete.argtypes = [ctypes.c_void_p]
lib.mp2k_render.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int]
lib.mp2k_render.restype = ctypes.c_bool
lib.mp2k_n_tracks.argtypes = [ctypes.c_void_p]
lib.mp2k_n_tracks.restype = ctypes.c_int

# ---- Webfoot engine (core/webfoot/webfoot.h) -------------------------------

lib.wbf_engine_size.argtypes = []
lib.wbf_engine_size.restype = ctypes.c_uint
lib.wbf_open.argtypes = [
    ctypes.c_void_p,
    ctypes.c_char_p,
    ctypes.c_long,
    ctypes.POINTER(ctypes.c_char_p),
]
lib.wbf_open.restype = ctypes.c_void_p
lib.wbf_song_count.argtypes = [ctypes.c_void_p]
lib.wbf_song_count.restype = ctypes.c_int
lib.wbf_start.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.wbf_render.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int]
lib.wbf_loops.argtypes = [ctypes.c_void_p]
lib.wbf_loops.restype = ctypes.c_int

# ---- extraction API ---------------------------------------------------------


def extract_songs(rom: bytes, indices: list[int], table_pos: int = 0) -> tuple[bytes, int]:
    """Extract the given song indices into a self-contained .pak (MP2K) or
    .wbf (Webfoot) image; returns (output_bytes, format)."""
    ext = lib.extractor_new(rom, len(rom), table_pos)
    if not ext:
        raise ValueError("Unknown ROM format")
    try:
        fmt = lib.extractor_format(ext)
        idx_array = (ctypes.c_int * len(indices))(*indices)
        out_ptr = ctypes.POINTER(ctypes.c_uint8)()
        size = lib.extractor_build(ext, idx_array, len(indices), ctypes.byref(out_ptr))
        if size == 0:
            raise ValueError("Extractor build failed")
        out_bytes = ctypes.string_at(out_ptr, size)
        _libc.free(ctypes.cast(out_ptr, ctypes.c_void_p))
        return out_bytes, fmt
    finally:
        lib.extractor_free(ext)


class WebfootSong(NamedTuple):
    index: int
    duration_sec: float
    channels: int
    loops: bool


class WebfootInfo(NamedTuple):
    n_songs: int
    n_insts: int
    songs: list[WebfootSong]


def detect_webfoot(rom: bytes) -> WebfootInfo | None:
    """Return song/instrument counts and per-song metadata (duration, channel
    count, loop flag) if this is a Webfoot ROM, else None."""
    ext = lib.extractor_new(rom, len(rom), 0)
    if not ext:
        return None
    try:
        if lib.extractor_format(ext) != FMT_WEBFOOT:
            return None
        n_songs = lib.extractor_song_count(ext)
        songs = [
            WebfootSong(
                index=i,
                duration_sec=lib.extractor_song_duration_ms(ext, i) / 1000.0,
                channels=lib.extractor_song_channels(ext, i),
                loops=bool(lib.extractor_song_loops(ext, i)),
            )
            for i in range(n_songs)
        ]
        return WebfootInfo(n_songs=n_songs, n_insts=lib.extractor_inst_count(ext), songs=songs)
    finally:
        lib.extractor_free(ext)


def build_wbf(rom: bytes, out_path: str, indices: list[int] | None = None) -> dict:
    info = detect_webfoot(rom)
    if not info:
        raise ValueError("Not Webfoot format")
    if indices is None:
        indices = list(range(info.n_songs))
    out, _ = extract_songs(rom, indices)
    with open(out_path, "wb") as f:
        f.write(out)
    return {"songs": len(indices), "bytes": len(out)}


def build_pak(rom: bytes, table, indices: list[int], out_path: str) -> dict:
    out, _ = extract_songs(rom, indices, table.pos if table else 0)
    with open(out_path, "wb") as f:
        f.write(out)
    # The C core extracts the requested set as a unit (no per-song skip), so
    # every requested index is written or the whole build raises above.
    return {"written": indices, "errors": {}, "bytes": len(out)}


# ---- renderer API -----------------------------------------------------------


class Mp2kRenderer:
    """Streams one song of a .pak as interleaved stereo int16 at
    MP2K_SAMPLE_RATE. The pak bytes are borrowed, not copied: this object
    keeps them alive for the player."""

    def __init__(self, pak: bytes, song: int, *, loop_count: int = 2, fade_ms: int = 4000):
        self._pak = pak  # the engine reads the blob in place
        cfg = Mp2kConfig(sample_rate=MP2K_SAMPLE_RATE, loop_count=loop_count, fade_ms=fade_ms)
        if not lib.mp2k_pak_get(pak, len(pak), song, ctypes.byref(cfg)):
            raise ValueError(f"no song {song} in pak")
        self._p = lib.mp2k_new(ctypes.byref(cfg))
        if not self._p:
            raise ValueError(f"song {song}: bad song blob")
        self.ended = False

    def render(self, n: int) -> bytes:
        """Render n stereo frames (2*n int16 samples). Sets .ended once the
        song is over (one-shot finished, or loop_count loops + fade done)."""
        buf = (ctypes.c_int16 * (2 * n))()
        self.ended = not lib.mp2k_render(self._p, buf, 2 * n)
        return bytes(buf)

    @property
    def n_tracks(self) -> int:
        return lib.mp2k_n_tracks(self._p)

    def close(self) -> None:
        if self._p:
            lib.mp2k_delete(self._p)
            self._p = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def pak_songs(pak: bytes) -> int:
    """Number of songs in a .pak, or -1 if not a valid pak."""
    return lib.mp2k_pak_songs(pak, len(pak))


def pak_index(pak: bytes, song: int) -> int:
    """Original ROM songtable index of pak entry `song`, or -1."""
    return lib.mp2k_pak_index(pak, len(pak), song)


class WbfRenderer:
    """Streams one song of a .wbf as mono int16 at WBF_SAMPLE_RATE. The .wbf
    bytes are borrowed, not copied: this object keeps them alive."""

    def __init__(self, wbf: bytes, song: int | None = None):
        self._wbf = wbf
        self._mem = ctypes.create_string_buffer(lib.wbf_engine_size())
        err = ctypes.c_char_p()
        self._w = lib.wbf_open(self._mem, wbf, len(wbf), ctypes.byref(err))
        if not self._w:
            msg = err.value.decode() if err.value else "invalid .wbf"
            raise ValueError(f"wbf_open: {msg}")
        if song is not None:
            self.start(song)

    @property
    def song_count(self) -> int:
        return lib.wbf_song_count(self._w)

    def start(self, song: int) -> None:
        if not 0 <= song < self.song_count:
            raise ValueError(f"song out of range (0..{self.song_count - 1})")
        lib.wbf_start(self._w, song)

    def render(self, n: int) -> bytes:
        """Render n mono int16 samples."""
        buf = (ctypes.c_int16 * n)()
        lib.wbf_render(self._w, buf, n)
        return bytes(buf)

    @property
    def loops(self) -> int:
        """Times playback has crossed the song's loop point."""
        return lib.wbf_loops(self._w)
