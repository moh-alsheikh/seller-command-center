"""Tests for scc.data.db: the business-rule views and the locked-down connection."""

import duckdb
import pytest

from scc.data.db import (
    DatabaseMissingError,
    connect_readonly,
    describe_schema,
    read_as_of,
    resolve_as_of,
)
from tests.conftest import scalar


def test_sales_view_applies_the_revenue_rules(con):
    assert scalar(con, "SELECT COUNT(*) FROM sales") == 6
    assert scalar(con, "SELECT SUM(revenue) FROM sales") == pytest.approx(100.0)
    # Excluded: postage, the cancellation and the zero-price write-off.
    assert scalar(con, "SELECT COUNT(*) FROM sales WHERE stock_code = 'POST'") == 0
    assert scalar(con, "SELECT COUNT(*) FROM sales WHERE invoice LIKE 'C%'") == 0
    assert scalar(con, "SELECT MIN(price) FROM sales") > 0


def test_returns_view_uses_positive_numbers(con):
    row = con.execute("SELECT stock_code, quantity_returned, refund_value FROM returns").fetchall()
    assert row == [("10001", 2, 4.0)]


def test_products_view(con):
    rows = con.execute(
        "SELECT stock_code, description, units_sold, revenue, orders FROM products "
        "ORDER BY stock_code"
    ).fetchall()
    assert rows == [
        # "RED MUG" is on 2 sales lines, "red mug old" on 1, so the common name wins.
        ("10001", "RED MUG", 35, 60.0, 3),
        ("20002", "CANDLE", 10, 30.0, 2),
        ("30003A", "BAG", 1, 10.0, 1),
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE x AS SELECT 1",
        "DELETE FROM transactions",
        "INSERT INTO transactions SELECT * FROM transactions",
        "SELECT * FROM read_csv('/etc/hosts')",
        "ATTACH '/tmp/other.duckdb'",
        "COPY (SELECT 1) TO '/tmp/out.csv'",
        "SET enable_external_access = true",
    ],
)
def test_connection_blocks_writes_and_file_access(con, sql):
    with pytest.raises(duckdb.Error):
        con.execute(sql)


def test_missing_database_has_a_helpful_error(tmp_path):
    with (
        pytest.raises(DatabaseMissingError, match="scc.data.ingest"),
        connect_readonly(tmp_path / "nope.duckdb"),
    ):
        pass


def test_describe_schema_lists_views_with_types(con):
    text = describe_schema(con)
    assert "GBP" in text
    assert "sales: Product sales lines" in text
    assert "revenue DOUBLE" in text
    assert text.index("sales:") < text.index("transactions:")  # most useful first


def test_coverage_note_comes_from_the_data(con):
    text = describe_schema(con)
    assert "Coverage: 2010-11-05 to 2011-12-09" in text  # the fixture's own range
    assert "December 2011 is a partial month (9 days)" in text


def test_resolve_as_of(retail_db, tmp_path):
    from datetime import date

    from scc.config import Settings

    assert read_as_of(retail_db) == date(2011, 12, 9)  # last invoice day of the fixture
    s = Settings(_env_file=None, data_dir=retail_db.parent)
    assert resolve_as_of(s).as_of_date == date(2011, 12, 9)
    explicit = Settings(_env_file=None, data_dir=retail_db.parent, as_of_date=date(2026, 1, 2))
    assert resolve_as_of(explicit).as_of_date == date(2026, 1, 2)  # an override wins
    missing = Settings(_env_file=None, data_dir=tmp_path / "none")
    assert resolve_as_of(missing).as_of_date < date.today()  # app can still start
