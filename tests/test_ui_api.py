"""Tests for the UI controller (with a fake view) and the FastAPI routes (no Chainlit, no API)."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from scc.agents.hub import HubRuntime
from scc.api.app import create_app
from scc.config import Settings
from scc.permissions import ApprovalBroker
from scc.runtime.events import (
    Finished,
    HubTextDelta,
    SpokeFinished,
    SpokeStarted,
    ToolCalled,
    ToolReturned,
)
from scc.store.app_db import AppStore
from scc.tools._common import ToolContext
from scc.ui import render
from scc.ui.controller import ChatController
from tests.conftest import AS_OF

# --- render helpers -------------------------------------------------------------------------


def test_render_helpers():
    assert render.spoke_label("inventory_planner") == "Inventory planner"
    assert render.tool_label("mcp__inventory__forecast_demand") == "forecast_demand"
    assert render.tool_label("Agent") == "Agent"
    assert render.finished_step_name("sales_analyst", 3.21, False) == "Used Sales analyst · 3.2s"
    assert render.footer(3, 29012, 0.0719) == "3 turns · 29.0s · $0.0719"
    assert render.pretty('{"a":1}') == '{\n  "a": 1\n}'
    assert render.pretty("x" * 10_000).endswith("(truncated)")
    assert "step limit" in render.error_message("error_max_turns")


# --- controller -----------------------------------------------------------------------------


class FakeView:
    def __init__(self, answer=True):
        self.calls = []
        self.answer = answer

    async def stream(self, text):
        self.calls.append(("stream", text))

    async def end_text(self):
        self.calls.append(("end_text",))

    async def step_start(self, key, name, parent, kind, input):
        self.calls.append(("step_start", key, name, parent, kind))

    async def step_end(self, key, output, is_error, name=None):
        self.calls.append(("step_end", key, is_error, name))

    async def footer(self, text):
        self.calls.append(("footer", text))

    async def notice(self, text):
        self.calls.append(("notice", text))

    async def ask_approval(self, text, timeout_s):
        self.calls.append(("ask", text))
        return self.answer


FORECAST = "mcp__inventory__forecast_demand"


async def test_controller_orders_text_and_nests_steps():
    view = FakeView()
    c = ChatController(view, approval_timeout_s=60)
    for ev in [
        HubTextDelta("Let me "),
        HubTextDelta("check."),
        SpokeStarted("a1", "inventory_planner", "Forecast December"),
        ToolCalled("t1", FORECAST, {"top_n": 3}, "inventory_planner", "a1"),
        ToolReturned("t1", FORECAST, '{"rows":[]}', False, "inventory_planner", "a1"),
        SpokeFinished("a1", "inventory_planner", False, "Report"),
        HubTextDelta("Restock."),
        Finished("sdk", "success", False, 3, 1500, 0.01, "Restock."),
    ]:  # fmt: skip
        await c.handle(ev)

    kinds = [call[0] for call in view.calls]
    # Text streams, then the bubble is closed before the first step appears.
    assert kinds[:4] == ["stream", "stream", "end_text", "step_start"]
    assert ("step_start", "a1", "Inventory planner…", None, "agent") in view.calls
    assert ("step_start", "t1", "forecast_demand", "a1", "tool") in view.calls  # nested
    [spoke_end] = [x for x in view.calls if x[0] == "step_end" and x[1] == "a1"]
    assert spoke_end[3].startswith("Used Inventory planner · ")
    assert view.calls[-1] == ("footer", "3 turns · 1.5s · $0.0100")


async def test_controller_reports_error_results():
    view = FakeView()
    await ChatController(view, approval_timeout_s=60).handle(
        Finished("sdk", "error_max_budget_usd", True, 9, 1000, 1.0, None)
    )
    assert ("notice", render.error_message("error_max_budget_usd")) in view.calls


@pytest.mark.parametrize(
    "answer,approved,decided_by",
    [(True, True, "seller"), (False, False, "seller"), (None, False, "timeout")],
)
async def test_controller_approval_flow(tmp_path, answer, approved, decided_by):
    store = AppStore(tmp_path / "app.sqlite")
    broker = ApprovalBroker(store, timeout_s=5)
    view = FakeView(answer)
    controller = ChatController(view, approval_timeout_s=60)
    broker.on_request = controller.approval_handler(broker)

    approval_id, decision = await broker.request("s1", "mcp__actions__x", {"a": 1}, "Create PO")
    assert (decision.approved, decision.decided_by) == (approved, decided_by)
    assert view.calls[0][0] == "ask" and "Create PO" in view.calls[0][1]
    await asyncio.sleep(0)  # let the approval task post its notice
    notices = [x for x in view.calls if x[0] == "notice"]
    assert bool(notices) == (not approved)  # Chainlit itself shows "Selected: Approve"


async def test_controller_denies_if_the_approval_ui_breaks(tmp_path):
    class BrokenView(FakeView):
        async def ask_approval(self, text, timeout_s):
            raise RuntimeError("socket closed")

    broker = ApprovalBroker(AppStore(tmp_path / "app.sqlite"), timeout_s=5)
    broker.on_request = ChatController(BrokenView(), approval_timeout_s=60).approval_handler(broker)
    _, decision = await broker.request("s1", "t", {}, "summary")
    assert decision.approved is False


# --- API ------------------------------------------------------------------------------------


@pytest.fixture
def runtime(retail_db, tmp_path):
    settings = Settings(_env_file=None, data_dir=retail_db.parent)
    return HubRuntime(settings=settings, store=AppStore(tmp_path / "app.sqlite"),
                      tool_ctx=ToolContext(db_path=retail_db, as_of=AS_OF))  # fmt: skip


@pytest.fixture
def client(runtime):
    return TestClient(create_app(runtime, mount_ui=False))


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() | {} == {**r.json(), "status": "ok", "retail_db": True, "app_db": True}


def test_health_degraded_without_retail_db(runtime, tmp_path):
    runtime.settings = Settings(_env_file=None, data_dir=tmp_path / "empty")
    r = TestClient(create_app(runtime, mount_ui=False)).get("/health")
    assert r.status_code == 503 and r.json()["retail_db"] is False


def test_read_only_routes(client, runtime):
    store = runtime.store
    sid = store.create_session()
    store.record_request(sid, "hi", sdk_session_id="sdk-1", subtype="success", is_error=False,
                         num_turns=1, duration_ms=10, cost_usd=0.01, usage=None)  # fmt: skip
    store.audit("tool_ok", session_id=sid, agent="hub", tool_name="x")
    store.add_memory("Focus on Germany")
    aid = store.create_approval(sid, "t", {}, "s")
    store.decide_approval(aid, True, "seller")
    lines = [{"stock_code": "10001", "quantity": 3}]
    store.insert_purchase_order(
        session_id=sid, approval_id=aid, supplier=None, lines=lines, reason="r"
    )

    assert client.get("/api/memory").json()[0]["text"] == "Focus on Germany"
    assert client.get("/api/purchase-orders").json()[0]["total_units"] == 3
    assert client.get("/api/drafts").json() == []
    assert client.get("/api/approvals").json()[0]["status"] == "approved"
    assert client.get("/api/sessions").json()[0]["id"] == sid
    trace = client.get(f"/api/sessions/{sid}/trace").json()
    assert trace["requests"][0]["cost_usd"] == 0.01 and trace["audit"][0]["tool_name"] == "x"
    assert client.get("/api/sessions/nope/trace").status_code == 404
    assert client.get("/api/audit?limit=5000").status_code == 422  # limit is bounded


def test_api_is_read_only(client):
    for path in ("/api/memory", "/api/purchase-orders"):
        assert client.post(path, json={}).status_code == 405
        assert client.delete(path).status_code == 405


def test_api_token(runtime):
    runtime.settings = Settings(
        _env_file=None, data_dir=runtime.settings.data_dir, api_token="s3cret"
    )
    client = TestClient(create_app(runtime, mount_ui=False))
    assert client.get("/api/memory").status_code == 401
    assert client.get("/api/memory", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/memory", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/health").status_code == 200  # health stays open for the container
