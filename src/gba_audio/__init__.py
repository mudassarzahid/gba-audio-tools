"""gba-audio-tools: extract and render music from GBA ROMs, covering MP2K
(Sappy) and the Webfoot Technologies engine (Legacy of Goku II / Buu's Fury).

Pure-Python MP2K songtable scanning lives in gba_audio.scanner (importable
without a C toolchain); extraction and rendering load the C core on first
use via gba_audio.native / gba_audio.wav.
"""

from .scanner import SongEntry, SongTable, find_songtable

__version__ = "0.1.0"

__all__ = [
    "SongEntry",
    "SongTable",
    "find_songtable",
    "__version__",
]
