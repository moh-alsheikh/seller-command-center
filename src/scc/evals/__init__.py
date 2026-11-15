"""Evaluation harness: run the agent on fixed cases and grade the results automatically.

Pieces:
    cases.py       the case format (loaded from evals/cases.yaml) and the structured-output schema
    references.py  ground truth, computed at eval time from reference SQL or tested analytics
    checks.py      pure grading functions (numbers with tolerance, codes, routing, safety)
    runner.py      isolated workspace, one query() per case, concurrency, invariants
    report.py      evals/report.md

Evals use ``query()`` rather than ``ClaudeSDKClient`` (workshop lesson 5): each case is one
independent task with no follow-up turns, and ``output_format`` gives a JSON answer that can
be graded without parsing prose.
"""
