#!/bin/sh
# Container start: build the database on first run, then serve the web UI + API.
set -eu

DATA_DIR="${SCC_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR" "${CLAUDE_CONFIG_DIR:-$DATA_DIR/claude}"

if [ ! -f "$DATA_DIR/retail.duckdb" ]; then
    echo "First start: downloading the UCI dataset and building $DATA_DIR/retail.duckdb (1-2 min)..."
    python -m scc.data.ingest
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "WARNING: ANTHROPIC_API_KEY is not set; the UI will start but questions will fail." >&2
fi

# exec: uvicorn replaces this shell as PID 1, so `docker stop` (SIGTERM) reaches it and it shuts
# down cleanly. 0.0.0.0 inside the container; compose publishes it on 127.0.0.1 only.
exec uvicorn scc.api.app:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers
