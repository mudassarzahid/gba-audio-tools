# Task runner for gba-audio-tools. Install `just` (brew install just), then
# run `just` to list recipes. Every recipe shells out to uv or cc, so nothing
# here is required to build or use the package; it is a convenience layer.

CORE := "core/extractor/extractor.c core/extractor/extractor_mp2k.c core/extractor/extractor_webfoot.c core/mp2k/mp2k.c core/webfoot/webfoot.c"

# List the available recipes.
default:
    @just --list

# Everything CI runs: lint, format check, typecheck, build, test.
check: lint fmt-check typecheck build test

# Lint C (warnings as errors) and Python (ruff).
lint: lint-c lint-py

# Compile the core with warnings as errors; no output, just diagnostics.
lint-c: lint-c-cc lint-c-gcc

lint-c-cc:
    cc -std=c99 -Wall -Wextra -Werror -Wno-unused-parameter -Icore -fsyntax-only {{CORE}}

# Also compile with a real GCC when one is installed. CI builds on Linux/GCC,
# which diagnoses things Apple clang does not (-Wmisleading-indentation among
# them), so on macOS `cc` alone lets those through to a red CI run.
lint-c-gcc:
    #!/usr/bin/env bash
    set -euo pipefail
    for c in gcc-16 gcc-15 gcc-14 gcc-13 gcc; do
        if command -v "$c" >/dev/null && ! "$c" --version 2>&1 | head -1 | grep -qi clang; then
            echo "gcc lint: $c"
            "$c" -std=c99 -Wall -Wextra -Werror -Wno-unused-parameter -Icore \
                 -fsyntax-only {{CORE}}
            exit 0
        fi
    done
    echo "gcc lint: no non-clang gcc found, skipping (CI still checks)"

# Static checks on the Python sources.
lint-py:
    uv run ruff check .

# Type-check the Python sources.
typecheck:
    uv run ty check

# Format the Python sources in place.
fmt:
    uv run ruff format .

# Report what `fmt` would change, without writing.
fmt-check:
    uv run ruff format --check --diff .

# Build the C core into the package as gba_audio._native.
build:
    uv run python setup.py build_ext --inplace

# Run the test suite.
test:
    uv run pytest tests -q

# Regenerate the homebrew .wbf test fixture from scratch.
fixture:
    uv run python tools/make_wbf_fixture.py tests/fixtures/homebrew/chiptune.wbf

# Hearing-test A/B for the MIDI export: engine WAV vs the exported .mid
# through the macOS GM synth AND through the game's own SoundFont
# (gba-audio sf2), chroma-cosine scored (see tools/midi_ab.py)
midi-ab rom song out="":
    uv run tools/midi_ab.py {{rom}} {{song}} --sf2 {{ if out != "" { "-o " + out } else { "" } }}

# Build the sdist + a wheel for this platform into dist/. The wheels that
# ship to PyPI are built for every platform by .github/workflows/release.yml;
# this is for checking the packaging locally.
dist: clean
    uv build

# Install the built wheel into a throwaway venv and run the suite against it,
# exactly as cibuildwheel does before publishing. Catches a wheel that cannot
# load its bundled _native.
dist-check: dist
    #!/usr/bin/env bash
    set -euo pipefail
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    uv venv "$tmp/venv" -q
    uv pip install --python "$tmp/venv/bin/python" -q dist/*.whl pytest
    (cd "$tmp" && "$tmp/venv/bin/python" -m pytest {{justfile_directory()}}/tests -q)

# Delete build artifacts and caches.
clean:
    rm -rf build dist src/*.egg-info .pytest_cache .ruff_cache
    find . -name '__pycache__' -not -path './.venv/*' -exec rm -rf {} +
    find src -name '*.so' -delete
