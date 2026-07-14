# Contributing to gba-audio-tools

Thanks for helping! One hard rule first:

Pull requests containing ROMs, extracted `.pak`/`.wbf` song packs from
commercial games, or any Nintendo/Webfoot/licensor assets will be declined
without review.

The `.gitignore` blocks these extensions; do not work around
it. The only game-music files allowed in this repo are generated homebrew
fixtures with provenance recorded in `tests/fixtures/homebrew/SOURCES.md`.

## License

Everything here is GPL-3.0-or-later. The format documentation
under `docs/` is CC-BY-SA 4.0.

## Development

- Python ≥ 3.10, no runtime dependencies beyond the stdlib. The C core
  (`core/`) is portable C99 and builds as part of `pip install`; in a git
  checkout it is compiled with `cc` on first import.
- Tasks run through [`just`](https://github.com/casey/just). `just` on its
  own lists them. `just check` is what CI runs: `lint` (C under
  `-Wall -Wextra -Werror`, plus ruff), `typecheck` (ty), `build`, `test`.
  Every recipe just shells out to `uv` or `cc`, so `just` is a convenience,
  never a requirement: `uv run pytest tests -q` still works on its own.
- Comment style: `/* */` in C (not `//`), `#` in Python. Run `just lint`
  before opening a PR.
- Verification philosophy: every audio-affecting change should be checkable
  against a known-good reference. The engines were validated against
  agbplay (MP2K) and mGBA hardware emulation (Webfoot). Synthetic 
  in-memory ROMs (see `tests/test_pak.py`, `tests/test_webfoot.py`)
  keep that testable.
