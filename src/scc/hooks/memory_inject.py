"""UserPromptSubmit hook: add the seller's saved preferences to every prompt.

Output format (workshop lesson 3)::

    {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "..."}}

The context is added for that turn only and is read fresh from SQLite each time, so a
preference saved (or forgotten) in one chat applies to the next message in every chat.
"""

from __future__ import annotations

from typing import Any

from scc.store.app_db import AppStore
from scc.tools.memory import render_memories


def make_memory_inject(store: AppStore):
    async def memory_inject(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        block = render_memories(store)
        if block is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": block,
            }
        }

    return memory_inject
