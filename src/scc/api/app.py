"""FastAPI app: health check, read-only JSON routes, and the Chainlit UI mounted at "/".

Run (one process, one port):

    uv run uvicorn scc.api.app:create_app --factory --port 8000

Routes:
    GET /health                       liveness + whether both databases are reachable
    GET /api/memory                   saved seller preferences
    GET /api/drafts                   approved listing drafts
    GET /api/purchase-orders          approved draft purchase orders
    GET /api/approvals                every approval request and its decision
    GET /api/sessions                 recent chats with request count and cost
    GET /api/sessions/{id}/trace      one chat's requests (cost, turns) and audit log
    GET /api/audit                    the latest audit records
    /                                 the Chainlit seller UI

The API is read-only on purpose: every change goes through the agent and its approval flow,
so there is exactly one way to write and it is audited. When SCC_API_TOKEN is set, /api/
routes require "Authorization: Bearer <token>".

Route order matters: the API routes are registered before Chainlit is mounted at "/", and
Starlette matches routes in the order they were added.
"""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from scc import __version__
from scc.agents.hub import HubRuntime, get_runtime
from scc.data.db import DatabaseMissingError, connect_readonly

# Absolute path, so the app starts from any working directory. Chainlit still reads
# .chainlit/config.toml and chainlit.md from the working directory (the project root).
# Module level (not inside create_app): with `from __future__ import annotations`, FastAPI
# resolves annotations by name from the module, so a function-local alias is invisible to it.
Limit = Annotated[int, Query(ge=1, le=1000)]

CHAINLIT_TARGET = str(Path(__file__).resolve().parents[1] / "ui" / "chainlit_app.py")


def create_app(runtime: HubRuntime | None = None, *, mount_ui: bool = True) -> FastAPI:
    """Build the app. Tests pass their own runtime and mount_ui=False."""
    rt = runtime or get_runtime()
    app = FastAPI(
        title="Seller Command Center API",
        version=__version__,
        description="Read-only views of the app's state. The chat UI is served at /.",
    )

    @app.get("/health", tags=["ops"])
    def health() -> JSONResponse:
        checks: dict[str, Any] = {}
        try:
            with connect_readonly(rt.settings.duckdb_path) as con:
                checks["retail_db"] = con.execute("SELECT COUNT(*) FROM sales").fetchone()[0] > 0
        except DatabaseMissingError:
            checks["retail_db"] = False
        try:
            rt.store.list_memory()
            checks["app_db"] = True
        except Exception:
            checks["app_db"] = False
        healthy = all(checks.values())
        body = {
            "status": "ok" if healthy else "degraded",
            "version": __version__,
            "as_of": rt.settings.as_of_date.isoformat(),
            **checks,
        }
        return JSONResponse(body, status_code=200 if healthy else 503)

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        token = rt.settings.api_token
        if token is None:
            return
        expected = f"Bearer {token.get_secret_value()}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Missing or invalid bearer token")

    api = APIRouter(prefix="/api", tags=["read-only"], dependencies=[Depends(require_token)])

    @api.get("/memory")
    def memory() -> list[dict]:
        return rt.store.list_memory()

    @api.get("/drafts")
    def drafts(limit: Limit = 50) -> list[dict]:
        return rt.store.list_listing_drafts(limit)

    @api.get("/purchase-orders")
    def purchase_orders(limit: Limit = 50) -> list[dict]:
        return rt.store.list_purchase_orders(limit)

    @api.get("/approvals")
    def approvals(limit: Limit = 50) -> list[dict]:
        return rt.store.list_approvals(limit)

    @api.get("/sessions")
    def sessions(limit: Limit = 20) -> list[dict]:
        return rt.store.list_sessions(limit)

    @api.get("/sessions/{session_id}/trace")
    def trace(session_id: str) -> dict:
        session = rt.store.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Unknown session")
        return {
            "session": session,
            "requests": rt.store.list_requests(session_id),
            "audit": rt.store.list_audit(session_id),
        }

    @api.get("/audit")
    def audit(limit: Limit = 200) -> list[dict]:
        return rt.store.list_audit(limit=limit)

    app.include_router(api)

    if mount_ui:
        # Imported here so tests (mount_ui=False) never load Chainlit's server.
        from chainlit.utils import mount_chainlit

        mount_chainlit(app=app, target=CHAINLIT_TARGET, path="/")
    return app
