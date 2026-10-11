import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.public_market_data import ORIGINAL_INTERVALS, ORIGINAL_SYMBOLS, PublicMarketData, original_market_warm_loop


def test_fifty_users_graph_analysis_and_live_share_one_candle_request():
    async def run():
        service = PublicMarketData(lambda: 100.0)
        calls = []

        async def fetch(path, params):
            calls.append((path, params.copy()))
            await asyncio.sleep(0)
            return httpx.Response(200, json=[[i, "1", "2", "0", "1", "1", i + 1, "1"] for i in range(500)],
                                  request=httpx.Request("GET", "https://offline.invalid"))

        responses = await asyncio.gather(*(service.get("/fapi/v1/klines", {
            "symbol": "BTCUSDT", "interval": "15m", "limit": limit,
        }, fetch) for limit in [160, 260, 500] * 50))
        assert len(calls) == 1
        assert calls[0][1]["limit"] == 500
        assert [len(response.json()) for response in responses] == [160, 260, 500] * 50
        assert responses[0].json()[-1][0] == 499
        assert service.counts["shared_waiters"] == 149
        assert await service.get("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "15m", "limit": 260}, fetch)
        assert len(calls) == 1

    asyncio.run(run())


def test_expired_candles_are_refetched_not_used_for_trading(monkeypatch):
    monkeypatch.setattr("app.public_market_data.time.time", lambda: 100.0)

    async def run():
        now = [100.0]
        service = PublicMarketData(lambda: now[0])
        calls = []

        async def fetch(path, params):
            calls.append(path)
            return httpx.Response(200 if len(calls) == 1 else 503, json=[[len(calls)]],
                                  request=httpx.Request("GET", "https://offline.invalid"))

        params = {"symbol": "BTCUSDT", "interval": "15m", "limit": 260}
        assert (await service.get("/fapi/v1/klines", params, fetch)).status_code == 200
        now[0] = 116
        assert (await service.get("/fapi/v1/klines", params, fetch)).status_code == 503
        assert len(calls) == 2

    asyncio.run(run())


def test_one_minute_original_polling_is_bounded_independent_of_user_count(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("app.public_market_data.time.time", lambda: 1000.0 + now[0])

    async def run():
        service = PublicMarketData(lambda: now[0])
        calls = []
        downstream = 0

        async def fetch(path, params):
            calls.append((params["symbol"], params["interval"]))
            await asyncio.sleep(0)
            return httpx.Response(200, json=[[index] for index in range(500)],
                                  request=httpx.Request("GET", "https://offline.invalid"))

        for second in range(0, 60, 5):
            now[0] = float(second)
            requests = [service.get("/fapi/v1/klines", {
                "symbol": symbol, "interval": interval, "limit": (160, 260, 360)[user % 3],
            }, fetch) for user in range(50) for symbol in ORIGINAL_SYMBOLS for interval in ORIGINAL_INTERVALS]
            downstream += len(requests)
            await asyncio.gather(*requests)
        assert downstream == 14400
        assert len(calls) == 96
        assert set(calls) == {(symbol, interval) for symbol in ORIGINAL_SYMBOLS for interval in ORIGINAL_INTERVALS}

    asyncio.run(run())


def test_candle_boundary_invalidates_cache_even_before_fifteen_seconds(monkeypatch):
    monkeypatch.setattr("app.public_market_data.time.time", lambda: 899.0)

    async def run():
        now = [100.0]
        service = PublicMarketData(lambda: now[0])
        calls = []

        async def fetch(path, params):
            calls.append(path)
            return httpx.Response(200, json=[], request=httpx.Request("GET", "https://offline.invalid"))

        params = {"symbol": "BTCUSDT", "interval": "15m"}
        await service.get("/fapi/v1/klines", params, fetch)
        now[0] = 101.1
        await service.get("/fapi/v1/klines", params, fetch)
        assert len(calls) == 2

    asyncio.run(run())


def test_429_waits_full_retry_after_across_symbols_and_paths():
    async def run():
        now = [100.0]
        service = PublicMarketData(lambda: now[0])
        calls = []

        async def fetch(path, params):
            calls.append(path)
            response = httpx.Response(429, headers={"Retry-After": "180"},
                                      json={"code": -1003}, request=httpx.Request("GET", "https://offline.invalid"))
            service.observe(path, response)
            return response

        await service.get("/fapi/v1/klines", {"symbol": "BTCUSDT"}, fetch)
        now[0] = 170
        for path in ["/fapi/v1/exchangeInfo", "/fapi/v1/ticker/24hr", "/fapi/v1/klines"]:
            response = await service.get(path, {"symbol": "ETHUSDT"}, fetch)
            assert response.status_code == 429 and response.headers["Retry-After"] == "110"
        assert len(calls) == 1
        now[0] = 280
        await service.get("/fapi/v1/klines", {"symbol": "BTCUSDT"}, fetch)
        assert len(calls) == 2

    asyncio.run(run())


def test_original_requests_take_priority_over_queued_scanner_symbols():
    async def run():
        service = PublicMarketData()
        release = asyncio.Event()
        started = []

        async def fetch(path, params):
            started.append(params["symbol"])
            await release.wait()
            return httpx.Response(200, json=[], request=httpx.Request("GET", "https://offline.invalid"))

        tasks = [asyncio.create_task(service.get("/fapi/v1/klines", {"symbol": s}, fetch))
                 for s in ["OTHER1USDT", "OTHER2USDT", "OTHER3USDT", "BTCUSDT"]]
        while len(started) < 2 or len(service.waiters) < 2:
            await asyncio.sleep(0)
        assert started == ["OTHER1USDT", "OTHER2USDT"]
        release.set()
        await asyncio.gather(*tasks)
        assert started == ["OTHER1USDT", "OTHER2USDT", "BTCUSDT", "OTHER3USDT"]

    asyncio.run(run())


def test_cancelled_browser_does_not_cancel_shared_live_read():
    async def run():
        service = PublicMarketData()
        release = asyncio.Event()
        calls = []

        async def fetch(path, params):
            calls.append(path)
            await release.wait()
            return httpx.Response(200, json=[], request=httpx.Request("GET", "https://offline.invalid"))

        first = asyncio.create_task(service.get("/fapi/v1/klines", {"symbol": "BTCUSDT"}, fetch))
        while not calls:
            await asyncio.sleep(0)
        second = asyncio.create_task(service.get("/fapi/v1/klines", {"symbol": "BTCUSDT"}, fetch))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await second).status_code == 200
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("path,params", [("/fapi/v1/order", {}), ("/fapi/v1/klines", {"signature": "not-allowed"})])
def test_signed_paths_and_params_never_enter_public_transport(path, params):
    async def run():
        async def fetch(*args):
            pytest.fail("Forbidden public transport request")
        with pytest.raises(ValueError, match="public read-only"):
            await PublicMarketData().get(path, params, fetch)
    asyncio.run(run())


def test_original_warmer_only_reads_all_eight_symbols_and_required_frames():
    async def run():
        service = PublicMarketData()
        application = SimpleNamespace(state=SimpleNamespace(public_market_data=service))
        calls = []
        done = asyncio.Event()

        async def request(app, path, params):
            calls.append((path, params.copy()))
            if len(calls) == 24:
                done.set()
            return httpx.Response(200, json=[], request=httpx.Request("GET", "https://offline.invalid"))

        task = asyncio.create_task(original_market_warm_loop(application, request))
        await done.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 24
        assert {p["symbol"] for _, p in calls} == set(ORIGINAL_SYMBOLS)
        assert {p["interval"] for _, p in calls} == set(ORIGINAL_INTERVALS)
        assert all(path == "/fapi/v1/klines" for path, _ in calls)
    asyncio.run(run())


def test_priority_universe_matches_immutable_original_profile():
    from app.strategies.original_v2_demo import INTERVALS, fixed_profile
    assert set(ORIGINAL_SYMBOLS) == set(fixed_profile()["execution_policy"]["allowed_symbols"])
    assert ORIGINAL_INTERVALS == INTERVALS


def test_market_metrics_continue_during_cooldown_without_exposing_query_secrets(caplog):
    now = [100.0]
    service = PublicMarketData(lambda: now[0])
    response = httpx.Response(429, headers={"Retry-After": "180", "X-MBX-USED-WEIGHT-1M": "2400"},
                              request=httpx.Request("GET", "https://offline.invalid/fapi/v1/klines"))
    service.observe("/fapi/v1/klines", response)
    now[0] = 160.0
    with caplog.at_level("INFO", logger="app.public_market_data"):
        service.report_metrics()
    assert "transport=direct" in caplog.text
    assert "used_weight_1m=2400" in caplog.text
    assert "cooldown_seconds=120" in caplog.text
    assert "upstream:/fapi/v1/klines:429" in caplog.text
    assert service.counts == {}


def test_live_public_reads_use_direct_cache_and_accounts_keep_signed_transport(monkeypatch):
    from app import main, v25_execution as live
    from app.binance_rate_limit import BinanceRateLimiter
    calls = []

    async def public_get(url, params=None):
        calls.append(("direct", httpx.URL(url).path))
        path = httpx.URL(url).path
        payload = {"serverTime": int(main.time.time() * 1000)} if path.endswith("time") else []
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    async def private_request(method, url, **kwargs):
        assert method == "GET" and "signature=" in url
        assert kwargs["headers"] == {"X-MBX-APIKEY": "offline-key"}
        calls.append(("signed", httpx.URL(url).path))
        return httpx.Response(200, json={}, request=httpx.Request(method, url))

    public_http = SimpleNamespace(get=public_get)
    signed_http = SimpleNamespace(request=private_request)
    application = SimpleNamespace(state=SimpleNamespace(http=signed_http, market_http=public_http))
    client = live.BinanceLiveClient(signed_http, "offline-key", "offline-secret", application=application)
    monkeypatch.setattr(live, "BINANCE_RATE_LIMITER", BinanceRateLimiter())

    async def run():
        for limit in (160, 260, 500):
            assert await client.public_get("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "15m", "limit": limit}) == []
        assert await client.signed("GET", "/fapi/v3/account") == {}
    asyncio.run(run())
    assert calls == [("direct", "/fapi/v1/klines"), ("direct", "/fapi/v1/time"), ("signed", "/fapi/v3/account")]
    assert application.state.http is signed_http


def test_spot_depth_and_prices_share_direct_transport_not_signed_proxy(monkeypatch):
    from app import main
    calls = []

    async def direct_get(url, params=None):
        calls.append((url, params))
        return httpx.Response(200, json={"price": "100"}, request=httpx.Request("GET", url))

    async def forbidden(*args, **kwargs):
        pytest.fail("Public spot data must never use the signed proxy")

    application = SimpleNamespace(state=SimpleNamespace(
        market_http=SimpleNamespace(get=direct_get), http=SimpleNamespace(get=forbidden)))

    async def run():
        for path in ("/api/v3/depth", "/api/v3/ticker/price"):
            for _ in range(10):
                response = await main.market_data_request(application, path, {"symbol": "BTCUSDT"})
                assert response.status_code == 200

    asyncio.run(run())
    assert [url for url, _ in calls] == [f"{main.BINANCE_API}/api/v3/depth", f"{main.BINANCE_API}/api/v3/ticker/price"]
