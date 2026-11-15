"""The hub's memory tools: remember and forget the seller's standing preferences.

Preferences ("focus on UK and Germany", "target margin 40%") are stored in SQLite and added
to every new prompt by the memory_inject UserPromptSubmit hook, so they apply in every chat,
not just the one where they were said. Saving one needs no approval: it changes nothing
outside the app, the seller can see the list, and forget_preference undoes it.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import SdkMcpTool, ToolAnnotations, tool

from scc.store.app_db import AppStore
from scc.tools._common import ToolContext, ToolInputError, ok, safe
from scc.tools.schemas import obj

SERVER_KEY = "memory"
MAX_MEMORIES = 50


def build_tools(ctx: ToolContext, store: AppStore, session_id: str | None) -> list[SdkMcpTool[Any]]:
    @tool(
        "remember_preference",
        "Save a standing preference the seller states (markets to focus on, target margin, tone "
        "for listings, suppliers to avoid). Only save what the seller said, in one short "
        "sentence. Do not save figures from the data or one-off requests.",
        obj({"text": {"type": "string", "minLength": 3, "maxLength": 300}}, required=["text"]),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    )
    @safe
    async def remember_preference(args: dict) -> dict:
        text = str(args["text"]).strip()
        existing = store.list_memory()
        if any(m["text"].lower() == text.lower() for m in existing):
            return ok(ctx, result={"saved": False, "reason": "Already remembered."})
        if len(existing) >= MAX_MEMORIES:
            raise ToolInputError("Memory is full. Ask the seller which preference to forget.")
        mem_id = store.add_memory(text, session_id)
        return ok(ctx, result={"saved": True, "id": mem_id, "text": text})

    @tool(
        "forget_preference",
        "Delete a saved preference by its id (ids are shown in <seller_preferences>). Use when "
        "the seller asks to forget or change a preference.",
        obj({"id": {"type": "integer", "minimum": 1}}, required=["id"]),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
    )
    @safe
    async def forget_preference(args: dict) -> dict:
        deleted = store.delete_memory(int(args["id"]))
        if not deleted:
            raise ToolInputError(f"No saved preference with id {args['id']}.")
        return ok(ctx, result={"deleted": True, "id": int(args["id"])})

    return [remember_preference, forget_preference]


def render_memories(store: AppStore) -> str | None:
    """The block the UserPromptSubmit hook adds to each prompt, or None when empty."""
    memories = store.list_memory()
    if not memories:
        return None
    lines = "\n".join(f"- [{m['id']}] {m['text']}" for m in memories)
    return (
        "<seller_preferences>\n"
        "Standing preferences the seller asked you to remember. Apply them unless the "
        "current request says otherwise.\n"
        f"{lines}\n"
        "</seller_preferences>"
    )
