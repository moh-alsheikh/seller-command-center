# Short aliases for common commands. Each target is a plain `uv run ...` you can also type by hand.

.PHONY: install test lint fmt ingest check-sdk chat serve ui ui-e2e evals evals-dry docker-up docker-down docker-logs

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

evals-dry: ## Compute eval ground truth only (free)
	uv run python evals/run_evals.py --dry-run

evals:    ## Full eval suite -> evals/report.md (live, about $1.50-4)
	uv run python evals/run_evals.py

docker-up:   ## Build and start the container on http://localhost:8000 (reuses ./data)
	docker compose up -d --build

docker-down: ## Stop the container (data in ./data is kept)
	docker compose down

docker-logs: ## Follow the container logs
	docker compose logs -f app
