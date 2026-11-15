"""The hub's write tools: save a listing draft, create a purchase order. Approval-gated.

Two independent checks protect every write:
  1. ``can_use_tool`` (scc.permissions.ToolPolicy) asks the seller before the call runs.
  2. The handler below runs only with a valid ``approval_id``: one approved by the seller,
     unused, and issued for this tool with exactly this input (checked by hash).
     ``AppStore.consume_approval`` does that atomically.

So even if the permission layer were misconfigured (say, someone added these tools to
allowed_tools), nothing would be written without a recorded human decision.

Nothing leaves the app: a "purchase order" is a draft row for the seller to send to a
supplier. Connecting to a real supplier or shop API is out of scope for version 1.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import SdkMcpTool, ToolAnnotations, tool

from scc.store.app_db import ApprovalError, AppStore
from scc.tools._common import ToolContext, ToolInputError, fail, ok, run_query, safe
from scc.tools.schemas import STOCK_CODE, obj

SERVER_KEY = "actions"
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

APPROVAL_ID = {
    "type": "string",
    "description": "Filled in by the system after the seller approves. Never set it yourself.",
}


def describe_action(tool_name: str, args: dict[str, Any]) -> str:
    """One readable paragraph for the approval prompt. The seller decides based on this."""
    short = tool_name.rsplit("__", 1)[-1]
    if short == "save_listing_draft":
        bullets = "\n".join(f"  - {b}" for b in args.get("bullets", []))
        return (
            f"Save a listing draft for {args.get('stock_code')}:\n"
            f"Title: {args.get('title')}\n{bullets}\n"
            f"Description: {args.get('description', '')[:400]}\n"
            f"Tags: {', '.join(args.get('tags', []))}"
        )
    if short == "create_purchase_order":
        lines = args.get("lines", [])
        total = sum(int(line.get("quantity", 0)) for line in lines)
        detail = "\n".join(f"  - {ln.get('stock_code')} x {ln.get('quantity')}" for ln in lines)
        supplier = args.get("supplier") or "supplier not specified"
        return (
            f"Create a DRAFT purchase order ({supplier}): {len(lines)} line(s), "
            f"{total:,} units in total.\n{detail}\nReason: {args.get('reason', '-')}"
        )
    return f"Run {tool_name} with {args}"


def build_tools(ctx: ToolContext, store: AppStore, session_id: str | None) -> list[SdkMcpTool[Any]]:
    @tool(
        "save_listing_draft",
        "Save a product listing draft for the seller. The seller is asked to approve it first; "
        "if they decline, do not retry. Use the listing writer's draft as the content.",
        obj(
            {
                "stock_code": STOCK_CODE,
                "title": {"type": "string", "minLength": 5, "maxLength": 80},
                "bullets": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 3, "maxLength": 200},
                    "minItems": 3,
                    "maxItems": 7,
                },
                "description": {"type": "string", "minLength": 50, "maxLength": 2000},
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 2, "maxLength": 40},
                    "minItems": 3,
                    "maxItems": 15,
                },
                "approval_id": APPROVAL_ID,
            },
            required=["stock_code", "title", "bullets", "description", "tags"],
        ),
        annotations=WRITE,
    )
    @safe
    async def save_listing_draft(args: dict) -> dict:
        code = str(args["stock_code"]).strip().upper()
        found = await run_query(
            ctx,
            lambda con: con.execute(
                "SELECT 1 FROM products WHERE stock_code = $c", {"c": code}
            ).fetchone(),
        )
        if not found:
            raise ToolInputError(f"No product with stock code {code!r}.")
        try:
            approval = store.consume_approval(
                args.get("approval_id"), f"mcp__{SERVER_KEY}__save_listing_draft", args
            )
        except ApprovalError as err:
            return fail(f"Not saved: {err}")
        draft_id = store.insert_listing_draft(
            session_id=session_id,
            approval_id=approval["id"],
            stock_code=code,
            title=args["title"],
            bullets=list(args["bullets"]),
            description=args["description"],
            tags=list(args["tags"]),
        )
        return ok(ctx, result={"saved": True, "listing_draft_id": draft_id, "stock_code": code})

    @tool(
        "create_purchase_order",
        "Create a DRAFT purchase order (restock request) for the seller to send to a supplier. "
        "The seller is asked to approve it first; if they decline, do not retry. Base the "
        "quantities on the inventory planner's forecast.",
        obj(
            {
                "lines": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 30,
                    "items": {
                        "type": "object",
                        "properties": {
                            "stock_code": STOCK_CODE,
                            "quantity": {"type": "integer", "minimum": 1, "maximum": 100000},
                        },
                        "required": ["stock_code", "quantity"],
                        "additionalProperties": False,
                    },
                },
                "supplier": {"type": "string", "maxLength": 100},
                "reason": {
                    "type": "string",
                    "maxLength": 500,
                    "description": "Why these quantities (cite the forecast figures).",
                },
                "approval_id": APPROVAL_ID,
            },
            required=["lines", "reason"],
        ),
        annotations=WRITE,
    )
    @safe
    async def create_purchase_order(args: dict) -> dict:
        lines = [
            {"stock_code": str(ln["stock_code"]).strip().upper(), "quantity": int(ln["quantity"])}
            for ln in args["lines"]
        ]
        codes = [ln["stock_code"] for ln in lines]
        if len(set(codes)) != len(codes):
            raise ToolInputError("Each stock code may appear only once; combine the quantities.")
        names = dict(
            await run_query(
                ctx,
                lambda con: con.execute(
                    "SELECT stock_code, description FROM products "
                    "WHERE list_contains($c, stock_code)",
                    {"c": codes},
                ).fetchall(),
            )
        )
        unknown = [c for c in codes if c not in names]
        if unknown:
            raise ToolInputError(
                f"Unknown stock codes: {unknown}. Check them with the specialists."
            )
        try:
            approval = store.consume_approval(
                args.get("approval_id"), f"mcp__{SERVER_KEY}__create_purchase_order", args
            )
        except ApprovalError as err:
            return fail(f"Not created: {err}")
        for ln in lines:
            ln["description"] = names[ln["stock_code"]]
        po_id = store.insert_purchase_order(
            session_id=session_id,
            approval_id=approval["id"],
            supplier=args.get("supplier"),
            lines=lines,
            reason=args.get("reason"),
        )
        return ok(
            ctx,
            result={
                "created": True,
                "purchase_order_id": po_id,
                "status": "draft",
                "total_units": sum(ln["quantity"] for ln in lines),
                "lines": lines,
            },
        )

    return [save_listing_draft, create_purchase_order]
