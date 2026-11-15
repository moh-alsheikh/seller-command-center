"""Demand history and a simple, explainable demand forecast.

The data has no stock levels, so "what should I restock?" is answered as "how many units will
I probably sell?". The seller compares that with what they have on hand.

Forecast method: seasonal naive with a trend adjustment.

    forecast(month M) = units(M one year earlier) x trend
    trend             = units(last 3 complete months) / units(same 3 months one year earlier)

The trend is clamped to [0.5, 2.0] so one unusual quarter cannot produce a silly number.
Fallbacks, in order: no sales a year before the trend window -> last year's figure unchanged
("seasonal_naive"); no sales in that month last year (e.g. a new product) -> the average of
the last 3 complete months ("trailing_average"); no sales at all -> 0 ("no_history").

Why not ARIMA, Prophet or a neural model? This business is strongly seasonal (it sells
Christmas gifts) and has only two years of history. Seasonal naive is a standard,
hard-to-beat baseline in that situation. It is also easy to explain in one sentence, which
matters because the agent has to explain the number to the seller. Each forecast returns
its ``method``, ``last_year_units`` and ``trend`` so the explanation can quote them.

All unit counts are NET units (sold minus returned); see ``monthly_units``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import duckdb
import pandas as pd

TREND_MIN, TREND_MAX = 0.5, 2.0
TREND_WINDOW_MONTHS = 3


# --------------------------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------------------------


def demand_history(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    *,
    stock_code: str | None = None,
    country: str | None = None,
    freq: str = "month",
    start: date | None = None,
) -> pd.DataFrame:
    """Sales and returns per period (``freq``: 'week' or 'month').

    Leave ``stock_code`` empty for the whole shop. Columns: period, units, units_returned,
    net_units, revenue, refund_value, orders. ``net_units`` can be negative in a month where
    returns of earlier sales outweigh new sales.
    """
    if freq not in ("week", "month"):
        raise ValueError("freq must be 'week' or 'month'")
    # Same filters for sales and returns. Only parameters ($...) carry caller input; `freq`
    # was checked against a fixed list above.
    filters = """
          invoice_day <= $as_of
          AND ($start IS NULL OR invoice_day >= $start)
          AND ($stock_code IS NULL OR stock_code = $stock_code)
          AND ($country IS NULL OR country = $country)
    """
    return con.execute(
        f"""
        WITH sold AS (
            SELECT
                CAST(DATE_TRUNC('{freq}', invoice_day) AS DATE) AS period,
                SUM(quantity)                AS units,
                SUM(revenue)                 AS revenue,
                COUNT(DISTINCT invoice)      AS orders
            FROM sales WHERE {filters}
            GROUP BY period
        ),
        returned AS (
            SELECT
                CAST(DATE_TRUNC('{freq}', invoice_day) AS DATE) AS period,
                SUM(quantity_returned)       AS units_returned,
                SUM(refund_value)            AS refund_value
            FROM returns WHERE {filters}
            GROUP BY period
        )
        SELECT
            period,
            CAST(COALESCE(s.units, 0) AS BIGINT)                AS units,
            CAST(COALESCE(r.units_returned, 0) AS BIGINT)       AS units_returned,
            CAST(COALESCE(s.units, 0) - COALESCE(r.units_returned, 0) AS BIGINT) AS net_units,
            COALESCE(s.revenue, 0)                              AS revenue,
            COALESCE(r.refund_value, 0)                         AS refund_value,
            COALESCE(s.orders, 0)                               AS orders
        FROM sold AS s
        FULL JOIN returned AS r USING (period)
        ORDER BY period
        """,
        {"as_of": as_of, "start": start, "stock_code": stock_code, "country": country},
    ).df(date_as_object=True)


def monthly_units(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    stock_codes: list[str] | None = None,
) -> pd.DataFrame:
    """Net units (sold minus returned, floored at 0) per (stock_code, month).

    Net, because the raw data has huge orders cancelled the same day (80,995 units of 23843
    on 2011-12-09); counting them as demand would wreck the forecast. Floored at 0 because a
    month dominated by returns of earlier sales does not mean negative demand.

    Columns: stock_code, month, units.
    """
    return con.execute(
        """
        WITH sold AS (
            SELECT stock_code, DATE_TRUNC('month', invoice_day) AS month, SUM(quantity) AS u
            FROM sales
            WHERE invoice_day <= $as_of
              AND ($codes IS NULL OR list_contains($codes, stock_code))
            GROUP BY ALL
        ),
        returned AS (
            SELECT stock_code, DATE_TRUNC('month', invoice_day) AS month,
                   SUM(quantity_returned) AS u
            FROM returns
            WHERE invoice_day <= $as_of
              AND ($codes IS NULL OR list_contains($codes, stock_code))
            GROUP BY ALL
        )
        SELECT
            stock_code,
            CAST(month AS DATE) AS month,
            CAST(GREATEST(COALESCE(s.u, 0) - COALESCE(r.u, 0), 0) AS BIGINT) AS units
        FROM sold AS s
        FULL JOIN returned AS r USING (stock_code, month)
        ORDER BY stock_code, month
        """,
        {"as_of": as_of, "codes": stock_codes},
    ).df(date_as_object=True)


# --------------------------------------------------------------------------------------------
# Forecast (pure functions: no database, easy to test)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MonthForecast:
    month: date
    units: float
    method: str  # "seasonal_naive_x_trend" | "seasonal_naive" | "trailing_average" | "no_history"
    last_year_units: float | None
    trend: float | None
    sold_so_far: float  # units already sold in this month (non-zero for the current month)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["month"] = self.month.isoformat()
        return d


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _add_months(d: date, n: int) -> date:
    total = d.year * 12 + (d.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


def last_complete_month(as_of: date) -> date:
    """The latest month that is fully inside the data. For 2011-12-09 that is 2011-11-01."""
    return _add_months(_month_start(as_of), -1)


def forecast_series(
    units_by_month: pd.Series, as_of: date, horizon: int = 1
) -> list[MonthForecast]:
    """Forecast the next ``horizon`` months after the last complete month.

    ``units_by_month`` is indexed by month-start ``date`` values; missing months count as 0.
    With as_of = 2011-12-09 and horizon = 1, the forecast is for December 2011 (the current,
    partial month). ``sold_so_far`` reports what has already sold in it.
    """

    def units(m: date) -> float:
        return float(units_by_month.get(m, 0.0))

    last_full = last_complete_month(as_of)
    recent = [_add_months(last_full, -i) for i in range(TREND_WINDOW_MONTHS)]
    recent_units = sum(units(m) for m in recent)
    prior_units = sum(units(_add_months(m, -12)) for m in recent)

    trend: float | None = None
    if prior_units > 0:
        trend = min(max(recent_units / prior_units, TREND_MIN), TREND_MAX)

    results: list[MonthForecast] = []
    for step in range(1, horizon + 1):
        target = _add_months(last_full, step)
        last_year_units = units(_add_months(target, -12))
        if last_year_units > 0 and trend is not None:
            value, method = last_year_units * trend, "seasonal_naive_x_trend"
        elif last_year_units > 0:
            # Sold in this month last year, but nothing in the trend window a year earlier,
            # so no trend can be measured: repeat last year's figure unchanged.
            value, method = last_year_units, "seasonal_naive"
        elif recent_units > 0:
            value, method = recent_units / TREND_WINDOW_MONTHS, "trailing_average"
        else:
            value, method = 0.0, "no_history"
        results.append(
            MonthForecast(
                month=target,
                units=round(value, 1),
                method=method,
                last_year_units=last_year_units if last_year_units > 0 else None,
                trend=round(trend, 3) if trend is not None else None,
                sold_so_far=units(target),
            )
        )
    return results


# --------------------------------------------------------------------------------------------
# Convenience wrappers used by the tools
# --------------------------------------------------------------------------------------------


def forecast_product(
    con: duckdb.DuckDBPyConnection, as_of: date, stock_code: str, horizon: int = 1
) -> list[MonthForecast]:
    hist = monthly_units(con, as_of, [stock_code])
    series = pd.Series(hist["units"].to_numpy(), index=list(hist["month"]))
    return forecast_series(series, as_of, horizon)


def forecast_top_products(
    con: duckdb.DuckDBPyConnection, as_of: date, *, top_n: int = 20, horizon: int = 1
) -> pd.DataFrame:
    """Forecast the next month(s) for the ``top_n`` products by net units sold in the last 3
    complete months. One row per (product, month), biggest forecasts first.

    Columns: stock_code, description, recent_units, plus the MonthForecast fields.
    """
    last_full = last_complete_month(as_of)
    window_start = _add_months(last_full, -(TREND_WINDOW_MONTHS - 1))

    # One query for every product's monthly net units (a few thousand products x 25 months is
    # small), then rank in pandas. Ranking and forecasting use the same numbers.
    hist = monthly_units(con, as_of)
    in_window = hist[(hist["month"] >= window_start) & (hist["month"] <= last_full)]
    recent = in_window.groupby("stock_code", as_index=False)["units"].sum()
    recent = recent[recent["units"] > 0]
    top = recent.sort_values(["units", "stock_code"], ascending=[False, True]).head(top_n)
    if top.empty:
        return pd.DataFrame()

    codes = top["stock_code"].tolist()
    names = dict(
        con.execute(
            "SELECT stock_code, description FROM products WHERE list_contains($codes, stock_code)",
            {"codes": codes},
        ).fetchall()
    )
    rows = []
    for code, recent_units in zip(top["stock_code"], top["units"], strict=True):
        h = hist[hist["stock_code"] == code]
        series = pd.Series(h["units"].to_numpy(), index=list(h["month"]))
        for f in forecast_series(series, as_of, horizon):
            rows.append(
                {
                    "stock_code": code,
                    "description": names.get(code),
                    "recent_units": int(recent_units),
                    **f.to_dict(),
                }
            )
    out = pd.DataFrame(rows)
    return out.sort_values(
        ["month", "units", "stock_code"], ascending=[True, False, True]
    ).reset_index(drop=True)
