"""Run the eval suite (LIVE: uses API credit; a full run costs roughly $2-5).

    uv run python evals/run_evals.py                       # all cases -> evals/report.md
    uv run python evals/run_evals.py --dry-run             # compute references only (free)
    uv run python evals/run_evals.py --only acc_champions_count,safety_sql_delete
    uv run python evals/run_evals.py --category safety --concurrency 2

Exit code 0 when the targets in docs/PLAN.md are met (>= 85% overall, 100% safety).
See evals/README.md for what each case checks and how grading works.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scc.config import get_settings  # noqa: E402
from scc.evals.cases import load_cases  # noqa: E402
from scc.evals.report import TARGET_OVERALL, TARGET_SAFETY, render, summary  # noqa: E402
from scc.evals.runner import Outcome, run_all  # noqa: E402


def progress(o: Outcome) -> None:
    mark = "PASS" if o.passed else "FAIL"
    cost = f"${o.cost_usd:.3f}" if o.cost_usd is not None else "$?"
    why = "" if o.passed else " | " + "; ".join(f"{c.name}: {c.detail}" for c in o.failed_checks)
    print(f"  {mark} {o.case.id:<40} {o.duration_s:5.0f}s {cost}{why[:200]}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cases", type=Path, default=ROOT / "evals" / "cases.yaml")
    p.add_argument("--only", help="comma-separated case ids")
    p.add_argument("--category", choices=["accuracy", "routing", "safety"])
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--max-budget", type=float, default=0.75, help="USD cap per case")
    p.add_argument("--dry-run", action="store_true", help="compute references only (free)")
    p.add_argument("--report", type=Path, default=ROOT / "evals" / "report.md")
    args = p.parse_args()

    cases = load_cases(args.cases)
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c.id in wanted]
    if args.category:
        cases = [c for c in cases if c.category == args.category]
    if not cases:
        sys.exit("No cases selected.")

    work = ROOT / "evals" / ".work"
    print(f"{len(cases)} case(s); workspace {work}")
    result = asyncio.run(
        run_all(cases, get_settings(), work, concurrency=args.concurrency,
                max_budget_usd=args.max_budget, dry_run=args.dry_run, on_done=progress)
    )  # fmt: skip

    if args.dry_run:
        for case in cases:
            print(f"  {case.id:<40} {json.dumps(result.references[case.id], default=str)}")
        return

    args.report.write_text(render(result))
    raw = [
        {"id": o.case.id, "passed": o.passed, "expected": o.expected, "value": o.value,
         "answer": o.answer, "spokes_used": o.spokes_used, "spokes_claimed": o.spokes_claimed,
         "cost_usd": o.cost_usd, "duration_s": round(o.duration_s, 1), "turns": o.num_turns,
         "checks": [c.__dict__ for c in o.checks], "error": o.error}
        for o in result.outcomes
    ]  # fmt: skip
    (work / "results.json").write_text(json.dumps(raw, indent=2, default=str))

    s = summary(result.outcomes)
    print(
        f"\n{s['passed']}/{s['total']} passed ({s['overall']:.0%}); "
        f"safety {s['safety_rate']:.0%}; cost ${s['cost']:.2f}; report {args.report}"
    )
    ok = s["overall"] >= TARGET_OVERALL and (
        "safety" not in s["by_category"] or s["safety_rate"] >= TARGET_SAFETY
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
