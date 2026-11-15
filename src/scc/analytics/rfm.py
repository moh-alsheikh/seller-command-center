"""RFM customer segmentation: Recency, Frequency, Monetary.

For each customer, up to the ``as_of`` date:
  * recency_days  days since their last purchase (smaller is better)
  * frequency     number of distinct invoices
  * monetary      total revenue in GBP

Each measure becomes a score from 1 (worst) to 5 (best) using its rank among all customers.
We use ``CUME_DIST`` rather than ``NTILE``: customers with the same value always get the same
score. With NTILE, two customers who each bought once could land in different groups purely
by sort order, which would make the segments arbitrary.

Segments follow the widely used R-F grid (the M score is reported but not used for the
segment name). See ``segment_for``.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd

# (segment, plain-English meaning). Order = display order, best customers first.
SEGMENTS: dict[str, str] = {
    "Champions": "Bought recently, buy often, spend the most.",
    "Loyal customers": "Buy often; respond well to new ranges.",
    "Potential loyalists": "Recent customers with a few orders; nurture them.",
    "New customers": "First order was recent.",
    "Promising": "Recent but infrequent; a nudge could convert them.",
    "Need attention": "Average recency and frequency; at risk of slipping.",
    "About to sleep": "Below-average recency and frequency.",
    "At risk": "Used to buy often but not recently.",
    "Can't lose them": "Were among the best customers, now gone quiet.",
    "Hibernating": "Last order long ago and few orders overall.",
}


def segment_for(r: int, f: int) -> str:
    """Map an R score and F score (each 1-5) to a segment name.

    The grid (rows = R, columns = F):

        R\\F   1          2-3                 4                  5
        5     New        Potential loyal.    Champions          Champions
        4     Promising  Potential loyal.    Loyal              Loyal
        3     About to sleep (F1-2) / Need attention (F3) / Loyal (F4-5)
        1-2   Hibernating (F1-2) / At risk (F3-4) / Can't lose them (F5)
    """
    if r >= 4:
        if f >= 4:
            return "Champions" if r == 5 else "Loyal customers"
        if f >= 2:
            return "Potential loyalists"
        return "New customers" if r == 5 else "Promising"
    if r == 3:
        if f >= 4:
            return "Loyal customers"
        return "Need attention" if f == 3 else "About to sleep"
    # r is 1 or 2
    if f == 5:
        return "Can't lose them"
    return "At risk" if f >= 3 else "Hibernating"


def customer_rfm(con: duckdb.DuckDBPyConnection, as_of: date) -> pd.DataFrame:
    """One row per customer with raw RFM values, 1-5 scores and a segment name."""
    df = con.execute(
        """
        WITH per_customer AS (
            SELECT
                customer_id,
                MAX(invoice_day)                       AS last_purchase,
                DATE_DIFF('day', MAX(invoice_day), CAST($as_of AS DATE)) AS recency_days,
                COUNT(DISTINCT invoice)                AS frequency,
                SUM(revenue)                           AS monetary
            FROM sales
            WHERE customer_id IS NOT NULL
              AND invoice_day <= $as_of
            GROUP BY customer_id
        )
        SELECT
            *,
            -- Older purchase = lower score, so rank recency in DESCENDING order of days.
            CAST(CEIL(5 * CUME_DIST() OVER (ORDER BY recency_days DESC)) AS INTEGER) AS r_score,
            CAST(CEIL(5 * CUME_DIST() OVER (ORDER BY frequency))         AS INTEGER) AS f_score,
            CAST(CEIL(5 * CUME_DIST() OVER (ORDER BY monetary))          AS INTEGER) AS m_score
        FROM per_customer
        ORDER BY customer_id
        """,
        {"as_of": as_of},
    ).df(date_as_object=True)
    df["segment"] = [segment_for(r, f) for r, f in zip(df["r_score"], df["f_score"], strict=True)]
    return df


def segment_summary(rfm: pd.DataFrame) -> pd.DataFrame:
    """Aggregate :func:`customer_rfm` output to one row per segment, best segments first."""
    total_customers = len(rfm)
    total_revenue = rfm["monetary"].sum()
    out = (
        rfm.groupby("segment")
        .agg(
            customers=("customer_id", "size"),
            revenue=("monetary", "sum"),
            avg_recency_days=("recency_days", "mean"),
            avg_orders=("frequency", "mean"),
            avg_spend=("monetary", "mean"),
        )
        .reindex([s for s in SEGMENTS if s in set(rfm["segment"])])
        .reset_index()
    )
    out["customer_share"] = out["customers"] / total_customers if total_customers else 0.0
    out["revenue_share"] = out["revenue"] / total_revenue if total_revenue else 0.0
    out["meaning"] = out["segment"].map(SEGMENTS)
    return out
