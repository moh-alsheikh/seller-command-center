# Short aliases for common commands. Each target is a plain `uv run ...` you can also type by hand.
# More targets (ingest, chat, ui, evals, docker) are added as the milestones land.

.PHONY: install test lint fmt

install:  ## Create .venv and install the pinned dependencies from uv.lock
	uv sync

test:     ## Offline unit tests (no Anthropic or HTTP calls)
	uv run pytest

lint:     ## Static checks
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt:      ## Auto-format and auto-fix
	uv run ruff format src tests
	uv run ruff check --fix src tests
