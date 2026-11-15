"""Pure formatting helpers for the chat UI (no Chainlit imports, easy to test)."""

from __future__ import annotations

import contextlib
import json

SPOKE_LABELS = {
    "sales_analyst": "Sales analyst",
    "customer_insights": "Customer insights",
    "inventory_planner": "Inventory planner",
    "pricing_strategist": "Pricing strategist",
    "listing_writer": "Listing writer",
}

MAX_STEP_CHARS = 6000


def spoke_label(agent: str) -> str:
    return SPOKE_LABELS.get(agent, agent.replace("_", " ").capitalize())


def tool_label(name: str) -> str:
    """mcp__inventory__forecast_demand -> forecast_demand"""
    return name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name


def finished_step_name(agent: str, seconds: float, is_error: bool) -> str:
    verb = "Failed" if is_error else "Used"
    return f"{verb} {spoke_label(agent)} · {seconds:.1f}s"


def pretty(value: object) -> str:
    """Tool input/output for a step: indented JSON when possible, truncated when huge."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    with contextlib.suppress(TypeError, ValueError):  # not JSON: show as plain text
        text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    if len(text) > MAX_STEP_CHARS:
        text = text[:MAX_STEP_CHARS] + "\n... (truncated)"
    return text


def footer(num_turns: int, duration_ms: int, cost_usd: float | None) -> str:
    cost = f"${cost_usd:.4f}" if cost_usd is not None else "cost n/a"
    return f"{num_turns} turns · {duration_ms / 1000:.1f}s · {cost}"


# Messages for requests that ended without a normal answer (ResultMessage.subtype).
ERROR_MESSAGES = {
    "error_max_turns": "I hit the step limit for one request before finishing. "
    "Try a narrower question, or ask me to continue.",
    "error_max_budget_usd": "This request reached its cost limit before finishing. "
    "Try a narrower question.",
    "error_during_execution": "Something went wrong while working on that. Please try again.",
}


def error_message(subtype: str) -> str:
    return ERROR_MESSAGES.get(subtype, f"The request ended early ({subtype}). Please try again.")


def approval_text(summary: str) -> str:
    return f"**Approval needed**\n\n{summary}\n\nNothing happens unless you approve."
