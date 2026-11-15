"""Tests for the Frankfurter client. All HTTP is mocked with respx: no network access."""

from datetime import date

import httpx
import pytest
import respx

from scc.integrations.fx import FxClient, FxError, normalize_currencies

PRIMARY = "https://fx.primary.test/v1"
FALLBACK = "https://fx.fallback.test"


def body(day="2011-12-09", **rates):
    return {"amount": 1.0, "base": "GBP", "date": day, "rates": rates or {"EUR": 1.1721}}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fx(clock):
    return FxClient(
        base_url=PRIMARY, fallback_url=FALLBACK, ttl_s=60, backoff_s=0, retries=2, clock=clock
    )


def test_normalize_currencies():
    assert normalize_currencies(["eur", " usd", "EUR"]) == ["EUR", "USD"]
    for bad in (["EURO"], ["E1R"], ["../latest"], []):
        with pytest.raises(ValueError):
            normalize_currencies(bad)


@respx.mock
async def test_historical_rates_parse_and_cache_forever(fx, clock):
    route = respx.get(f"{PRIMARY}/2011-12-09").mock(return_value=httpx.Response(200, json=body()))
    first = await fx.rates(["EUR"], on=date(2011, 12, 9))
    clock.now += 10**6  # far beyond the TTL: historical rates never expire
    second = await fx.rates(["eur"], on=date(2011, 12, 9))

    assert first == second
    assert first.rates == {"EUR": 1.1721}
    assert first.date == date(2011, 12, 9)
    assert route.call_count == 1
    assert route.calls[0].request.url.params["symbols"] == "EUR"
    assert route.calls[0].request.url.params["base"] == "GBP"


@respx.mock
async def test_latest_rates_expire_after_ttl(fx, clock):
    route = respx.get(f"{PRIMARY}/latest").mock(return_value=httpx.Response(200, json=body()))
    await fx.rates(["EUR"])
    clock.now += 30
    await fx.rates(["EUR"])  # within TTL: cached
    clock.now += 31
    await fx.rates(["EUR"])  # TTL passed: fetched again
    assert route.call_count == 2


@respx.mock
async def test_weekend_returns_previous_business_day(fx):
    respx.get(f"{PRIMARY}/2011-12-10").mock(return_value=httpx.Response(200, json=body()))
    rates = await fx.rates(["EUR"], on=date(2011, 12, 10))
    assert rates.date == date(2011, 12, 9)


async def test_base_currency_needs_no_http(fx):
    # respx is not active here, so any real HTTP call would fail the test.
    rates = await fx.rates(["GBP"], on=date(2011, 12, 9))
    assert rates.rates == {"GBP": 1.0}


@respx.mock
async def test_base_currency_is_added_when_requested_with_others(fx):
    respx.get(f"{PRIMARY}/2011-12-09").mock(return_value=httpx.Response(200, json=body()))
    rates = await fx.rates(["GBP", "EUR"], on=date(2011, 12, 9))
    assert rates.rates == {"GBP": 1.0, "EUR": 1.1721}
    assert rates.convert(10, "EUR") == pytest.approx(11.721)


@respx.mock
async def test_unknown_currency_is_not_retried_or_sent_to_fallback(fx):
    primary = respx.get(f"{PRIMARY}/latest").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    fallback = respx.get(f"{FALLBACK}/latest")
    with pytest.raises(FxError, match="not found"):
        await fx.rates(["XXX"])
    assert primary.call_count == 1
    assert fallback.call_count == 0


@respx.mock
async def test_server_error_is_retried(fx):
    route = respx.get(f"{PRIMARY}/latest").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=body())]
    )
    rates = await fx.rates(["EUR"])
    assert rates.rates["EUR"] == 1.1721
    assert route.call_count == 2


@respx.mock
async def test_falls_back_to_second_host(fx):
    primary = respx.get(f"{PRIMARY}/latest").mock(side_effect=httpx.ConnectError("down"))
    fallback = respx.get(f"{FALLBACK}/latest").mock(return_value=httpx.Response(200, json=body()))
    rates = await fx.rates(["EUR"])
    assert rates.rates["EUR"] == 1.1721
    assert primary.call_count == 3  # first attempt + 2 retries
    assert fallback.call_count == 1


@respx.mock
async def test_both_hosts_down_raises_readable_error(fx):
    respx.get(f"{PRIMARY}/latest").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get(f"{FALLBACK}/latest").mock(return_value=httpx.Response(500))
    with pytest.raises(FxError, match="unavailable"):
        await fx.rates(["EUR"])


@respx.mock
async def test_malformed_response_raises_fx_error(fx):
    respx.get(f"{PRIMARY}/latest").mock(return_value=httpx.Response(200, json={"oops": 1}))
    with pytest.raises(FxError, match="Unexpected response"):
        await fx.rates(["EUR"])
