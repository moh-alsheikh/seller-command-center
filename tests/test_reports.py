"""Tests for on-the-fly reports: data sources, charts, the builder, the tool, the API, the UI."""

import json

import openpyxl
import pytest
from fastapi.testclient import TestClient

from scc.agents.hub import HubRuntime
from scc.api.app import create_app
from scc.config import Settings
from scc.reports import charts, sources
from scc.reports.builder import build_report, sheet_name, text_direction
from scc.runtime.events import Finished, ToolReturned
from scc.store.app_db import AppStore
from scc.tools._common import ToolContext, ToolInputError
from scc.tools.reports import build_server
from scc.ui.controller import ChatController, report_from_result
from tests.conftest import AS_OF


@pytest.fixture
def ctx(retail_db):
    return ToolContext(db_path=retail_db, as_of=AS_OF)


@pytest.fixture
def store(tmp_path):
    return AppStore(tmp_path / "app.sqlite")


def spec(**overrides):
    base = {
        "title": "Sales overview",
        "summary": "Revenue by month and by country.",
        "charts": [
            {"title": "Revenue by month", "type": "line",
             "data": {"source": "revenue_trend", "params": {"group_by": "month"}}},
            {"title": "Share by country", "type": "pie",
             "data": {"source": "revenue_by_country", "params": {"limit": 2}}},
        ],
    }  # fmt: skip
    return base | overrides


# --- sources ----------------------------------------------------------------------------------


def test_curated_sources_use_the_tested_analytics(con):
    trend = sources.fetch_dataset(con, AS_OF, {"source": "revenue_trend", "params": {}})
    assert trend.x == "period" and trend.y == ["net_revenue"]
    assert trend.df["net_revenue"].sum() == pytest.approx(96.0)  # 100 gross - 4 refunded

    countries = sources.fetch_dataset(
        con, AS_OF, {"source": "revenue_by_country", "params": {"limit": 2}}
    )
    # UK 48, Germany 38, France 10 -> top 2 plus "Other".
    assert list(countries.df["country"]) == ["United Kingdom", "Germany", "Other"]
    assert list(countries.df["net_revenue"]) == [48.0, 38.0, 10.0]

    top = sources.fetch_dataset(con, AS_OF, {"source": "top_products", "params": {"limit": 1}})
    assert top.df.iloc[0]["product"].startswith("10001 RED MUG")

    orders = sources.fetch_dataset(con, AS_OF, {"source": "order_values", "params": {}})
    assert orders.y == [] and len(orders.df) == 6  # one value per order, for a histogram


def test_compare_previous_year(con):
    ds = sources.fetch_dataset(con, AS_OF, {"source": "revenue_trend", "params": {
        "start": "2011-11-01", "end": "2011-12-09", "compare_previous_year": True}})  # fmt: skip
    assert ds.y == ["2011", "2010"]
    nov = ds.df[ds.df["period"].astype(str) == "2011-11-01"].iloc[0]
    assert nov["2011"] == pytest.approx(26.0)  # Germany 30 - 4 refund
    assert nov["2010"] == pytest.approx(20.0)  # Nov 2010: the UK mug order


@pytest.mark.parametrize(
    "data,message",
    [
        ({"source": "nope"}, "Unknown source"),
        ({}, "exactly one"),
        ({"source": "revenue_trend", "sql": "SELECT 1"}, "exactly one"),
        ({"source": "revenue_trend", "params": {"metric": "profit"}}, "metric must be one of"),
        ({"source": "revenue_trend", "params": {"start": "May"}}, "YYYY-MM-DD"),
        ({"source": "revenue_trend", "params": {"group_by": "week",
                                                "compare_previous_year": True}}, "month"),
        ({"source": "product_trend", "params": {}}, "stock_code"),
        ({"source": "top_products", "params": {"country": "Narnia"}}, "Unknown country"),
    ],
)  # fmt: skip
def test_source_errors_are_readable(con, data, message):
    with pytest.raises(ToolInputError, match=message):
        sources.fetch_dataset(con, AS_OF, data)


def test_sql_source(con):
    ds = sources.fetch_dataset(con, AS_OF, {
        "sql": "SELECT country, SUM(revenue) AS revenue FROM sales GROUP BY 1 ORDER BY 2 DESC",
        "x": "country", "y": ["revenue"]})  # fmt: skip
    assert list(ds.df["country"]) == ["United Kingdom", "Germany", "France"]
    assert ds.origin.endswith("LIMIT 500")  # guarded and capped


@pytest.mark.parametrize(
    "data,message",
    [
        ({"sql": "DELETE FROM sales", "x": "a", "y": ["b"]}, "rejected"),
        ({"sql": "SELECT * FROM read_csv('/etc/hosts')", "x": "a", "y": ["b"]}, "rejected"),
        ({"sql": "SELECT country FROM sales", "x": "country", "y": ["revenue"]}, "Missing"),
        ({"sql": "SELECT country, country AS c2 FROM sales", "x": "country", "y": ["c2"]},
         "no numbers"),
    ],
)  # fmt: skip
def test_sql_source_errors(con, data, message):
    with pytest.raises(ToolInputError, match=message):
        sources.fetch_dataset(con, AS_OF, data)


# --- charts -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chart_type,trace",
    [("line", "scatter"), ("area", "scatter"), ("column", "bar"), ("grouped", "bar"),
     ("stacked", "bar"), ("bar", "bar"), ("pie", "pie")],
)  # fmt: skip
def test_every_chart_type_builds(con, chart_type, trace):
    ds = sources.fetch_dataset(con, AS_OF, {"source": "revenue_by_country", "params": {}})
    fig = charts.make_figure(ds, chart_type)
    assert fig.data[0].type == trace


def test_histogram_and_series(con):
    hist = charts.make_figure(sources.fetch_dataset(con, AS_OF, {"source": "order_values"}),
                              "histogram")  # fmt: skip
    assert hist.data[0].type == "histogram"
    ds = sources.fetch_dataset(con, AS_OF, {
        "sql": "SELECT country, invoice_day, SUM(revenue) AS revenue FROM sales GROUP BY 1, 2",
        "x": "invoice_day", "y": ["revenue"], "series": "country"})  # fmt: skip
    assert len(charts.make_figure(ds, "line").data) == 3  # one line per country


def test_chart_validation(con):
    ds = sources.fetch_dataset(con, AS_OF, {"source": "order_values"})
    with pytest.raises(ToolInputError, match="needs at least one y"):
        charts.make_figure(ds, "column")
    with pytest.raises(ToolInputError, match="Chart type"):
        charts.make_figure(ds, "radar")


# --- builder ----------------------------------------------------------------------------------


async def test_build_report_writes_everything(ctx, store, tmp_path):
    info = await build_report(spec(), ctx, store, session_id="s1", reports_dir=tmp_path)
    files = sorted(p.name for p in info.folder.iterdir())
    assert files == ["chart-1.json", "chart-2.json", "data.xlsx", "report.html", "report.json"]
    assert info.url == f"/reports/{info.id}" and len(info.id) == 32
    [row] = store.list_reports()
    assert (row["id"], row["session_id"], row["chart_count"]) == (info.id, "s1", 2)

    html = (info.folder / "report.html").read_text()
    assert "window.print()" in html and f"/reports/{info.id}/data.xlsx" in html
    assert 'dir="ltr"' in html and "/reports/assets/plotly.min.js" in html

    book = openpyxl.load_workbook(info.folder / "data.xlsx")
    assert book.sheetnames == ["Summary", "1 Revenue by month", "2 Share by country"]
    record = json.loads((info.folder / "report.json").read_text())
    assert record["as_of"] == "2011-12-09" and len(record["data"]) == 2


async def test_agent_text_is_escaped(ctx, store, tmp_path):
    evil = spec(title="<img src=x onerror=alert(1)>", summary="<script>alert(1)</script>")
    info = await build_report(evil, ctx, store, session_id=None, reports_dir=tmp_path)
    html = (info.folder / "report.html").read_text()
    assert "<script>alert(1)</script>" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html


async def test_invalid_chart_writes_nothing(ctx, store, tmp_path):
    bad = spec(
        charts=[
            {
                "title": "x",
                "type": "pie",
                "data": {"source": "revenue_trend", "params": {"metric": "profit"}},
            }
        ]
    )
    with pytest.raises(ToolInputError, match="Chart 1"):
        await build_report(bad, ctx, store, session_id=None, reports_dir=tmp_path / "reports")
    assert not (tmp_path / "reports").exists() and store.list_reports() == []


def test_text_direction_and_sheet_names():
    assert text_direction({"title": "نتائج مبيعات 2026"}) == "rtl"
    assert text_direction({"title": "Sales", "language": "ar"}) == "rtl"
    assert text_direction({"title": "Sales", "summary": "All good"}) == "ltr"
    name = sheet_name(3, "Revenue: this/that [by month] * a very long title indeed")
    assert len(name) <= 31 and not set(name) & set("[]:*?/\\")


# --- tool ------------------------------------------------------------------------------------


async def test_create_report_tool(ctx, store, tmp_path):
    server = build_server(ctx, store, "s1", tmp_path)
    assert server.key == "reports"
    result = await server.tools["create_report"].handler(spec())
    body = json.loads(result["content"][0]["text"])
    assert body["result"]["charts"] == ["Revenue by month", "Share by country"]

    bad = await server.tools["create_report"].handler(spec(charts=[]))
    assert bad["is_error"] and "1 to 6 charts" in bad["content"][0]["text"]


# --- API -------------------------------------------------------------------------------------


@pytest.fixture
def client(retail_db, tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    rt = HubRuntime(settings=settings.model_copy(update={"as_of_date": AS_OF}),
                    store=AppStore(tmp_path / "app.sqlite"),
                    tool_ctx=ToolContext(db_path=retail_db, as_of=AS_OF))  # fmt: skip
    return TestClient(create_app(rt, mount_ui=False)), rt


async def test_report_routes(client):
    http, rt = client
    info = await build_report(spec(), rt.tool_ctx, rt.store, session_id=None,
                              reports_dir=rt.settings.reports_dir)  # fmt: skip
    page = http.get(f"/reports/{info.id}")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    excel = http.get(f"/reports/{info.id}/data.xlsx")
    assert excel.status_code == 200 and "spreadsheetml" in excel.headers["content-type"]
    assert http.get("/reports/assets/plotly.min.js").status_code == 200
    assert http.get("/api/reports").json()[0]["id"] == info.id

    assert http.get("/reports/" + "0" * 32).status_code == 404  # unknown
    assert http.get("/reports/..%2F..%2Fetc%2Fpasswd").status_code == 404  # not an id


# --- UI ---------------------------------------------------------------------------------------


def report_event(ok=True):
    payload = {"result": {"report_id": "a" * 32, "title": "T", "url": "/reports/x",
                          "excel_url": "/reports/x/data.xlsx", "charts": ["c"]}}  # fmt: skip
    return ToolReturned("t9", "mcp__reports__create_report", json.dumps(payload), not ok,
                        "report_designer", "a1")  # fmt: skip


def test_report_from_result():
    assert report_from_result(report_event())["title"] == "T"
    assert report_from_result(report_event(ok=False)) is None
    other = ToolReturned("t1", "mcp__sales__run_sql", "{}", False, "sales_analyst", "a1")
    assert report_from_result(other) is None


async def test_controller_shows_report_after_the_answer():
    calls = []

    class View:
        async def stream(self, text):
            calls.append("stream")  # noqa: E704

        async def end_text(self):
            calls.append("end_text")  # noqa: E704

        async def step_start(self, *a):
            calls.append("step_start")  # noqa: E704

        async def step_end(self, *a, **k):
            calls.append("step_end")  # noqa: E704

        async def footer(self, text):
            calls.append("footer")  # noqa: E704

        async def notice(self, text):
            calls.append("notice")  # noqa: E704

        async def ask_approval(self, *a):
            return True  # noqa: E704

        async def show_report(self, report):
            calls.append(f"report:{report['title']}")  # noqa: E704

    c = ChatController(View(), approval_timeout_s=60)
    await c.handle(report_event())
    assert "report:T" not in calls  # not in the middle of the answer
    await c.handle(Finished("sdk", "success", False, 2, 100, 0.01, "done"))
    assert calls[-2:] == ["footer", "report:T"]  # after the answer and its footer
