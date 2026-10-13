import asyncio
import json

import pytest

from app.binance_kline_stream import BinanceKlineStream, KlineStreamUnavailable
from app.binance_rate_limit import BINANCE_GLOBAL_BAN


def run(awaitable):
    return asyncio.run(awaitable)


class FakeSocket:
    def __init__(self, *, recv_error=None):
        self.recv_error = recv_error
        self.sent = []
        self.recv_started = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        self.recv_started.set()
        if self.recv_error is not None:
            raise self.recv_error
        await asyncio.Event().wait()


class FakeConnector:
    def __init__(self, sockets):
        self.sockets = iter(sockets)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return next(self.sockets)


def test_configure_subscribes_to_100_symbols_and_all_original_intervals():
    async def scenario():
        socket = FakeSocket()
        connector = FakeConnector([socket])
        stream = BinanceKlineStream(connector=connector)
        symbols = [f"TOKEN{index}USDT" for index in range(100)]

        await stream.configure(symbols)
        try:
            await asyncio.wait_for(socket.recv_started.wait(), timeout=1)
            subscription = socket.sent[0]
            assert subscription["method"] == "SUBSCRIBE"
            assert len(subscription["params"]) == 300
            assert subscription["params"][0] == "token0usdt@kline_15m"
            assert "token99usdt@kline_4h" in subscription["params"]
            assert stream.status()["stream_count"] == 300
        finally:
            await stream.close()

    run(scenario())


def test_ingested_kline_is_cached_and_replaces_the_same_open_candle():
    async def scenario():
        socket = FakeSocket()
        stream = BinanceKlineStream(connector=FakeConnector([socket]))
        await stream.configure(["BTCUSDT"])
        try:
            stream._ingest({
                "e": "kline",
                "s": "BTCUSDT",
                "k": {
                    "i": "15m", "t": 900_000, "T": 1_799_999,
                    "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10",
                    "q": "15", "n": 4, "V": "5", "Q": "7.5", "B": "0",
                },
            })
            first = stream.candles("BTCUSDT", "15m")
            assert len(first) == 1
            assert first[0][4] == "1.5"

            stream._ingest({
                "e": "kline",
                "s": "BTCUSDT",
                "k": {
                    "i": "15m", "t": 900_000, "T": 1_799_999,
                    "o": "1", "h": "3", "l": "0.5", "c": "2.5", "v": "11",
                },
            })
            cached = stream.candles("BTCUSDT", "15m")
            assert len(cached) == 1
            assert cached[0][4] == "2.5"
            assert stream.messages_received == 2
        finally:
            await stream.close()

    run(scenario())


def test_bootstrap_backfill_runs_once_then_uses_current_stream_cache():
    async def scenario():
        socket = FakeSocket()
        stream = BinanceKlineStream(
            connector=FakeConnector([socket]),
            wall_clock=lambda: 446_400,
        )
        await stream.configure(["BTCUSDT"])
        backfill_calls = []
        rows = [
            [index * 900_000, "1", "2", "0.5", "1.5", "10"]
            for index in range(237, 497)
        ]

        async def fetch(params):
            backfill_calls.append(params)
            return rows

        try:
            first = await stream.get_candles("BTCUSDT", "15m", 260, fetch)
            second = await stream.get_candles("BTCUSDT", "15m", 260, fetch)
            assert len(first) == len(second) == 260
            assert len(backfill_calls) == 1
            assert stream.rest_backfills == 1
        finally:
            await stream.close()

    run(scenario())


def test_active_global_ban_blocks_missing_cache_backfill(monkeypatch):
    async def scenario():
        socket = FakeSocket()
        stream = BinanceKlineStream(connector=FakeConnector([socket]))
        await stream.configure(["BTCUSDT"])

        async def forbidden_fetch(_params):
            raise AssertionError("REST backfill must be blocked during a global ban")

        monkeypatch.setattr(BINANCE_GLOBAL_BAN, "remaining_seconds", lambda: 120)

        try:
            with pytest.raises(KlineStreamUnavailable) as error:
                await stream.get_candles("BTCUSDT", "15m", 260, forbidden_fetch)
            assert error.value.retry_after == 120
            assert stream.rest_backfills == 0
        finally:
            await stream.close()

    run(scenario())


def test_stream_reconnects_after_socket_failure(monkeypatch):
    async def scenario():
        second_socket = FakeSocket()
        connector = FakeConnector([FakeSocket(recv_error=OSError("disconnected")), second_socket])
        stream = BinanceKlineStream(connector=connector)
        real_sleep = asyncio.sleep
        monkeypatch.setattr("app.binance_kline_stream.asyncio.sleep", lambda _seconds: real_sleep(0))
        await stream.configure(["BTCUSDT"])

        try:
            await asyncio.wait_for(second_socket.recv_started.wait(), timeout=1)
            assert connector.calls >= 2
            assert stream.reconnects >= 1
        finally:
            await stream.close()

    run(scenario())
