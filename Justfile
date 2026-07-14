# Task runner for gba-audio-tools. Install `just` (brew install just), then
# run `just` to list recipes. Every recipe shells out to uv or cc, so nothing
# here is required to build or use the package; it is a convenience layer.

CORE := "core/extractor/extractor.c core/extractor/extractor_mp2k.c core/extractor/extractor_webfoot.c core/mp2k/mp2k.c core/webfoot/webfoot.c"

# List the available recipes.
default:
    @just --list

# Everything CI runs: lint, typecheck, build, test.
check: lint typecheck build test

# Lint C (warnings as errors) and Python (ruff).
lint: lint-c lint-py

# Compile the core with warnings as errors; no output, just diagnostics.
lint-c:
    cc -std=c99 -Wall -Wextra -Werror -Wno-unused-parameter -Icore -fsyntax-only {{CORE}}

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

# Delete build artifacts and caches.
clean:
    rm -rf build dist src/*.egg-info .pytest_cache .ruff_cache
    find . -name '__pycache__' -not -path './.venv/*' -exec rm -rf {} +
    find src -name '*.so' -delete
