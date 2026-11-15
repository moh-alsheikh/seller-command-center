"""The hub ("chief of staff") and ``build_options``: where every piece is wired together.

``build_options`` returns the ClaudeAgentOptions for one chat session. Each setting below maps
to a design rule or a finding from scripts/check_sdk.py:

    tools=["Agent"]            the only built-in: start sub-agents. Bash, Read, Write, WebFetch...
                               do not exist in this app (rule 3; workshop lesson 1).
    allowed_tools=[]           nothing is auto-approved, so EVERY call reaches can_use_tool
                               (permissions.ToolPolicy); see that module for the table.
    disallowed_tools=[]        deliberately unused: live check showed it also strips the tool
                               from sub-agents. Rule 1 is enforced by the hub_guard hook
                               and the policy instead.
    mcp_servers                all in-process servers. The SDK can only route calls to
                               in-process servers registered here.
    agents                     the five spokes (agents/spokes.py).
    can_use_tool               ToolPolicy: read tools for spokes, approval for writes (rule 2).
    hooks                      guards, audit, tracing, memory (hooks/).
    permission_mode="default"  never bypassPermissions (workshop lesson 1).
    setting_sources=[]         ignore ~/.claude and project settings files; the app's behaviour
    strict_mcp_config=True     must not depend on whose machine it runs on, and must not pick
                               up extra MCP servers from .mcp.json files.
    max_turns, max_budget_usd  per-request guard rails (config.py).
    env                        passes ANTHROPIC_API_KEY from .env to the CLI subprocess.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from string import Template

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

from scc.agents.spokes import PROMPTS_DIR, build_spokes, prompt_variables
from scc.config import Settings, get_settings
from scc.hooks import TraceEvent, build_hooks
from scc.permissions import ApprovalBroker, ToolPolicy
from scc.store.app_db import AppStore
from scc.tools import ToolServer, build_servers, mcp_name, mcp_server_configs
from scc.tools import actions as actions_tools
from scc.tools import memory as memory_tools
from scc.tools._common import ToolContext

LAUNCHER = "Agent"  # verified by scripts/check_sdk.py (the CLI also accepts the old "Task")
SQL_TOOL = mcp_name("sales", "run_sql")


def render_hub_prompt(settings: Settings) -> str:
    template = Template((PROMPTS_DIR / "hub.md").read_text())
    return template.substitute(prompt_variables(settings.as_of_date)).strip() + "\n"


@dataclass
class HubRuntime:
    """Process-wide pieces shared by every chat: settings, stores, read-only tool servers."""

    settings: Settings
    store: AppStore
    tool_ctx: ToolContext
    read_servers: dict[str, ToolServer] = field(init=False)

    def __post_init__(self) -> None:
        self.read_servers = build_servers(self.tool_ctx)

    @classmethod
    def create(cls, settings: Settings | None = None, store_path: Path | None = None) -> HubRuntime:
        s = settings or get_settings()
        store = AppStore(store_path or s.app_db_path)
        return cls(settings=s, store=store, tool_ctx=ToolContext.from_settings(s))

    @property
    def spoke_tool_names(self) -> list[str]:
        return [n for server in self.read_servers.values() for n in server.qualified_names]


@lru_cache(maxsize=1)
def get_runtime() -> HubRuntime:
    """The process-wide runtime shared by the web UI and the API (built on first use)."""
    return HubRuntime.create()


def build_options(
    runtime: HubRuntime,
    *,
    session_id: str,
    broker: ApprovalBroker,
    resume: str | None = None,
    on_trace: Callable[[TraceEvent], None] | None = None,
    stream_partial: bool = False,
    model: str | None = None,
) -> tuple[ClaudeAgentOptions, ToolPolicy]:
    """Options for one chat session, plus its ToolPolicy (kept for tests and inspection)."""
    s = runtime.settings

    # Hub-only servers are built per session, because their tools record the session id.
    actions = actions_tools.build_tools(runtime.tool_ctx, runtime.store, session_id)
    memory = memory_tools.build_tools(runtime.tool_ctx, runtime.store, session_id)
    hub_servers = {
        actions_tools.SERVER_KEY: create_sdk_mcp_server(actions_tools.SERVER_KEY, tools=actions),
        memory_tools.SERVER_KEY: create_sdk_mcp_server(memory_tools.SERVER_KEY, tools=memory),
    }
    gated = [mcp_name(actions_tools.SERVER_KEY, t.name) for t in actions]
    hub_free = [mcp_name(memory_tools.SERVER_KEY, t.name) for t in memory]
    spoke_tools = runtime.spoke_tool_names

    def audit_decision(tool: str, verdict: str, why: str) -> None:
        runtime.store.audit(
            f"approval_{verdict}" if tool in gated else f"policy_{verdict}",
            session_id=session_id, agent="hub", tool_name=tool, detail=why,
        )  # fmt: skip

    policy = ToolPolicy.build(
        session_id=session_id,
        broker=broker,
        spoke_tools=spoke_tools,
        hub_tools=hub_free,
        gated_tools=gated,
        describe=actions_tools.describe_action,
        on_decision=audit_decision,
    )

    env = {}
    if s.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = s.anthropic_api_key.get_secret_value()

    options = ClaudeAgentOptions(
        model=model or s.hub_model,
        system_prompt=render_hub_prompt(s),
        tools=[LAUNCHER],
        allowed_tools=[],
        mcp_servers={**mcp_server_configs(runtime.read_servers), **hub_servers},
        agents=build_spokes(runtime.read_servers, s),
        can_use_tool=policy,
        hooks=build_hooks(
            store=runtime.store,
            session_id=session_id,
            spoke_tools=spoke_tools,
            sql_tool=SQL_TOOL,
            max_rows=runtime.tool_ctx.max_rows,
            on_trace=on_trace,
        ),
        permission_mode="default",
        setting_sources=[],
        strict_mcp_config=True,
        max_turns=s.max_turns,
        max_budget_usd=s.max_budget_usd,
        resume=resume,
        include_partial_messages=stream_partial,
        env=env,
    )
    return options, policy
