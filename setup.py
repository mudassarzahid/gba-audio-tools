"""Builds the C core (core/) as the extension module gba_audio._native.

The module exports no PyInit_ symbol and is never imported. gba_audio.native
locates the built file next to the package and loads it with ctypes. Building
it as an Extension just borrows setuptools' compiler handling so wheels and
`pip install` work everywhere without a hand-rolled build step.
"""

import os

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

SOURCES = [
    "core/extractor/extractor.c",
    "core/extractor/extractor_mp2k.c",
    "core/extractor/extractor_webfoot.c",
    "core/mp2k/mp2k.c",
    "core/webfoot/webfoot.c",
]


class BuildSharedLib(build_ext):
    # Not a real Python extension: skip the PyInit_ export check some
    # platforms enforce.
    def get_export_symbols(self, ext):
        return []


setup(
    ext_modules=[
        Extension(
            "gba_audio._native",
            sources=SOURCES,
            include_dirs=["core"],
            libraries=["m"] if os.name == "posix" else [],
            # Marks the public entry points __declspec(dllexport) so ctypes can
            # find them in the DLL; expands to nothing off Windows. See
            # core/gba_export.h.
            define_macros=[("GBA_AUDIO_BUILD_SHARED", "1")],
        )
    ],
    cmdclass={"build_ext": BuildSharedLib},
)
