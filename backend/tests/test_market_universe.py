import asyncio
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, Query
from fastapi.responses import JSONResponse

from app.market_universe import MarketUniverseCache, market_rows, ranked_original_live_symbols


def contract(symbol, **fields):
    return {"symbol": symbol, "baseAsset": symbol[:-4], "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", **fields}


def ticker(symbol):
    return {"symbol": symbol, "lastPrice": "1.2", "priceChangePercent": "-2.3", "quoteVolume": "100"}

@pytest.mark.parametrize("age_days,eligible", [(30, False), (43.34, False), (44.999, False), (45, True), (46, True)])
def test_original_live_age_covers_longest_strategy_history(age_days, eligible):
    now_ms = 2_000_000_000_000
    contracts = [
        contract("OLDUSDT", onboardDate=now_ms - 90 * 86400000),
        contract("PONSUSDT", onboardDate=now_ms - int(age_days * 86400000)),
    ]
    ranked, _ = ranked_original_live_symbols(
        {"symbols": contracts},
        [{"symbol": row["symbol"], "quoteVolume": "2000000"} for row in contracts],
        [{"symbol": row["symbol"], "bidPrice": "100", "askPrice": "100.04"} for row in contracts],
        now_ms=now_ms,
    )
    assert ("PONSUSDT" in ranked) is eligible


def test_universe_has_no_500_limit_and_keeps_missing_quotes_unknown():
    info = {"symbols": [contract(f"TOKEN{index}USDT") for index in range(650)] + [
        contract("CLOSEDUSDT", status="BREAK"), contract("QUARTERUSDT", contractType="CURRENT_QUARTER"), contract("OTHERUSDC", quoteAsset="USDC"),
    ]}
    rows = market_rows(info, [ticker("TOKEN0USDT")])
    assert len(rows) == 650
    assert rows[0]["price"] == 1.2
    assert next(row for row in rows if row["symbol"] == "TOKEN1USDT")["price"] is None
    assert all(row["status"] == "TRADING" and row["contractType"] == "PERPETUAL" and row["quoteAsset"] == "USDT" for row in rows)


def test_original_live_universe_filters_age_volume_spread_and_ranks_by_quote_volume():
    now_ms = 2_000_000_000_000
    old = now_ms - 46 * 24 * 60 * 60 * 1000
    recent = now_ms - 44 * 24 * 60 * 60 * 1000
    contracts = [
        contract("BTCUSDT", onboardDate=old),
        contract("ETHUSDT", onboardDate=old),
        contract("NEWUSDT", onboardDate=recent),
        contract("LOWUSDT", onboardDate=old),
        contract("WIDEUSDT", onboardDate=old),
        contract("CLOSEDUSDT", onboardDate=old, status="BREAK"),
    ]
    tickers = [
        {"symbol": "BTCUSDT", "quoteVolume": "1000000"},
        {"symbol": "ETHUSDT", "quoteVolume": "3000000"},
        {"symbol": "NEWUSDT", "quoteVolume": "9000000"},
        {"symbol": "LOWUSDT", "quoteVolume": "999999"},
        {"symbol": "WIDEUSDT", "quoteVolume": "8000000"},
        {"symbol": "CLOSEDUSDT", "quoteVolume": "9000000"},
    ]
    books = [
        {"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "100.04"},
        {"symbol": "ETHUSDT", "bidPrice": "100", "askPrice": "100.04"},
        {"symbol": "NEWUSDT", "bidPrice": "100", "askPrice": "100.04"},
        {"symbol": "LOWUSDT", "bidPrice": "100", "askPrice": "100.04"},
        {"symbol": "WIDEUSDT", "bidPrice": "100", "askPrice": "100.10"},
        {"symbol": "CLOSEDUSDT", "bidPrice": "100", "askPrice": "100.04"},
    ]

    filter_counts = {}
    ranked, eligible_count = ranked_original_live_symbols(
        {"symbols": contracts}, tickers, books, now_ms=now_ms,
        diagnostics=filter_counts,
    )

    assert ranked == ["ETHUSDT", "BTCUSDT"]
    assert eligible_count == 2
    assert filter_counts == {
        "active_contracts": 5,
        "age_below_required_history": 1,
        "market_data_missing": 0,
        "invalid_market_data": 0,
        "volume_below_minimum": 1,
        "spread_too_wide": 1,
        "eligible": 2,
    }


def test_original_live_universe_exposes_missing_daily_market_data_before_failing():
    now_ms = 2_000_000_000_000
    old = now_ms - 46 * 24 * 60 * 60 * 1000
    contracts = [contract("BTCUSDT", onboardDate=old), contract("ETHUSDT", onboardDate=old)]
    diagnostics = {}

    with pytest.raises(ValueError, match="Incomplete Binance Futures daily market data"):
        ranked_original_live_symbols(
            {"symbols": contracts},
            [{"symbol": "BTCUSDT", "quoteVolume": "1000000"}],
            [
                {"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "100.04"},
                {"symbol": "ETHUSDT", "bidPrice": "100", "askPrice": "100.04"},
            ],
            now_ms=now_ms,
            diagnostics=diagnostics,
        )

    assert diagnostics["market_data_missing"] == 1


def test_original_live_universe_caps_cached_ranked_list_at_owner_maximum():
    now_ms = 2_000_000_000_000
    onboard_date = now_ms - 46 * 24 * 60 * 60 * 1000
    contracts = [contract(f"TOKEN{index}USDT", onboardDate=onboard_date) for index in range(101)]
    tickers = [{"symbol": row["symbol"], "quoteVolume": str(2_000_000 - index)} for index, row in enumerate(contracts)]
    books = [{"symbol": row["symbol"], "bidPrice": "100", "askPrice": "100.04"} for row in contracts]

    ranked, eligible_count = ranked_original_live_symbols(
        {"symbols": contracts}, tickers, books, now_ms=now_ms,
    )

    assert eligible_count == 101
    assert len(ranked) == 100
    assert "TOKEN100USDT" not in ranked


def test_singleflight_ticker_3_seconds_metadata_10_minutes_and_explicit_stale_backoff():
    async def run():
        now = [0.0]
        cache = MarketUniverseCache(lambda: now[0])
        calls = []
        fail = [False]

        async def fetch(path):
            calls.append(path)
            request = httpx.Request("GET", f"https://fixture.invalid{path}")
            if fail[0]:
                return httpx.Response(429, headers={"Retry-After": "120"}, request=request)
            payload = {"symbols": [contract("BTCUSDT")]} if path.endswith("exchangeInfo") else [ticker("BTCUSDT")]
            return httpx.Response(200, json=payload, request=request)

        await asyncio.gather(*(cache.get(fetch) for _ in range(10)))
        assert calls == ["/fapi/v1/exchangeInfo", "/fapi/v1/ticker/24hr"]
        now[0] = 3
        rows, stale = await cache.get(fetch)
        assert not stale
        assert calls.count("/fapi/v1/exchangeInfo") == 1
        assert calls.count("/fapi/v1/ticker/24hr") == 2
        fail[0] = True
        now[0] = 6
        old, stale = await cache.get(fetch)
        assert stale and old == rows
        count = len(calls)
        now[0] = 100
        assert (await cache.get(fetch))[1]
        assert len(calls) == count
        fail[0] = False
        now[0] = 601
        assert not (await cache.get(fetch))[1]
        assert calls.count("/fapi/v1/exchangeInfo") == 2

    asyncio.run(run())


def test_cold_failure_is_error_not_empty_success():
    async def run():
        async def fetch(path):
            raise httpx.ConnectError("Offline fixture")
        with pytest.raises(HTTPException) as error:
            await MarketUniverseCache().get(fetch)
        assert error.value.status_code == 502
    asyncio.run(run())


def test_existing_market_limit_and_internal_calls_remain_unchanged():
    source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    node = next(node for node in ast.parse(source).body if isinstance(node, ast.AsyncFunctionDef) and node.name == "markets")
    node.decorator_list = []
    calls = []

    async def limited(limit):
        calls.append(limit)
        return [{"legacy": True}]

    namespace = {"Query": Query, "_markets": limited, "JSONResponse": JSONResponse, "MarketUniverseCache": MarketUniverseCache, "app": SimpleNamespace(state=SimpleNamespace())}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<markets>", "exec"), namespace)
    assert asyncio.run(namespace["markets"](limit=40)) == [{"legacy": True}]
    assert calls == [40]

    async def fetch(_app, path):
        calls.append(path)
        payload = {"symbols": [contract(f"TOKEN{index}USDT") for index in range(650)]} if path.endswith("exchangeInfo") else [ticker("TOKEN0USDT")]
        return httpx.Response(200, json=payload, request=httpx.Request("GET", f"https://fixture.invalid{path}"))

    namespace["market_data_request"] = fetch
    response = asyncio.run(namespace["markets"](all=True))
    assert len(json.loads(response.body)) == 650
    assert response.headers["X-Market-Stale"] == "0"
    assert response.headers["Cache-Control"] == "no-store"
    assert calls == [40, "/fapi/v1/exchangeInfo", "/fapi/v1/ticker/24hr"]


@pytest.mark.parametrize("header", ["NaN", "Infinity", "-100", "invalid"])
def test_invalid_retry_after_never_creates_permanent_cache_backoff(header, caplog):
    async def run():
        cache = MarketUniverseCache(lambda: 10)

        async def fetch(path):
            return httpx.Response(429, headers={"Retry-After": header}, request=httpx.Request("GET", f"https://fixture.invalid{path}"))

        with pytest.raises(HTTPException):
            await cache.get(fetch)
        assert cache.retry_at == 40
        assert "Invalid market Retry-After header" in caplog.text

    asyncio.run(run())
