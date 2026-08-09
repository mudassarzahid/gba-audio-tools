# gba-audio-tools

[![CI](https://github.com/mudassarzahid/gba-audio-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/mudassarzahid/gba-audio-tools/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/gba-audio-tools)](https://pypi.org/project/gba-audio-tools/)
[![Python](https://img.shields.io/pypi/pyversions/gba-audio-tools)](https://pypi.org/project/gba-audio-tools/)

Extract and render music from Game Boy Advance ROMs that use either of
these two sound engines:

- **MP2K / "Sappy" / m4a**: Nintendo's standard GBA driver, used by most
  commercial games (Pokémon, Fire Emblem, Golden Sun, ...).
- **The Webfoot Technologies engine**: the sample-tracker driver used by
  *Dragon Ball Z: The Legacy of Goku II*, *Buu's Fury*, and others.
  It was reverse-engineered for this project and is documented in
  [docs/webfoot-format.md](docs/webfoot-format.md).

The pipeline: scan a ROM, extract its songs into a self-contained image
(`.pak` for MP2K, `.wbf` for Webfoot: sequences + instruments + samples,
pointers relocated, no ROM needed afterwards), and render to WAV with C
implementations of both engines. MP2K songs (compiled MIDI to begin with)
can also be converted back to standard `.mid` files for DAW or notation
work (pure Python, with the song's loop as `loopStart`/`loopEnd` markers)
and `gba-audio sf2` builds the matching SoundFont from the song's
voicegroup, so the MIDI plays with the game's own instruments instead of
General MIDI guesses.

Where it differs from [agbplay](https://github.com/ipatix/agbplay) (MP2K
playback and GSF ripping) and
[GBA Mus Ripper](https://github.com/CaptainSwag101/gba-mus-ripper) (MP2K to
MIDI + SoundFont): it is a `pip`-installable Python library rather than only
a CLI, and it classifies every songtable slot as *music / jingle / sfx* from
an audio-free pass over the song bytecode, so you can pull a soundtrack
without auditioning the sound-effect slots. For Webfoot games no other
public tool works at all. The extractor finds every driver table by content
signature. Extraction and rendering are verified on all six known Webfoot 
games (181 songs), with the renderer matching mGBA hardware emulation at 
≥ 0.99 chroma-cosine similarity on *Legacy of Goku II*.

```
pip install gba-audio-tools

gba-audio list game.gba                      # songs, with music/jingle/sfx classification
gba-audio extract game.gba -o game.pak       # every song classified as music
gba-audio wav game.gba --all -o outdir/      # straight to WAV, one file per song
gba-audio wav game.pak --song 3 -o song3.wav # from an extracted image
gba-audio midi game.gba -o outdir/           # MP2K sequences back to .mid
gba-audio sf2 game.gba --song 3 -o song3.sf2 # that song's instruments, as a SoundFont
```

The same from Python. The scanner is pure Python, importable with no native
code:

```python
from gba_audio import find_songtable

table = find_songtable(open("game.gba", "rb").read())
for s in table.songs:
    if s.kind == "music":
        print(s.index, round(s.duration_sec), "sec", "loops" if s.loops else "")
```

## Legal

This tool reads ROMs of games **you own** and extracts data for personal
use. It ships no game data. Test fixtures are synthesized from scratch. Don't
distribute extracted `.pak`/`.wbf` files or renders of commercial music.

## Development

```
git clone git@github.com:mudassarzahid/gba-audio-tools.git && cd gba-audio-tools
just                                 # list the available tasks
just check                           # lint + typecheck + build + test
uv run gba-audio list yourgame.gba   # run the CLI from the checkout
```

Tasks run through [`just`](https://github.com/casey/just); each recipe is a
one-liner over `uv` or `cc`. Python is linted with [ruff](https://docs.astral.sh/ruff/)
and type-checked with [ty](https://docs.astral.sh/ty/); the C core is compiled
under `-Wall -Wextra -Werror`.

The C core lives in `core/` (portable C99, GPL-3.0-or-later): the
format-detecting extractor, the MP2K player (semantics follow
[agbplay](https://github.com/ipatix/agbplay), the reference MP2K
implementation), and the Webfoot engine (integer-exact timing, mono
32 768 Hz). `pip install` builds it automatically; a git checkout compiles
it with `cc` on first import.

## Related work

- [agbplay](https://github.com/ipatix/agbplay): MP2K player + GSF tooling (C++)
- [GBA Mus Ripper](https://github.com/CaptainSwag101/gba-mus-ripper): MP2K → MIDI + SoundFont
- [loveemu's MP2K documentation](https://loveemu.github.io/vgmdocs/Summary_of_GBA_Standard_Sound_Driver_MusicPlayer2000.html)
- [engine-software-gba-tools](https://github.com/lunasorcery/engine-software-gba-tools): the Engine Software replayer
- [VG Music Studio](https://github.com/Kermalis/VGMusicStudio): multi-format player (C#)

## License

GPL-3.0-or-later (code) / CC-BY-SA 4.0 (`docs/`). See [LICENSE](LICENSE).
