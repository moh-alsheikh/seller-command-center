"""Tests for the event translator and AgentSession, with a fake SDK client (no API calls)."""

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from scc.agents.hub import HubRuntime
from scc.config import Settings
from scc.runtime.agent_session import AgentSession
from scc.runtime.events import (
    EventTranslator,
    Finished,
    HubText,
    HubTextDelta,
    SpokeFinished,
    SpokeStarted,
    SpokeText,
    ToolCalled,
    ToolReturned,
)
from scc.store.app_db import AppStore
from scc.tools._common import ToolContext
from tests.conftest import AS_OF

MODEL = "claude-test"


def conversation():
    """A realistic hub -> spoke -> tool -> spoke done -> hub answer sequence."""
    return [
        AssistantMessage(
            content=[
                TextBlock("Let me check."),
                ToolUseBlock(
                    "agent-1",
                    "Agent",
                    {
                        "subagent_type": "inventory_planner",
                        "description": "Forecast December",
                        "prompt": "...",
                    },
                ),
            ],
            model=MODEL,
        ),  # fmt: skip
        AssistantMessage(
            content=[ToolUseBlock("tool-1", "mcp__inventory__forecast_demand", {"top_n": 3})],
            model=MODEL,
            parent_tool_use_id="agent-1",
        ),
        UserMessage(
            content=[ToolResultBlock("tool-1", [{"type": "text", "text": '{"rows":[]}'}], False)],
            parent_tool_use_id="agent-1",
        ),
        AssistantMessage(
            content=[TextBlock("**Answer:** ...")], model=MODEL, parent_tool_use_id="agent-1"
        ),
        UserMessage(content=[ToolResultBlock("agent-1", "Report: restock 23084", False)]),
        AssistantMessage(content=[TextBlock("Restock the Rabbit Night Light.")], model=MODEL),
        ResultMessage(
            subtype="success",
            duration_ms=1500,
            duration_api_ms=1200,
            is_error=False,
            num_turns=3,
            session_id="sdk-abc",
            total_cost_usd=0.0123,
            result="Restock ...",
        ),  # fmt: skip
    ]


def test_translator_nests_spoke_activity():
    t = EventTranslator()
    events = [e for msg in conversation() for e in t.translate(msg)]
    assert events == [
        HubText("Let me check."),
        SpokeStarted("agent-1", "inventory_planner", "Forecast December"),
        ToolCalled("tool-1", "mcp__inventory__forecast_demand", {"top_n": 3},
                   "inventory_planner", "agent-1"),
        ToolReturned("tool-1", "mcp__inventory__forecast_demand", '{"rows":[]}', False,
                     "inventory_planner", "agent-1"),
        SpokeText("agent-1", "inventory_planner", "**Answer:** ..."),
        SpokeFinished("agent-1", "inventory_planner", False, "Report: restock 23084"),
        HubText("Restock the Rabbit Night Light."),
        Finished("sdk-abc", "success", False, 3, 1500, 0.0123, "Restock ...", None),
    ]  # fmt: skip


def test_translator_streams_only_hub_deltas():
    t = EventTranslator(stream_partial=True)
    delta = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}
    assert t.translate(StreamEvent("u1", "s", delta)) == [HubTextDelta("Hel")]
    assert t.translate(StreamEvent("u2", "s", delta, parent_tool_use_id="agent-1")) == []
    # With streaming on, complete hub text blocks are not repeated.
    assert t.translate(AssistantMessage(content=[TextBlock("Hello")], model=MODEL)) == []


class FakeClient:
    """Stands in for ClaudeSDKClient: records calls and replays a canned conversation."""

    instances: list["FakeClient"] = []

    def __init__(self, options):
        self.options = options
        self.prompts: list[str] = []
        self.connected = False
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        for msg in conversation():
            yield msg

    async def interrupt(self):
        pass

    async def disconnect(self):
        self.connected = False


@pytest.fixture
def runtime(retail_db, tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    return HubRuntime(
        settings=settings,
        store=AppStore(tmp_path / "app.sqlite"),
        tool_ctx=ToolContext(db_path=retail_db, as_of=AS_OF),
    )


async def test_agent_session_records_result_and_resumes(runtime):
    FakeClient.instances.clear()
    async with AgentSession(runtime, client_factory=FakeClient) as session:
        events = [e async for e in session.ask("What should I restock?")]
        sid = session.session_id
    assert isinstance(events[-1], Finished)
    assert FakeClient.instances[0].connected is False  # closed by the context manager
    assert FakeClient.instances[0].options.resume is None

    [req] = runtime.store.list_requests(sid)
    assert (req["sdk_session_id"], req["cost_usd"], req["num_turns"]) == ("sdk-abc", 0.0123, 3)

    # Re-opening the same session resumes the SDK conversation.
    async with AgentSession(runtime, session_id=sid, client_factory=FakeClient):
        pass
    assert FakeClient.instances[1].options.resume == "sdk-abc"


async def test_ask_requires_connect(runtime):
    session = AgentSession(runtime, client_factory=FakeClient)
    with pytest.raises(RuntimeError, match="connect"):
        async for _ in session.ask("hi"):
            pass
