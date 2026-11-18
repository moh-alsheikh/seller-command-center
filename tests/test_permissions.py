"""Tests for the ToolPolicy (can_use_tool) and the ApprovalBroker."""

import asyncio

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from scc.permissions import ApprovalBroker, ToolPolicy, auto_policy
from scc.store.app_db import AppStore

SPOKE = "mcp__sales__run_sql"
MEMORY = "mcp__memory__remember_preference"
GATED = "mcp__actions__create_purchase_order"


@pytest.fixture
def store(tmp_path):
    return AppStore(tmp_path / "app.sqlite")


def hub_ctx():
    return ToolPermissionContext()


def spoke_ctx():
    return ToolPermissionContext(agent_id="a123")


def make_policy(store, on_request=None, timeout_s=5.0, decisions=None):
    broker = ApprovalBroker(store, timeout_s=timeout_s)
    if on_request is not None:
        broker.on_request = on_request(broker)
    return ToolPolicy.build(
        session_id="s1",
        broker=broker,
        spoke_tools=[SPOKE],
        hub_tools=[MEMORY],
        gated_tools=[GATED],
        describe=lambda tool, inp: f"{tool} {inp}",
        on_decision=(lambda *a: decisions.append(a)) if decisions is not None else None,
    )


@pytest.mark.parametrize(
    "tool,ctx,allowed",
    [
        ("Agent", hub_ctx(), True),
        ("Task", hub_ctx(), True),
        ("Agent", spoke_ctx(), False),  # no nested delegation
        (SPOKE, spoke_ctx(), True),
        (SPOKE, hub_ctx(), False),  # rule 1: the hub has no data tools
        (MEMORY, hub_ctx(), True),
        (MEMORY, spoke_ctx(), False),
        (GATED, spoke_ctx(), False),  # rule 2: spokes never act
        ("Bash", hub_ctx(), False),  # default deny
        ("mcp__evil__exfiltrate", spoke_ctx(), False),
    ],
)
async def test_policy_table(store, tool, ctx, allowed):
    result = await make_policy(store)(tool, {}, ctx)
    assert isinstance(result, PermissionResultAllow if allowed else PermissionResultDeny)


async def test_hub_data_tool_denial_tells_it_to_delegate(store):
    result = await make_policy(store)(SPOKE, {"sql": "SELECT 1"}, hub_ctx())
    assert "Delegate" in result.message


async def test_gated_tool_approved_adds_approval_token(store):
    decisions = []
    policy = make_policy(store, on_request=auto_policy(True, "test"), decisions=decisions)
    inp = {"lines": [{"stock_code": "10001", "quantity": 5}], "reason": "r"}
    result = await policy(GATED, inp, hub_ctx())

    assert isinstance(result, PermissionResultAllow)
    approval_id = result.updated_input["approval_id"]
    assert {k: v for k, v in result.updated_input.items() if k != "approval_id"} == inp
    row = store.get_approval(approval_id)
    assert row["status"] == "approved" and row["decided_by"] == "policy:test"
    assert decisions == [(GATED, "approved", "policy:test")]


async def test_gated_tool_denied(store):
    policy = make_policy(store, on_request=auto_policy(False, "eval"))
    result = await policy(GATED, {"lines": [], "reason": "r"}, hub_ctx())
    assert isinstance(result, PermissionResultDeny)
    assert "declined" in result.message and "Do not retry" in result.message


async def test_no_ui_fails_closed(store):
    result = await make_policy(store)(GATED, {"reason": "r"}, hub_ctx())
    assert isinstance(result, PermissionResultDeny)
    [row] = store.list_approvals()
    assert row["decided_by"] == "policy:no_ui"


async def test_timeout_denies(store):
    async def never_answers(req):
        return None

    policy = make_policy(store, on_request=lambda broker: never_answers, timeout_s=0.05)
    result = await policy(GATED, {"reason": "r"}, hub_ctx())
    assert isinstance(result, PermissionResultDeny)
    assert "did not answer in time" in result.message
    [row] = store.list_approvals()
    assert (row["status"], row["decided_by"]) == ("denied", "timeout")


async def test_broker_resolves_from_another_task(store):
    """The real UI pattern: on_request returns at once, a click resolves later."""
    broker = ApprovalBroker(store, timeout_s=5)

    async def show_buttons(req):
        async def click_later():
            await asyncio.sleep(0.01)
            assert broker.resolve(req.id, approved=True, reason="ok")

        asyncio.create_task(click_later())

    broker.on_request = show_buttons
    approval_id, decision = await broker.request("s1", GATED, {"a": 1}, "summary")
    assert decision.approved and decision.decided_by == "seller"
    assert broker.pending_ids == []
    assert broker.resolve(approval_id, approved=False) is False  # already decided


@pytest.mark.parametrize(
    "extra",
    [
        {"run_in_background": True},  # the hub asked for background
        {},  # the hub left it out: this CLI version then defaults to background
        {"run_in_background": False},
    ],
)
async def test_launcher_always_runs_in_the_foreground(store, extra):
    policy = make_policy(store)
    inp = {"subagent_type": "sales_analyst", "prompt": "p", **extra}
    result = await policy("Agent", inp, hub_ctx())
    assert isinstance(result, PermissionResultAllow)
    # Always written explicitly: an absent field means "background" to the CLI.
    assert result.updated_input == {**inp, "run_in_background": False}
