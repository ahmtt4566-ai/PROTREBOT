"""Queue delays must not distort clock offsets or expire signed requests."""
import asyncio
import hashlib
import hmac
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app import exchange_connections as connections, v25_execution as live


@pytest.fixture
def queued_clock(monkeypatch):
    clock = SimpleNamespace(now=1_700_000_000.0)
    monkeypatch.setattr(connections.time, "time", lambda: clock.now)
    connections._SERVER_TIME_CACHE.clear()
    connections._SERVER_TIME_REJECTION_CACHE.clear()
    connections._SERVER_TIME_LOCKS.clear()

    @asynccontextmanager
    async def slot(host):
        clock.now += 8
        yield SimpleNamespace(observe=lambda response: None)

    monkeypatch.setattr(connections.BINANCE_RATE_LIMITER, "slot", slot)
    yield clock
    connections._SERVER_TIME_CACHE.clear()
    connections._SERVER_TIME_REJECTION_CACHE.clear()
    connections._SERVER_TIME_LOCKS.clear()


def clock_response(clock, url):
    server_time = round(clock.now * 1000) + 300
    clock.now += 0.1
    return httpx.Response(200, json={"serverTime": server_time}, request=httpx.Request("GET", url))


def assert_fresh_signature(url, clock):
    query, signature = urlsplit(url).query.rsplit("&signature=", 1)
    params = parse_qs(query)
    assert int(params["timestamp"][0]) == round(clock.now * 1000) + 250
    assert params["recvWindow"] == ["5000"]
    assert signature == hmac.new(b"offline-secret", query.encode(), hashlib.sha256).hexdigest()


def test_verification_clock_excludes_rate_limit_queue(queued_clock):
    async def get(url, headers=None):
        return clock_response(queued_clock, url)

    assert asyncio.run(connections._server_time_offset(SimpleNamespace(get=get), "LIVE")) == 250


def test_verification_signs_each_read_after_its_queue(queued_clock):
    calls = []

    async def get(url, headers=None):
        path = urlsplit(url).path
        calls.append(path)
        if path == "/fapi/v1/time":
            return clock_response(queued_clock, url)
        assert_fresh_signature(url, queued_clock)
        queued_clock.now += 6
        payload = {
            "/sapi/v1/account/apiRestrictions": {"enableWithdrawals": False, "enableFutures": True},
            "/fapi/v3/account": {"canTrade": True, "positions": []},
            "/fapi/v1/positionSide/dual": {"dualSidePosition": False},
        }[path]
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    result = asyncio.run(connections.test_binance_credentials(
        SimpleNamespace(get=get), "LIVE", "offline-key", "offline-secret",
    ))
    assert result["orders_created"] is False
    assert calls == ["/fapi/v1/time", "/sapi/v1/account/apiRestrictions",
                     "/fapi/v3/account", "/fapi/v1/positionSide/dual"]


def test_live_clock_excludes_rate_limit_queue(queued_clock):
    async def request(method, url, **kwargs):
        assert method == "GET"
        return clock_response(queued_clock, url)

    client = live.BinanceLiveClient(SimpleNamespace(request=request), "offline-key", "offline-secret")
    asyncio.run(client.sync_clock())
    assert client.time_offset_ms == 250


def test_live_read_signs_after_rate_limit_queue(queued_clock):
    calls = []

    async def request(method, url, **kwargs):
        assert method == "GET"
        calls.append(urlsplit(url).path)
        if urlsplit(url).path == "/fapi/v1/time":
            return clock_response(queued_clock, url)
        assert_fresh_signature(url, queued_clock)
        return httpx.Response(200, json={}, request=httpx.Request(method, url))

    client = live.BinanceLiveClient(SimpleNamespace(request=request), "offline-key", "offline-secret")
    asyncio.run(client.signed("GET", "/fapi/v3/account"))
    assert calls == ["/fapi/v1/time", "/fapi/v3/account"]


def test_verification_time_is_public_but_permission_and_account_reads_are_signed(queued_clock):
    calls = []

    async def public_get(url, headers=None):
        assert headers is None and urlsplit(url).path == "/fapi/v1/time"
        calls.append(("direct", urlsplit(url).path))
        return clock_response(queued_clock, url)

    async def signed_get(url, headers=None):
        assert headers == {"X-MBX-APIKEY": "offline-key"}
        assert_fresh_signature(url, queued_clock)
        path = urlsplit(url).path
        calls.append(("signed", path))
        payload = {"enableWithdrawals": False} if path.endswith("apiRestrictions") else {}
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    result = asyncio.run(connections.test_binance_credentials(
        SimpleNamespace(get=signed_get), "LIVE", "offline-key", "offline-secret",
        public_http=SimpleNamespace(get=public_get),
    ))
    assert result["orders_created"] is False
    assert calls == [("direct", "/fapi/v1/time"), ("signed", "/sapi/v1/account/apiRestrictions"),
                     ("signed", "/fapi/v3/account"), ("signed", "/fapi/v1/positionSide/dual")]
