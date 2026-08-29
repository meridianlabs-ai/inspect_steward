.PHONY: typecheck
typecheck:
	uv run pyright

.PHONY: check
check: typecheck
	uv run ruff check --fix
	uv run ruff format

.PHONY: test
test:
	uv run pytest

# the hawk tests reinstall inspect-ai into this venv (see pyproject's `network`
# marker), so they run alone and the sync afterwards puts the editable install
# back. Never fold these into `test`.
.PHONY: test-network
test-network:
	uv run pytest -m network -n0
	uv sync
