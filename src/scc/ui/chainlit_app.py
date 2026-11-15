"""Chainlit seller UI: a thin adapter between Chainlit and ChatController / AgentSession.

Run on its own (development, auto-reload):   uv run chainlit run src/scc/ui/chainlit_app.py -w
Run inside FastAPI (one process, one port):  uv run uvicorn scc.api.app:create_app --factory

Chat lifecycle:
    on_chat_start  build the controller and session (no CLI process yet: connect lazily)
    on_message     connect on the first message, then stream events into the controller
    on_chat_resume the seller reopened a saved chat from the sidebar: reattach our session,
                   so the agent continues the same SDK conversation (resume=)
    on_stop        the seller pressed Stop: interrupt the running request
    on_chat_end    close the SDK client (stops the CLI subprocess)

Chat history (login + data layer) is explained in scc/ui/persistence.py.

Approvals: can_use_tool runs in the SDK's own task, not in the Chainlit handler. The view
therefore re-enters the chat's Chainlit context explicitly (init_ws_context) before showing
the Approve/Deny buttons, rather than relying on context variables being inherited.
"""

from __future__ import annotations

import hmac
import logging

import chainlit as cl
from chainlit.context import init_ws_context
from chainlit.utils import utc_now

from scc.agents.hub import get_runtime
from scc.runtime.agent_session import AgentSession
from scc.ui import persistence
from scc.ui.controller import ChatController

log = logging.getLogger(__name__)
runtime = get_runtime()  # process-wide: settings, stores, read-only tool servers


class ChainlitView:
    """ChatView implemented with Chainlit messages and steps."""

    def __init__(self, session):
        self.session = session  # the Chainlit websocket session of this chat
        self.message: cl.Message | None = None  # the answer bubble being streamed
        self.last_message: cl.Message | None = None
        self.steps: dict[str, cl.Step] = {}

    async def stream(self, text: str) -> None:
        if self.message is None:
            self.message = cl.Message(content="")
        await self.message.stream_token(text)

    async def end_text(self) -> None:
        if self.message is not None:
            await self.message.update()
            self.last_message, self.message = self.message, None

    async def step_start(self, key, name, parent, kind, input) -> None:
        parent_step = self.steps.get(parent) if parent else None
        step = cl.Step(
            name=name,
            type="tool" if kind == "tool" else "run",
            parent_id=parent_step.id if parent_step else None,
            language="json" if kind == "tool" else None,
            show_input="json" if kind == "tool" else False,
        )
        step.input = input
        step.start = utc_now()
        await step.send()
        self.steps[key] = step

    async def step_end(self, key, output, is_error, name=None) -> None:
        step = self.steps.get(key)
        if step is None:
            return
        step.output = output
        step.is_error = is_error
        if name:
            step.name = name
        step.end = utc_now()
        await step.update()

    async def footer(self, text: str) -> None:
        await self.end_text()
        if self.last_message is not None:
            # Markdown, not HTML: unsafe_allow_html stays off, so tags would show as text.
            self.last_message.content += f"\n\n*{text}*"
            await self.last_message.update()

    async def notice(self, text: str) -> None:
        init_ws_context(self.session)
        await cl.Message(content=text).send()

    async def ask_approval(self, text: str, timeout_s: float) -> bool | None:
        init_ws_context(self.session)  # we may be running in the SDK's task
        # The summary goes in its own message: after a click, AskActionMessage replaces its
        # content with "Selected: ...", and the record of WHAT was approved must stay visible.
        await cl.Message(content=text).send()
        res = await cl.AskActionMessage(
            content="Do you approve this action?",
            actions=[
                cl.Action(name="approve", payload={"decision": "approve"}, label="✅ Approve"),
                cl.Action(name="deny", payload={"decision": "deny"}, label="❌ Deny"),
            ],
            timeout=int(timeout_s),
        ).send()
        if res is None:
            return None
        return res.get("payload", {}).get("decision") == "approve"


# --- login + chat history (both on, or both off) --------------------------------------------

if runtime.settings.login_enabled:
    persistence.ensure_auth_secret(runtime.settings.data_dir)

    @cl.password_auth_callback
    def auth(username: str, password: str) -> cl.User | None:
        s = runtime.settings
        ok_user = hmac.compare_digest(username, s.app_user or "")
        ok_pass = hmac.compare_digest(password, s.app_password.get_secret_value())
        return cl.User(identifier=username) if (ok_user and ok_pass) else None

    @cl.data_layer
    def data_layer():
        from chainlit.data.sql_alchemy import SQLAlchemyDataLayer

        db = runtime.settings.chainlit_db_path
        persistence.ensure_schema(db)
        return SQLAlchemyDataLayer(conninfo=persistence.conninfo(db))


# --- chat handlers ---------------------------------------------------------------------------


@cl.set_starters
async def starters(user=None, language=None) -> list[cl.Starter]:
    return [
        cl.Starter(
            label="Revenue trend by country",
            message="How has net revenue developed by month in 2011 for our top 5 countries?",
        ),
        cl.Starter(
            label="What should I restock for December?",
            message="What should I restock for the rest of December, and roughly how many units?",
        ),
        cl.Starter(
            label="Who are my best customers?",
            message="Who are my best customers, and which valuable customers are at risk?",
        ),
        cl.Starter(
            label="Write a listing for my top product",
            message="Write a listing for my best-selling product of 2011.",
        ),
    ]


def _chat_state(session_id: str | None = None) -> tuple[ChatController, AgentSession]:
    """This chat's controller and session, created on first use.

    Normally on_chat_start creates them, but on_chat_start runs as its own task: a client
    that sends a message right after connecting (or reconnects) can reach on_message first.
    The headless UI test (scripts/ui_e2e.py) found this race, so both handlers use this.

    ``session_id``: reattach one of our existing sessions (when a saved chat is reopened).
    """
    controller: ChatController | None = cl.user_session.get("controller")
    session: AgentSession | None = cl.user_session.get("session")
    if controller is None or session is None:
        controller = ChatController(
            ChainlitView(cl.context.session),
            approval_timeout_s=runtime.settings.approval_timeout_s,
        )
        session = AgentSession(
            runtime,
            session_id=session_id,
            on_approval=controller.approval_handler,
            stream_partial=True,
        )
        cl.user_session.set("controller", controller)
        cl.user_session.set("session", session)
        cl.user_session.set("connected", False)
        # Link this Chainlit thread to our session, so the chat can be resumed later.
        thread_id = cl.context.session.thread_id
        if thread_id:
            runtime.store.link_thread(session.session_id, thread_id)
    return controller, session


@cl.on_chat_start
async def on_chat_start() -> None:
    _chat_state()  # no CLI process yet: it starts with the first message


@cl.on_chat_resume
async def on_chat_resume(thread: dict) -> None:
    """A saved chat was reopened from the sidebar.

    Chainlit has already restored the messages and steps on screen. We reattach our session
    for this thread; its SDK session id makes the next question continue the same
    conversation (resume=), so the agent remembers what was said before.
    """
    existing = runtime.store.session_for_thread(thread["id"])
    _chat_state(session_id=existing["id"] if existing else None)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    controller, session = _chat_state()
    try:
        if not cl.user_session.get("connected"):
            await session.connect()  # starts the CLI subprocess on first use
            cl.user_session.set("connected", True)
        async for event in session.ask(message.content):
            await controller.handle(event)
        await controller.view.end_text()
    except Exception:
        log.exception("Request failed")
        await controller.view.end_text()
        await cl.Message(content="Sorry, something went wrong on my side. Please try again.").send()
        # Start from a clean client next time, but keep the conversation (resume).
        await session.close()
        cl.user_session.set("connected", False)


@cl.on_stop
async def on_stop() -> None:
    """Stop button. Chainlit cancels on_message mid-stream, which can leave the SDK client
    half-way through a response. So: interrupt, then close; the next message reconnects
    with resume= (the conversation so far is kept)."""
    session: AgentSession | None = cl.user_session.get("session")
    if session is not None:
        try:
            await session.interrupt()
        finally:
            await session.close()
            cl.user_session.set("connected", False)


@cl.on_chat_end
async def on_chat_end() -> None:
    session: AgentSession | None = cl.user_session.get("session")
    if session is not None:
        await session.close()
