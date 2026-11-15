"""Shared fixtures: a tiny, hand-checkable retail database built through the real ingest code.

The rows below use the raw UCI column names, so the fixture exercises the same normalize ->
add_flags -> build_database path as the real ingest. Every expected number in the tests can be
worked out by hand from this table:

  sales revenue   20 + 12 + 10 + 30 + 18 + 10 = 100.0 over 6 sales lines
  returns         1 line: 2 units of 10001, refund 4.0
  excluded        POST (not a product), a zero-price write-off, the cancellation itself
  products        10001 (35 units, 60.0), 20002 (10 units, 30.0), 30003A (1 unit, 10.0)
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from scc.data.ingest import add_flags, build_database, normalize

AS_OF = date(2011, 12, 9)

RAW_ROWS = [
    # Invoice,  StockCode, Description,   Qty, InvoiceDate,                 Price, Cust, Country
    ("500001", "10001", "RED MUG", 10, datetime(2010, 11, 5, 10), 2.0, 1.0, "United Kingdom"),
    ("500001", "POST", "POSTAGE", 1, datetime(2010, 11, 5, 10), 5.0, 1.0, "United Kingdom"),
    ("500002", "20002", "CANDLE", 4, datetime(2010, 12, 3, 9), 3.0, 2.0, "Germany"),
    ("500003", "10001", "RED MUG ", 5, datetime(2011, 10, 15, 11), 2.0, 1.0, "United Kingdom"),
    ("500004", "10001", "red mug old", 20, datetime(2011, 11, 20, 14), 1.5, 2.0, "Germany"),
    ("C500005", "10001", "RED MUG", -2, datetime(2011, 11, 21, 9), 2.0, 2.0, "Germany"),
    ("500006", "20002", "CANDLE", 6, datetime(2011, 12, 8, 16), 3.0, None, "United Kingdom"),
    ("500007", "20002", "damaged", -3, datetime(2011, 11, 1, 8), 0.0, None, "United Kingdom"),
    ("500008", "30003a", "BAG", 1, datetime(2011, 12, 9, 12), 10.0, 3.0, "France"),
]
RAW_COLUMNS = [
    "Invoice", "StockCode", "Description", "Quantity", "InvoiceDate", "Price", "Customer ID",
    "Country",
]  # fmt: skip


def raw_frame(rows=RAW_ROWS) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=RAW_COLUMNS)
    # Match what pandas.read_excel returns for the real file.
    return df.astype({"Invoice": str, "StockCode": str, "Customer ID": "float64"})


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch):
    """Keep the developer's .env out of every test.

    Importing chainlit runs load_dotenv(), which copies .env (API key, login, tokens) into
    os.environ for the whole test process. Tests must behave the same on any machine, so
    the app's variables are removed before each test; tests that need one set it themselves.
    """
    import os

    for name in list(os.environ):
        if name.startswith("SCC_") or name in ("ANTHROPIC_API_KEY", "CHAINLIT_AUTH_SECRET"):
            monkeypatch.delenv(name)


@pytest.fixture(scope="session")
def retail_db(tmp_path_factory) -> Path:
    """Path to a built test database (read it with connect_readonly)."""
    path = tmp_path_factory.mktemp("data") / "retail.duckdb"
    df = add_flags(normalize(raw_frame()))
    build_database(df, path, {"source": "test fixture", "currency": "GBP", "rows": len(df)})
    return path


@pytest.fixture
def con(retail_db):
    """A read-only connection to the test database, closed after each test."""
    from scc.data.db import connect_readonly

    with connect_readonly(retail_db) as c:
        yield c


@pytest.fixture
def as_of() -> date:
    return AS_OF


def scalar(c: duckdb.DuckDBPyConnection, sql: str):
    return c.execute(sql).fetchone()[0]
