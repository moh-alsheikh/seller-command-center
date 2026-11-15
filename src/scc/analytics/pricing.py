"""Price history for one product.

This shop sells mostly to wholesalers, so one product often has several prices at the same
time: the unit price drops for larger quantities (e.g. 2.95 for a few, 2.55 for a case).
That is why we report both:
  * price_history  the volume-weighted average price per month (revenue / units), and
  * price_points   how many units sold at each distinct price, which shows the tiers.

All prices are in GBP. Currency conversion (live FX rates) happens in the pricing tool, not
here, so these functions stay deterministic and offline.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd


def price_history(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    stock_code: str,
    *,
    start: date | None = None,
) -> pd.DataFrame:
    """Monthly price statistics. Columns: month, avg_price, min_price, max_price, units, revenue.

    ``avg_price`` is weighted by units (revenue / units), which is what the seller actually
    earned per unit. A simple mean of line prices would over-weight small orders.
    """
    return con.execute(
        """
        SELECT
            CAST(DATE_TRUNC('month', invoice_day) AS DATE) AS month,
            SUM(revenue) / SUM(quantity) AS avg_price,
            MIN(price)                   AS min_price,
            MAX(price)                   AS max_price,
            CAST(SUM(quantity) AS BIGINT) AS units,
            SUM(revenue)                 AS revenue
        FROM sales
        WHERE stock_code = $code
          AND invoice_day <= $as_of
          AND ($start IS NULL OR invoice_day >= $start)
        GROUP BY month
        ORDER BY month
        """,
        {"code": stock_code, "as_of": as_of, "start": start},
    ).df(date_as_object=True)


def price_points(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    stock_code: str,
    *,
    start: date | None = None,
) -> pd.DataFrame:
    """Units and revenue per distinct unit price, cheapest first.

    Columns: price, units, lines, avg_line_quantity, revenue, first_seen, last_seen.
    A low price with a high ``avg_line_quantity`` is a bulk tier, not a discount.
    """
    return con.execute(
        """
        SELECT
            price,
            CAST(SUM(quantity) AS BIGINT) AS units,
            COUNT(*)                     AS lines,
            AVG(quantity)                AS avg_line_quantity,
            SUM(revenue)                 AS revenue,
            MIN(invoice_day)             AS first_seen,
            MAX(invoice_day)             AS last_seen
        FROM sales
        WHERE stock_code = $code
          AND invoice_day <= $as_of
          AND ($start IS NULL OR invoice_day >= $start)
        GROUP BY price
        ORDER BY price
        """,
        {"code": stock_code, "as_of": as_of, "start": start},
    ).df(date_as_object=True)
