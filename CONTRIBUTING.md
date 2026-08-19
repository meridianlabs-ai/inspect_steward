# Contributing to Inspect Steward

Thanks for your interest in contributing. Bug reports, doc fixes, and core changes are all welcome.

## Development setup

Steward uses [uv](https://docs.astral.sh/uv/) for environment and dependency management, and requires Python 3.12+.

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

# install the inspect-docs extension (once per checkout)
mv _quarto.yml _quarto.yml.bak
quarto add meridianlabs-ai/inspect-docs --no-prompt
mv _quarto.yml.bak _quarto.yml

quarto render        # outputs to docs/_site/
quarto preview       # live-reload server
```

The extension install is required once per checkout: `_quarto.yml` declares a project type contributed by the [inspect-docs](https://github.com/meridianlabs-ai/inspect-docs) extension, and rendering fails without it. The extension installs to `docs/_extensions/`, which is gitignored.

The `mv` dance is a bootstrap workaround, not superstition — `quarto add` parses `_quarto.yml` to choose an install directory, so it chokes on the very project type it is about to provide. Moving the file aside for the duration is the only way through.

The venv must be activated (not just `uv run quarto`) so the reference-page filter can import `griffe` and the `inspect_steward` package itself.

## Pull requests

- Branch from `main`; we squash-merge.
- Keep PRs focused on one change.
- Add or update tests for behavior changes.
- Update `CHANGELOG.md` under a new top entry if the change is user-visible.

## Reporting issues

Use the [issue tracker](https://github.com/meridianlabs-ai/inspect_steward/issues). For bugs, include the `inspect_steward` and `inspect_ai` versions (`pip show inspect-steward inspect-ai`) and a minimal repro.
