"""PreToolUse guards: deny a tool call before it runs.

Deny output format (workshop lesson 3):

    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": "..."}}

The reason is shown to the model, so it is written as an instruction it can follow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scc.data.sql_guard import UnsafeSQLError, guard_sql

if TYPE_CHECKING:
    from scc.hooks.audit import AuditHooks

Hook = Any  # async (input_data, tool_use_id, context) -> dict


def deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def make_hub_guard(spoke_tools: frozenset[str], audit: AuditHooks) -> Hook:
    """Design rule 1: the hub has no data tools.

    Why a hook and not ``disallowed_tools``? scripts/check_sdk.py showed that tools disallowed
    on the main session are removed from sub-agents too, so the spokes broke. Hooks receive
    ``agent_id`` only when a sub-agent makes the call, so "no agent_id + data tool" means the
    hub tried to query data itself.
    """

    async def hub_guard(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        if input_data.get("tool_name") in spoke_tools and not input_data.get("agent_id"):
            reason = "You have no data tools. Delegate to the right specialist with the Agent tool."
            audit.record_denial(input_data, tool_use_id, f"hub_guard: {reason}")
            return deny(reason)
        return {}

    return hub_guard


def make_sql_guard(max_rows: int, audit: AuditHooks) -> Hook:
    """Design rule 5, layer 2: only a single read-only SELECT reaches run_sql.

    The run_sql tool runs the same check itself. The hook adds the policy record: a denied
    query appears in the audit log as tool_denied, and the tool never starts.
    """

    async def sql_guard(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        sql = (input_data.get("tool_input") or {}).get("sql", "")
        try:
            guard_sql(sql, max_rows=max_rows)
        except UnsafeSQLError as err:
            audit.record_denial(input_data, tool_use_id, f"sql_guard: {err}")
            return deny(f"Query rejected: {err}")
        return {}

    return sql_guard
