"""Tools for the pricing_strategist spoke: price history, price tiers, live exchange rates."""

from __future__ import annotations

from datetime import date
from typing import Any

from claude_agent_sdk import SdkMcpTool, ToolAnnotations, tool

from scc.analytics import pricing
from scc.integrations.fx import FxRates, normalize_currencies
from scc.tools._common import (
    ToolContext,
    ToolInputError,
    check_product,
    ok,
    opt_date,
    opt_str,
    records,
    run_query,
    safe,
    stock_code,
)
from scc.tools.schemas import DATE, STOCK_CODE, obj

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# The FX tool calls the internet, so it is "open world"; it is still read-only.
READ_ONLY_WEB = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

CURRENCIES = {
    "type": "array",
    "items": {"type": "string", "pattern": "^[A-Za-z]{3}$"},
    "minItems": 1,
    "maxItems": 10,
    "description": "ISO currency codes, e.g. ['EUR', 'USD'].",
}
FX_DATE = {
    "type": "string",
    "description": (
        "'as_of' (default: the rate on the reference date, consistent with the data), "
        "'latest' (today's rate), or a date YYYY-MM-DD."
    ),
}
TIERS_NOTE = (
    "This is a wholesaler: one product sells at several unit prices at once, with lower "
    "prices for larger quantities. avg_price is weighted by units (revenue / units). In "
    "price_points, a low price with a high avg_line_quantity is a bulk tier, not a discount."
)


def _fx_date(ctx: ToolContext, args: dict) -> date | None:
    """Resolve the fx_date argument. None means 'latest'."""
    raw = (opt_str(args, "fx_date") or "as_of").lower()
    if raw == "as_of":
        return ctx.as_of
    if raw == "latest":
        return None
    return opt_date({"fx_date": raw}, "fx_date")


async def _rates(ctx: ToolContext, currencies: list[str], on: date | None) -> FxRates:
    try:
        codes = normalize_currencies(currencies)
    except ValueError as err:
        raise ToolInputError(str(err)) from err
    return await ctx.fx.rates(codes, base="GBP", on=on)


def build_tools(ctx: ToolContext) -> list[SdkMcpTool[Any]]:
    @tool(
        "price_history",
        "A product's monthly average (unit-weighted), min and max price, units and revenue, "
        "plus its price points (units sold at each distinct price). Optionally converts the "
        "latest average price into other currencies using exchange rates.",
        obj(
            {
                "stock_code": STOCK_CODE,
                "start": DATE | {"description": "First day included. Default: all history."},
                "currencies": CURRENCIES,
                "fx_date": FX_DATE,
            },
            required=["stock_code"],
        ),
        annotations=READ_ONLY_WEB,
    )
    @safe
    async def price_history(args: dict) -> dict:
        code, start = stock_code(args), opt_date(args, "start")

        def work(con):
            check_product(con, code)
            return (
                pricing.price_history(con, ctx.as_of, code, start=start),
                pricing.price_points(con, ctx.as_of, code, start=start),
            )

        history, points = await run_query(ctx, work)
        monthly, _ = records(history, ctx.max_rows)
        # The 15 busiest price points are enough to show the tiers.
        busiest = points.sort_values(["units", "price"], ascending=[False, True]).head(15)
        tiers, _ = records(busiest.sort_values("price"), ctx.max_rows)

        converted = None
        if args.get("currencies") and not history.empty:
            fx = await _rates(ctx, args["currencies"], _fx_date(ctx, args))
            latest_price = float(history.iloc[-1]["avg_price"])
            converted = {
                "latest_month": history.iloc[-1]["month"],
                "avg_price_gbp": latest_price,
                "prices": {cur: fx.convert(latest_price, cur) for cur in fx.rates},
                "fx": fx.to_dict(),
            }
        return ok(
            ctx,
            stock_code=code,
            monthly=monthly,
            price_points=tiers,
            converted=converted,
            notes=[TIERS_NOTE],
        )

    @tool(
        "fx_rates",
        "Exchange rates from GBP (the shop's currency) to other currencies, from the European "
        "Central Bank via the Frankfurter API. Use to quote prices for foreign markets.",
        obj({"currencies": CURRENCIES, "fx_date": FX_DATE}, required=["currencies"]),
        annotations=READ_ONLY_WEB,
    )
    @safe
    async def fx_rates(args: dict) -> dict:
        on = _fx_date(ctx, args)
        fx = await _rates(ctx, args["currencies"], on)
        notes = []
        if on is not None and fx.date != on:
            notes.append(f"No rates are published for {on}; these are from {fx.date}.")
        return ok(ctx, result=fx.to_dict(), notes=notes or None)

    return [price_history, fx_rates]
