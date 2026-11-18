"""Customer cohorts and retention.

A customer's cohort is the month of their first purchase. Retention for "month k" is the share
of the cohort that bought again k months after that first month (k = 0 is always 100%).

Caveat: the first month in the data also contains every customer who had been buying before
the data starts (left-censoring), so its retention looks higher than a real "new customer"
cohort. The tools mention this whenever that cohort is shown.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd


def cohort_retention(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    *,
    max_months: int = 12,
) -> pd.DataFrame:
    """Long-format retention table.

    Columns: cohort_month, months_since_first, active_customers, cohort_size, retention_rate.
    """
    return con.execute(
        """
        WITH activity AS (
            SELECT DISTINCT customer_id, DATE_TRUNC('month', invoice_day) AS month
            FROM sales
            WHERE customer_id IS NOT NULL AND invoice_day <= $as_of
        ),
        first_month AS (
            SELECT customer_id, MIN(month) AS cohort_month
            FROM activity
            GROUP BY customer_id
        ),
        joined AS (
            SELECT
                f.cohort_month,
                DATE_DIFF('month', f.cohort_month, a.month) AS months_since_first,
                a.customer_id
            FROM activity AS a
            JOIN first_month AS f USING (customer_id)
        ),
        sizes AS (
            SELECT cohort_month, COUNT(*) AS cohort_size
            FROM first_month
            GROUP BY cohort_month
        )
        SELECT
            CAST(j.cohort_month AS DATE)              AS cohort_month,
            j.months_since_first,
            COUNT(*)                                  AS active_customers,
            s.cohort_size,
            COUNT(*) / s.cohort_size                  AS retention_rate
        FROM joined AS j
        JOIN sizes AS s USING (cohort_month)
        WHERE j.months_since_first <= $max_months
        GROUP BY j.cohort_month, j.months_since_first, s.cohort_size
        ORDER BY cohort_month, months_since_first
        """,
        {"as_of": as_of, "max_months": max_months},
    ).df(date_as_object=True)


def retention_matrix(long: pd.DataFrame) -> pd.DataFrame:
    """Pivot :func:`cohort_retention` output: one row per cohort, one column per month offset."""
    if long.empty:
        return pd.DataFrame()
    matrix = long.pivot(index="cohort_month", columns="months_since_first", values="retention_rate")
    matrix.columns = [f"m{int(c)}" for c in matrix.columns]
    sizes = long.groupby("cohort_month")["cohort_size"].first()
    matrix.insert(0, "cohort_size", sizes)
    return matrix.reset_index()


def repeat_customer_rate(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    *,
    start: date | None = None,
) -> dict[str, float | int]:
    """Share of customers with two or more invoices, within [start, as_of].

    "Repeat" means at least two distinct invoices in the window, not two lines on one invoice.
    """
    customers, repeaters = con.execute(
        """
        WITH per_customer AS (
            SELECT customer_id, COUNT(DISTINCT invoice) AS orders
            FROM sales
            WHERE customer_id IS NOT NULL
              AND invoice_day <= $as_of
              AND ($start IS NULL OR invoice_day >= $start)
            GROUP BY customer_id
        )
        SELECT COUNT(*), COUNT(*) FILTER (WHERE orders >= 2) FROM per_customer
        """,
        {"as_of": as_of, "start": start},
    ).fetchone()
    return {
        "customers": int(customers),
        "repeat_customers": int(repeaters),
        "repeat_rate": (repeaters / customers) if customers else 0.0,
    }
