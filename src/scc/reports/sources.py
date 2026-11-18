"""Where a chart's numbers come from. Two kinds, both deterministic:

1. **Curated sources**: thin wrappers over the tested analytics functions, for the common
   questions (revenue over time, by country, top products, customer segments, ...).
2. **SQL**: a read-only SELECT written for this request, for anything the curated sources do
   not cover. It goes through the same guard as the analyst's run_sql (design rule 5).

Either way the result is a ``Dataset``: a DataFrame plus which column is the x axis, which
columns are the y values, and an optional series column (one line or bar group per value).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import duckdb
import pandas as pd

from scc.analytics import catalog, forecast, pricing, rfm
from scc.analytics.sales import order_values, revenue_summary
from scc.data.sql_guard import UnsafeSQLError, guard_sql
from scc.tools._common import ToolInputError, check_country, check_product

MAX_SQL_ROWS = 500


@dataclass
class Dataset:
    df: pd.DataFrame
    x: str
    y: list[str] = field(default_factory=list)  # empty for histograms (x is the measure)
    series: str | None = None
    origin: str = ""  # shown in the report's footer: the source name, or the SQL text


# --------------------------------------------------------------------------------------------
# Parameter helpers (readable errors the agent can act on)
# --------------------------------------------------------------------------------------------


def _date(params: dict, name: str) -> date | None:
    raw = params.get(name)
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError as err:
        raise ToolInputError(f"{name} must be a date like YYYY-MM-DD, got {raw!r}.") from err


def _choice(params: dict, name: str, options: tuple[str, ...], default: str) -> str:
    value = str(params.get(name) or default)
    if value not in options:
        raise ToolInputError(f"{name} must be one of {list(options)}, got {value!r}.")
    return value


def _int(params: dict, name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(params.get(name) or default)
    except (TypeError, ValueError) as err:
        raise ToolInputError(f"{name} must be a whole number.") from err
    return max(lo, min(hi, value))


def _shift_year(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28)


def _labels(df: pd.DataFrame) -> list[str]:
    """Readable product labels for chart axes: "85123A WHITE HANGING HEART T-LI..."."""
    return [
        f"{code} {str(desc or '')[:28]}".strip()
        for code, desc in zip(df["stock_code"], df["description"], strict=True)
    ]


# --------------------------------------------------------------------------------------------
# Curated sources
# --------------------------------------------------------------------------------------------

METRICS = ("net_revenue", "revenue", "refunds", "orders", "customers", "units")


def revenue_trend(con, as_of: date, p: dict) -> Dataset:
    """Revenue (or orders, customers, units) per month or week. Optionally the same months
    one year earlier as a second series, for "this year vs last year" comparisons."""
    start, end = _date(p, "start"), _date(p, "end") or as_of
    group_by = _choice(p, "group_by", ("month", "week"), "month")
    metric = _choice(p, "metric", METRICS, "net_revenue")
    country = p.get("country") or None
    check_country(con, country)

    df = revenue_summary(con, as_of, start=start, end=end, group_by=group_by, country=country)
    df = df.rename(columns={"period_or_group": "period"})[["period", metric]]
    if not p.get("compare_previous_year"):
        return Dataset(df, "period", [metric], origin=f"revenue_trend ({metric})")

    if group_by != "month":
        raise ToolInputError("compare_previous_year works with group_by='month' only.")
    if start is None:
        raise ToolInputError("compare_previous_year needs a start date.")
    prev = revenue_summary(con, as_of, start=_shift_year(start, -1), end=_shift_year(end, -1),
                           group_by="month", country=country)  # fmt: skip
    prev["period"] = [_shift_year(d, 1) for d in prev["period_or_group"]]
    current_label, previous_label = f"{start.year}", f"{start.year - 1}"
    if start.year != end.year:
        current_label, previous_label = "selected period", "one year earlier"
    merged = df.rename(columns={metric: current_label}).merge(
        prev[["period", metric]].rename(columns={metric: previous_label}), on="period", how="left"
    )
    return Dataset(merged, "period", [current_label, previous_label],
                   origin=f"revenue_trend ({metric}, vs previous year)")  # fmt: skip


def revenue_by_country(con, as_of: date, p: dict) -> Dataset:
    """Revenue per country: the top N, plus one "Other" row for the rest."""
    metric = _choice(p, "metric", METRICS, "net_revenue")
    limit = _int(p, "limit", 8, 2, 20)
    df = revenue_summary(con, as_of, start=_date(p, "start"), end=_date(p, "end"),
                         group_by="country")  # fmt: skip
    df = df.rename(columns={"period_or_group": "country"}).sort_values(metric, ascending=False)
    top, rest = df.head(limit), df.iloc[limit:]
    out = top[["country", metric]].copy()
    if not rest.empty:
        out.loc[len(out)] = ["Other", rest[metric].sum()]
    return Dataset(out.reset_index(drop=True), "country", [metric],
                   origin=f"revenue_by_country ({metric})")  # fmt: skip


def top_products(con, as_of: date, p: dict) -> Dataset:
    by = _choice(p, "by", tuple(sorted(catalog.RANK_BY)), "net_revenue")
    country = p.get("country") or None
    check_country(con, country)
    df = catalog.top_products(con, as_of, start=_date(p, "start"), end=_date(p, "end"), by=by,
                              country=country, limit=_int(p, "limit", 10, 1, 25))  # fmt: skip
    df["product"] = _labels(df)
    return Dataset(df[["product", by]], "product", [by], origin=f"top_products (by {by})")


def customer_segments(con, as_of: date, p: dict) -> Dataset:
    metric = _choice(p, "metric", ("customers", "revenue"), "customers")
    df = rfm.segment_summary(rfm.customer_rfm(con, as_of))
    return Dataset(df[["segment", metric]], "segment", [metric],
                   origin=f"customer_segments ({metric})")  # fmt: skip


def product_trend(con, as_of: date, p: dict) -> Dataset:
    code = str(p.get("stock_code") or "").strip().upper()
    if not code:
        raise ToolInputError("product_trend needs a stock_code.")
    check_product(con, code)
    metric = _choice(p, "metric", ("units", "net_units", "revenue", "orders"), "units")
    freq = _choice(p, "group_by", ("month", "week"), "month")
    df = forecast.demand_history(con, as_of, stock_code=code, freq=freq, start=_date(p, "start"))
    return Dataset(df[["period", metric]], "period", [metric],
                   origin=f"product_trend ({code}, {metric})")  # fmt: skip


def demand_forecast(con, as_of: date, p: dict) -> Dataset:
    """Forecast for the current month vs what has sold so far, for the top sellers."""
    columns = ["product", "forecast", "sold so far"]
    df = forecast.forecast_top_products(con, as_of, top_n=_int(p, "limit", 8, 1, 20))
    if df.empty:
        return Dataset(pd.DataFrame(columns=columns), "product", columns[1:], origin="forecast")
    df["product"] = _labels(df)
    out = df.rename(columns={"units": "forecast", "sold_so_far": "sold so far"})
    return Dataset(out[columns], "product", columns[1:], origin="demand_forecast")


def price_points(con, as_of: date, p: dict) -> Dataset:
    code = str(p.get("stock_code") or "").strip().upper()
    if not code:
        raise ToolInputError("price_points needs a stock_code.")
    check_product(con, code)
    df = pricing.price_points(con, as_of, code, start=_date(p, "start"))
    df["price"] = [f"£{v:.2f}" for v in df["price"]]
    return Dataset(df[["price", "units"]], "price", ["units"], origin=f"price_points ({code})")


def order_value_distribution(con, as_of: date, p: dict) -> Dataset:
    country = p.get("country") or None
    check_country(con, country)
    df = order_values(con, as_of, start=_date(p, "start"), end=_date(p, "end"), country=country)
    return Dataset(df[["order_value"]], "order_value", [], origin="order_values")


CURATED = {
    "revenue_trend": revenue_trend,
    "revenue_by_country": revenue_by_country,
    "top_products": top_products,
    "customer_segments": customer_segments,
    "product_trend": product_trend,
    "demand_forecast": demand_forecast,
    "price_points": price_points,
    "order_values": order_value_distribution,
}

# Shown to the agent in the tool description: what each source takes.
CURATED_HELP = {
    "revenue_trend": "start, end, group_by (month|week), metric, country, compare_previous_year",
    "revenue_by_country": "start, end, metric, limit (top N + Other)",
    "top_products": "start, end, by (net_revenue|revenue|units|orders), country, limit",
    "customer_segments": "metric (customers|revenue)",
    "product_trend": "stock_code, start, group_by, metric (units|net_units|revenue|orders)",
    "demand_forecast": "limit (top sellers: forecast vs sold so far, current month)",
    "price_points": "stock_code, start",
    "order_values": "start, end, country (one value per order: use a histogram)",
}


# --------------------------------------------------------------------------------------------
# SQL written on the fly
# --------------------------------------------------------------------------------------------


def sql_dataset(con: duckdb.DuckDBPyConnection, data: dict) -> Dataset:
    sql = str(data.get("sql") or "")
    try:
        safe_sql = guard_sql(sql, max_rows=MAX_SQL_ROWS)
    except UnsafeSQLError as err:
        raise ToolInputError(f"Chart SQL rejected: {err}") from err
    df = con.execute(safe_sql).df(date_as_object=True)

    x = data.get("x")
    y = data.get("y") or []
    y = [y] if isinstance(y, str) else list(y)
    series = data.get("series") or None
    missing = [c for c in [x, *y, series] if c and c not in df.columns]
    if not x or missing:
        raise ToolInputError(
            f"SQL charts need x (and y) to be result columns. Missing: {missing or ['x']}. "
            f"The query returned: {list(df.columns)}."
        )
    for col in y or [x]:  # a histogram's x must be numeric; otherwise y must be
        df[col] = pd.to_numeric(df[col], errors="coerce")
        if df[col].isna().all() and len(df):
            raise ToolInputError(f"Column {col!r} has no numbers to plot.")
    return Dataset(df, x, y, series, origin=safe_sql)


# --------------------------------------------------------------------------------------------


def fetch_dataset(con: duckdb.DuckDBPyConnection, as_of: date, data: dict[str, Any]) -> Dataset:
    """Resolve one chart's data spec: {"source": ..., "params": {...}} or {"sql": ..., ...}."""
    has_source, has_sql = bool(data.get("source")), bool(data.get("sql"))
    if has_source == has_sql:
        raise ToolInputError("Each chart needs exactly one of data.source or data.sql.")
    if has_sql:
        return sql_dataset(con, data)
    name = data["source"]
    if name not in CURATED:
        raise ToolInputError(f"Unknown source {name!r}. Use one of {sorted(CURATED)}.")
    return CURATED[name](con, as_of, dict(data.get("params") or {}))
