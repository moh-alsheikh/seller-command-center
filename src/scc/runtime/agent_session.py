"""One chat = one ``ClaudeSDKClient``. UI-agnostic: used by the CLI and the Chainlit UI.

Why ClaudeSDKClient (and not query()) for chats (workshop lesson 5):
  * multi-turn: follow-up questions ("and what about France?") keep the context,
  * interrupt(): the seller can stop a long answer,
  * can_use_tool needs streaming mode, which the client always uses.
The evals (milestone 6) use query() instead: each eval case is one independent task.

Lifecycle::

    session = AgentSession(runtime, on_approval=show_buttons)
    await session.connect()
    async for event in session.ask("What should I restock for December?"):
        render(event)
    await session.close()

Each ``ask`` holds a slot of ``runtime.limiter``, a process-wide cap on concurrent requests.
Every request runs a CLI subprocess plus sub-agents, so unlimited concurrency would exhaust
memory and the API rate limit. When the request ends, its ResultMessage (cost, turns,
duration, SDK session id) is stored, so ``scc chat --resume <id>`` can continue the chat
later with ``resume=``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import ClaudeSDKClient

from scc.agents.hub import HubRuntime, build_options
from scc.hooks import TraceEvent
from scc.permissions import ApprovalBroker, OnRequest, ToolPolicy
from scc.runtime.events import Event, EventTranslator, Finished

log = logging.getLogger(__name__)

_limiters: dict[int, asyncio.Semaphore] = {}


def request_limiter(max_concurrent: int) -> asyncio.Semaphore:
    """One semaphore per event loop (asyncio primitives must not cross loops)."""
    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _limiters:
        _limiters[loop_id] = asyncio.Semaphore(max_concurrent)
    return _limiters[loop_id]


class AgentSession:
    def __init__(
        self,
        runtime: HubRuntime,
        *,
        session_id: str | None = None,
        resume: str | None = None,
        on_approval: OnRequest | Callable[[ApprovalBroker], OnRequest] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        stream_partial: bool = False,
        model: str | None = None,
        client_factory: Callable[..., Any] = ClaudeSDKClient,
    ):
        """
        session_id: continue one of our sessions (its SDK session is resumed automatically).
        resume: an SDK session id to resume directly (used when session_id is unknown).
        on_approval: async callback(ApprovalRequest), or a factory taking the broker (for
                     policies that need to call broker.resolve, e.g. auto-deny in evals).
        """
        self.runtime = runtime
        store = runtime.store
        existing = store.get_session(session_id) if session_id else None
        self.session_id = existing["id"] if existing else store.create_session()
        self._resume = resume or (existing or {}).get("sdk_session_id")
        self.broker = ApprovalBroker(store, timeout_s=runtime.settings.approval_timeout_s)
        if on_approval is not None:
            # A factory receives the broker; a plain callback is used as-is.
            maybe = on_approval(self.broker) if _is_factory(on_approval) else on_approval
            self.broker.on_request = maybe
        self._on_trace = on_trace
        self._stream_partial = stream_partial
        self._model = model
        self._client_factory = client_factory
        self._client: Any = None
        self.policy: ToolPolicy | None = None
        self.last_result: Finished | None = None

    async def connect(self) -> None:
        options, self.policy = build_options(
            self.runtime,
            session_id=self.session_id,
            broker=self.broker,
            resume=self._resume,
            on_trace=self._on_trace,
            stream_partial=self._stream_partial,
            model=self._model,
        )
        self._client = self._client_factory(options=options)
        await self._client.connect()

    async def ask(self, prompt: str) -> AsyncIterator[Event]:
        if self._client is None:
            raise RuntimeError("Call connect() before ask().")
        translator = EventTranslator(stream_partial=self._stream_partial)
        async with request_limiter(self.runtime.settings.max_concurrent_requests):
            await self._client.query(prompt)
            async for msg in self._client.receive_response():
                for event in translator.translate(msg):
                    if isinstance(event, Finished):
                        self._record(prompt, event)
                    yield event

    def _record(self, prompt: str, result: Finished) -> None:
        self.last_result = result
        self._resume = result.sdk_session_id or self._resume
        try:
            self.runtime.store.record_request(
                self.session_id,
                prompt,
                sdk_session_id=result.sdk_session_id,
                subtype=result.subtype,
                is_error=result.is_error,
                num_turns=result.num_turns,
                duration_ms=result.duration_ms,
                cost_usd=result.cost_usd,
                usage=result.usage,
            )
        except Exception:
            log.exception("Could not record request result")

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None

    async def __aenter__(self) -> AgentSession:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


def _is_factory(fn: Callable) -> bool:
    """A factory takes the broker and returns the callback; a callback is a coroutine function."""
    return not inspect.iscoroutinefunction(fn)
