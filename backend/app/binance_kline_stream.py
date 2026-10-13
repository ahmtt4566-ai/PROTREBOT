from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import WebSocketException

from .binance_rate_limit import BINANCE_GLOBAL_BAN
from .public_market_data import ORIGINAL_INTERVALS

logger = logging.getLogger(__name__)

KLINE_STREAM_URL = "wss://fstream.binance.com/ws"
MAX_CACHED_CANDLES = 1500
ALLOWED_INTERVALS = frozenset(ORIGINAL_INTERVALS)
INTERVAL_MILLISECONDS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}


class KlineStreamUnavailable(RuntimeError):
    def __init__(self, retry_after: int):
        super().__init__("Binance geçici engeli sürerken mum önbelleği eksik.")
        self.retry_after = retry_after


class BinanceKlineStream:
    def __init__(
        self,
        *,
        connector: Callable[..., Any] | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._connector = connector or websockets.connect
        self._wall_clock = wall_clock
        self._symbols: tuple[str, ...] = ()
        self._intervals: tuple[str, ...] = ()
        self._candles: dict[tuple[str, str], list[list[Any]]] = {}
        self._configure_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._generation = 0
        self.connected = False
        self.messages_received = 0
        self.invalid_messages = 0
        self.reconnects = 0
        self.rest_backfills = 0
        self.last_message_at: float | None = None

    def is_subscribed(self, symbol: str, interval: str) -> bool:
        return symbol.upper() in self._symbols and interval in self._intervals

    def candles(self, symbol: str, interval: str) -> list[list[Any]]:
        return [row.copy() for row in self._candles.get((symbol.upper(), interval), [])]

    def set_candles(self, symbol: str, interval: str, rows: list[list[Any]]) -> None:
        self._merge_rows(symbol.upper(), interval, rows)

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "symbols": len(self._symbols),
            "intervals": list(self._intervals),
            "stream_count": len(self._symbols) * len(self._intervals),
            "messages_received": self.messages_received,
            "invalid_messages": self.invalid_messages,
            "reconnects": self.reconnects,
            "rest_backfills": self.rest_backfills,
            "last_message_at": self.last_message_at,
            "source": "binance_futures_kline_websocket",
        }

    async def configure(self, symbols: list[str], intervals: tuple[str, ...] = ORIGINAL_INTERVALS) -> None:
        if (
            not isinstance(symbols, list)
            or len(symbols) > 100
            or any(
                not isinstance(symbol, str)
                or not symbol.isascii()
                or not symbol.isalnum()
                or not symbol.isupper()
                or not symbol.endswith("USDT")
                for symbol in symbols
            )
            or len(set(symbols)) != len(symbols)
            or not intervals
            or any(interval not in ALLOWED_INTERVALS for interval in intervals)
            or len(set(intervals)) != len(intervals)
        ):
            raise ValueError("Invalid Original V2 kline stream configuration")

        next_symbols = tuple(symbols)
        next_intervals = tuple(intervals)
        async with self._configure_lock:
            if next_symbols == self._symbols and next_intervals == self._intervals:
                if self._symbols and (self._task is None or self._task.done()):
                    self._generation += 1
                    self._task = asyncio.create_task(self._run(self._generation))
                return

            self._generation += 1
            task = self._task
            self._task = None
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            self.connected = False
            self._symbols = next_symbols
            self._intervals = next_intervals
            active_keys = {
                (symbol, interval)
                for symbol in self._symbols
                for interval in self._intervals
            }
            self._candles = {
                key: rows for key, rows in self._candles.items() if key in active_keys
            }
            if self._symbols:
                self._task = asyncio.create_task(self._run(self._generation))
                logger.info(
                    "ORIGINAL_KLINE_STREAM configured symbols=%s intervals=%s stream_count=%s",
                    len(self._symbols),
                    ",".join(self._intervals),
                    len(self._symbols) * len(self._intervals),
                )

    async def close(self) -> None:
        async with self._configure_lock:
            self._generation += 1
            task = self._task
            self._task = None
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.connected = False

    async def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int,
        rest_fetch: Callable[[dict[str, Any]], Awaitable[Any]],
    ) -> list[list[Any]]:
        symbol = symbol.upper()
        if not self.is_subscribed(symbol, interval):
            raise ValueError("Symbol and interval are not subscribed to the Original V2 stream")
        if limit < 1:
            raise ValueError("Kline limit must be positive")

        rows = self.candles(symbol, interval)
        interval_ms = INTERVAL_MILLISECONDS[interval]
        expected_open_time = int(self._wall_clock() * 1000 // interval_ms * interval_ms)
        needs_backfill = (
            len(rows) < limit
            or not rows
            or int(rows[-1][0]) < expected_open_time
        )
        if needs_backfill:
            retry_after = BINANCE_GLOBAL_BAN.remaining_seconds()
            if retry_after > 0:
                raise KlineStreamUnavailable(retry_after)
            reason = "bootstrap" if len(rows) < limit else "missing_current_candle"
            logger.info(
                "ORIGINAL_KLINE_REST_BACKFILL symbol=%s interval=%s reason=%s limit=%s source=original_live",
                symbol,
                interval,
                reason,
                limit,
            )
            self.rest_backfills += 1
            fetched = await rest_fetch({"symbol": symbol, "interval": interval, "limit": limit})
            if not isinstance(fetched, list):
                raise ValueError("Binance kline backfill returned an invalid payload")
            self._merge_rows(symbol, interval, fetched)
            rows = self.candles(symbol, interval)
        return rows[-limit:]

    def _merge_rows(self, symbol: str, interval: str, rows: list[list[Any]]) -> None:
        key = (symbol, interval)
        candles = {int(row[0]): row for row in self._candles.get(key, []) if isinstance(row, list) and row}
        for row in rows:
            if isinstance(row, list) and len(row) >= 6:
                try:
                    open_time = int(row[0])
                except (TypeError, ValueError, OverflowError):
                    continue
                candles[open_time] = row.copy()
        self._candles[key] = [
            candles[open_time] for open_time in sorted(candles)[-MAX_CACHED_CANDLES:]
        ]

    def _ingest(self, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("e") != "kline":
            return
        kline = payload.get("k")
        if not isinstance(kline, dict):
            self.invalid_messages += 1
            return
        symbol = str(payload.get("s") or "").upper()
        interval = str(kline.get("i") or "")
        if not self.is_subscribed(symbol, interval):
            return
        try:
            row = [
                int(kline["t"]),
                kline["o"],
                kline["h"],
                kline["l"],
                kline["c"],
                kline["v"],
                int(kline["T"]),
                kline.get("q", "0"),
                int(kline.get("n", 0)),
                kline.get("V", "0"),
                kline.get("Q", "0"),
                kline.get("B", "0"),
            ]
        except (KeyError, TypeError, ValueError, OverflowError):
            self.invalid_messages += 1
            return
        self._merge_rows(symbol, interval, [row])
        self.messages_received += 1
        self.last_message_at = self._wall_clock()

    async def _run(self, generation: int) -> None:
        params = [
            f"{symbol.lower()}@kline_{interval}"
            for symbol in self._symbols
            for interval in self._intervals
        ]
        delay = 1
        while generation == self._generation:
            try:
                async with self._connector(
                    KLINE_STREAM_URL,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    max_queue=4096,
                ) as socket:
                    if generation != self._generation:
                        return
                    await socket.send(json.dumps({"method": "SUBSCRIBE", "params": params, "id": generation}))
                    self.connected = True
                    delay = 1
                    logger.info(
                        "ORIGINAL_KLINE_STREAM connected symbols=%s intervals=%s stream_count=%s",
                        len(self._symbols),
                        ",".join(self._intervals),
                        len(params),
                    )
                    while generation == self._generation:
                        raw = await asyncio.wait_for(socket.recv(), timeout=45)
                        self.last_message_at = self._wall_clock()
                        try:
                            payload = json.loads(raw)
                        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                            self.invalid_messages += 1
                            logger.warning("ORIGINAL_KLINE_STREAM invalid_message")
                            continue
                        self._ingest(payload)
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.TimeoutError, WebSocketException) as exc:
                self.connected = False
                self.reconnects += 1
                logger.warning(
                    "ORIGINAL_KLINE_STREAM disconnected error_type=%s retry_seconds=%s",
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
            finally:
                self.connected = False
