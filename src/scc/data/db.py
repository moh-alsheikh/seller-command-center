"""The analytical database: its views, its documentation, and read-only connections to it.

Layout of ``retail.duckdb`` (built once by ``scc.data.ingest``):

    transactions   TABLE  every cleaned invoice line, including postage, fees and cancellations
    dataset_info   TABLE  one row of metadata: source, row counts, date range, currency
    sales          VIEW   real product sales only (what "revenue" means everywhere in the app)
    returns        VIEW   product lines on cancellation invoices
    products       VIEW   one row per product with lifetime totals

The view definitions below are the single source of truth for the business rules. The analytics
functions, the agents' SQL and the eval reference queries all read the same views, so a number
such as "revenue" means the same thing everywhere.

Design rule 5 (SQL is read-only, enforced twice): this module provides the first layer. Every
runtime connection is opened with ``read_only=True`` and ``enable_external_access=False``. Even
a query that slips past the SQL guard cannot write, attach other databases, install extensions
or read files from disk.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from scc.config import get_settings

if TYPE_CHECKING:
    from scc.config import Settings

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------------
# Views: the business rules
# --------------------------------------------------------------------------------------------

# A line counts as a sale when it is a real product (not postage, fees or adjustments), is not
# on a cancellation invoice, and has a positive quantity and price. Zero-price lines in the raw
# data are stock write-offs ("damaged", "check", "?") rather than sales.
SALES_VIEW = """
CREATE OR REPLACE VIEW sales AS
SELECT
    invoice,
    stock_code,
    description,
    quantity,
    price,
    quantity * price             AS revenue,
    invoice_date,
    CAST(invoice_date AS DATE)   AS invoice_day,
    customer_id,
    country
FROM transactions
WHERE is_product
  AND NOT is_cancellation
  AND quantity > 0
  AND price > 0
"""

# Cancellation invoices start with "C" and carry negative quantities. We flip the sign so that
# returned units and refund value are positive numbers, which are easier to reason about.
RETURNS_VIEW = """
CREATE OR REPLACE VIEW returns AS
SELECT
    invoice,
    stock_code,
    description,
    -quantity                    AS quantity_returned,
    price,
    -quantity * price            AS refund_value,
    invoice_date,
    CAST(invoice_date AS DATE)   AS invoice_day,
    customer_id,
    country
FROM transactions
WHERE is_product
  AND is_cancellation
  AND quantity < 0
"""

# A product's description changes slightly over time in the raw data. We use the description
# that appears on the most sales lines (ties broken alphabetically, so the result is stable).
PRODUCTS_VIEW = """
CREATE OR REPLACE VIEW products AS
WITH name_counts AS (
    SELECT
        stock_code,
        description,
        ROW_NUMBER() OVER (
            PARTITION BY stock_code ORDER BY COUNT(*) DESC, description
        ) AS rank
    FROM sales
    WHERE description IS NOT NULL
    GROUP BY stock_code, description
)
SELECT
    s.stock_code,
    n.description,
    MIN(s.invoice_date)          AS first_sold,
    MAX(s.invoice_date)          AS last_sold,
    CAST(SUM(s.quantity) AS BIGINT) AS units_sold,
    SUM(s.revenue)               AS revenue,
    COUNT(DISTINCT s.invoice)    AS orders,
    MEDIAN(s.price)              AS median_price
FROM sales AS s
LEFT JOIN name_counts AS n
       ON n.stock_code = s.stock_code AND n.rank = 1
GROUP BY s.stock_code, n.description
"""

VIEWS: dict[str, str] = {
    "sales": SALES_VIEW,
    "returns": RETURNS_VIEW,
    "products": PRODUCTS_VIEW,
}

# Tables and views that agent-written SQL may read. The SQL guard enforces this list.
ALLOWED_TABLES: frozenset[str] = frozenset({"transactions", "dataset_info", *VIEWS})

# --------------------------------------------------------------------------------------------
# Documentation shown to the agents (via the schema tool) and to humans (DESIGN.md)
# --------------------------------------------------------------------------------------------

TABLE_DOCS: dict[str, str] = {
    "sales": (
        "Product sales lines. USE THIS FOR REVENUE. Excludes cancellations, postage/fees/"
        "adjustments, zero-price write-offs. revenue = quantity * price, in GBP."
    ),
    "returns": (
        "Product lines on cancellation invoices (invoice starts with 'C'). quantity_returned "
        "and refund_value are positive numbers, in GBP."
    ),
    "products": (
        "One row per product (stock_code) with lifetime sales totals, most common "
        "description and median unit price in GBP."
    ),
    "transactions": (
        "All cleaned invoice lines, including non-product lines (POST, DOT, M, AMAZONFEE...) "
        "and cancellations. Use only when sales/returns cannot answer the question."
    ),
    "dataset_info": "One row of metadata about the dataset (source, row counts, date range).",
}

DATASET_NOTES = """\
Dataset: UCI Online Retail II, a UK online gift retailer selling mostly to wholesalers.
All money is in GBP (pounds sterling). Dates are UK local time.
customer_id is NULL for about 1 in 4 lines (guest checkouts); customer analyses ignore those.
Net revenue = SUM(sales.revenue) - SUM(returns.refund_value) for the same period."""


def coverage_note(con: duckdb.DuckDBPyConnection) -> str:
    """Which dates the data covers, read from dataset_info (never hard-coded).

    The demo build shifts the real 2009-2011 dates forward by whole weeks, so the exact range
    depends on the day the database was built.
    """
    first, last = con.execute(
        "SELECT CAST(MIN(invoice_date) AS DATE), CAST(MAX(invoice_date) AS DATE) FROM transactions"
    ).fetchone()
    shift = 0
    if _has_column(con, "dataset_info", "date_shift_days"):  # absent in pre-shift builds
        shift = con.execute("SELECT MAX(date_shift_days) FROM dataset_info").fetchone()[0] or 0
    note = (
        f"Coverage: {first} to {last}. {last:%B %Y} is a partial month "
        f"({last.day} days); the last complete month is the one before it."
    )
    if shift:
        note += (
            f" Dates are the real transactions moved forward by {shift} days (whole weeks, "
            "so weekdays are real) to give current demo dates; all figures are real."
        )
    return note


def _has_column(con: duckdb.DuckDBPyConnection, table: str, column: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = $t AND column_name = $c",
            {"t": table, "c": column},
        ).fetchone()
    )


def read_as_of(path: Path) -> date:
    """The data's "today": dataset_info.as_of_date, or the last invoice day for old builds."""
    with connect_readonly(path) as con:
        if _has_column(con, "dataset_info", "as_of_date"):
            value = con.execute("SELECT MAX(as_of_date) FROM dataset_info").fetchone()[0]
            if value is not None:
                return value if isinstance(value, date) else value.date()
        return con.execute("SELECT CAST(MAX(invoice_date) AS DATE) FROM transactions").fetchone()[0]


def resolve_as_of(settings: Settings) -> Settings:
    """Return settings with as_of_date filled in from the database (design rule 6).

    One source of truth: the database records the last day in the data when it is built, so
    "today" always matches the data, however the dates were shifted. An explicit
    SCC_AS_OF_DATE still wins. If the database is missing (first start, before ingest), fall
    back to yesterday so the app can start; /health reports the missing database.
    """
    if settings.as_of_date is not None:
        return settings
    try:
        as_of = read_as_of(settings.duckdb_path)
    except DatabaseMissingError:
        as_of = date.today() - timedelta(days=1)
        log.warning("Retail database missing; using %s as the reference date for now", as_of)
    return settings.model_copy(update={"as_of_date": as_of})


def create_views(con: duckdb.DuckDBPyConnection) -> None:
    """Create (or replace) all views. Called by ingest, on a writable connection."""
    for sql in VIEWS.values():
        con.execute(sql)


# --------------------------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------------------------


class DatabaseMissingError(FileNotFoundError):
    """Raised when the DuckDB file has not been built yet."""


@contextmanager
def connect_readonly(path: Path | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open a locked-down, read-only connection, and always close it afterwards.

    Usage::

        with connect_readonly() as con:
            df = con.execute("SELECT COUNT(*) FROM sales").df()

    Opening a DuckDB file takes milliseconds, so we open one connection per request instead
    of sharing one across async tasks. That keeps concurrent requests independent.
    """
    path = Path(path) if path else get_settings().duckdb_path
    if not path.exists():
        raise DatabaseMissingError(
            f"{path} does not exist. Build it first with: uv run python -m scc.data.ingest"
        )
    con = duckdb.connect(
        str(path),
        read_only=True,
        # Blocks read_csv('/etc/...'), ATTACH, COPY ... TO, INSTALL and httpfs. Once set to
        # false it cannot be switched back on from SQL.
        config={"enable_external_access": False},
    )
    try:
        yield con
    finally:
        con.close()


def describe_schema(con: duckdb.DuckDBPyConnection) -> str:
    """Return a compact, human-readable description of the queryable tables and views.

    Column names and types come from DuckDB's catalogue, so the text can never drift from the
    real schema; the descriptions come from ``TABLE_DOCS``.
    """
    rows = con.execute(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'main'
        ORDER BY table_name, ordinal_position
        """
    ).fetchall()

    columns: dict[str, list[str]] = {}
    for table, column, dtype in rows:
        if table in ALLOWED_TABLES:
            columns.setdefault(table, []).append(f"{column} {dtype}")

    # Most useful first: the views the agents should normally query.
    order = ["sales", "returns", "products", "transactions", "dataset_info"]
    parts = [DATASET_NOTES, coverage_note(con), ""]
    for table in order:
        if table in columns:
            parts.append(f"{table}: {TABLE_DOCS[table]}")
            parts.append("  columns: " + ", ".join(columns[table]))
    return "\n".join(parts)
