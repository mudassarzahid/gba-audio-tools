# gba-audio-tools

[![CI](https://github.com/mudassarzahid/gba-audio-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/mudassarzahid/gba-audio-tools/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)

Extract and render music from Game Boy Advance ROMs that use either of
these two sound drivers:

- **MP2K / "Sappy" / m4a**: Nintendo's standard GBA driver, used by most
  commercial games (Pokémon, Fire Emblem, Golden Sun, ...).
- **The Webfoot Technologies driver**: a sample-tracker system used by
  *Dragon Ball Z: The Legacy of Goku II*, *Buu's Fury*, and others.
  This project reverse-engineered it; see
  [docs/webfoot-format.md](docs/webfoot-format.md).

The pipeline runs in three steps:

1. **Scan** a ROM to find its songtable.
2. **Extract** the songs into a self-contained image (`.pak` for MP2K,
   `.wbf` for Webfoot: sequences, instruments, and samples, with pointers
   relocated). The image needs no ROM afterward.
3. **Render** the image to WAV with a C implementation of the matching
   engine.

MP2K songs start out as compiled MIDI, so `gba-audio midi` converts them
back to standard `.mid` files for a DAW or notation program (pure Python;
the song's loop becomes `loopStart`/`loopEnd` markers). `gba-audio sf2`
builds a matching SoundFont from the same song's voicegroup, so the MIDI
plays with the game's own instruments instead of a General MIDI guess.
`sf2` also works on Webfoot ROMs: it exports the game's whole instrument
bank as a playable SoundFont. Webfoot has no MIDI export — `gba-audio midi`
reports which effects block it.

This tool differs from [agbplay](https://github.com/ipatix/agbplay) (MP2K
playback and GSF ripping) and
[GBA Mus Ripper](https://github.com/CaptainSwag101/gba-mus-ripper) (MP2K to
MIDI + SoundFont) in two ways. First, it is a `pip`-installable Python
library, not only a CLI. Second, it classifies every songtable slot as
*music*, *jingle*, or *sfx* from an audio-free pass over the song bytecode,
so you can pull a soundtrack without auditioning the sound-effect slots.

For Webfoot games, no other public tool exists. The extractor finds every
driver table by content signature. Testing has verified extraction and
rendering on all six known Webfoot games (181 songs). On *Legacy of Goku
II*, the renderer matches mGBA hardware emulation at ≥ 0.99 chroma-cosine
similarity.

```shell
gba-audio list game.gba                      # songs, with music/jingle/sfx classification
gba-audio extract game.gba -o game.pak       # every song classified as music
gba-audio wav game.gba --all -o outdir/      # straight to WAV, one file per song
gba-audio wav game.pak --song 3 -o song3.wav # from an extracted image
gba-audio midi game.gba -o outdir/           # MP2K sequences back to .mid
gba-audio sf2 game.gba --song 3 -o song3.sf2 # that song's instruments, as a SoundFont
gba-audio sf2 webfoot.gba -o bank.sf2        # Webfoot: the game's whole instrument bank
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
use. It ships no game data; this repo synthesizes its test fixtures from
scratch. Don't distribute extracted `.pak`/`.wbf` files or renders of
commercial music.

## Development

```shell
git clone git@github.com:mudassarzahid/gba-audio-tools.git && cd gba-audio-tools
just                                 # list the available tasks
just check                           # lint + format + typecheck + build + test
uv run gba-audio list yourgame.gba   # run the CLI from the checkout
```

Tasks run through [`just`](https://github.com/casey/just); each recipe is a
one-liner over `uv` or `cc`. [ruff](https://docs.astral.sh/ruff/) lints the
Python code and [ty](https://docs.astral.sh/ty/) type-checks it; the build
compiles the C core under `-Wall -Wextra -Werror`.

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
