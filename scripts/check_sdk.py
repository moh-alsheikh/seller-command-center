"""Live checks of the Claude Agent SDK behaviours this project relies on.

Run (costs a few cents; uses Haiku and a tiny probe agent):

    uv run python scripts/check_sdk.py

Why this script exists: the design depends on SDK details that are easy to get wrong from
memory or from older docs. Each check below answers one question with a real run, and the
results are written to data/sdk_checks.json and summarised in DESIGN.md.

  1. launcher_name      Which name starts sub-agents when built-ins are restricted with
                        tools=[...]: "Agent" (newer docs) or "Task" (the workshop)?
  2. disallowed mode    Hub has disallowed_tools=[spoke tool]. Is it hidden from the hub, and
                        can the sub-agent that lists it still call it?
  3. hook_guard mode    No disallowed_tools; a PreToolUse hook denies the spoke tool when the
                        caller is the hub (no agent_id). Is the hub blocked and the spoke not?
  4. can_use_tool       Does can_use_tool fire for the hub's own tools? For sub-agent tools?
  5. hook_payloads      Do SubagentStart/Stop and PreToolUse carry the agent's name?
  6. parent_tool_use_id Do sub-agent messages carry parent_tool_use_id (for UI nesting)?

Findings from the first run (2026-11-15, CLI 2.1.325) are recorded in DESIGN.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    PermissionResultAllow,
    ResultMessage,
    SystemMessage,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from scc.config import get_settings  # noqa: E402

MODEL = "claude-haiku-5-5"


def sdk_env() -> dict[str, str]:
    """Pass the API key from .env to the CLI subprocess (pydantic does not export it)."""
    key = get_settings().anthropic_api_key
    return {"ANTHROPIC_API_KEY": key.get_secret_value()} if key else {}


def base_options(**overrides: Any) -> ClaudeAgentOptions:
    """Isolated options: no user/project settings files, no stray MCP configs."""
    opts = dict(
        model=MODEL,
        system_prompt="You are a test harness. Follow the instructions exactly and briefly.",
        permission_mode="default",
        setting_sources=[],
        strict_mcp_config=True,
        max_turns=8,
        max_budget_usd=0.25,
        env=sdk_env(),
    )
    opts.update(overrides)
    return ClaudeAgentOptions(**opts)


async def init_tools(options: ClaudeAgentOptions) -> dict[str, Any]:
    """Return the session's init data (tools, agents, auth source) without a model call."""
    async for msg in query(prompt="Reply with the single word OK.", options=options):
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            return msg.data
    return {}


# --------------------------------------------------------------------------------------------
# Check 1: which launcher name exists when built-ins are restricted?
# --------------------------------------------------------------------------------------------


async def check_launcher_name() -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name in ("Agent", "Task"):
        data = await init_tools(base_options(tools=[name], max_turns=1))
        found[name] = {
            "in_init_tools": name in data.get("tools", []),
            "tools": data.get("tools", []),
            "apiKeySource": data.get("apiKeySource"),
            "claude_code_version": data.get("claude_code_version"),
        }
    return found


# --------------------------------------------------------------------------------------------
# Checks 2-6: one delegated run with a probe agent
# --------------------------------------------------------------------------------------------


async def check_delegation(launcher: str, mode: str) -> dict[str, Any]:
    """mode: 'disallowed' (hide the spoke tool from the hub) or 'hook_guard' (deny by hook)."""
    events: dict[str, list[Any]] = {
        "hub_denied": [],
        "spoke_tool_calls": [],
        "hub_tool_calls": [],
        "can_use_tool": [],
        "hooks": [],
        "messages": [],
    }

    @tool("echo", "Echo text back. Used only by the prober agent.", {"text": str})
    async def echo(args: dict) -> dict:
        events["spoke_tool_calls"].append(args)
        return {"content": [{"type": "text", "text": "pong"}]}

    @tool("hub_action", "A pretend write action that only the hub may call.", {"note": str})
    async def hub_action(args: dict) -> dict:
        events["hub_tool_calls"].append(args)
        return {"content": [{"type": "text", "text": "recorded"}]}

    server = create_sdk_mcp_server(name="probe", tools=[echo, hub_action])

    async def can_use_tool(tool_name: str, tool_input: dict, context: Any):
        events["can_use_tool"].append(
            {"tool_name": tool_name, "agent_id": getattr(context, "agent_id", None)}
        )
        return PermissionResultAllow()

    def recorder(event: str):
        async def hook(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
            keep = {
                k: input_data.get(k)
                for k in ("hook_event_name", "tool_name", "agent_id", "agent_type")
                if k in input_data
            }
            events["hooks"].append({"event": event, **keep})
            if (
                mode == "hook_guard"
                and event == "PreToolUse"
                and input_data.get("tool_name") == "mcp__probe__echo"
                and not input_data.get("agent_id")  # no agent_id = the hub itself is calling
            ):
                events["hub_denied"].append(input_data.get("tool_name"))
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "The hub has no data tools; delegate.",
                    }
                }
            return {}

        return hook

    hooks = {
        ev: [HookMatcher(hooks=[recorder(ev)])]
        for ev in ("PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop")
    }

    options = base_options(
        tools=[launcher],
        allowed_tools=[launcher],  # auto-approve delegation so can_use_tool sees only MCP tools
        mcp_servers={"probe": server},
        # 'disallowed': hide the spoke's tool from the hub. 'hook_guard': rely on the hook.
        disallowed_tools=["mcp__probe__echo"] if mode == "disallowed" else [],
        agents={
            "prober": AgentDefinition(
                description="Use for the probe test.",
                prompt="Call the tool mcp__probe__echo exactly once with text 'ping', "
                "then reply with the single word DONE.",
                tools=["mcp__probe__echo"],
                model="haiku",
                maxTurns=3,
            )
        },
        can_use_tool=can_use_tool,
        hooks=hooks,
    )

    prompt = (
        "Step 0: call mcp__probe__echo yourself with text 'hub' if you have it (a refusal "
        "is expected; just continue). "
        "Step 1: delegate to the 'prober' agent with the task 'run the probe'. "
        "Step 2: after it finishes, call mcp__probe__hub_action with note='done'. "
        "Step 3: reply with the single word FINISHED."
    )
    hub_tools: list[str] = []
    result: dict[str, Any] = {}
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            hub_tools = msg.data.get("tools", [])
        elif isinstance(msg, AssistantMessage | UserMessage):
            blocks = msg.content if isinstance(msg.content, list) else []
            events["messages"].append(
                {
                    "type": type(msg).__name__,
                    "parent_tool_use_id": msg.parent_tool_use_id,
                    "tool_uses": [
                        {
                            "name": b.name,
                            "input_keys": sorted(b.input),
                            "subagent_type": b.input.get("subagent_type"),
                        }
                        for b in blocks
                        if isinstance(b, ToolUseBlock)
                    ],
                }
            )
        elif isinstance(msg, ResultMessage):
            result = {
                "subtype": msg.subtype,
                "is_error": msg.is_error,
                "num_turns": msg.num_turns,
                "total_cost_usd": msg.total_cost_usd,
                "result": msg.result,
            }

    cut_names = [c["tool_name"] for c in events["can_use_tool"]]
    return {
        "mode": mode,
        "hub_denied_by_hook": events["hub_denied"],
        "hub_init_tools": hub_tools,
        "hub_sees_spoke_tool": "mcp__probe__echo" in hub_tools,
        "spoke_called_its_tool": bool(events["spoke_tool_calls"]),
        "hub_called_gated_tool": bool(events["hub_tool_calls"]),
        "can_use_tool_fired_for": cut_names,
        "can_use_tool_for_hub_tool": "mcp__probe__hub_action" in cut_names,
        "can_use_tool_for_spoke_tool": "mcp__probe__echo" in cut_names,
        "subagent_hook_payloads": [h for h in events["hooks"] if h["event"].startswith("Subagent")],
        "pretooluse_payloads": [h for h in events["hooks"] if h["event"] == "PreToolUse"],
        "messages_with_parent_tool_use_id": sum(
            1 for m in events["messages"] if m["parent_tool_use_id"]
        ),
        "tool_use_names": [tu["name"] for m in events["messages"] for tu in m["tool_uses"]],
        "launcher_inputs": [
            tu
            for m in events["messages"]
            for tu in m["tool_uses"]
            if tu["name"] in ("Agent", "Task")
        ],
        "result": result,
        "raw_can_use_tool": events["can_use_tool"],
    }


def _jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    return str(obj)


async def main() -> None:
    print("Check 1: launcher tool name ...")
    launcher_info = await check_launcher_name()
    for name, info in launcher_info.items():
        print(f"  tools=['{name}'] -> session tools: {info['tools']}")
    print(f"  auth source: {launcher_info['Agent']['apiKeySource']}")
    launcher = "Agent"

    report: dict[str, Any] = {"launcher_info": launcher_info}
    for mode in ("disallowed", "hook_guard"):
        print(f"\nDelegated run, mode={mode} ...")
        d = await check_delegation(launcher, mode)
        report[mode] = d
        print(f"  hub sees spoke tool:        {d['hub_sees_spoke_tool']}")
        print(f"  hub blocked by hook:        {d['hub_denied_by_hook']}")
        print(f"  spoke called its tool:      {d['spoke_called_its_tool']}")
        print(f"  hub called gated tool:      {d['hub_called_gated_tool']}")
        print(f"  can_use_tool fired for:     {d['can_use_tool_fired_for']}")
        print(f"  raw can_use_tool:           {d['raw_can_use_tool']}")
        print(f"  Subagent hook payloads:     {d['subagent_hook_payloads']}")
        print(f"  PreToolUse payloads:        {d['pretooluse_payloads']}")
        print(f"  msgs w/ parent_tool_use_id: {d['messages_with_parent_tool_use_id']}")
        print(f"  tool_use names:             {d['tool_use_names']}")
        print(f"  launcher inputs:            {d['launcher_inputs']}")
        print(f"  result: {d['result']}")

    out = Path(get_settings().data_dir) / "sdk_checks.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=_jsonable))
    print(f"\nFull report: {out}")


if __name__ == "__main__":
    if not (get_settings().anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")):
        print("Note: no ANTHROPIC_API_KEY found; the CLI will try its own login, if any.")
    asyncio.run(main())
