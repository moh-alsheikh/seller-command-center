"""Eval case format and the structured answer schema.

A case is a question plus expectations. Every expectation is optional, so one format covers
accuracy (a figure within tolerance), routing (which specialists ran) and safety (what must
NOT happen). Example (YAML)::

    - id: acc_net_revenue_germany_last_month
      category: accuracy
      question: What was our net revenue from Germany in $last_month_name?
      expect:
        value_sql: SELECT ... BETWEEN DATE '$last_month_start' AND DATE '$last_month_end'
        tolerance_pct: 0.5
        spokes_include: [sales_analyst]

Dates are never fixed in a case. ``$variables`` are filled in from the data's "today"
(``render_case``), because the demo data is re-dated to the present whenever it is built.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from string import Template
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scc.analytics.forecast import add_months, last_complete_month

Category = Literal["accuracy", "routing", "safety"]
SPOKES = {
    "sales_analyst",
    "customer_insights",
    "inventory_planner",
    "pricing_strategist",
    "listing_writer",
}


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo in cases.yaml fails loudly

    # --- accuracy: the key figure (exactly one reference source) --------------------------
    value_sql: str | None = None  # one row, one column: number or text
    value_ref: dict[str, Any] | None = None  # {"fn": name, "args": {...}} in references.py
    tolerance_pct: float | None = None  # relative, for amounts
    tolerance_abs: float | None = None  # absolute, e.g. percentage points
    # Every row of this query's first column must appear in the answer (e.g. top-3 codes).
    contains_all_sql: str | None = None

    # --- routing ----------------------------------------------------------------------------
    spokes_include: list[str] = Field(default_factory=list)
    spokes_exclude: list[str] = Field(default_factory=list)

    # --- safety -----------------------------------------------------------------------------
    answer_excludes: list[str] = Field(default_factory=list)  # case-insensitive
    approval_requested: bool | None = None  # an approval prompt must (not) have appeared
    sales_rows_unchanged: bool = False  # the retail data must be identical afterwards

    @model_validator(mode="after")
    def _check(self) -> Expect:
        if self.value_sql and self.value_ref:
            raise ValueError("use value_sql or value_ref, not both")
        unknown = set(self.spokes_include + self.spokes_exclude) - SPOKES
        if unknown:
            raise ValueError(f"unknown spokes: {sorted(unknown)}")
        return self

    @property
    def has_value(self) -> bool:
        return bool(self.value_sql or self.value_ref)


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: Category
    question: str
    expect: Expect = Field(default_factory=Expect)
    notes: str | None = None


def load_cases(path: Path) -> list[EvalCase]:
    raw = yaml.safe_load(path.read_text())
    cases = [EvalCase.model_validate(item) for item in raw]
    ids = [c.id for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate case ids: {sorted(duplicates)}")
    return cases


def eval_variables(as_of: date) -> dict[str, str]:
    """Date variables available to cases, relative to the data's "today"."""
    last = last_complete_month(as_of)
    prev = add_months(last, -1)
    current = as_of.replace(day=1)

    def month_end(m: date) -> str:
        return (add_months(m, 1) - timedelta(days=1)).isoformat()

    return {
        "as_of": as_of.isoformat(),
        "year": str(as_of.year),
        "year_start": f"{as_of.year}-01-01",
        "last_month_start": last.isoformat(),
        "last_month_end": month_end(last),
        "last_month_name": f"{last:%B %Y}",
        "prev_month_start": prev.isoformat(),
        "prev_month_end": month_end(prev),
        "prev_month_name": f"{prev:%B %Y}",
        "current_month_start": current.isoformat(),
        "current_month_name": f"{current:%B %Y}",
    }


def render_case(case: EvalCase, as_of: date) -> EvalCase:
    """Fill in $variables in the question, the reference SQL and reference arguments.

    ``substitute`` (not ``safe_substitute``): a misspelt variable fails loudly instead of
    reaching the model or the database as "$last_mnth_name".
    """
    variables = eval_variables(as_of)

    def fill(text: str | None) -> str | None:
        return Template(text).substitute(variables) if text else text

    e = case.expect
    value_ref = None
    if e.value_ref:
        args = {
            k: fill(v) if isinstance(v, str) else v for k, v in e.value_ref.get("args", {}).items()
        }
        value_ref = {**e.value_ref, "args": args}
    expect = e.model_copy(
        update={
            "value_sql": fill(e.value_sql),
            "contains_all_sql": fill(e.contains_all_sql),
            "value_ref": value_ref,
        }
    )
    return case.model_copy(update={"question": fill(case.question), "expect": expect})


# The hub must end every eval run with this JSON (query(..., output_format=...)).
# Descriptions matter: they are the only instructions about the format the model gets, so the
# questions themselves stay exactly as a seller would ask them.
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "Your complete answer to the seller, as you would normally write it.",
        },
        "value": {
            "type": ["number", "string", "null"],
            "description": (
                "The single key result the question asks for. A number must be plain: no "
                "currency symbols or thousands separators; percentages as e.g. 12.5 (signed "
                "when asked for a change). Use a string for a product stock code or a country "
                "name. null when the question has no single key result."
            ),
        },
        "unit": {
            "type": ["string", "null"],
            "description": "Unit of value, e.g. GBP, EUR, units, orders, customers, percent.",
        },
        "spokes_used": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The specialists you delegated to.",
        },
    },
    "required": ["answer", "value", "unit", "spokes_used"],
    "additionalProperties": False,
}
