"""Tools for the customer_insights spoke: RFM segments, cohorts and repeat-purchase rate."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import SdkMcpTool, ToolAnnotations, tool

from scc.analytics import cohorts, rfm
from scc.tools._common import ToolContext, ok, opt_date, run_query, safe, table
from scc.tools.schemas import DATE, int_range, obj

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

RFM_NOTE = (
    "Scores 1-5 rank customers against each other (5 = most recent / most orders / highest "
    "spend). Guests without a customer ID are excluded."
)


def cohort_note(first_month) -> str:
    """Caveat for the first cohort in the data (its month depends on how the data was dated)."""
    return (
        f"The {first_month:%Y-%m} cohort is the first month in the data, so it includes every "
        "customer who was already buying before the data starts; its retention is overstated. "
        "Compare later cohorts with each other."
    )


def build_tools(ctx: ToolContext) -> list[SdkMcpTool[Any]]:
    @tool(
        "rfm_segments",
        "Segment all customers by Recency, Frequency and Monetary value (RFM) as of the "
        "reference date. Returns each segment's size, revenue share, averages and meaning.",
        obj({}),
        annotations=READ_ONLY,
    )
    @safe
    async def rfm_segments(args: dict) -> dict:
        summary = await run_query(
            ctx, lambda con: rfm.segment_summary(rfm.customer_rfm(con, ctx.as_of))
        )
        return table(ctx, summary, notes=[RFM_NOTE])

    @tool(
        "segment_customers",
        "List the highest-spending customers in one RFM segment, with their recency, order "
        "count and spend. Use for targeted actions (e.g. win back 'At risk' customers).",
        obj(
            {
                "segment": {"type": "string", "enum": list(rfm.SEGMENTS)},
                "limit": int_range(1, 50, 10, "How many customers"),
            },
            required=["segment"],
        ),
        annotations=READ_ONLY,
    )
    @safe
    async def segment_customers(args: dict) -> dict:
        segment, limit = args["segment"], int(args.get("limit") or 10)
        df = await run_query(ctx, lambda con: rfm.customer_rfm(con, ctx.as_of))
        members = df[df["segment"] == segment]
        top = members.sort_values(["monetary", "customer_id"], ascending=[False, True]).head(limit)
        cols = ["customer_id", "last_purchase", "recency_days", "frequency", "monetary",
                "r_score", "f_score", "m_score"]  # fmt: skip
        return table(
            ctx,
            top[cols],
            segment=segment,
            meaning=rfm.SEGMENTS[segment],
            segment_size=len(members),
            notes=[RFM_NOTE],
        )

    @tool(
        "cohort_retention",
        "Monthly acquisition cohorts: for customers whose first order was in month X, the "
        "share who ordered again 1, 2, ... months later. Columns m0..mN are retention rates.",
        obj(
            {
                "max_months": int_range(1, 24, 6, "How many months after the first order"),
                "from_cohort": DATE | {"description": "Only cohorts from this month onwards."},
            }
        ),
        annotations=READ_ONLY,
    )
    @safe
    async def cohort_retention(args: dict) -> dict:
        max_months = int(args.get("max_months") or 6)
        from_cohort = opt_date(args, "from_cohort")
        long = await run_query(
            ctx, lambda con: cohorts.cohort_retention(con, ctx.as_of, max_months=max_months)
        )
        matrix = cohorts.retention_matrix(long)
        if from_cohort is not None and not matrix.empty:
            matrix = matrix[matrix["cohort_month"] >= from_cohort.replace(day=1)]
        # Show the caveat only when the table includes the first cohort in the data.
        notes = None
        if not long.empty and not matrix.empty:
            first_month = long["cohort_month"].min()
            if (matrix["cohort_month"] == first_month).any():
                notes = [cohort_note(first_month)]
        return table(ctx, matrix, notes=notes)

    @tool(
        "repeat_customer_rate",
        "Share of known customers with 2+ orders (distinct invoices) between an optional "
        "start date and the reference date.",
        obj({"start": DATE | {"description": "First day included. Default: all history."}}),
        annotations=READ_ONLY,
    )
    @safe
    async def repeat_customer_rate(args: dict) -> dict:
        start = opt_date(args, "start")
        result = await run_query(
            ctx, lambda con: cohorts.repeat_customer_rate(con, ctx.as_of, start=start)
        )
        return ok(ctx, result=result, window={"start": start, "end": ctx.as_of})

    return [rfm_segments, segment_customers, cohort_retention, repeat_customer_rate]
