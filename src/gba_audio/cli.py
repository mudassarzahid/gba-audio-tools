"""gba-audio CLI.

gba-audio list game.gba
gba-audio extract game.gba --songs music -o game.pak
gba-audio wav game.gba --all -o outdir/
gba-audio wav game.pak --song 3 -o song3.wav
gba-audio midi game.gba -o outdir/
gba-audio midi game.pak --song 3 -o song3.mid
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

from .scanner import AGB_MAP_ROM, find_songtable


def no_songtable_message(name: str, data: bytes) -> str:
    """Explain why there's no MP2K songtable: .spc sound file, SNES ROM,
    GBA ROM with a custom driver (GAX named when its signature is present),
    or not a GBA ROM at all."""
    if data[:27] == b"SNES-SPC700 Sound File Data":
        return (
            "That's a .spc, a SNES sound file, not a GBA ROM. This tool "
            "only reads GBA ROMs; play .spc files with an SPC-capable "
            "player (game-music-emu, foobar2000, Audacious)."
        )

    def pair(off: int) -> bool:  # SNES header: checksum ^ complement == 0xFFFF
        if len(data) <= off + 3:
            return False
        lo = data[off] | (data[off + 1] << 8)
        hi = data[off + 2] | (data[off + 3] << 8)
        return (lo ^ hi) == 0xFFFF

    # A valid GBA header wins: never call a real GBA ROM "SNES" on a
    # 4-byte checksum coincidence.
    is_gba = len(data) >= 0xB3 and data[0xB2] == 0x96
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if not is_gba and (
        ext in ("sfc", "smc", "fig")
        or pair(0x7FDC)
        or pair(0xFFDC)
        or pair(0x7FDC + 512)
        or pair(0xFFDC + 512)
    ):
        return (
            "This is a SNES ROM, and this tool only reads GBA ROMs. "
            "SNES music also can't be extracted from a ROM file at all "
            "(the game uploads it to the sound chip while running). To "
            "get these songs, dump .spc files from an emulator "
            "(Snes9x/Mesen have an SPC dump key; one press per song) and "
            "play them with an SPC-capable player (game-music-emu, "
            "foobar2000, Audacious)."
        )
    if len(data) < 0xB3 or data[0xB2] != 0x96:
        return "This doesn't look like a GBA ROM. This tool reads GBA ROMs (.gba)."
    if b"GAX Sound Engine" in data:
        return (
            "No MP2K songtable: this game uses Shin'en's GAX engine, "
            "which this tool doesn't support."
        )
    return (
        "No MP2K songtable and no Webfoot driver found. This game uses "
        "a custom sound driver (known example: the Super Mario Advance "
        "ports). Most GBA games do use MP2K, so try another game."
    )


def _mtime(sec: float, loops: bool = False) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}{'⟳' if loops else ' '}"


def _load(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _music_indices(table, min_tracks: int) -> list[int]:
    return [s.index for s in table.songs if s.kind == "music" and s.n_tracks >= min_tracks]


def cmd_list(args) -> int:
    data = _load(args.rom)
    table = find_songtable(data)
    if table is not None:

        def n(k: str) -> int:
            return sum(1 for s in table.songs if s.kind == k)

        print(
            f"MP2K songtable at 0x{table.pos:06X}, {len(table.songs)} "
            f"entries, {n('music')} music, {n('jingle')} jingles, "
            f"{n('sfx')} sound effects"
        )
        for s in table.songs:
            if s.n_tracks == 0:
                continue  # empty slot
            print(
                f"  [{s.index:3d}] song@0x{s.song_pos:06X}  {s.kind:6s}  "
                f"{_mtime(s.duration_sec, s.loops):>7s}  "
                f"tracks={s.n_tracks:2d}  notes={s.note_count:4d}  "
                f"player={s.player}"
            )
        return 0

    from .native import detect_webfoot

    wf = detect_webfoot(data)
    if wf is not None:
        print(f"Webfoot ROM: {wf.n_songs} songs, {wf.n_insts} instruments")
        for s in wf.songs:
            print(
                f"  [{s.index:3d}]  {_mtime(s.duration_sec, s.loops):>7s}  channels={s.channels:2d}"
            )
        return 0

    print(no_songtable_message(args.rom, data), file=sys.stderr)
    return 1


def cmd_extract(args) -> int:
    from .native import FMT_MP2K, extract_songs

    data = _load(args.rom)
    table = find_songtable(data)

    if table is not None:
        if args.songs in (None, "music"):
            indices = _music_indices(table, args.min_tracks)
            if not indices:
                print("no songs classified as music", file=sys.stderr)
                return 1
        else:
            indices = [int(x) for x in args.songs.split(",")]
        out_bytes, fmt = extract_songs(data, indices, table.pos)
    else:
        from .native import detect_webfoot

        wf = detect_webfoot(data)
        if wf is None:
            print(no_songtable_message(args.rom, data), file=sys.stderr)
            return 1
        indices = (
            [int(x) for x in args.songs.split(",")]
            if args.songs not in (None, "music")
            else list(range(wf.n_songs))
        )
        out_bytes, fmt = extract_songs(data, indices)

    ext = ".pak" if fmt == FMT_MP2K else ".wbf"
    out = args.out or (args.rom.rsplit(".", 1)[0] + ext)
    with open(out, "wb") as f:
        f.write(out_bytes)
    print(f"wrote {out}: {len(indices)} songs, {len(out_bytes)} bytes")
    return 0


def cmd_wav(args) -> int:
    from .native import (
        FMT_MP2K,
        PAK_MAGIC,
        WbfRenderer,
        detect_webfoot,
        extract_songs,
        pak_index,
        pak_songs,
    )
    from .wav import render_pak_song, render_wbf_song

    data = _load(args.input)

    # Accept an extracted image or a ROM (extracted in memory). --song /
    # --songs select ROM songtable indices for a ROM, entry numbers for an
    # already-extracted image.
    selected = (
        [args.song]
        if args.song is not None
        else [int(x) for x in args.songs.split(",")]
        if args.songs
        else None
    )

    if data[:8] == PAK_MAGIC:
        image, is_pak, from_rom = data, True, False
    elif data[:4] == b"WBF1":
        image, is_pak, from_rom = data, False, False
    else:
        from_rom = True
        table = find_songtable(data)
        if table is not None:
            indices = selected or _music_indices(table, 0)
            if not indices:
                print("no songs classified as music", file=sys.stderr)
                return 1
            image, fmt = extract_songs(data, indices, table.pos)
            is_pak = fmt == FMT_MP2K
        else:
            wf = detect_webfoot(data)
            if wf is None:
                print(no_songtable_message(args.input, data), file=sys.stderr)
                return 1
            image, _ = extract_songs(data, selected or list(range(wf.n_songs)))
            is_pak = False

    if is_pak:
        count = pak_songs(image)

        def label(i: int) -> int:
            # name output by original ROM songtable index (the pak keeps it)
            return idx if (idx := pak_index(image, i)) >= 0 else i
    else:
        count = WbfRenderer(image).song_count

        def label(i: int) -> int:
            return i

    # A ROM input was already narrowed by the extraction: render the whole
    # image, named by original songtable index (the pak keeps it; a .wbf
    # from a selection would renumber, so keep the selection as labels).
    entries = list(range(count))
    if not from_rom and selected:
        entries = selected
    wbf_labels = selected if (from_rom and not is_pak and selected) else None

    single = len(entries) == 1
    if single and args.out and not os.path.isdir(args.out):
        outs = [args.out]
    else:
        outdir = args.out or "."
        os.makedirs(outdir, exist_ok=True)
        names = wbf_labels or [label(i) for i in entries]
        outs = [os.path.join(outdir, f"song{n:03d}.wav") for n in names]

    for i, out in zip(entries, outs, strict=True):
        if is_pak:
            sec = render_pak_song(
                image,
                i,
                out,
                loop_count=args.loops,
                fade_ms=args.fade,
                max_seconds=args.seconds or 900,
            )
            print(f"wrote {out} ({sec:.1f}s)")
        else:
            passes = max(1, args.loops)
            res = render_wbf_song(image, i, out, loops=passes, max_seconds=args.seconds)
            # Spell out the loop policy: the WAV is longer than the song
            # length `list` (and the web converter) reports, because the
            # render plays the song and then loops it.
            n = f"{passes} pass" + ("es" if passes != 1 else "")
            detail = f" = {n} of a {_mtime(res.song_sec).strip()} song" if res.song_sec else ""
            print(f"wrote {out} ({res.seconds:.1f}s{detail})")
    return 0


_NO_MIDI_FOR_WEBFOOT = (
    "MIDI export supports MP2K songs only. Most of the Webfoot sequencer "
    "would survive the trip (notes, tempo, pan, volume slides), but sample "
    "offset (fx15) has no MIDI equivalent at all and vibrato/portamento are "
    "absolute period deltas rather than a synth-defined bend range, so an "
    "export would drift from what the game plays. Render the audio with "
    "`gba-audio wav`, or take the instruments with `gba-audio sf2`."
)


def cmd_midi(args) -> int:
    # Everything on this path is pure Python (scanner + midi): like `list`,
    # MIDI export works without the C core. native is only imported to name
    # a Webfoot ROM in the error message.
    from .midi import PAK_MAGIC, pak_entry_count, pak_entry_index, pak_song_to_midi, song_to_midi

    data = _load(args.input)
    selected = (
        [args.song]
        if args.song is not None
        else [int(x) for x in args.songs.split(",")]
        if args.songs
        else None
    )

    # jobs: (label for the file name, SMF bytes); .pak entries are addressed
    # by entry number but named by original songtable index, as in cmd_wav.
    jobs: list[tuple[int, bytes]] = []
    if data[:8] == PAK_MAGIC:
        entries = selected if selected is not None else list(range(pak_entry_count(data)))
        for e in entries:
            idx = pak_entry_index(data, e)
            jobs.append((idx if idx >= 0 else e, pak_song_to_midi(data, e)))
    elif data[:4] == b"WBF1":
        print(_NO_MIDI_FOR_WEBFOOT, file=sys.stderr)
        return 1
    else:
        table = find_songtable(data)
        if table is None:
            from .native import detect_webfoot

            msg = (
                _NO_MIDI_FOR_WEBFOOT
                if detect_webfoot(data) is not None
                else no_songtable_message(args.input, data)
            )
            print(msg, file=sys.stderr)
            return 1
        indices = selected if selected is not None else _music_indices(table, 0)
        if not indices:
            print("no songs classified as music", file=sys.stderr)
            return 1
        for i in indices:
            if not 0 <= i < len(table.songs) or table.songs[i].n_tracks == 0:
                print(f"skipping song {i}: empty or out of range", file=sys.stderr)
                continue
            jobs.append((i, song_to_midi(data, table.songs[i].song_pos)))

    if not jobs:
        print("nothing to convert", file=sys.stderr)
        return 1
    if len(jobs) == 1 and args.out and not os.path.isdir(args.out):
        outs = [args.out]
    else:
        outdir = args.out or "."
        os.makedirs(outdir, exist_ok=True)
        outs = [os.path.join(outdir, f"song{n:03d}.mid") for n, _ in jobs]
    for (_, smf), out in zip(jobs, outs, strict=True):
        with open(out, "wb") as f:
            f.write(smf)
        print(f"wrote {out} ({len(smf)} bytes)")
    return 0


def _webfoot_sf2(wbf: bytes, args) -> int:
    """Write the SoundFont for a .wbf's instrument table. Webfoot instruments
    are shared ROM-wide rather than per-song, so this is one bank per game and
    --song/--songs do not narrow it."""
    from .sf2 import wbf_to_sf2

    if args.song is not None or args.songs:
        print(
            "note: Webfoot instruments are shared by every song in the ROM, "
            "so the whole bank is exported and --song/--songs are ignored.",
            file=sys.stderr,
        )
    name = os.path.basename(args.input).rsplit(".", 1)[0]
    try:
        sf2, notes = wbf_to_sf2(wbf, name=name)
    except ValueError as e:
        print(f"sf2: {e}", file=sys.stderr)
        return 1
    for n in notes:
        print(n, file=sys.stderr)
    out = args.out or (args.input.rsplit(".", 1)[0] + ".sf2")
    if out.endswith(os.sep) or os.path.isdir(out):
        out = os.path.join(out, f"{name}.sf2")
    with open(out, "wb") as f:
        f.write(sf2)
    print(f"wrote {out}: {len(sf2)} bytes")
    return 0


def cmd_sf2(args) -> int:
    # Pure Python like cmd_midi; native is only for naming Webfoot ROMs.
    from .midi import PAK_MAGIC, pak_entry_count, pak_entry_index, song_used_programs
    from .sf2 import song_to_sf2

    data = _load(args.input)
    selected = (
        [args.song]
        if args.song is not None
        else [int(x) for x in args.songs.split(",")]
        if args.songs
        else None
    )

    def build(blob: bytes, hdr_off: int, base: int, label: int) -> tuple[int, bytes] | None:
        progs = song_used_programs(blob, hdr_off, base=base)
        try:
            sf2, skipped = song_to_sf2(blob, hdr_off, progs, base=base, name=f"song{label:03d}")
        except ValueError as e:
            print(f"skipping song {label}: {e}", file=sys.stderr)
            return None
        for note in skipped:
            print(f"song {label}: {note}", file=sys.stderr)
        return label, sf2

    jobs: list[tuple[int, bytes]] = []
    if data[:8] == PAK_MAGIC:
        entries = selected if selected is not None else list(range(pak_entry_count(data)))
        for e in entries:
            if not 0 <= e < pak_entry_count(data):
                print(f"skipping entry {e}: out of range", file=sys.stderr)
                continue
            off, size, hdr_off = struct.unpack_from("<III", data, 12 + e * 16)
            if size == 0:
                print(f"skipping entry {e}: dropped at extraction", file=sys.stderr)
                continue
            idx = pak_entry_index(data, e)
            job = build(data[off : off + size], hdr_off, 0, idx if idx >= 0 else e)
            if job:
                jobs.append(job)
    elif data[:4] == b"WBF1":
        return _webfoot_sf2(data, args)
    else:
        table = find_songtable(data)
        if table is None:
            from .native import detect_webfoot, extract_songs

            wf = detect_webfoot(data)
            if wf is None:
                print(no_songtable_message(args.input, data), file=sys.stderr)
                return 1
            # The instrument table is shared by every song, so extracting the
            # whole ROM and exporting its bank is the same result as any
            # single-song extraction, without needing a song selection.
            wbf, _ = extract_songs(data, list(range(wf.n_songs)))
            return _webfoot_sf2(wbf, args)
        indices = selected if selected is not None else _music_indices(table, 0)
        if not indices:
            print("no songs classified as music", file=sys.stderr)
            return 1
        for i in indices:
            if not 0 <= i < len(table.songs) or table.songs[i].n_tracks == 0:
                print(f"skipping song {i}: empty or out of range", file=sys.stderr)
                continue
            job = build(data, table.songs[i].song_pos, AGB_MAP_ROM, i)
            if job:
                jobs.append(job)

    if not jobs:
        print("nothing to convert", file=sys.stderr)
        return 1
    if len(jobs) == 1 and args.out and not os.path.isdir(args.out):
        outs = [args.out]
    else:
        outdir = args.out or "."
        os.makedirs(outdir, exist_ok=True)
        outs = [os.path.join(outdir, f"song{n:03d}.sf2") for n, _ in jobs]
    for (_, sf2), out in zip(jobs, outs, strict=True):
        with open(out, "wb") as f:
            f.write(sf2)
        print(f"wrote {out} ({len(sf2)} bytes)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="gba-audio",
        description="Extract and render music from GBA ROMs you own "
        "(MP2K/Sappy and the Webfoot engine)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list songs in a ROM")
    pl.add_argument("rom", help="GBA ROM file (a game you legally own)")
    pl.set_defaults(fn=cmd_list)

    pe = sub.add_parser("extract", help="extract songs to a self-contained .pak/.wbf")
    pe.add_argument("rom", help="GBA ROM file (a game you legally own)")
    pe.add_argument(
        "--songs",
        help="comma-separated song indices, or 'music' (default) "
        "for every slot classified as music",
    )
    pe.add_argument(
        "--min-tracks",
        type=int,
        default=0,
        metavar="N",
        help="with 'music': also require at least N tracks",
    )
    pe.add_argument("-o", "--out", help="output path (.pak or .wbf)")
    pe.set_defaults(fn=cmd_extract)

    pw = sub.add_parser("wav", help="render songs to WAV")
    pw.add_argument("input", help=".pak, .wbf, or GBA ROM")
    sel = pw.add_mutually_exclusive_group()
    sel.add_argument("--song", type=int, help="render one song")
    sel.add_argument("--songs", help="comma-separated song indices")
    sel.add_argument(
        "--all",
        action="store_true",
        help="render every song (the default when no selection is given)",
    )
    pw.add_argument("-o", "--out", help="output .wav (single song) or directory")
    pw.add_argument(
        "--loops",
        type=int,
        default=2,
        metavar="N",
        help="stop after N loops (MP2K: then fade; Webfoot: loop-point passes; "
        "default 2, so a Webfoot WAV runs ~2x the song length `list` reports — "
        "use --loops 1 for a single play-through)",
    )
    pw.add_argument(
        "--fade",
        type=int,
        default=4000,
        metavar="MS",
        help="MP2K fade-out length in ms (default 4000)",
    )
    pw.add_argument(
        "--seconds",
        type=int,
        default=0,
        metavar="S",
        help="hard length cap in seconds (0 = policy decides)",
    )
    pw.set_defaults(fn=cmd_wav)

    pm = sub.add_parser(
        "midi",
        help="convert MP2K songs to standard MIDI files",
        description="Convert MP2K sequences back to standard MIDI "
        "(SMF format 1, one pass with loopStart/loopEnd markers). "
        "Program numbers are the game's voicegroup indices, not "
        "General MIDI, so expect odd sounds on a GM synth; the export "
        "is meant for DAW/notation work. MP2K only: Webfoot data is "
        "a PCM tracker format with no MIDI equivalent.",
    )
    pm.add_argument("input", help=".pak or GBA ROM (a game you legally own)")
    pm.add_argument("--song", type=int, help="convert one song")
    pm.add_argument("--songs", help="comma-separated song indices")
    pm.add_argument("-o", "--out", help="output .mid (single song) or directory")
    pm.set_defaults(fn=cmd_midi)

    ps = sub.add_parser(
        "sf2",
        help="build a SoundFont (.sf2) of a song's instruments",
        description="Build a SoundFont 2 bank from an MP2K song's "
        "voicegroup: preset n = the song's program n, so the .sf2 loaded "
        "next to the matching `gba-audio midi` export plays the notes "
        "with the game's own samples. MP2K only.",
    )
    ps.add_argument("input", help=".pak or GBA ROM (a game you legally own)")
    ps.add_argument("--song", type=int, help="build for one song")
    ps.add_argument("--songs", help="comma-separated song indices")
    ps.add_argument("-o", "--out", help="output .sf2 (single song) or directory")
    ps.set_defaults(fn=cmd_sf2)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
