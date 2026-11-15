# Seller Command Center: one image, one process, one port (web UI + API on 8000).
#
#   docker compose up --build        (see docker-compose.yml)
#
# Notes:
# - python:3.13-slim (glibc), not Alpine: the Claude Code CLI bundled inside claude-agent-sdk
#   is a glibc binary. Because the SDK bundles the CLI, the image needs no Node.js.
# - Dependencies come from uv.lock (`uv sync --frozen`), so the container runs exactly the
#   versions that were tested.
# - Runs as a non-root user. All state lives in /data (a volume): the DuckDB analytics file,
#   the SQLite app and chat-history databases, and the CLI's session files (for resume=).

FROM python:3.13-slim

# uv, pinned to the version used to create uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    SCC_DATA_DIR=/data \
    CLAUDE_CONFIG_DIR=/data/claude

WORKDIR /app

# 1) Dependencies only, in their own layer: rebuilt only when pyproject.toml/uv.lock change.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) The application.
COPY src ./src
COPY .chainlit/config.toml ./.chainlit/config.toml
COPY chainlit.md ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN uv sync --frozen --no-dev \
    && chmod +x /usr/local/bin/entrypoint.sh

# Non-root user. APP_UID can match your host user on Linux, so files written to a bind-mounted
# ./data stay yours (docker compose build --build-arg APP_UID=$(id -u)).
ARG APP_UID=1000
RUN useradd --create-home --uid "${APP_UID}" app \
    && mkdir -p /data /app/.files /app/.chainlit/translations \
    && chown -R app:app /data /app/.files /app/.chainlit
USER app

EXPOSE 8000
VOLUME ["/data"]

# The slim image has no curl; the health check uses Python's standard library.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
