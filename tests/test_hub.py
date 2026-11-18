"""Tests for the hub's write and memory tools, the hooks, and build_options."""

import json

import pytest

from scc.agents.hub import LAUNCHER, HubRuntime, build_options, render_hub_prompt
from scc.config import Settings
from scc.hooks import build_hooks
from scc.permissions import ApprovalBroker
from scc.store.app_db import AppStore
from scc.tools import actions, memory
from scc.tools._common import ToolContext
from tests.conftest import AS_OF

PO = "mcp__actions__create_purchase_order"
LISTING = "mcp__actions__save_listing_draft"


@pytest.fixture
def store(tmp_path):
    return AppStore(tmp_path / "app.sqlite")


@pytest.fixture
def ctx(retail_db):
    return ToolContext(db_path=retail_db, as_of=AS_OF)


@pytest.fixture
def runtime(retail_db, store):
    settings = Settings(_env_file=None, anthropic_api_key="sk-test", data_dir=retail_db.parent)
    return HubRuntime(
        settings=settings, store=store, tool_ctx=ToolContext(db_path=retail_db, as_of=AS_OF)
    )


def approve(store, tool, inp):
    aid = store.create_approval("s1", tool, inp, "summary")
    store.decide_approval(aid, True, "seller")
    return {**inp, "approval_id": aid}


async def run(tool, args):
    result = await tool.handler(args)
    text = result["content"][0]["text"]
    return (text, True) if result.get("is_error") else (json.loads(text), False)


# --- write tools ----------------------------------------------------------------------------


PO_INPUT = {"lines": [{"stock_code": "10001", "quantity": 40}], "reason": "Forecast 7.7/month"}
LISTING_INPUT = {
    "stock_code": "10001",
    "title": "Red Mug",
    "bullets": ["One", "Two", "Three"],
    "description": "A red mug that is one of our best sellers in the UK and Germany. " * 2,
    "tags": ["mug", "red", "kitchen"],
}


async def test_purchase_order_needs_a_valid_approval(ctx, store):
    po = {t.name: t for t in actions.build_tools(ctx, store, "s1")}["create_purchase_order"]

    text, err = await run(po, PO_INPUT)  # no approval token
    assert err and "Not created" in text
    assert store.list_purchase_orders() == []

    data, err = await run(po, approve(store, PO, PO_INPUT))
    assert not err
    assert data["result"]["total_units"] == 40
    [row] = store.list_purchase_orders()
    assert row["lines"] == [{"stock_code": "10001", "quantity": 40, "description": "RED MUG"}]
    assert row["status"] == "draft"


async def test_purchase_order_rejects_changed_input(ctx, store):
    po = {t.name: t for t in actions.build_tools(ctx, store, "s1")}["create_purchase_order"]
    approved = approve(store, PO, PO_INPUT)
    tampered = {**approved, "lines": [{"stock_code": "10001", "quantity": 4000}]}
    text, err = await run(po, tampered)
    assert err and "changed after" in text


async def test_purchase_order_validates_stock_codes_before_using_approval(ctx, store):
    po = {t.name: t for t in actions.build_tools(ctx, store, "s1")}["create_purchase_order"]
    bad = {"lines": [{"stock_code": "NOPE", "quantity": 1}], "reason": "r"}
    approved = approve(store, PO, bad)
    text, err = await run(po, approved)
    assert err and "Unknown stock codes" in text
    assert store.get_approval(approved["approval_id"])["used_at"] is None


async def test_listing_draft_saved_with_approval(ctx, store):
    tool = {t.name: t for t in actions.build_tools(ctx, store, "s1")}["save_listing_draft"]
    data, err = await run(tool, approve(store, LISTING, LISTING_INPUT))
    assert not err and data["result"]["saved"] is True
    [row] = store.list_listing_drafts()
    assert row["title"] == "Red Mug" and row["tags"] == ["mug", "red", "kitchen"]


def test_describe_action_is_readable():
    text = actions.describe_action(PO, PO_INPUT)
    assert "DRAFT purchase order" in text and "40 units" in text and "10001 x 40" in text


# --- memory ---------------------------------------------------------------------------------


async def test_memory_tools_and_rendering(ctx, store):
    tools = {t.name: t for t in memory.build_tools(ctx, store, "s1")}
    data, _ = await run(tools["remember_preference"], {"text": "Focus on UK and Germany"})
    assert data["result"]["saved"] is True
    again, _ = await run(tools["remember_preference"], {"text": "focus on uk and germany"})
    assert again["result"]["saved"] is False  # duplicates are ignored

    block = memory.render_memories(store)
    assert "<seller_preferences>" in block and "[1] Focus on UK and Germany" in block

    await run(tools["forget_preference"], {"id": 1})
    assert memory.render_memories(store) is None
    text, err = await run(tools["forget_preference"], {"id": 1})
    assert err and "No saved preference" in text


# --- hooks ----------------------------------------------------------------------------------


def hooks_for(store, traces=None):
    return build_hooks(
        store=store, session_id="s1", spoke_tools=["mcp__sales__run_sql"],
        sql_tool="mcp__sales__run_sql", max_rows=50,
        on_trace=traces.append if traces is not None else None,
    )  # fmt: skip


async def test_hub_guard_denies_hub_but_not_spokes(store):
    guard = hooks_for(store)["PreToolUse"][0].hooks[0]
    hub_call = {"tool_name": "mcp__sales__run_sql", "tool_input": {"sql": "SELECT 1"}}
    out = await guard(hub_call, "t1", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        await guard({**hub_call, "agent_id": "a1", "agent_type": "sales_analyst"}, "t2", None) == {}
    )
    [row] = [r for r in store.list_audit("s1") if r["event"] == "tool_denied"]
    assert row["agent"] == "hub"


async def test_sql_guard_hook(store):
    matcher = hooks_for(store)["PreToolUse"][1]
    assert matcher.matcher == "mcp__sales__run_sql"
    guard = matcher.hooks[0]
    ok = {"tool_name": "mcp__sales__run_sql", "tool_input": {"sql": "SELECT 1"}, "agent_id": "a"}
    assert await guard(ok, "t1", None) == {}
    bad = {**ok, "tool_input": {"sql": "DROP TABLE sales"}}
    out = (await guard(bad, "t2", None))["hookSpecificOutput"]
    assert (
        out["permissionDecision"] == "deny"
        and "Only read queries" in out["permissionDecisionReason"]
    )


async def test_audit_and_tracing_hooks(store):
    traces = []
    h = hooks_for(store, traces)
    spoke = {"agent_id": "a1", "agent_type": "inventory_planner"}
    await h["SubagentStart"][0].hooks[0]({"hook_event_name": "SubagentStart", **spoke}, None, None)
    call = {"tool_name": "mcp__inventory__forecast_demand", "tool_input": {"top_n": 3}, **spoke}
    await h["PreToolUse"][2].hooks[0](call, "t1", None)
    await h["PostToolUse"][0].hooks[0](
        {**call, "tool_response": {"content": []}, "duration_ms": 42}, "t1", None
    )
    await h["PostToolUseFailure"][0].hooks[0]({**call, "error": "boom"}, "t2", None)
    await h["SubagentStop"][0].hooks[0]({"hook_event_name": "SubagentStop", **spoke}, None, None)

    events = [(r["event"], r["agent"], r["duration_ms"]) for r in store.list_audit("s1")]
    assert events == [
        ("subagent_start", "inventory_planner", None),
        ("tool_start", "inventory_planner", None),
        ("tool_ok", "inventory_planner", 42),
        ("tool_error", "inventory_planner", None),
        ("subagent_stop", "inventory_planner", None),
    ]
    assert [t.event for t in traces] == [e[0] for e in events]


async def test_memory_inject_hook(store):
    inject = hooks_for(store)["UserPromptSubmit"][0].hooks[0]
    assert await inject({"prompt": "hi"}, None, None) == {}
    store.add_memory("Target margin 40%")
    out = (await inject({"prompt": "hi"}, None, None))["hookSpecificOutput"]
    assert out["hookEventName"] == "UserPromptSubmit"
    assert "Target margin 40%" in out["additionalContext"]


# --- build_options --------------------------------------------------------------------------


def test_build_options_wiring(runtime):
    sid = runtime.store.create_session()
    opts, policy = build_options(
        runtime, session_id=sid, broker=ApprovalBroker(runtime.store, timeout_s=5)
    )

    assert opts.tools == [LAUNCHER]  # no Bash/Read/Write/WebFetch
    assert opts.allowed_tools == []  # every call reaches can_use_tool
    assert opts.disallowed_tools == []  # would strip tools from spokes (check_sdk.py)
    assert opts.permission_mode == "default"
    assert opts.setting_sources == [] and opts.strict_mcp_config is True
    assert opts.can_use_tool is policy
    assert opts.max_turns == runtime.settings.max_turns
    assert opts.max_budget_usd == runtime.settings.max_budget_usd
    assert opts.env == {"ANTHROPIC_API_KEY": "sk-test"}
    assert set(opts.mcp_servers) == {"sales", "customers", "inventory", "pricing", "catalog",
                                     "reports", "actions", "memory"}  # fmt: skip
    assert len(opts.agents) == 6
    # Reports read data, so the report tool is a SPOKE tool: denied to the hub (rule 1).
    assert "mcp__reports__create_report" in policy.spoke_tools

    # The write tools are gated; none of the hub's tools are spoke data tools.
    assert policy.gated_tools == {PO, LISTING}
    assert policy.hub_tools == {
        "mcp__memory__remember_preference",
        "mcp__memory__forget_preference",
    }
    assert not (policy.spoke_tools & (policy.gated_tools | policy.hub_tools))
    # No spoke lists a write or memory tool.
    for agent in opts.agents.values():
        assert not set(agent.tools) & (policy.gated_tools | policy.hub_tools)


def test_hub_prompt_states_date_and_rules(runtime):
    text = render_hub_prompt(runtime.settings)
    assert "Friday 9 December 2011" in text
    assert "You have no data tools" in text
    assert "$" not in text
