# Contributing to Inspect Steward

Thanks for your interest in contributing. Bug reports, doc fixes, and core changes are all welcome.

## Development setup

Steward uses [uv](https://docs.astral.sh/uv/) for environment and dependency management, and requires Python 3.12+.

The development environment pins 3.13 (`.python-version`) even though 3.12 is supported, because the optional `hawk` extra requires 3.13 — on 3.12 it resolves to nothing and the Hawk tests skip. CI covers both: 3.12 proves the floor still works without Hawk, and 3.14 exercises the Hawk path.

That asymmetry is worth remembering when touching anything that imports an optional package: `make check` only ever sees the 3.13 environment, where Hawk *is* installed, so it cannot catch a type error that appears only where Hawk is absent. CI typechecks 3.12 as well and will. To reproduce that half locally:

```bash
uv venv --python 3.12 /tmp/py312 && uv pip install --python /tmp/py312/bin/python -e . --group dev
.venv/bin/pyright --pythonpath /tmp/py312/bin/python
```

```bash
git clone https://github.com/meridianlabs-ai/inspect_steward
cd inspect_steward
uv sync --group dev
```

This installs Steward in editable mode along with the dev tools (ruff, pyright, pytest), and puts the `steward` CLI on your path.

## Checks and tests

Before opening a PR, make sure these pass:

```bash
make check   # pyright + ruff check --fix + ruff format
make test    # pytest
```

To run a single test:

```bash
uv run pytest tests/path/to/test.py::test_name -v
```

## Code style

- Type-annotate all functions (including tests). Use modern syntax (`X | None`, `list[str]`).
- Google-style docstrings for public APIs.
- `ruff` handles formatting and import order; don't fight it.
- Don't catch exceptions defensively — let unexpected errors propagate.

See [`CLAUDE.md`](CLAUDE.md) for the full conventions used in this repo.

## Building the docs

Docs live in `docs/` and are built with [Quarto](https://quarto.org). The Quarto CLI and the doc-build dependencies are in the `doc` group:

```bash
uv sync --group doc
cd docs
source ../.venv/bin/activate
quarto render        # outputs to docs/_site/
quarto preview       # live-reload server
```

The venv must be activated (not just `uv run quarto`) so the reference-page filter can import `griffe` and the `inspect_steward` package itself.

## Updating the docs extension

`docs/_extensions/` holds a checked-in copy of the [inspect-docs](https://github.com/meridianlabs-ai/inspect-docs) Quarto extension, which contributes the project type `_quarto.yml` declares. It is committed (per Quarto convention) so that a fresh clone can render without any install step.

To pull a newer version:

```bash
cd docs
mv _quarto.yml _quarto.yml.bak
quarto update meridianlabs-ai/inspect-docs --no-prompt
mv _quarto.yml.bak _quarto.yml
```

The `mv` is a bootstrap workaround, not superstition: `quarto add`/`update` parses `_quarto.yml` to choose an install directory, so it chokes on the very project type the extension provides. Commit the resulting diff.

The venv must be activated (not just `uv run quarto`) so the reference-page filter can import `griffe` and the `inspect_steward` package itself.

## Pull requests

- Branch from `main`; we squash-merge.
- Keep PRs focused on one change.
- Add or update tests for behavior changes.
- Update `CHANGELOG.md` under a new top entry if the change is user-visible.

## Reporting issues

Use the [issue tracker](https://github.com/meridianlabs-ai/inspect_steward/issues). For bugs, include the `inspect_steward` and `inspect_ai` versions (`pip show inspect-steward inspect-ai`) and a minimal repro.
