"""SDK hooks: policy checks before tools run, and observability after.

Hook callback signature (workshop lesson 3): ``async def hook(input_data, tool_use_id, context)
-> dict``. An empty dict means "no opinion, carry on".

``build_hooks`` assembles everything into the ``hooks=`` mapping for ClaudeAgentOptions:

    PreToolUse          hub_has_no_data_tools  (design rule 1, enforced)
                        sql_guard              (design rule 5, layer 2) on run_sql only
                        audit_start            (remember start times for durations)
    PostToolUse         audit_ok
    PostToolUseFailure  audit_error
    SubagentStart/Stop  tracing                (which spoke ran, for the UI and /trace)
    UserPromptSubmit    memory_inject          (seller preferences on every turn)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from claude_agent_sdk import HookMatcher

from scc.hooks.audit import AuditHooks, TraceEvent
from scc.hooks.guards import make_hub_guard, make_sql_guard
from scc.hooks.memory_inject import make_memory_inject
from scc.store.app_db import AppStore

__all__ = ["TraceEvent", "build_hooks"]


def build_hooks(
    *,
    store: AppStore,
    session_id: str | None,
    spoke_tools: Iterable[str],
    sql_tool: str,
    max_rows: int,
    on_trace: Callable[[TraceEvent], None] | None = None,
) -> dict[str, list[HookMatcher]]:
    audit = AuditHooks(store=store, session_id=session_id, on_trace=on_trace)
    return {
        "PreToolUse": [
            # No matcher = runs for every tool. Order matters: the guards decide first.
            HookMatcher(hooks=[make_hub_guard(frozenset(spoke_tools), audit)]),
            # The matcher is matched against the tool name, so this runs for run_sql only.
            HookMatcher(matcher=sql_tool, hooks=[make_sql_guard(max_rows, audit)]),
            HookMatcher(hooks=[audit.pre_tool_use]),
        ],
        "PostToolUse": [HookMatcher(hooks=[audit.post_tool_use])],
        "PostToolUseFailure": [HookMatcher(hooks=[audit.post_tool_use_failure])],
        "SubagentStart": [HookMatcher(hooks=[audit.subagent_start])],
        "SubagentStop": [HookMatcher(hooks=[audit.subagent_stop])],
        "UserPromptSubmit": [HookMatcher(hooks=[make_memory_inject(store)])],
    }
