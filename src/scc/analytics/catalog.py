"""Product lookups: top sellers, one product's profile, and text search.

Used by the listing writer (to know what it is writing about) and by other spokes when the
seller names a product in words ("the white heart t-light holder") instead of by stock code.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd

# Columns a ranking may use. The default is net revenue: the raw data contains huge orders
# that were cancelled the same day (e.g. 80,995 units of 23843 on 2011-12-09, the original
# UCI date). Ranked by gross revenue, such a cancelled order would be "the best seller of the
# year".
RANK_BY: frozenset[str] = frozenset({"net_revenue", "revenue", "units", "orders"})


def top_products(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    *,
    start: date | None = None,
    end: date | None = None,
    by: str = "net_revenue",
    country: str | None = None,
    limit: int = 10,
) -> pd.DataFrame:
    """Best-selling products in [start, end] (both inclusive; ``end`` defaults to ``as_of``).

    ``by``: 'net_revenue' (default; gross revenue minus refunds in the same window),
    'revenue' (gross), 'units' or 'orders'. Ties are broken by stock_code so the ranking is
    stable.

    Columns: rank, stock_code, description, net_revenue, revenue, refund_value, units,
    units_returned, orders.
    """
    if by not in RANK_BY:
        raise ValueError(f"by must be one of {sorted(RANK_BY)}")
    end = min(end, as_of) if end else as_of
    # `by` is checked against RANK_BY above, so only our own column names reach the f-string.
    return con.execute(
        f"""
        WITH sold AS (
            SELECT
                stock_code,
                SUM(revenue)                      AS revenue,
                CAST(SUM(quantity) AS BIGINT)     AS units,
                COUNT(DISTINCT invoice)           AS orders
            FROM sales
            WHERE invoice_day <= $end
              AND ($start IS NULL OR invoice_day >= $start)
              AND ($country IS NULL OR country = $country)
            GROUP BY stock_code
        ),
        refunded AS (
            SELECT
                stock_code,
                SUM(refund_value)                       AS refund_value,
                CAST(SUM(quantity_returned) AS BIGINT)  AS units_returned
            FROM returns
            WHERE invoice_day <= $end
              AND ($start IS NULL OR invoice_day >= $start)
              AND ($country IS NULL OR country = $country)
            GROUP BY stock_code
        ),
        combined AS (
            SELECT
                sold.*,
                COALESCE(r.refund_value, 0)                 AS refund_value,
                COALESCE(r.units_returned, 0)               AS units_returned,
                sold.revenue - COALESCE(r.refund_value, 0)  AS net_revenue
            FROM sold
            LEFT JOIN refunded AS r USING (stock_code)
        )
        SELECT
            ROW_NUMBER() OVER (ORDER BY c.{by} DESC, c.stock_code) AS rank,
            c.stock_code,
            p.description,
            c.net_revenue,
            c.revenue,
            c.refund_value,
            c.units,
            c.units_returned,
            c.orders
        FROM combined AS c
        JOIN products AS p USING (stock_code)
        ORDER BY rank
        LIMIT $limit
        """,
        {"end": end, "start": start, "country": country, "limit": limit},
    ).df(date_as_object=True)


def get_product(con: duckdb.DuckDBPyConnection, stock_code: str) -> dict | None:
    """Profile of one product, or None if the stock code is unknown.

    Includes lifetime totals, the top 5 countries by revenue and the return rate
    (units returned / units sold).
    """
    code = stock_code.strip().upper()
    row = con.execute("SELECT * FROM products WHERE stock_code = $code", {"code": code}).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in con.description]
    product = dict(zip(cols, row, strict=True))

    countries = con.execute(
        """
        SELECT country, SUM(revenue) AS revenue
        FROM sales WHERE stock_code = $code
        GROUP BY country ORDER BY revenue DESC, country LIMIT 5
        """,
        {"code": code},
    ).fetchall()
    (returned,) = con.execute(
        "SELECT COALESCE(SUM(quantity_returned), 0) FROM returns WHERE stock_code = $code",
        {"code": code},
    ).fetchone()

    units = product["units_sold"] or 0
    product["top_countries"] = [{"country": c, "revenue": r} for c, r in countries]
    product["units_returned"] = int(returned)
    product["return_rate"] = (returned / units) if units else 0.0
    return product


def search_products(con: duckdb.DuckDBPyConnection, text: str, limit: int = 10) -> pd.DataFrame:
    """Case-insensitive search by description words or stock code, best sellers first.

    Every word must appear: "heart holder" matches "WHITE HANGING HEART T-LIGHT HOLDER".
    """
    words = [w for w in text.upper().split() if w]
    if not words:
        return pd.DataFrame(columns=["stock_code", "description", "units_sold", "revenue"])
    return con.execute(
        """
        SELECT stock_code, description, units_sold, revenue
        FROM products
        WHERE stock_code = $code
           OR list_bool_and(
                  list_transform($words, lambda w: contains(upper(coalesce(description, '')), w))
              )
        ORDER BY revenue DESC, stock_code
        LIMIT $limit
        """,
        {"words": words, "code": text.strip().upper(), "limit": limit},
    ).df(date_as_object=True)
