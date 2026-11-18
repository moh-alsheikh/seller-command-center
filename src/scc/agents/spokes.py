"""The five specialist sub-agents ("spokes") as SDK ``AgentDefinition`` objects.

An AgentDefinition has four parts that matter here:
  * ``description``  read by the HUB to decide whom to delegate to. It is written as
                     routing guidance ("Use for ..."), not as a job title.
  * ``prompt``       the spoke's own system prompt (Markdown files in ./prompts).
  * ``tools``        the exact tools the spoke may call. This is design rule 3 (least
                     privilege): the customer specialist cannot run SQL, the listing writer
                     cannot see exchange rates, and no spoke has a write tool (rule 2).
  * ``model``        Sonnet for the SQL-writing analyst, Haiku for the rest (see config.py).

``maxTurns`` caps each spoke's own loop, so one confused spoke cannot use up the whole
request's turn budget.

The in-process MCP servers themselves are registered once, on the hub's
``ClaudeAgentOptions.mcp_servers`` (milestone 4): the SDK can only route calls to in-process
servers registered there. A spoke refers to its tools by qualified name.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from string import Template

from claude_agent_sdk import AgentDefinition

from scc.analytics.forecast import add_months, last_complete_month
from scc.config import Settings, get_settings
from scc.tools import ToolServer, mcp_name

PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class SpokeSpec:
    """Everything that defines a spoke, apart from the rendered prompt and model ID."""

    description: str
    tools: tuple[tuple[str, str], ...]  # (server key, tool name)
    model_setting: str  # which Settings field holds the model ID
    max_turns: int = 10


SPOKES: dict[str, SpokeSpec] = {
    "sales_analyst": SpokeSpec(
        description=(
            "Use for revenue, orders, units and trends: totals by period, week, month or "
            "country; period-over-period changes and what drove them; any ad-hoc question "
            "answerable with read-only SQL over sales, returns and products."
        ),
        tools=(("sales", "revenue_summary"), ("sales", "describe_schema"), ("sales", "run_sql")),
        model_setting="analyst_model",
        max_turns=15,  # SQL work often needs a schema look-up and a retry
    ),
    "customer_insights": SpokeSpec(
        description=(
            "Use for questions about customers: RFM segments (champions, loyal, at risk, "
            "hibernating), the top customers in a segment, cohort retention, and the "
            "repeat-purchase rate."
        ),
        tools=(
            ("customers", "rfm_segments"),
            ("customers", "segment_customers"),
            ("customers", "cohort_retention"),
            ("customers", "repeat_customer_rate"),
        ),
        model_setting="spoke_model",
    ),
    "inventory_planner": SpokeSpec(
        description=(
            "Use for demand and restocking: unit sales history for a product or the whole "
            "shop, next-month demand forecasts, which products to restock and roughly how "
            "many units."
        ),
        tools=(
            ("inventory", "forecast_demand"),
            ("inventory", "demand_history"),
            ("catalog", "search_products"),
        ),
        model_setting="spoke_model",
    ),
    "pricing_strategist": SpokeSpec(
        description=(
            "Use for prices: a product's price history and bulk price tiers, and exchange "
            "rates to quote prices in other currencies (EUR, USD, ...)."
        ),
        tools=(
            ("pricing", "price_history"),
            ("pricing", "fx_rates"),
            ("catalog", "search_products"),
        ),
        model_setting="spoke_model",
    ),
    "listing_writer": SpokeSpec(
        description=(
            "Use to write a product listing draft (title, bullets, description, tags) from "
            "the product's real sales facts. Also looks up products and best sellers. "
            "Returns the draft only; it cannot save it."
        ),
        tools=(
            ("catalog", "get_product"),
            ("catalog", "search_products"),
            ("catalog", "top_products"),
        ),
        model_setting="spoke_model",
    ),
    "report_designer": SpokeSpec(
        description=(
            "Use ALONGSIDE the specialist that answers, whenever the answer involves figures "
            "over time, comparisons, rankings, shares of a whole or distributions: builds a "
            "report for this request with charts, a printable PDF page and an Excel file. Give "
            "it the seller's exact question, the period and the language to write in."
        ),
        tools=(("sales", "describe_schema"), ("reports", "create_report")),
        model_setting="analyst_model",  # it may write SQL for charts
        max_turns=12,
    ),
}


# --------------------------------------------------------------------------------------------
# Prompt rendering
# --------------------------------------------------------------------------------------------


def prompt_variables(as_of: date) -> dict[str, str]:
    """Values substituted into every prompt (design rule 6: the date is always stated).

    Example dates in the prompts are relative ("last full month") rather than fixed, so the
    prompts stay correct whatever date the data was shifted to.
    """
    last_full = last_complete_month(as_of)
    prev_full = add_months(last_full, -1)
    return {
        "as_of": as_of.isoformat(),
        "as_of_long": f"{as_of:%A} {as_of.day} {as_of:%B %Y}",  # e.g. "Friday 13 November 2026"
        "current_month": f"{as_of:%B %Y}",
        "current_year": str(as_of.year),
        "last_full_month": f"{last_full:%B %Y}",
        "last_full_month_start": last_full.isoformat(),
        "last_full_month_end": (add_months(last_full, 1) - timedelta(days=1)).isoformat(),
        "last_full_month_range": f"1-{monthrange(last_full.year, last_full.month)[1]} "
        f"{last_full:%b %Y}",
        "prev_full_month": f"{prev_full:%B %Y}",
    }


def render_prompt(name: str, as_of: date) -> str:
    """Load prompts/<name>.md and fill in $placeholders.

    ``string.Template`` rather than ``str.format``, because prompts contain ``{`` and ``}``
    (SQL, JSON examples) that would otherwise need escaping. ``substitute`` (not
    ``safe_substitute``) raises on a missing variable, so a typo fails a test instead of
    reaching the model as "$as_of".
    """
    variables = prompt_variables(as_of)
    rules = Template((PROMPTS_DIR / "_spoke_rules.md").read_text()).substitute(variables)
    template = Template((PROMPTS_DIR / f"{name}.md").read_text())
    return template.substitute(variables, rules=rules).strip() + "\n"


# --------------------------------------------------------------------------------------------
# Building the definitions
# --------------------------------------------------------------------------------------------


def spoke_tool_names(name: str) -> list[str]:
    return [mcp_name(server, tool) for server, tool in SPOKES[name].tools]


def build_spokes(
    servers: dict[str, ToolServer], settings: Settings | None = None
) -> dict[str, AgentDefinition]:
    """Return {agent name: AgentDefinition} for ClaudeAgentOptions.agents.

    Every tool a spoke lists is checked against the built servers, so a typo in a tool name
    fails at start-up rather than as a silent "tool not available" during a conversation.
    """
    settings = settings or get_settings()
    if settings.as_of_date is None:
        raise ValueError(
            "as_of_date is not resolved; pass settings through scc.data.db.resolve_as_of first"
        )
    available = {name for server in servers.values() for name in server.qualified_names}

    agents: dict[str, AgentDefinition] = {}
    for name, spec in SPOKES.items():
        tools = spoke_tool_names(name)
        missing = [t for t in tools if t not in available]
        if missing:
            raise ValueError(f"Spoke {name!r} lists unknown tools: {missing}")
        agents[name] = AgentDefinition(
            description=spec.description,
            prompt=render_prompt(name, settings.as_of_date),
            tools=tools,
            model=getattr(settings, spec.model_setting),
            maxTurns=spec.max_turns,
            background=False,  # the hub needs the report before it can answer
        )
    return agents
