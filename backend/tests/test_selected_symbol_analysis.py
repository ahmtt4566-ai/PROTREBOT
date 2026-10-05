import asyncio
import ast
import math
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, Query

from app.analysis import analyze


def public_analysis_routes(fetch):
    source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    names = {"fetch_candles", "klines", "technical_analysis", "normalize_analysis_signal"}
    nodes = [
        node for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    assert {node.name for node in nodes} == names
    for node in nodes:
        node.decorator_list = []
    namespace = {
        "asyncio": asyncio, "time": time, "httpx": httpx, "HTTPException": HTTPException,
        "Query": Query, "analyze": analyze, "app": SimpleNamespace(),
        "ALLOWED_INTERVALS": {"1m", "5m", "15m", "1h", "4h"},
        "CANDLE_CACHE": {}, "CANDLE_INFLIGHT": {}, "market_data_request": fetch,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<public-analysis-routes>", "exec"), namespace)
    return namespace


def candle_rows(price, count):
    rows = []
    for index in range(count):
        close = price * (1 + index * 0.00001 + math.sin(index * 0.3) * 0.001)
        rows.append([
            1_791_200_000_000 + index * 60_000, str(close * 0.9999), str(close * 1.0004),
            str(close * 0.9996), str(close), str(1000 + index),
        ])
    return rows


@pytest.mark.parametrize("symbol,price", [
    ("BTCUSDT", 85000), ("ETHUSDT", 3200), ("SOLUSDT", 145),
    ("1000PEPEUSDT", 0.01), ("1000SHIBUSDT", 0.02),
    ("1MBABYDOGEUSDT", 0.001), ("BTCDOMUSDT", 3600),
])
def test_each_selected_symbol_uses_its_own_candles_and_real_analysis(symbol, price):
    calls = []

    async def fetch(_app, path, params):
        calls.append((path, params.copy()))
        assert path == "/fapi/v1/klines"
        assert params["symbol"] == symbol
        return httpx.Response(
            200, json=candle_rows(price, params["limit"]),
            request=httpx.Request("GET", f"https://offline.invalid{path}"),
        )

    routes = public_analysis_routes(fetch)

    async def run():
        candles = await routes["klines"](symbol, interval="15m", limit=160)
        assert len(candles) == 160
        assert candles[-1]["close"] == float(candle_rows(price, 160)[-1][4])
        for interval in ["1m", "5m", "15m", "1h", "4h"]:
            result = await routes["technical_analysis"](symbol, interval=interval)
            expected = analyze([
                {"time": row[0] // 1000, "open": float(row[1]), "high": float(row[2]),
                 "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])}
                for row in candle_rows(price, 500)
            ])
            assert result == {**expected, "normalized_signal": routes["normalize_analysis_signal"](expected["direction"])}
            assert result["entry"] == float(candle_rows(price, 500)[-1][4])
            for field in ["confidence", "stop_loss", "tp1", "tp2", "tp3", "rsi", "macd", "atr", "support", "resistance"]:
                assert math.isfinite(result[field])

    asyncio.run(run())
    assert len(calls) == 6
    assert all(params["symbol"] == symbol for _, params in calls)
    assert {params["interval"] for _, params in calls} == {"1m", "5m", "15m", "1h", "4h"}


def test_new_contract_without_enough_history_remains_unavailable_not_fabricated():
    async def fetch(_app, path, params):
        return httpx.Response(
            200, json=candle_rows(0.01, 219),
            request=httpx.Request("GET", f"https://offline.invalid{path}"),
        )

    routes = public_analysis_routes(fetch)
    with pytest.raises(HTTPException) as error:
        asyncio.run(routes["technical_analysis"]("1000PEPEUSDT"))
    assert error.value.status_code == 422
    assert error.value.detail == "Analiz için yeterli mum verisi yok"
