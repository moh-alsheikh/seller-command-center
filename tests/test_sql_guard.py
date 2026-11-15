"""Tests for scc.data.sql_guard: the second layer of read-only enforcement (design rule 5)."""

import pytest

from scc.data.sql_guard import UnsafeSQLError, guard_sql

# --- Allowed ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT country, SUM(revenue) FROM sales GROUP BY country",
        "WITH m AS (SELECT stock_code FROM sales) SELECT * FROM m",
        "SELECT 1 AS a UNION ALL SELECT 2",
        "SELECT * FROM products p JOIN sales s USING (stock_code)",
        "FROM sales SELECT country",  # DuckDB's FROM-first syntax
        "select * from SALES",  # table names are case-insensitive
        "SELECT * FROM sales;",  # one trailing semicolon is fine
    ],
)
def test_read_queries_pass(sql):
    assert guard_sql(sql)


def test_limit_is_added_when_missing():
    assert guard_sql("SELECT * FROM sales", max_rows=50).endswith("LIMIT 50")


def test_large_limit_is_capped():
    assert guard_sql("SELECT * FROM sales LIMIT 100000", max_rows=50).endswith("LIMIT 50")


def test_small_limit_is_kept():
    assert guard_sql("SELECT * FROM sales LIMIT 5", max_rows=50).endswith("LIMIT 5")


def test_union_gets_one_outer_limit():
    out = guard_sql("SELECT 1 AS a UNION ALL SELECT 2 ORDER BY a", max_rows=10)
    assert out.endswith("ORDER BY a LIMIT 10")


# --- Rejected -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,reason",
    [
        ("", "empty"),
        ("SELECT 1; DROP TABLE sales", "exactly one"),
        ("DELETE FROM sales", "Only read queries"),
        ("DROP TABLE sales", "Only read queries"),
        ("CREATE TEMP TABLE t AS SELECT 1", "Only read queries"),
        ("INSERT INTO sales VALUES (1)", "Only read queries"),
        ("ATTACH 'x.db'", "Only read queries"),
        ("PRAGMA version", "Only read queries"),
        ("SET threads = 1", "Only read queries"),
        ("SELECT * FROM read_csv('/etc/hosts')", "Table functions"),
        ("SELECT * FROM '/etc/passwd'", "Unknown or forbidden table"),
        ("SELECT * FROM duckdb_settings()", "Table functions"),
        ("SELECT * FROM information_schema.tables", "schema prefix"),
        ("SELECT * FROM main.sales", "schema prefix"),
        ("SELECT * FROM customers_secret", "Unknown or forbidden table"),
        ("SELECT current_setting('home_directory')", "not allowed"),
        ("SELECT getenv('ANTHROPIC_API_KEY')", "not allowed"),
        ("SELECT * FROM sales WHERE x IN (SELECT * FROM read_parquet('a'))", "Table functions"),
        ("SELECT FROM WHERE", "parse"),
    ],
)
def test_unsafe_queries_are_rejected(sql, reason):
    with pytest.raises(UnsafeSQLError, match=reason):
        guard_sql(sql)


def test_cte_names_are_allowed_but_only_for_that_query():
    guard_sql("WITH secret AS (SELECT * FROM sales) SELECT * FROM secret")
    with pytest.raises(UnsafeSQLError):
        guard_sql("SELECT * FROM secret")


def test_error_lists_allowed_tables_so_the_agent_can_recover():
    with pytest.raises(UnsafeSQLError) as err:
        guard_sql("SELECT * FROM orders")
    assert "sales" in str(err.value) and "products" in str(err.value)
