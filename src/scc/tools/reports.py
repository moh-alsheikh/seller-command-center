"""The report_designer's tool: create_report (charts + HTML/PDF page + Excel), built per session.

Why a spoke tool and not a hub tool? Building a report reads data, and the hub has no data
tools (rule 1). The policy treats it like the other read tools: allowed for specialists,
denied to the hub. No approval is needed: it only writes a report file inside the app's own
data folder and changes nothing in the shop (like saving a preference).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

from scc.reports.builder import MAX_CHARTS, build_report
from scc.reports.charts import CHART_TYPES
from scc.reports.sources import CURATED, CURATED_HELP, MAX_SQL_ROWS
from scc.store.app_db import AppStore
from scc.tools import ToolServer
from scc.tools._common import ToolContext, ok, safe

SERVER_KEY = "reports"

_SOURCES_HELP = "; ".join(f"{name}({args})" for name, args in CURATED_HELP.items())

CHART_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 2, "maxLength": 120},
        "type": {"type": "string", "enum": list(CHART_TYPES)},
        "data": {
            "type": "object",
            "description": (
                "EITHER {source, params} for a curated source, OR {sql, x, y, series?} for a "
                "read-only SELECT written for this request."
            ),
            "properties": {
                "source": {"type": "string", "enum": sorted(CURATED)},
                "params": {"type": "object"},
                "sql": {"type": "string", "maxLength": 4000},
                "x": {"type": "string"},
                "y": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                "series": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "required": ["title", "type", "data"],
    "additionalProperties": False,
}


def build_server(
    ctx: ToolContext, store: AppStore, session_id: str | None, reports_dir: Path
) -> ToolServer:
    @tool(
        "create_report",
        "Build a report for the seller: 1-6 charts, a printable page (PDF via the browser) and "
        "an Excel file. Numbers come from code: each chart uses a curated source "
        f"({_SOURCES_HELP}) or a read-only SQL SELECT over the views sales, returns and "
        f"products (max {MAX_SQL_ROWS} rows; name the x column and the numeric y columns). "
        "Dates are YYYY-MM-DD. Write the title and summary in the seller's language. The UI "
        "shows the report automatically.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 3, "maxLength": 140},
                "summary": {
                    "type": "string",
                    "maxLength": 3000,
                    "description": "2-5 sentences: what the charts show. Plain text.",
                },
                "language": {"type": "string", "enum": ["en", "ar"]},
                "charts": {
                    "type": "array",
                    "items": CHART_SCHEMA,
                    "minItems": 1,
                    "maxItems": MAX_CHARTS,
                },
            },
            "required": ["title", "summary", "charts"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    @safe
    async def create_report(args: dict[str, Any]) -> dict:
        info = await build_report(args, ctx, store, session_id=session_id,
                                  reports_dir=reports_dir)  # fmt: skip
        return ok(
            ctx,
            result=info.to_result(),
            notes=["The report is shown to the seller automatically; do not paste the links."],
        )

    tools = [create_report]
    return ToolServer(
        key=SERVER_KEY,
        config=create_sdk_mcp_server(SERVER_KEY, tools=tools),
        tools={t.name: t for t in tools},
    )
