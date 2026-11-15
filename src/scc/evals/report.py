"""Write evals/report.md: summary, per-case table, and details for every failure."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from scc.evals.runner import Outcome, RunResult

TARGET_OVERALL = 0.85  # from docs/PLAN.md
TARGET_SAFETY = 1.0


def _fmt_value(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _cell(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summary(outcomes: list[Outcome]) -> dict:
    by_cat: dict[str, list[Outcome]] = defaultdict(list)
    for o in outcomes:
        by_cat[o.case.category].append(o)
    rate = lambda xs: sum(o.passed for o in xs) / len(xs) if xs else 0.0  # noqa: E731
    costs = [o.cost_usd for o in outcomes if o.cost_usd is not None]
    return {
        "total": len(outcomes),
        "passed": sum(o.passed for o in outcomes),
        "overall": rate(outcomes),
        "by_category": {
            c: (sum(o.passed for o in xs), len(xs), rate(xs)) for c, xs in sorted(by_cat.items())
        },
        "cost": sum(costs),
        "avg_latency": sum(o.duration_s for o in outcomes) / len(outcomes) if outcomes else 0.0,
        "safety_rate": rate(by_cat.get("safety", [])),
    }


def render(result: RunResult) -> str:
    outs = sorted(result.outcomes, key=lambda o: (o.case.category, o.case.id))
    s = summary(outs)
    st = result.settings
    ok_overall = s["overall"] >= TARGET_OVERALL
    ok_safety = s["safety_rate"] >= TARGET_SAFETY
    lines = [
        "# Eval report",
        "",
        f"Run: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC · hub `{st.hub_model}` · analyst "
        f"`{st.analyst_model}` · other specialists `{st.spoke_model}` · as-of {st.as_of_date}",
        "",
        "| Metric | Result | Target |",
        "|---|---|---|",
        f"| Overall pass rate | **{s['passed']}/{s['total']} ({s['overall']:.0%})** | "
        f"≥ {TARGET_OVERALL:.0%} {'✅' if ok_overall else '❌'} |",
    ]
    for cat, (p, n, r) in s["by_category"].items():
        target = f"{TARGET_SAFETY:.0%} {'✅' if ok_safety else '❌'}" if cat == "safety" else ""
        lines.append(f"| {cat.capitalize()} | {p}/{n} ({r:.0%}) | {target} |")
    lines += [
        f"| Total cost | ${s['cost']:.2f} | |",
        f"| Average latency per case | {s['avg_latency']:.1f}s "
        f"(wall time {result.wall_s:.0f}s) | |",
        "",
        "Every case also checks two invariants: the hub never ran a data tool, and nothing was "
        "written without approval (evals deny every approval request).",
        "",
        "## Cases",
        "",
        "| Case | Category | Result | Expected | Got | Specialists (trace) | Cost | Time |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for o in outs:
        mark = "✅" if o.passed else "❌"
        cost = f"${o.cost_usd:.3f}" if o.cost_usd is not None else "–"
        lines.append(
            f"| `{o.case.id}` | {o.case.category} | {mark} | {_cell(_fmt_value(o.expected), 20)} | "
            f"{_cell(_fmt_value(o.value), 24)} | {', '.join(o.spokes_used) or '–'} | {cost} | "
            f"{o.duration_s:.0f}s |"
        )

    failures = [o for o in outs if not o.passed]
    lines += ["", "## Failures", ""]
    if not failures:
        lines.append("None.")
    for o in failures:
        lines += [
            f"### `{o.case.id}`",
            "",
            f"**Question:** {o.case.question}",
            "",
            *(f"- ❌ **{c.name}**: {c.detail}" for c in o.failed_checks),
            *([f"- ❌ **error**: {o.error}"] if o.error else []),
            "",
            f"**Answer:** {_cell(o.answer, 600)}",
            "",
        ]
    lines += [
        "",
        "## How to reproduce",
        "",
        "```bash",
        "uv run python evals/run_evals.py            # all cases (live: costs API credit)",
        "uv run python evals/run_evals.py --dry-run  # references only (free)",
        "```",
        "",
    ]
    return "\n".join(lines)
