# Contributing to Inspect Steward

Thanks for your interest in contributing. Bug reports, doc fixes, and core changes are all welcome.

## Development setup

Steward uses [uv](https://docs.astral.sh/uv/) 0.13.4 or later for environment and dependency management, and requires Python 3.12+.

The development environment pins 3.13 (`.python-version`) even though 3.12 is supported, because the optional `hawk` extra requires 3.13 — on 3.12 it resolves to nothing and the Hawk tests skip. CI covers both: 3.12 proves the floor still works without Hawk, and 3.14 exercises the Hawk path.

That asymmetry is worth remembering when touching anything that imports an optional package: `make check` only ever sees the 3.13 environment, where Hawk *is* installed, so it cannot catch a type error that appears only where Hawk is absent. CI typechecks 3.12 as well and will. To reproduce that half locally:

```bash
uv venv --python 3.12 /tmp/py312 && uv pip install --python /tmp/py312/bin/python -e '.[dev]'
.venv/bin/pyright --pythonpath /tmp/py312/bin/python
```

```bash
git clone https://github.com/meridianlabs-ai/inspect_steward
cd inspect_steward
uv sync --extra dev
```

This installs Steward in editable mode along with the dev tools (ruff, pyright, pytest), and puts the `steward` CLI on your path. The dev tools are in the `dev` extra. The default `dev` dependency group includes that extra, so a plain `uv sync` or `uv run` (as the Makefile does) installs them too.

### Dependencies and the cooldown

`uv.lock` pins every Python dependency except the build toolchain. CI's lint, type-check, test and build jobs install from it, and fail when `uv.lock` is out of date with `pyproject.toml`. CI's build job and the release build use the build toolchain (build, hatchling, hatch-vcs) pinned with hashes in `scripts/build-dist/requirements.txt`. Dependabot updates that file weekly, and `.github/workflows/publish.yaml` gives the command to update it by hand. For editable installs with `uv sync`, uv resolves the build requirements (hatchling, hatch-vcs) without `uv.lock` or hashes, but within the cooldown. When you change dependencies in `pyproject.toml`, run `uv lock` and commit both files.

`pyproject.toml` sets `exclude-newer = "7 days"` under `[tool.uv]`, so `uv lock`, `uv sync` and `uv pip` only pick releases that have been on PyPI for at least seven days. A compromised release is usually found and removed within that time, before it reaches `uv.lock`, CI or your machine. Only a direct dependency (a package in `dependencies` or the `dev` extra in `pyproject.toml`) that is a model provider SDK or an Inspect package can wait 6 hours instead, so Steward can adopt a new release the day it ships. Today these are `anthropic`, `inspect-ai`, `inspect-flow`, `inspect-sandboxes`, `inspect-scout` and `openai`, listed with `"6 hours"` in `[tool.uv.exclude-newer-package]`. Every other package keeps the seven-day cooldown. Dependabot's `uv` entry in `.github/dependabot.yml` repeats the cooldown and the list. When you add a `"6 hours"` entry, also add the package to `cooldown.exclude` and the `six-hour-cooldown` group there. `tests/test_dependency_cooldown.py` checks that each entry is a direct dependency, that dated entries (below) are less than seven days old, and that the two files match. Whether a package is a provider SDK or an Inspect package is up to review.

`inspect-harbor` comes from the `main` branch of its git repository (see `[tool.uv.sources]`). The cooldown applies only to releases on an index, so it does not apply to that commit. `uv.lock` keeps the locked commit until you run `uv lock --upgrade-package inspect-harbor`. Dependabot ignores it.

Dependabot proposes `uv.lock` updates daily, once a release has cleared its cooldown. To adopt a release sooner, run `uv lock --upgrade-package <package>` and commit `uv.lock`. A release still inside its cooldown can't be locked; wait for it rather than overriding `exclude-newer`, unless it's an emergency (below).

In an emergency, for example a security fix, you can adopt a release that is less than seven days old with a one-time dated entry. This is not necessary for a package with a `"6 hours"` entry; wait the 6 hours.

1. Under `[tool.uv.exclude-newer-package]` in `pyproject.toml`, add `<package> = "<timestamp>"`. The package must be a direct dependency. Use an RFC 3339 timestamp just after the upload time of the fixed release. Don't add a dated entry to `.github/dependabot.yml`.
2. Run `uv lock --upgrade-package <package>` and commit both files.
3. Within seven days, once the normal cooldown includes the release, delete the entry, run `uv lock --upgrade-package <package>`, and commit both files. A plain `uv lock` doesn't reliably remove the entry from `uv.lock`, and CI then fails. A test fails when a dated entry is seven days old or older.

The nightly canary in [meridianlabs-ai/canaries](https://github.com/meridianlabs-ai/canaries) tests Steward against the newest release of every dependency, so Steward's own CI doesn't need to.

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

Docs live in `docs/` and are built with [Quarto](https://quarto.org). The Quarto CLI and the doc-build dependencies are in the `doc` group, which a plain `uv sync` leaves out (and removes):

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
uv sync --group doc
cd docs
source ../.venv/bin/activate
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
