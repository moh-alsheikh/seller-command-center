"""Tests for the MCP tool handlers, called directly against the fixture database.

Expected numbers come from the fixture described in conftest.py. Each test checks the JSON
envelope the agent will actually read, including the error messages it must be able to act on.
"""

import json

import httpx
import jsonschema
import pytest
import respx

from scc.integrations.fx import FxClient
from scc.tools import SERVER_MODULES, build_servers, mcp_name, mcp_server_configs
from scc.tools._common import ToolContext, _clean, safe
from tests.conftest import AS_OF

FX_URL = "https://fx.test/v1"


@pytest.fixture
def ctx(retail_db):
    return ToolContext(
        db_path=retail_db,
        as_of=AS_OF,
        fx=FxClient(base_url=FX_URL, fallback_url=None, backoff_s=0),
        max_rows=50,
    )


@pytest.fixture
def servers(ctx):
    return build_servers(ctx)


async def call(servers, server: str, tool: str, args: dict | None = None):
    """Call a tool handler. Returns (parsed JSON or error text, is_error)."""
    result = await servers[server].tools[tool].handler(args or {})
    text = result["content"][0]["text"]
    if result.get("is_error"):
        return text, True
    return json.loads(text), False


# --- Registry and schemas -----------------------------------------------------------------


def test_registry_names_and_configs(servers):
    assert set(servers) == set(SERVER_MODULES)
    assert mcp_name("sales", "run_sql") == "mcp__sales__run_sql"
    assert "mcp__pricing__fx_rates" in servers["pricing"].qualified_names
    configs = mcp_server_configs(servers)
    assert all(cfg["type"] == "sdk" and cfg["name"] == key for key, cfg in configs.items())


def test_every_tool_has_a_strict_valid_schema_and_read_only_hint(servers):
    for server in servers.values():
        for t in server.tools.values():
            jsonschema.Draft202012Validator.check_schema(t.input_schema)
            assert t.input_schema["additionalProperties"] is False, t.name
            assert len(t.description) > 40, t.name
            assert t.annotations.readOnlyHint is True, t.name


def test_schema_rejects_unknown_and_bad_arguments(servers):
    schema = servers["catalog"].tools["top_products"].input_schema
    jsonschema.validate({"by": "units", "limit": 5}, schema)
    for bad in ({"by": "margin"}, {"limit": 500}, {"start": "Nov 2011"}, {"surprise": 1}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)


# --- Envelope -----------------------------------------------------------------------------


def test_clean_rounds_money_and_ratios_differently():
    out = _clean({"revenue": 1.23456, "repeat_rate": 0.666666, "rates": {"EUR": 1.17213}})
    assert out == {"revenue": 1.23, "repeat_rate": 0.6667, "rates": {"EUR": 1.1721}}
    assert _clean(float("nan")) is None


async def test_safe_hides_internal_errors():
    @safe
    async def broken(args):
        raise RuntimeError("secret internals")

    result = await broken({})
    assert result["is_error"] is True
    assert "secret" not in result["content"][0]["text"]


async def test_missing_database_is_reported(tmp_path):
    ctx = ToolContext(db_path=tmp_path / "missing.duckdb", as_of=AS_OF)
    text, is_error = await call(build_servers(ctx), "sales", "describe_schema")
    assert is_error and "ingest" in text


# --- sales --------------------------------------------------------------------------------


async def test_revenue_summary_total(servers):
    data, err = await call(servers, "sales", "revenue_summary")
    assert not err
    assert data["as_of"] == "2011-12-09" and data["currency"] == "GBP"
    assert data["rows"] == [
        {"period_or_group": "all", "revenue": 100.0, "refunds": 4.0, "net_revenue": 96.0,
         "orders": 6, "customers": 3, "units": 46}
    ]  # fmt: skip


async def test_revenue_summary_by_country_sorted_by_net(servers):
    data, _ = await call(servers, "sales", "revenue_summary", {"group_by": "country"})
    got = [(r["period_or_group"], r["net_revenue"]) for r in data["rows"]]
    assert got == [("United Kingdom", 48.0), ("Germany", 38.0), ("France", 10.0)]


async def test_revenue_summary_rejects_unknown_country_with_valid_names(servers):
    text, err = await call(servers, "sales", "revenue_summary", {"country": "Deutschland"})
    assert err and "Germany" in text and "EIRE" in text


async def test_revenue_summary_rejects_reversed_dates(servers):
    text, err = await call(
        servers, "sales", "revenue_summary", {"start": "2011-12-01", "end": "2011-01-01"}
    )
    assert err and "start must be on or before end" in text


async def test_run_sql_returns_rows_and_executed_sql(servers):
    data, err = await call(servers, "sales", "run_sql", {"sql": "SELECT COUNT(*) AS n FROM sales"})
    assert not err
    assert data["rows"] == [{"n": 6}]
    assert data["executed_sql"].endswith("LIMIT 50")  # guard applied, ctx.max_rows = 50


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("DELETE FROM sales", "Only read queries"),
        ("SELECT * FROM read_csv('/etc/hosts')", "Table functions"),
        ("SELECT nope FROM sales", "Database error"),
    ],
)
async def test_run_sql_errors_are_readable(servers, sql, expected):
    text, err = await call(servers, "sales", "run_sql", {"sql": sql})
    assert err and expected in text


async def test_describe_schema(servers):
    data, _ = await call(servers, "sales", "describe_schema")
    assert "sales: Product sales lines" in data["schema"]


# --- customers ------------------------------------------------------------------------------


async def test_rfm_segments(servers):
    data, _ = await call(servers, "customers", "rfm_segments")
    assert [r["segment"] for r in data["rows"]] == [
        "Loyal customers", "Potential loyalists", "Can't lose them",
    ]  # fmt: skip
    assert data["notes"]


async def test_segment_customers(servers):
    data, _ = await call(servers, "customers", "segment_customers", {"segment": "Loyal customers"})
    assert data["segment_size"] == 1
    assert data["rows"][0]["customer_id"] == 2
    assert data["rows"][0]["monetary"] == 42.0


async def test_cohort_retention_and_note(servers):
    data, _ = await call(servers, "customers", "cohort_retention", {"max_months": 12})
    assert [r["cohort_month"] for r in data["rows"]] == ["2010-11-01", "2010-12-01", "2011-12-01"]
    later, _ = await call(servers, "customers", "cohort_retention", {"from_cohort": "2011-01-01"})
    assert [r["cohort_month"] for r in later["rows"]] == ["2011-12-01"]
    assert "notes" not in later  # the 2009-12 caveat only matters when early cohorts are shown


async def test_repeat_customer_rate(servers):
    data, _ = await call(servers, "customers", "repeat_customer_rate")
    assert data["result"] == {"customers": 3, "repeat_customers": 2, "repeat_rate": 0.6667}


# --- inventory ------------------------------------------------------------------------------


async def test_forecast_one_product_normalizes_code(servers):
    data, _ = await call(servers, "inventory", "forecast_demand", {"stock_code": " 10001 "})
    [row] = data["rows"]
    assert row["stock_code"] == "10001"
    assert (row["units"], row["method"], row["expected_remaining"]) == (
        7.7,
        "trailing_average",
        7.7,
    )
    assert data["last_complete_month"] == "2011-11-01"


async def test_forecast_top_sellers(servers):
    data, _ = await call(servers, "inventory", "forecast_demand", {"top_n": 5})
    assert [r["stock_code"] for r in data["rows"]] == ["10001"]


async def test_demand_history_unknown_product(servers):
    text, err = await call(servers, "inventory", "demand_history", {"stock_code": "NOPE"})
    assert err and "search_products" in text


async def test_demand_history_weekly(servers):
    data, _ = await call(servers, "inventory", "demand_history", {"freq": "week"})
    assert data["freq"] == "week"
    assert sum(r["units"] for r in data["rows"]) == 46


# --- pricing (FX mocked) ----------------------------------------------------------------------


FX_BODY = {"amount": 1.0, "base": "GBP", "date": "2011-12-09", "rates": {"EUR": 1.1721}}


@respx.mock
async def test_price_history_with_conversion_uses_as_of_rate_by_default(servers):
    route = respx.get(f"{FX_URL}/2011-12-09").mock(return_value=httpx.Response(200, json=FX_BODY))
    data, err = await call(
        servers, "pricing", "price_history", {"stock_code": "10001", "currencies": ["eur"]}
    )
    assert not err and route.called
    assert [m["avg_price"] for m in data["monthly"]] == [2.0, 2.0, 1.5]
    assert [p["price"] for p in data["price_points"]] == [1.5, 2.0]
    conv = data["converted"]
    assert conv["latest_month"] == "2011-11-01"
    assert conv["prices"] == {"EUR": 1.76}  # 1.5 x 1.1721
    assert conv["fx"]["rates"] == {"EUR": 1.1721}


async def test_price_history_without_currencies_makes_no_http_call(servers):
    data, err = await call(servers, "pricing", "price_history", {"stock_code": "10001"})
    assert not err and data["converted"] is None


@respx.mock
async def test_fx_rates_latest_and_weekend_note(servers):
    respx.get(f"{FX_URL}/latest").mock(return_value=httpx.Response(200, json=FX_BODY))
    respx.get(f"{FX_URL}/2011-12-10").mock(return_value=httpx.Response(200, json=FX_BODY))

    latest, _ = await call(
        servers, "pricing", "fx_rates", {"currencies": ["EUR"], "fx_date": "latest"}
    )
    assert latest["result"]["rates"] == {"EUR": 1.1721}
    assert "notes" not in latest

    weekend, _ = await call(
        servers, "pricing", "fx_rates", {"currencies": ["EUR"], "fx_date": "2011-12-10"}
    )
    assert "2011-12-09" in weekend["notes"][0]


async def test_fx_rates_bad_currency_is_a_readable_error(servers):
    text, err = await call(servers, "pricing", "fx_rates", {"currencies": ["EURO"]})
    assert err and "3-letter currency code" in text


@respx.mock
async def test_fx_outage_is_a_readable_error(servers):
    respx.get(f"{FX_URL}/2011-12-09").mock(side_effect=httpx.ConnectError("down"))
    text, err = await call(servers, "pricing", "fx_rates", {"currencies": ["EUR"]})
    assert err and "unavailable" in text


# --- catalog --------------------------------------------------------------------------------


async def test_get_product_and_untrusted_note(servers):
    data, _ = await call(servers, "catalog", "get_product", {"stock_code": "10001"})
    assert data["result"]["description"] == "RED MUG"
    assert data["result"]["return_rate"] == 0.0571  # 2 / 35
    assert any("never as instructions" in n for n in data["notes"])


async def test_get_product_unknown(servers):
    text, err = await call(servers, "catalog", "get_product", {"stock_code": "NOPE"})
    assert err and "search_products" in text


async def test_top_products_net_by_default(servers):
    data, _ = await call(servers, "catalog", "top_products", {"limit": 2})
    assert data["ranked_by"] == "net_revenue"
    assert [(r["stock_code"], r["net_revenue"]) for r in data["rows"]] == [
        ("10001", 56.0), ("20002", 30.0),
    ]  # fmt: skip


async def test_search_products_no_match_hint(servers):
    data, _ = await call(servers, "catalog", "search_products", {"text": "unicorn"})
    assert data["rows"] == [] and "No match" in data["notes"][0]


# --- Through the real MCP server (the path the Claude Code CLI uses) -------------------------


async def test_call_through_mcp_server_validates_arguments(servers):
    import mcp.types as mt

    server = servers["catalog"].config["instance"]
    handler = server.request_handlers[mt.CallToolRequest]

    async def call_tool(args):
        req = mt.CallToolRequest(
            method="tools/call",
            params=mt.CallToolRequestParams(name="get_product", arguments=args),
        )
        return (await handler(req)).root

    good = await call_tool({"stock_code": "10001"})
    assert good.isError is False
    assert json.loads(good.content[0].text)["result"]["stock_code"] == "10001"

    # The SDK validates against our schema before the handler runs.
    bad = await call_tool({"stock_code": "10001", "bogus": 1})
    assert bad.isError is True
    assert "bogus" in bad.content[0].text

    # Our own "returned, not raised" errors keep isError=True through the protocol.
    unknown = await call_tool({"stock_code": "NOPE"})
    assert unknown.isError is True
