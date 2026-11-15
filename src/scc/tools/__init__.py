"""Tool registry: builds one in-process MCP server per area and names every tool.

Each module (sales, customers, inventory, pricing, catalog) exposes
``build_tools(ctx) -> list[SdkMcpTool]``. This module wraps each list in an SDK MCP server
(``create_sdk_mcp_server``) and records the fully qualified names the model sees:

    server key "sales" + tool "run_sql"  ->  "mcp__sales__run_sql"

Why one server per area instead of one big server?
  * The names say which area a tool belongs to (``mcp__pricing__fx_rates``), which makes
    audit logs and traces easy to read.
  * Least privilege (design rule 3) can be set per area: a spoke's ``AgentDefinition.tools``
    lists exactly the qualified names it may call.

The hub's own write tools (save_listing_draft, create_purchase_order, remember_preference)
are added in milestone 4; they are deliberately not part of this read-only registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig

from scc.tools import catalog, customers, inventory, pricing, sales
from scc.tools._common import ToolContext

SERVER_MODULES: dict[str, ModuleType] = {
    "sales": sales,
    "customers": customers,
    "inventory": inventory,
    "pricing": pricing,
    "catalog": catalog,
}


def mcp_name(server: str, tool: str) -> str:
    """The name Claude sees for a custom tool: mcp__<server key>__<tool name>."""
    return f"mcp__{server}__{tool}"


@dataclass(frozen=True)
class ToolServer:
    key: str
    config: McpSdkServerConfig
    tools: dict[str, SdkMcpTool[Any]]  # short name -> tool (handlers are callable in tests)

    @property
    def qualified_names(self) -> list[str]:
        return [mcp_name(self.key, name) for name in self.tools]


def build_servers(ctx: ToolContext) -> dict[str, ToolServer]:
    """Build every read-only tool server for one context (one per process or per test)."""
    servers: dict[str, ToolServer] = {}
    for key, module in SERVER_MODULES.items():
        tools = module.build_tools(ctx)
        servers[key] = ToolServer(
            key=key,
            config=create_sdk_mcp_server(name=key, version="1.0.0", tools=tools),
            tools={t.name: t for t in tools},
        )
    return servers


def mcp_server_configs(servers: dict[str, ToolServer]) -> dict[str, McpSdkServerConfig]:
    """The ``mcp_servers`` mapping for ClaudeAgentOptions."""
    return {key: server.config for key, server in servers.items()}
