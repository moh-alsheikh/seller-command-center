"""Tools for the inventory_planner spoke: demand history and demand forecasts."""

from __future__ import annotations

from typing import Any

import pandas as pd
from claude_agent_sdk import SdkMcpTool, ToolAnnotations, tool

from scc.analytics import forecast
from scc.tools._common import (
    ToolContext,
    check_country,
    check_product,
    opt_date,
    opt_str,
    run_query,
    safe,
    stock_code,
    table,
)
from scc.tools.schemas import DATE, STOCK_CODE, int_range, obj

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

FORECAST_NOTE = (
    "Method: units in the same month last year x trend (last 3 full months vs the same months "
    "a year earlier, clamped to 0.5-2.0). Fallbacks: seasonal_naive (no trend), "
    "trailing_average (no sales that month last year), no_history. Units are NET of returns. "
    "sold_so_far = units already sold in the forecast month; if it is close to or above the "
    "forecast, demand is running ahead of last year. The data has no stock levels: compare "
    "the forecast with the seller's stock on hand."
)


def build_tools(ctx: ToolContext) -> list[SdkMcpTool[Any]]:
    @tool(
        "demand_history",
        "Units sold, units returned, net units, revenue and orders per week or month, for one "
        "product or the whole shop, optionally for one country.",
        obj(
            {
                "stock_code": STOCK_CODE | {"description": "Leave out for the whole shop."},
                "country": {"type": "string", "description": "Exact country name."},
                "freq": {"type": "string", "enum": ["month", "week"]},
                "start": DATE | {"description": "First day included. Default: all history."},
            }
        ),
        annotations=READ_ONLY,
    )
    @safe
    async def demand_history(args: dict) -> dict:
        code, country = stock_code(args), opt_str(args, "country")
        freq, start = opt_str(args, "freq") or "month", opt_date(args, "start")

        def work(con):
            check_product(con, code)
            check_country(con, country)
            return forecast.demand_history(
                con, ctx.as_of, stock_code=code, country=country, freq=freq, start=start
            )

        df = await run_query(ctx, work)
        notes = ["The last period is partial: data ends on the as-of date."]
        return table(ctx, df, notes=notes, stock_code=code, country=country, freq=freq)

    @tool(
        "forecast_demand",
        "Forecast net unit demand for the next month(s) after the last complete month. Give a "
        "stock_code for one product, or leave it out to forecast the top sellers (ranked by "
        "net units in the last 3 complete months). Each row explains its method and inputs.",
        obj(
            {
                "stock_code": STOCK_CODE | {"description": "Leave out to forecast top sellers."},
                "top_n": int_range(1, 50, 10, "How many top sellers when no stock_code"),
                "horizon": int_range(1, 3, 1, "How many months ahead"),
            }
        ),
        annotations=READ_ONLY,
    )
    @safe
    async def forecast_demand(args: dict) -> dict:
        code = stock_code(args)
        top_n, horizon = int(args.get("top_n") or 10), int(args.get("horizon") or 1)

        def work(con):
            if code is None:
                return forecast.forecast_top_products(con, ctx.as_of, top_n=top_n, horizon=horizon)
            check_product(con, code)
            rows = forecast.forecast_product(con, ctx.as_of, code, horizon)
            return pd.DataFrame([{"stock_code": code, **f.to_dict()} for f in rows])

        df = await run_query(ctx, work)
        return table(
            ctx,
            df,
            notes=[FORECAST_NOTE],
            last_complete_month=forecast.last_complete_month(ctx.as_of),
        )

    return [demand_history, forecast_demand]
