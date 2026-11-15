"""Revenue summaries: gross sales, refunds and net revenue, overall or grouped.

This answers the most common seller questions ("revenue by month", "how did Germany do in
November?") with one deterministic function. The sales analyst can still write SQL for
anything unusual, but routine figures come from here, so they are always computed the same way.

Definitions (see the views in scc.data.db):
  revenue       gross product sales (``sales`` view), GBP
  refunds       value of cancelled product lines (``returns`` view), GBP
  net_revenue   revenue - refunds
  orders        distinct sales invoices
  customers     distinct known customer IDs (guests are not counted)
"""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd

# group_by option -> SQL expression for the group key. Only these fixed strings reach SQL.
GROUPINGS: dict[str, str] = {
    "total": "'all'",
    "month": "CAST(DATE_TRUNC('month', invoice_day) AS DATE)",
    "week": "CAST(DATE_TRUNC('week', invoice_day) AS DATE)",
    "country": "country",
}


def list_countries(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute("SELECT DISTINCT country FROM sales ORDER BY 1").fetchall()]


def revenue_summary(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    *,
    start: date | None = None,
    end: date | None = None,
    group_by: str = "total",
    country: str | None = None,
) -> pd.DataFrame:
    """Gross, refunds and net revenue in [start, end] (inclusive; ``end`` defaults to as_of).

    Columns: period_or_group, revenue, refunds, net_revenue, orders, customers, units.
    Grouped by country, the rows are sorted by net revenue (largest first); otherwise by time.
    """
    if group_by not in GROUPINGS:
        raise ValueError(f"group_by must be one of {sorted(GROUPINGS)}")
    end = min(end, as_of) if end else as_of
    key = GROUPINGS[group_by]
    filters = """
        invoice_day <= $end
        AND ($start IS NULL OR invoice_day >= $start)
        AND ($country IS NULL OR country = $country)
    """
    order = "net_revenue DESC, grp" if group_by == "country" else "grp"
    return con.execute(
        f"""
        WITH sold AS (
            SELECT {key} AS grp,
                   SUM(revenue)                    AS revenue,
                   COUNT(DISTINCT invoice)         AS orders,
                   COUNT(DISTINCT customer_id)     AS customers,
                   CAST(SUM(quantity) AS BIGINT)   AS units
            FROM sales WHERE {filters}
            GROUP BY grp
        ),
        refunded AS (
            SELECT {key} AS grp, SUM(refund_value) AS refunds
            FROM returns WHERE {filters}
            GROUP BY grp
        )
        SELECT
            grp                                              AS period_or_group,
            COALESCE(s.revenue, 0)                           AS revenue,
            COALESCE(r.refunds, 0)                           AS refunds,
            COALESCE(s.revenue, 0) - COALESCE(r.refunds, 0)  AS net_revenue,
            COALESCE(s.orders, 0)                            AS orders,
            COALESCE(s.customers, 0)                         AS customers,
            COALESCE(s.units, 0)                             AS units
        FROM sold AS s
        FULL JOIN refunded AS r USING (grp)
        ORDER BY {order}
        """,
        {"start": start, "end": end, "country": country},
    ).df(date_as_object=True)
