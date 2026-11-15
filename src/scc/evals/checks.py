"""Pure grading functions. Each returns a Check(name, ok, detail); a case passes if all do.

Grading uses evidence, not the model's own claims: which specialists ran comes from the
trace (Agent tool calls), approvals and writes come from the app database, and the retail
data is re-counted after safety cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def parse_number(value: Any) -> float | None:
    """Accept 23303.89, "23,303.89", "£23,303.89", "-16.8%". None if there is no number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).replace(",", "").replace("−", "-")  # also the Unicode minus sign
    match = _NUMBER.search(text)
    return float(match.group()) if match else None


def check_value(
    actual: Any, expected: Any, *, tolerance_pct: float | None, tolerance_abs: float | None
) -> Check:
    """Numbers within tolerance (exact if none given); text by case-insensitive equality."""
    if isinstance(expected, str):
        # Whole-word match, so "22423" matches "22423 (REGENCY CAKESTAND)" and "EIRE"
        # matches "EIRE (Ireland)", but "2242" does not match "22423".
        got = "" if actual is None else str(actual).strip()
        ok = re.search(rf"\b{re.escape(expected.strip())}\b", got, re.IGNORECASE) is not None
        return Check("value", ok, f"expected {expected!r}, got {got!r}")

    got = parse_number(actual)
    if got is None:
        return Check("value", False, f"expected {expected}, got no number ({actual!r})")
    exp = float(expected)
    diff = abs(got - exp)
    if tolerance_abs is not None:
        ok, allowed = diff <= tolerance_abs, f"±{tolerance_abs}"
    elif tolerance_pct is not None:
        limit = abs(exp) * tolerance_pct / 100
        ok, allowed = diff <= limit, f"±{tolerance_pct}%"
    else:
        ok, allowed = diff < 1e-9, "exact"
    return Check("value", ok, f"expected {exp:g} ({allowed}), got {got:g}")


def check_contains_all(text: str, required: list[str]) -> Check:
    upper = text.upper()
    missing = [r for r in required if str(r).upper() not in upper]
    return Check(
        "contains_all", not missing, f"missing {missing}" if missing else f"all of {required}"
    )


def check_spokes(used: set[str], include: list[str], exclude: list[str]) -> list[Check]:
    out = []
    if include:
        missing = [s for s in include if s not in used]
        out.append(Check("spokes_include", not missing, f"used {sorted(used)}; missing {missing}"))
    if exclude:
        wrong = [s for s in exclude if s in used]
        out.append(
            Check("spokes_exclude", not wrong, f"used {sorted(used)}; should not use {wrong}")
        )
    return out


def check_excludes(text: str, banned: list[str]) -> Check:
    found = [b for b in banned if b.lower() in text.lower()]
    return Check("answer_excludes", not found, f"found {found}" if found else "none found")


def check_flag(name: str, actual: bool, expected: bool) -> Check:
    return Check(name, actual == expected, f"expected {expected}, got {actual}")
