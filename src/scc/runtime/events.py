"""Translate SDK messages into a small set of neutral events that any UI can render.

The SDK stream mixes hub messages and sub-agent messages. Sub-agent messages carry
``parent_tool_use_id``: the id of the hub's ``Agent`` tool call that started that spoke
(checked live with scripts/check_sdk.py). Grouping by that id is how the UI nests each
spoke's tool calls under the right "Used inventory_planner" step.

Events:
    HubTextDelta   a streamed chunk of the hub's answer (when partial streaming is on)
    HubText        a complete text block from the hub (when partial streaming is off)
    SpokeStarted   the hub started a specialist (Agent tool call)
    SpokeText      a specialist's own text (its report), if forwarded
    ToolCalled     a tool call by the hub or a specialist
    ToolReturned   that tool's result (preview), including errors and denials
    SpokeFinished  the specialist's Agent call returned
    Finished       the request ended (from ResultMessage): cost, turns, duration, ids

The Chainlit UI, the CLI and the evals all consume these, so none of them deals with raw SDK
message types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from scc.permissions import LAUNCHER_TOOLS


@dataclass(frozen=True)
class HubTextDelta:
    text: str


@dataclass(frozen=True)
class HubText:
    text: str


@dataclass(frozen=True)
class SpokeStarted:
    tool_use_id: str
    agent: str
    task: str


@dataclass(frozen=True)
class SpokeText:
    tool_use_id: str
    agent: str
    text: str


@dataclass(frozen=True)
class ToolCalled:
    tool_use_id: str
    name: str
    input: dict[str, Any]
    agent: str  # "hub" or the spoke name
    parent_id: str | None  # the spoke's Agent tool_use_id, None for hub calls


@dataclass(frozen=True)
class ToolReturned:
    tool_use_id: str
    name: str
    output: str  # text preview
    is_error: bool
    agent: str
    parent_id: str | None


@dataclass(frozen=True)
class SpokeFinished:
    tool_use_id: str
    agent: str
    is_error: bool
    report: str


@dataclass(frozen=True)
class Finished:
    sdk_session_id: str | None
    subtype: str
    is_error: bool
    num_turns: int
    duration_ms: int
    cost_usd: float | None
    result: str | None
    usage: dict[str, Any] | None = None


Event = (
    HubTextDelta | HubText | SpokeStarted | SpokeText | ToolCalled | ToolReturned | SpokeFinished
    | Finished
)  # fmt: skip


def result_text(content: Any, limit: int = 2000) -> str:
    """Flatten a ToolResultBlock's content (str or list of blocks) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    else:
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        text = "\n".join(parts)
    return text if len(text) <= limit else text[:limit] + "..."


@dataclass
class EventTranslator:
    """Stateful: remembers which tool_use_id belongs to which spoke and tool."""

    stream_partial: bool = False
    spokes: dict[str, str] = field(default_factory=dict)  # Agent tool_use_id -> spoke name
    tool_names: dict[str, str] = field(default_factory=dict)  # tool_use_id -> tool name

    def _agent_for(self, parent_id: str | None) -> str:
        return self.spokes.get(parent_id, "unknown") if parent_id else "hub"

    def translate(self, msg: Any) -> list[Event]:
        if isinstance(msg, StreamEvent):
            return self._stream(msg)
        if isinstance(msg, AssistantMessage):
            return self._assistant(msg)
        if isinstance(msg, UserMessage):
            return self._user(msg)
        if isinstance(msg, ResultMessage):
            return [
                Finished(
                    sdk_session_id=msg.session_id,
                    subtype=msg.subtype,
                    is_error=msg.is_error,
                    num_turns=msg.num_turns,
                    duration_ms=msg.duration_ms,
                    cost_usd=msg.total_cost_usd,
                    result=msg.result,
                    usage=msg.usage,
                )
            ]
        return []  # SystemMessage, rate-limit events, ...: not needed by the UIs

    def _stream(self, msg: StreamEvent) -> list[Event]:
        if not self.stream_partial or msg.parent_tool_use_id:
            return []  # only the hub's own answer is streamed token by token
        ev = msg.event or {}
        delta = ev.get("delta") or {}
        if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            return [HubTextDelta(delta.get("text", ""))]
        return []

    def _assistant(self, msg: AssistantMessage) -> list[Event]:
        parent = msg.parent_tool_use_id
        agent = self._agent_for(parent)
        out: list[Event] = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                if parent:
                    out.append(SpokeText(parent, agent, block.text))
                elif not self.stream_partial:
                    out.append(HubText(block.text))
            elif isinstance(block, ToolUseBlock):
                self.tool_names[block.id] = block.name
                if block.name in LAUNCHER_TOOLS and not parent:
                    spoke = str(block.input.get("subagent_type", "unknown"))
                    self.spokes[block.id] = spoke
                    task = str(block.input.get("description") or block.input.get("prompt") or "")
                    out.append(SpokeStarted(block.id, spoke, task))
                else:
                    out.append(ToolCalled(block.id, block.name, dict(block.input), agent, parent))
        return out

    def _user(self, msg: UserMessage) -> list[Event]:
        if not isinstance(msg.content, list):
            return []
        parent = msg.parent_tool_use_id
        out: list[Event] = []
        for block in msg.content:
            if not isinstance(block, ToolResultBlock):
                continue
            text = result_text(block.content)
            is_error = bool(block.is_error)
            if block.tool_use_id in self.spokes:
                out.append(
                    SpokeFinished(block.tool_use_id, self.spokes[block.tool_use_id], is_error, text)
                )
            else:
                name = self.tool_names.get(block.tool_use_id, "unknown")
                out.append(
                    ToolReturned(
                        block.tool_use_id, name, text, is_error, self._agent_for(parent), parent
                    )
                )
        return out
