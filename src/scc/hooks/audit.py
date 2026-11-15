"""Audit and tracing hooks: record every tool call and every sub-agent run.

Each record goes to SQLite (audit_log), so a session's full trace can be inspected later
(GET /api/sessions/{id}/trace in milestone 5). It can also be passed live to the UI through
``on_trace``.

Attribution: tool-lifecycle hooks carry ``agent_type`` when a sub-agent made the call (checked
live with scripts/check_sdk.py); without it, the caller is the hub.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from scc.store.app_db import AppStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TraceEvent:
    event: str  # tool_start | tool_ok | tool_error | tool_denied | subagent_start | subagent_stop
    agent: str  # "hub" or the spoke name
    tool_name: str | None = None
    tool_use_id: str | None = None
    agent_id: str | None = None
    duration_ms: int | None = None
    detail: str | None = None


def _agent(input_data: dict) -> str:
    return input_data.get("agent_type") or "hub"


def _summary(value: Any, limit: int = 500) -> str:
    """Short JSON text for the audit log. Tool inputs are small; results can be large."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


@dataclass
class AuditHooks:
    store: AppStore
    session_id: str | None
    on_trace: Callable[[TraceEvent], None] | None = None
    _started: dict[str, float] = field(default_factory=dict)

    def _emit(self, ev: TraceEvent) -> None:
        try:
            self.store.audit(
                ev.event,
                session_id=self.session_id,
                agent=ev.agent,
                agent_id=ev.agent_id,
                tool_name=ev.tool_name,
                tool_use_id=ev.tool_use_id,
                duration_ms=ev.duration_ms,
                detail=ev.detail,
            )
        except Exception:  # auditing must never break the conversation
            log.exception("Failed to write audit record")
        if self.on_trace:
            try:
                self.on_trace(ev)
            except Exception:
                log.exception("on_trace callback failed")

    def record_denial(self, input_data: dict, tool_use_id: str | None, reason: str) -> None:
        self._emit(
            TraceEvent(
                "tool_denied",
                _agent(input_data),
                tool_name=input_data.get("tool_name"),
                tool_use_id=tool_use_id,
                agent_id=input_data.get("agent_id"),
                detail=reason,
            )
        )

    # --- tool lifecycle -------------------------------------------------------------------

    async def pre_tool_use(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        if tool_use_id:
            self._started[tool_use_id] = time.monotonic()
        self._emit(
            TraceEvent(
                "tool_start",
                _agent(input_data),
                tool_name=input_data.get("tool_name"),
                tool_use_id=tool_use_id,
                agent_id=input_data.get("agent_id"),
                detail=_summary(input_data.get("tool_input")),
            )
        )
        return {}

    def _duration(self, input_data: dict, tool_use_id: str | None) -> int | None:
        started = self._started.pop(tool_use_id, None) if tool_use_id else None
        if input_data.get("duration_ms") is not None:
            return int(input_data["duration_ms"])  # the CLI's own timing, when provided
        return int((time.monotonic() - started) * 1000) if started else None

    async def post_tool_use(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        response = input_data.get("tool_response")
        # Our tools report their own errors as results with is_error (never raised).
        is_error = isinstance(response, dict) and bool(response.get("is_error"))
        self._emit(
            TraceEvent(
                "tool_error" if is_error else "tool_ok",
                _agent(input_data),
                tool_name=input_data.get("tool_name"),
                tool_use_id=tool_use_id,
                agent_id=input_data.get("agent_id"),
                duration_ms=self._duration(input_data, tool_use_id),
                detail=_summary(response, 300),
            )
        )
        return {}

    async def post_tool_use_failure(
        self, input_data: dict, tool_use_id: str | None, context: Any
    ) -> dict:
        self._emit(
            TraceEvent(
                "tool_error",
                _agent(input_data),
                tool_name=input_data.get("tool_name"),
                tool_use_id=tool_use_id,
                agent_id=input_data.get("agent_id"),
                duration_ms=self._duration(input_data, tool_use_id),
                detail=str(input_data.get("error"))[:500],
            )
        )
        return {}

    # --- sub-agents -----------------------------------------------------------------------

    async def subagent_start(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        self._emit(
            TraceEvent(
                "subagent_start",
                input_data.get("agent_type") or "unknown",
                agent_id=input_data.get("agent_id"),
            )
        )
        return {}

    async def subagent_stop(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        self._emit(
            TraceEvent(
                "subagent_stop",
                input_data.get("agent_type") or "unknown",
                agent_id=input_data.get("agent_id"),
                detail=(input_data.get("last_assistant_message") or "")[:500] or None,
            )
        )
        return {}
