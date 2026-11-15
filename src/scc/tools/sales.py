"""Tools for the sales_analyst spoke: schema, revenue summaries, and guarded read-only SQL."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import SdkMcpTool, ToolAnnotations, tool

from scc.analytics.sales import GROUPINGS, revenue_summary
from scc.data.db import describe_schema
from scc.data.sql_guard import UnsafeSQLError, guard_sql
from scc.tools._common import (
    ToolContext,
    ToolInputError,
    check_country,
    ok,
    opt_date,
    opt_str,
    run_query,
    safe,
    table,
)
from scc.tools.schemas import DATE, obj

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def build_tools(ctx: ToolContext) -> list[SdkMcpTool[Any]]:
    @tool(
        "describe_schema",
        "Describe the tables/views you can query with run_sql: columns, types, and the "
        "business rules (what counts as revenue, refunds, net revenue). Call this before "
        "writing SQL.",
        obj({}),
        annotations=READ_ONLY,
    )
    @safe
    async def describe_schema_tool(args: dict) -> dict:
        text = await run_query(ctx, describe_schema)
        return ok(ctx, schema=text)

    @tool(
        "revenue_summary",
        "Gross revenue, refunds, net revenue, orders, customers and units for a date range, "
        "either as one total or grouped by month, week or country. Prefer this over SQL for "
        "routine revenue questions: the definitions are fixed and tested.",
        obj(
            {
                "start": DATE | {"description": "First day included (YYYY-MM-DD)."},
                "end": DATE | {"description": "Last day included. Defaults to the as-of date."},
                "group_by": {
                    "type": "string",
                    "enum": sorted(GROUPINGS),
                    "description": "total (one row), month, week or country.",
                },
                "country": {
                    "type": "string",
                    "description": "Exact country name, e.g. 'Germany', 'EIRE', 'United Kingdom'.",
                },
            }
        ),
        annotations=READ_ONLY,
    )
    @safe
    async def revenue_summary_tool(args: dict) -> dict:
        start, end = opt_date(args, "start"), opt_date(args, "end")
        country = opt_str(args, "country")
        group_by = opt_str(args, "group_by") or "total"
        if start and end and start > end:
            raise ToolInputError("start must be on or before end.")

        def work(con):
            check_country(con, country)
            return revenue_summary(
                con, ctx.as_of, start=start, end=end, group_by=group_by, country=country
            )

        df = await run_query(ctx, work)
        return table(
            ctx,
            df,
            filters={"start": start, "end": end or ctx.as_of, "country": country},
            group_by=group_by,
        )

    @tool(
        "run_sql",
        "Run ONE read-only DuckDB SELECT query against the views sales, returns, products "
        "(and transactions, dataset_info). Use for questions revenue_summary cannot answer. "
        "Write/DDL statements, table functions and file paths are rejected. Results are "
        f"capped at {ctx.max_rows} rows; aggregate in SQL instead of fetching raw lines.",
        obj(
            {"sql": {"type": "string", "description": "A single SELECT (or WITH ... SELECT)."}},
            required=["sql"],
        ),
        annotations=READ_ONLY,
    )
    @safe
    async def run_sql(args: dict) -> dict:
        sql = opt_str(args, "sql") or ""
        try:
            # Same guard as the PreToolUse hook (milestone 4). Running it here as well means
            # the tool is safe on its own, even if it is ever used without the hook.
            safe_sql = guard_sql(sql, max_rows=ctx.max_rows)
        except UnsafeSQLError as err:
            raise ToolInputError(f"Query rejected: {err}") from err

        df = await run_query(ctx, lambda con: con.execute(safe_sql).df(date_as_object=True))
        hit_cap = len(df) >= ctx.max_rows
        notes = (
            [f"Hit the {ctx.max_rows}-row cap; results may be incomplete. Aggregate further."]
            if hit_cap
            else None
        )
        return table(ctx, df, notes=notes, executed_sql=safe_sql, columns=list(df.columns))

    return [describe_schema_tool, revenue_summary_tool, run_sql]
