"""Shared plumbing for every tool: context, result format, input checks, error handling.

Conventions all tools follow (so the agents see one consistent format):

* **Success** returns one text block holding a JSON object, the "envelope"::

      {"as_of": "2026-11-13", "currency": "GBP", "rows": [...], "row_count": 3,
       "truncated": false, "notes": ["..."]}

  JSON rather than prose, because models read structured data reliably and the UI can
  render it. Money is rounded to 2 decimals; ratios (rates, shares, trends) to 4.

* **Errors** are returned, never raised (lesson 2 from the workshop): the text explains what
  went wrong and how to fix it, and ``is_error`` is set. The agent can then correct its call
  instead of the whole run failing.

* **Database work runs in a worker thread** (``asyncio.to_thread``). DuckDB calls block, and
  tool handlers run on the same event loop as the Chainlit UI; a 300 ms query must not freeze
  every other user's chat.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from scc.analytics.sales import list_countries
from scc.config import Settings, get_settings
from scc.data.db import DatabaseMissingError, connect_readonly
from scc.integrations.fx import FxClient, FxError

log = logging.getLogger(__name__)

ToolResult = dict[str, Any]


# --------------------------------------------------------------------------------------------
# Context: everything a tool needs, passed in explicitly (easy to swap in tests)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolContext:
    db_path: Path
    as_of: date
    fx: FxClient = field(default_factory=FxClient)
    max_rows: int = 200

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ToolContext:
        s = settings or get_settings()
        return cls(
            db_path=s.duckdb_path,
            as_of=s.as_of_date,
            fx=FxClient(
                base_url=s.fx_base_url,
                fallback_url=s.fx_fallback_url,
                timeout_s=s.http_timeout_s,
                ttl_s=s.fx_cache_ttl_s,
            ),
        )


async def run_query[T](ctx: ToolContext, fn: Callable[[duckdb.DuckDBPyConnection], T]) -> T:
    """Run ``fn(con)`` on a fresh read-only connection in a worker thread."""

    def work() -> T:
        with connect_readonly(ctx.db_path) as con:
            return fn(con)

    return await asyncio.to_thread(work)


# --------------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------------

# Keys whose float values are ratios and need more precision than money. Everything inside
# a "rates" dict (exchange rates such as EUR: 1.1721) counts as a ratio too.
_RATIO_KEY = re.compile(r"(rate|share|trend|ratio)$|^rates$|^m\d+$")


def _clean(value: Any, key: str = "", ratio: bool = False) -> Any:
    """Make a value JSON-friendly: numpy -> Python, NaN -> None, dates -> ISO, round floats.

    Floats are rounded to 2 decimals (money), or 4 when the key, or a parent key, names a
    ratio.
    """
    ratio = ratio or bool(_RATIO_KEY.search(key))
    if isinstance(value, dict):
        return {k: _clean(v, str(k), ratio) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v, key, ratio) for v in value]
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 4 if ratio else 2)
    if isinstance(value, datetime | pd.Timestamp):
        return value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    return value


def records(df: pd.DataFrame, limit: int) -> tuple[list[dict], bool]:
    """DataFrame -> list of cleaned row dicts (at most ``limit``), plus a 'truncated' flag."""
    truncated = len(df) > limit
    rows = df.head(limit).to_dict(orient="records")
    return [_clean(row) for row in rows], truncated


def ok(ctx: ToolContext, *, notes: list[str] | None = None, **payload: Any) -> ToolResult:
    """Build a success envelope. ``payload`` usually holds ``rows`` or a ``result`` dict."""
    body: dict[str, Any] = {"as_of": ctx.as_of.isoformat(), "currency": "GBP"}
    body.update(_clean(payload))
    if "rows" in body:
        body.setdefault("row_count", len(body["rows"]))
    if notes:
        body["notes"] = notes
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}]}


def fail(message: str) -> ToolResult:
    return {"content": [{"type": "text", "text": f"Error: {message}"}], "is_error": True}


def table(ctx: ToolContext, df: pd.DataFrame, *, notes: list[str] | None = None, **extra):
    """Shortcut: a DataFrame as an envelope with rows / truncated."""
    rows, truncated = records(df, ctx.max_rows)
    if truncated:
        notes = [*(notes or []), f"Only the first {ctx.max_rows} rows are shown."]
    return ok(ctx, rows=rows, truncated=truncated, notes=notes, **extra)


# --------------------------------------------------------------------------------------------
# Input checks (raise ToolInputError; the decorator turns it into a readable error result)
# --------------------------------------------------------------------------------------------


class ToolInputError(ValueError):
    """The agent passed a bad argument. The message tells it how to fix the call."""


def opt_date(args: dict, name: str) -> date | None:
    raw = args.get(name)
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError as err:
        raise ToolInputError(f"{name} must be a date like YYYY-MM-DD, got {raw!r}.") from err


def opt_str(args: dict, name: str) -> str | None:
    raw = args.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def stock_code(args: dict, name: str = "stock_code") -> str | None:
    code = opt_str(args, name)
    return code.upper() if code else None


def check_country(con: duckdb.DuckDBPyConnection, country: str | None) -> None:
    """Fail with the list of valid names, so 'Deutschland' can be corrected to 'Germany'."""
    if country is None:
        return
    countries = list_countries(con)
    if country not in countries:
        raise ToolInputError(
            f"Unknown country {country!r}. Use one of these exact names: {', '.join(countries)}."
            " (Ireland is 'EIRE'; the UK is 'United Kingdom'.)"
        )


def check_product(con: duckdb.DuckDBPyConnection, code: str | None) -> None:
    if code is None:
        return
    found = con.execute("SELECT 1 FROM products WHERE stock_code = $c", {"c": code}).fetchone()
    if not found:
        raise ToolInputError(
            f"No product with stock code {code!r}. Use search_products to find the code."
        )


# --------------------------------------------------------------------------------------------
# Error boundary
# --------------------------------------------------------------------------------------------

Handler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


def safe(handler: Handler) -> Handler:
    """Turn every exception into an error result the agent can read and act on.

    The SDK would also catch a raised exception, but its message would be generic. Here we
    choose the wording: input mistakes are explained, and unexpected failures are logged with
    a traceback for us, while the agent gets a short message without internals.
    """

    @functools.wraps(handler)
    async def wrapper(args: dict[str, Any]) -> ToolResult:
        try:
            return await handler(args or {})
        except (ToolInputError, FxError) as err:
            return fail(str(err))
        except DatabaseMissingError:
            return fail(
                "The sales database has not been built yet. Ask the operator to run ingest."
            )
        except duckdb.Error as err:
            # Mostly SQL mistakes from run_sql; the first line is useful to the agent.
            return fail(f"Database error: {str(err).splitlines()[0]}")
        except Exception:
            log.exception("Tool %s failed", handler.__name__)
            return fail("Internal error in the tool. Try a different request or report it.")

    return wrapper
