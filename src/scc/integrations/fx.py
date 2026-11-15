"""Exchange rates from Frankfurter (https://frankfurter.dev): ECB reference rates, no key needed.

This is the project's one live HTTP integration, so it shows the patterns a production client
needs:
  * timeouts on every request,
  * retries with backoff for temporary failures (timeouts, connection errors, HTTP 5xx), but
    not for client errors (an unknown currency returns 404, and retrying won't fix that),
  * a fallback host (the older ``api.frankfurter.app`` domain),
  * an in-memory cache: historical rates never change, so they are cached forever; "latest"
    rates are cached for ``ttl_s`` seconds,
  * input validation before anything is put into a URL.

API facts (checked 2026-11-15):
  GET {base}/latest?base=GBP&symbols=EUR,USD
  GET {base}/2011-12-09?base=GBP&symbols=EUR
  -> {"amount": 1.0, "base": "GBP", "date": "2011-12-09", "rates": {"EUR": 1.1721}}
  On a weekend or holiday the API returns the previous business day and says so in "date".
  Unknown currency -> HTTP 404 {"message": "not found"}.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import httpx

log = logging.getLogger(__name__)

_CURRENCY = re.compile(r"^[A-Z]{3}$")


class FxError(RuntimeError):
    """Rates could not be fetched. The message is safe to show to the agent."""


@dataclass(frozen=True)
class FxRates:
    base: str
    date: date  # the date the rates are actually for (may be earlier than requested)
    rates: dict[str, float]
    source: str = "Frankfurter (European Central Bank reference rates)"

    def convert(self, amount: float, currency: str) -> float:
        """Convert ``amount`` in the base currency to ``currency``."""
        return amount * self.rates[currency]

    def to_dict(self) -> dict:
        return {
            "base": self.base,
            "date": self.date.isoformat(),
            "rates": self.rates,
            "source": self.source,
        }


def normalize_currencies(codes: list[str]) -> list[str]:
    """Upper-case, de-duplicate and validate ISO currency codes. Raises ValueError."""
    out: list[str] = []
    for raw in codes:
        code = str(raw).strip().upper()
        if not _CURRENCY.match(code):
            raise ValueError(f"{raw!r} is not a 3-letter currency code such as EUR or USD.")
        if code not in out:
            out.append(code)
    if not out:
        raise ValueError("Give at least one currency code, e.g. ['EUR', 'USD'].")
    return out


@dataclass
class FxClient:
    """Async Frankfurter client. Create one per process and reuse it (it keeps the cache)."""

    base_url: str = "https://api.frankfurter.dev/v1"
    fallback_url: str | None = "https://api.frankfurter.app"
    timeout_s: float = 10.0
    ttl_s: float = 3600.0
    retries: int = 2  # extra attempts per host after the first one
    backoff_s: float = 0.5
    # Injected in tests: a fake clock and an httpx transport.
    clock: Callable[[], float] = time.monotonic
    transport: httpx.AsyncBaseTransport | None = None
    _cache: dict[tuple, tuple[float, FxRates]] = field(default_factory=dict, repr=False)

    async def rates(
        self, symbols: list[str], *, base: str = "GBP", on: date | None = None
    ) -> FxRates:
        """Rates from ``base`` to each of ``symbols``, for date ``on`` (None = latest)."""
        base = normalize_currencies([base])[0]
        requested = normalize_currencies(symbols)
        # The base currency is always 1.0; only ask the API for the others.
        foreign = [s for s in requested if s != base]
        if not foreign:
            return FxRates(base=base, date=on or date.today(), rates={base: 1.0})

        result = await self._fetch(base, foreign, on)
        if base in requested:
            result = FxRates(base=result.base, date=result.date, rates={base: 1.0, **result.rates})
        return result

    async def _fetch(self, base: str, symbols: list[str], on: date | None) -> FxRates:
        """Fetch through the cache."""
        key = (base, tuple(sorted(symbols)), on)
        cached = self._cache.get(key)
        if cached is not None:
            stored_at, value = cached
            # Historical rates never change; "latest" expires after ttl_s.
            if on is not None or self.clock() - stored_at < self.ttl_s:
                return value

        path = on.isoformat() if on else "latest"
        params = {"base": base, "symbols": ",".join(symbols)}
        data = await self._get_with_fallback(path, params)
        try:
            result = FxRates(
                base=data["base"],
                date=date.fromisoformat(data["date"]),
                rates={k: float(v) for k, v in data["rates"].items()},
            )
        except (KeyError, TypeError, ValueError) as err:
            raise FxError(f"Unexpected response from the exchange-rate service: {data!r}") from err

        self._cache[key] = (self.clock(), result)
        return result

    async def _get_with_fallback(self, path: str, params: dict[str, str]) -> dict:
        hosts = [self.base_url] + ([self.fallback_url] if self.fallback_url else [])
        last_error: Exception | None = None
        async with httpx.AsyncClient(
            timeout=self.timeout_s, follow_redirects=True, transport=self.transport
        ) as client:
            for host in hosts:
                try:
                    return await self._get_with_retries(client, f"{host}/{path}", params)
                except _ClientError as err:
                    # A 4xx means our request is wrong (e.g. unknown currency). Another host
                    # will say the same, so stop here with a clear message.
                    raise FxError(str(err)) from err
                except (httpx.TransportError, _ServerError) as err:
                    log.warning("FX host %s failed: %s", host, err)
                    last_error = err
        raise FxError(f"Exchange-rate service unavailable ({last_error}). Try again later.")

    async def _get_with_retries(
        self, client: httpx.AsyncClient, url: str, params: dict[str, str]
    ) -> dict:
        for attempt in range(self.retries + 1):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code >= 500:
                    raise _ServerError(f"HTTP {resp.status_code} from {url}")
                if resp.status_code >= 400:
                    detail = _error_message(resp)
                    raise _ClientError(
                        f"The exchange-rate service rejected the request ({detail}). "
                        "Check the currency codes; supported: EUR, USD, JPY, CHF, AUD, CAD, ..."
                    )
                return resp.json()
            except (httpx.TransportError, _ServerError):
                if attempt == self.retries:
                    raise
                await asyncio.sleep(self.backoff_s * 2**attempt)
        raise AssertionError("unreachable")


class _ServerError(Exception):
    """HTTP 5xx: temporary, worth retrying."""


class _ClientError(Exception):
    """HTTP 4xx: our request is wrong, do not retry."""


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message", resp.status_code))
    except ValueError:
        return f"HTTP {resp.status_code}"
