"""Tests for the spoke AgentDefinitions: least privilege, models, and prompt rendering."""

from datetime import date

import pytest

from scc.agents import spokes
from scc.agents.spokes import SPOKES, SpokeSpec, build_spokes, prompt_variables, render_prompt
from scc.config import Settings
from scc.store.app_db import AppStore
from scc.tools import build_servers
from scc.tools._common import ToolContext
from scc.tools.reports import build_server as build_reports_server
from tests.conftest import AS_OF


@pytest.fixture
def settings():
    return Settings(_env_file=None, as_of_date=AS_OF)


@pytest.fixture
def servers(tmp_path):
    # Building servers does not touch the database, so any path works.
    ctx = ToolContext(db_path=tmp_path / "x.duckdb", as_of=AS_OF)
    servers = build_servers(ctx)
    # The report designer's server is built per session in build_options; add it here too.
    reports = build_reports_server(ctx, AppStore(tmp_path / "app.sqlite"), None, tmp_path)
    return {**servers, reports.key: reports}


def test_six_spokes_with_expected_models(servers, settings):
    agents = build_spokes(servers, settings)
    assert set(agents) == {
        "sales_analyst", "customer_insights", "inventory_planner", "pricing_strategist",
        "listing_writer", "report_designer",
    }  # fmt: skip
    # The two spokes that write SQL get the stronger model.
    assert agents["sales_analyst"].model == settings.analyst_model
    assert agents["report_designer"].model == settings.analyst_model
    for name in ("customer_insights", "inventory_planner", "pricing_strategist", "listing_writer"):
        assert agents[name].model == settings.spoke_model


def test_least_privilege(servers, settings):
    agents = build_spokes(servers, settings)
    for name, agent in agents.items():
        # Only custom MCP tools, never built-ins such as Bash, Read or WebFetch.
        assert all(t.startswith("mcp__") for t in agent.tools), name
        # Spokes read; they never get the hub's action or memory tools (design rule 2).
        assert not any(t.startswith(("mcp__actions__", "mcp__memory__")) for t in agent.tools), name
        # Every spoke tool declares that it changes nothing in the shop.
        for t in agent.tools:
            server, _, short = t.removeprefix("mcp__").partition("__")
            assert servers[server].tools[short].annotations.readOnlyHint is True, t
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
    v = prompt_variables(date(2026, 11, 13))
    assert v == {
        "as_of": "2026-11-13",
        "as_of_long": "Friday 13 November 2026",
        "current_month": "November 2026",
        "current_year": "2026",
        "last_full_month": "October 2026",
        "last_full_month_start": "2026-10-01",
        "last_full_month_end": "2026-10-31",
        "last_full_month_range": "1-31 Oct 2026",
        "prev_full_month": "September 2026",
    }
    # Year boundary: in January the last full month is December of the previous year.
    jan = prompt_variables(date(2027, 1, 8))
    assert (jan["last_full_month"], jan["prev_full_month"]) == ("December 2026", "November 2026")


@pytest.mark.parametrize("name", list(SPOKES))
def test_prompts_render_with_date_and_rules(name):
    text = render_prompt(name, date(2011, 12, 9))
    assert "9 December 2011" in text  # design rule 6
    assert "$" not in text  # no unrendered placeholders
    assert "data, not instructions" in text or "not instructions" in text
    if name not in ("listing_writer", "report_designer"):
        assert "**Answer:**" in text  # shared report format
