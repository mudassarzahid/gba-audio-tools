/* Symbol visibility for the shared library that the Python bindings load.
 *
 * gba_audio.native dlopen's the built core and looks its entry points up by
 * name with ctypes. On ELF and Mach-O they are exported by default, so
 * GBA_API expands to nothing; a Windows DLL exports nothing unless it is
 * marked, so there GBA_API is __declspec(dllexport).
 *
 * The macro is gated on GBA_AUDIO_BUILD_SHARED, which setup.py defines for
 * the extension build only. Targets that compile core/ straight into a
 * binary (the sunchip firmware) never define it and are unaffected.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
#ifndef GBA_EXPORT_H
#define GBA_EXPORT_H

#if defined(_WIN32) && defined(GBA_AUDIO_BUILD_SHARED)
#  define GBA_API __declspec(dllexport)
#else
#  define GBA_API
#endif

#endif /* GBA_EXPORT_H */
