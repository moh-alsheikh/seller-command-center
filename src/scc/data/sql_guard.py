"""Validate SQL written by an agent before it reaches the database.

Design rule 5 (SQL is read-only, enforced twice):
  * Layer 1 is the database connection itself (``read_only=True``, no external access).
    See ``scc.data.db.connect_readonly``.
  * Layer 2 is this module, run from a ``PreToolUse`` hook before the ``run_sql`` tool executes.

Why have two layers when the read-only connection already blocks writes?
  * The guard gives the agent a clear, early explanation ("only SELECT is allowed"), so it can
    fix its query instead of getting a confusing database error.
  * It narrows what can be read: only our own tables and views, with no system catalogue, no
    table functions, and no file paths. That covers risks a read-only flag does not.
  * It caps the result size with a LIMIT, so one query cannot flood the model's context window.
  * If one layer has a bug, the other still holds.

The guard parses SQL with ``sqlglot`` (a real SQL parser) instead of using regexes. A regex for
"starts with SELECT" is easy to bypass, e.g. ``SELECT 1; DROP TABLE x``.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from scc.data.db import ALLOWED_TABLES

DEFAULT_MAX_ROWS = 200

# Statement types that must never appear anywhere in the parsed tree. Only names that exist in
# the installed sqlglot version are used, so a sqlglot upgrade cannot break the import.
_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable",
    "Copy", "Attach", "Detach", "Install", "LoadData", "Export", "Set", "Pragma", "Use",
    "Command", "Transaction", "Commit", "Rollback", "Grant", "Analyze", "Into",
)  # fmt: skip
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name) for name in _FORBIDDEN_NODE_NAMES if hasattr(exp, name)
)

# Scalar functions that reveal settings or touch the file system. DuckDB's read-only,
# no-external-access connection blocks most of them already; this list adds the early,
# readable error and covers settings readers such as current_setting().
FORBIDDEN_FUNCTION_PREFIXES: tuple[str, ...] = (
    "read_", "glob", "getenv", "current_setting", "duckdb_", "pragma_", "sniff_",
    "parquet_", "query", "load", "install",
)  # fmt: skip


class UnsafeSQLError(ValueError):
    """The SQL was rejected. The message is written for the agent, so it can fix the query."""


def _function_name(node: exp.Func) -> str:
    # Known functions have a canonical name; unknown ones (exp.Anonymous) keep the raw name.
    name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
    return name.lower()


def guard_sql(
    sql: str,
    *,
    allowed_tables: frozenset[str] = ALLOWED_TABLES,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> str:
    """Return a safe version of ``sql`` (with a row LIMIT), or raise :class:`UnsafeSQLError`.

    Rules:
      1. Exactly one statement, and it must be a query (SELECT, WITH ... SELECT, UNION ...).
      2. No write, DDL, settings or file statements anywhere inside it.
      3. Only tables from ``allowed_tables`` (plus the query's own CTE names). No schema
         prefixes such as ``information_schema.tables``, no table functions such as
         ``read_csv(...)``, and no file paths such as ``'/etc/passwd'``.
      4. No functions that read settings or files.
      5. A LIMIT of at most ``max_rows`` is added, or an existing larger LIMIT is lowered.
    """
    if not sql or not sql.strip():
        raise UnsafeSQLError("The query is empty.")

    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except ParseError as err:
        first_line = str(err).splitlines()[0]
        raise UnsafeSQLError(f"Could not parse the SQL: {first_line}") from err

    # Rule 1
    if len(statements) != 1:
        raise UnsafeSQLError(
            f"Send exactly one SQL statement (got {len(statements)}). Do not use ';' to chain."
        )
    stmt = statements[0]
    if not isinstance(stmt, exp.Query):
        raise UnsafeSQLError(
            f"Only read queries (SELECT / WITH) are allowed, not {type(stmt).__name__.upper()}."
        )

    # Rule 2
    bad = next(stmt.find_all(*FORBIDDEN_NODES), None)
    if bad is not None:
        raise UnsafeSQLError(f"{type(bad).__name__.upper()} is not allowed in a read query.")

    # Rule 3
    cte_names = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}
    for table in stmt.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise UnsafeSQLError(
                "Table functions and file reads are not allowed in FROM. "
                f"Query one of: {', '.join(sorted(allowed_tables))}."
            )
        if table.args.get("db") or table.args.get("catalog"):
            raise UnsafeSQLError(
                f"Use plain table names without a schema prefix (got {table.sql('duckdb')})."
            )
        name = table.name.lower()
        if name not in allowed_tables and name not in cte_names:
            raise UnsafeSQLError(
                f"Unknown or forbidden table {table.name!r}. "
                f"Query one of: {', '.join(sorted(allowed_tables))}."
            )

    # Rule 4
    for func in stmt.find_all(exp.Func):
        name = _function_name(func)
        if name.startswith(FORBIDDEN_FUNCTION_PREFIXES):
            raise UnsafeSQLError(f"The function {name}() is not allowed.")

    # Rule 5: keep a smaller LIMIT the agent chose; otherwise cap at max_rows.
    limit = stmt.args.get("limit")
    current = limit.expression if limit is not None else None
    keep_existing = (
        isinstance(current, exp.Literal) and current.is_int and int(current.this) <= max_rows
    )
    if not keep_existing:
        stmt = stmt.limit(max_rows)

    return stmt.sql(dialect="duckdb")
