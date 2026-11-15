# Short aliases for common commands. Each target is a plain `uv run ...` you can also type by hand.
# More targets (evals, docker) are added as the milestones land.

.PHONY: install test lint fmt ingest check-sdk chat serve ui ui-e2e

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

ingest:   ## Download the UCI dataset (once) and build data/retail.duckdb (about 2 minutes)
	uv run python -m scc.data.ingest

check-sdk: ## Live checks of SDK behaviour the design relies on (costs about $0.01)
	uv run python scripts/check_sdk.py

chat:     ## Terminal chat (live; costs API credit)
	uv run scc chat

serve:    ## Web UI + API on http://localhost:8000 (one process; live chats cost API credit)
	uv run uvicorn scc.api.app:create_app --factory --host 127.0.0.1 --port 8000

ui:       ## Chainlit UI alone with auto-reload, for UI development (port 8000)
	uv run chainlit run src/scc/ui/chainlit_app.py -w

ui-e2e:   ## Headless end-to-end UI check against a running `make serve` (live, about $0.03)
	uv run python scripts/ui_e2e.py --url http://127.0.0.1:8000
