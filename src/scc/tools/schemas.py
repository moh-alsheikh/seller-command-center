"""Small helpers for writing tool input JSON Schemas.

We pass full JSON Schemas (not the ``{"name": str}`` shorthand) so we can add descriptions,
enums, ranges and ``additionalProperties: false``. The SDK validates every call against the
schema before our handler runs, so a wrong argument comes back to the model as a clear
validation error.
"""

from __future__ import annotations

from typing import Any

DATE: dict[str, Any] = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
STOCK_CODE: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 20,
    "description": "Product stock code, e.g. '85123A'. Use search_products if unknown.",
}


def obj(properties: dict[str, dict[str, Any]], required: list[str] | None = None) -> dict:
    """An object schema that rejects unknown keys."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def int_range(lo: int, hi: int, default: int, description: str) -> dict[str, Any]:
    return {
        "type": "integer",
        "minimum": lo,
        "maximum": hi,
        "description": f"{description} ({lo}-{hi}, default {default}).",
    }
