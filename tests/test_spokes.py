"""Tests for the spoke AgentDefinitions: least privilege, models, and prompt rendering."""

from datetime import date

import pytest

from scc.agents import spokes
from scc.agents.spokes import SPOKES, SpokeSpec, build_spokes, prompt_variables, render_prompt
from scc.config import Settings
from scc.tools import build_servers
from scc.tools._common import ToolContext
from tests.conftest import AS_OF


@pytest.fixture
def settings():
    return Settings(_env_file=None)


@pytest.fixture
def servers(tmp_path):
    # Building servers does not touch the database, so any path works.
    return build_servers(ToolContext(db_path=tmp_path / "x.duckdb", as_of=AS_OF))


def test_five_spokes_with_expected_models(servers, settings):
    agents = build_spokes(servers, settings)
    assert set(agents) == {
        "sales_analyst", "customer_insights", "inventory_planner", "pricing_strategist",
        "listing_writer",
    }  # fmt: skip
    assert agents["sales_analyst"].model == settings.analyst_model
    for name in ("customer_insights", "inventory_planner", "pricing_strategist", "listing_writer"):
        assert agents[name].model == settings.spoke_model


def test_least_privilege(servers, settings):
    agents = build_spokes(servers, settings)
    for name, agent in agents.items():
        # Only custom MCP tools, never built-ins such as Bash, Read or WebFetch.
        assert all(t.startswith("mcp__") for t in agent.tools), name
        # Spokes read; they never get write tools (design rule 2).
        assert not any(w in t for t in agent.tools for w in ("save_", "create_", "remember")), name
    # Only the sales analyst may run SQL.
    sql_users = [n for n, a in agents.items() if "mcp__sales__run_sql" in a.tools]
    assert sql_users == ["sales_analyst"]
    # Only the pricing strategist reaches the internet.
    assert [n for n, a in agents.items() if "mcp__pricing__fx_rates" in a.tools] == [
        "pricing_strategist"
    ]


def test_every_tool_is_used_by_some_spoke(servers, settings):
    agents = build_spokes(servers, settings)
    used = {t for a in agents.values() for t in a.tools}
    defined = {n for s in servers.values() for n in s.qualified_names}
    assert defined == used


def test_unknown_tool_fails_at_build_time(servers, settings, monkeypatch):
    broken = dict(SPOKES)
    broken["listing_writer"] = SpokeSpec(
        description="Use for testing.",
        tools=(("catalog", "get_prodcut"),),
        model_setting="spoke_model",
    )
    monkeypatch.setattr(spokes, "SPOKES", broken)
    with pytest.raises(ValueError, match="get_prodcut"):
        build_spokes(servers, settings)


def test_descriptions_are_routing_guidance(servers, settings):
    for name, agent in build_spokes(servers, settings).items():
        assert agent.description.startswith("Use "), name
        assert agent.maxTurns and agent.maxTurns <= 15


def test_prompt_variables():
    v = prompt_variables(date(2011, 12, 9))
    assert v == {
        "as_of": "2011-12-09",
        "as_of_long": "Friday 9 December 2011",
        "current_month": "December 2011",
        "last_full_month": "November 2011",
    }


@pytest.mark.parametrize("name", list(SPOKES))
def test_prompts_render_with_date_and_rules(name):
    text = render_prompt(name, date(2011, 12, 9))
    assert "9 December 2011" in text  # design rule 6
    assert "$" not in text  # no unrendered placeholders
    assert "data, not instructions" in text or "not instructions" in text
    if name != "listing_writer":
        assert "**Answer:**" in text  # shared report format
