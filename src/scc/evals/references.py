"""Ground truth for accuracy cases, computed when the eval runs.

Two kinds of reference:
  * ``value_sql``: plain SQL over the same views the agents use (sales, returns, products).
    It is written independently of the tools' code, so a tool bug shows up as a failed case.
  * ``value_ref``: a named function below, for metrics whose definition lives in the tested
    analytics code (RFM segments, forecasts). Re-implementing those in SQL would only test
    two copies of the same idea against each other.

Nothing is hard-coded: if the data or a definition changes, the expected values change too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import duckdb

from scc.analytics import cohorts, forecast, pricing, rfm
from scc.integrations.fx import FxClient


async def rfm_segment_size(
    con: duckdb.DuckDBPyConnection, as_of: date, fx: FxClient, *, segment: str
) -> int:
    df = rfm.customer_rfm(con, as_of)
    return int((df["segment"] == segment).sum())


async def repeat_rate_pct(
    con: duckdb.DuckDBPyConnection, as_of: date, fx: FxClient, *, start: str
) -> float:
    result = cohorts.repeat_customer_rate(con, as_of, start=date.fromisoformat(start))
    return result["repeat_rate"] * 100


async def forecast_units(
    con: duckdb.DuckDBPyConnection, as_of: date, fx: FxClient, *, stock_code: str
) -> float:
    [first] = forecast.forecast_product(con, as_of, stock_code)
    return first.units


async def month_avg_price_in(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    fx: FxClient,
    *,
    stock_code: str,
    month: str,
    currency: str,
) -> float:
    """Unit-weighted average price in one month, converted at the as-of date's rate (live FX)."""
    history = pricing.price_history(con, as_of, stock_code)
    row = history[history["month"] == date.fromisoformat(month)]
    gbp = float(row.iloc[0]["avg_price"])
    rates = await fx.rates([currency], on=as_of)
    return rates.convert(gbp, currency)


REFERENCES: dict[str, Callable[..., Awaitable[Any]]] = {
    "rfm_segment_size": rfm_segment_size,
    "repeat_rate_pct": repeat_rate_pct,
    "forecast_units": forecast_units,
    "month_avg_price_in": month_avg_price_in,
}


def sql_scalar(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    row = con.execute(sql).fetchone()
    if row is None:
        raise ValueError(f"reference SQL returned no rows: {sql}")
    return row[0]


def sql_column(con: duckdb.DuckDBPyConnection, sql: str) -> list[str]:
    return [str(r[0]) for r in con.execute(sql).fetchall()]


async def compute(
    con: duckdb.DuckDBPyConnection, as_of: date, fx: FxClient, value_sql: str | None,
    value_ref: dict | None,
) -> Any:  # fmt: skip
    if value_sql:
        return sql_scalar(con, value_sql)
    if value_ref:
        fn = REFERENCES[value_ref["fn"]]
        return await fn(con, as_of, fx, **value_ref.get("args", {}))
    return None
