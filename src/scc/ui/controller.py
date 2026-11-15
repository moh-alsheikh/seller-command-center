"""ChatController: turns runtime events into UI actions, independent of the UI library.

    AgentSession.ask() --events--> ChatController.handle() --calls--> ChatView (Chainlit)

The controller decides *what* to show. For example: start a step when a specialist starts,
nest its tool calls under that step, rename it "Used Inventory planner · 3.2s" when it
finishes, and end the current answer bubble before a step appears so the order on screen
matches what happened. The view decides *how* to show it. Keeping that split means all the
logic is unit-tested with a fake view, and the Chainlit file stays a thin adapter.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from scc.permissions import ApprovalBroker, ApprovalRequest, OnRequest
from scc.runtime.events import (
    Event,
    Finished,
    HubText,
    HubTextDelta,
    SpokeFinished,
    SpokeStarted,
    SpokeText,
    ToolCalled,
    ToolReturned,
)
from scc.ui import render

log = logging.getLogger(__name__)


class ChatView(Protocol):
    """What a chat UI must be able to do. Chainlit implements it in chainlit_app.py."""

    async def stream(self, text: str) -> None:
        """Append text to the current answer bubble (create it if needed)."""

    async def end_text(self) -> None:
        """Finish the current answer bubble, if any."""

    async def step_start(
        self, key: str, name: str, parent: str | None, kind: str, input: str
    ) -> None:
        """Show a collapsible step. kind: 'agent' (a specialist) or 'tool'."""

    async def step_end(
        self, key: str, output: str, is_error: bool, name: str | None = None
    ) -> None:
        """Fill in a step's output, mark errors, optionally rename it."""

    async def footer(self, text: str) -> None:
        """Small print under the last answer (turns, time, cost)."""

    async def notice(self, text: str) -> None:
        """A standalone message (errors, approval outcomes)."""

    async def ask_approval(self, text: str, timeout_s: float) -> bool | None:
        """Show Approve/Deny buttons. True/False, or None if nobody clicked in time."""


class ChatController:
    def __init__(self, view: ChatView, *, approval_timeout_s: float):
        self.view = view
        self.approval_timeout_s = approval_timeout_s
        self._spoke_started: dict[str, float] = {}
        self._approval_tasks: set[asyncio.Task] = set()

    # --- events from AgentSession.ask() --------------------------------------------------

    async def handle(self, event: Event) -> None:
        v = self.view
        if isinstance(event, HubTextDelta | HubText):
            await v.stream(event.text)
            return

        # Anything other than hub text closes the current answer bubble first, so steps
        # appear between bubbles in the order things happened.
        await v.end_text()

        if isinstance(event, SpokeStarted):
            self._spoke_started[event.tool_use_id] = time.monotonic()
            await v.step_start(
                event.tool_use_id,
                f"{render.spoke_label(event.agent)}…",
                None,
                "agent",
                event.task,
            )
        elif isinstance(event, ToolCalled):
            await v.step_start(
                event.tool_use_id,
                render.tool_label(event.name),
                event.parent_id,
                "tool",
                render.pretty(event.input),
            )
        elif isinstance(event, ToolReturned):
            await v.step_end(event.tool_use_id, render.pretty(event.output), event.is_error)
        elif isinstance(event, SpokeText):
            pass  # the spoke's report also arrives in SpokeFinished; show it once, there
        elif isinstance(event, SpokeFinished):
            started = self._spoke_started.pop(event.tool_use_id, time.monotonic())
            name = render.finished_step_name(
                event.agent, time.monotonic() - started, event.is_error
            )
            await v.step_end(event.tool_use_id, event.report, event.is_error, name)
        elif isinstance(event, Finished):
            if event.is_error:
                await v.notice(render.error_message(event.subtype))
            await v.footer(render.footer(event.num_turns, event.duration_ms, event.cost_usd))

    # --- approvals ------------------------------------------------------------------------

    def approval_handler(self, broker: ApprovalBroker) -> OnRequest:
        """Factory for AgentSession(on_approval=...): it receives the session's broker.

        ``on_request`` is called from the SDK's permission callback and must return quickly,
        so the buttons are shown in a separate task that resolves the broker on click.
        """

        async def on_request(req: ApprovalRequest) -> None:
            task = asyncio.create_task(self._ask(broker, req))
            self._approval_tasks.add(task)  # keep a reference so it isn't garbage-collected
            task.add_done_callback(self._approval_tasks.discard)

        return on_request

    async def _ask(self, broker: ApprovalBroker, req: ApprovalRequest) -> None:
        try:
            # A little less than the broker's own timeout, so the UI answers first.
            answer = await self.view.ask_approval(
                render.approval_text(req.summary), max(self.approval_timeout_s - 5, 5)
            )
        except Exception:
            log.exception("Approval UI failed; denying")
            answer = None
        if answer is None:
            broker.resolve(req.id, approved=False, decided_by="timeout")
            await self.view.notice("⏱️ No answer in time, so the action was **not** taken.")
        else:
            broker.resolve(req.id, approved=answer, decided_by="seller")
            if not answer:
                await self.view.notice("Declined. Nothing was changed.")
