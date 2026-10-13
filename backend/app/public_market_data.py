"""Shared, bounded public Binance reads. Never accepts signed requests."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx

from .binance_rate_limit import BINANCE_GLOBAL_BAN, retry_after_seconds

logger = logging.getLogger(__name__)
ORIGINAL_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT")
ORIGINAL_INTERVALS = ("15m", "1h", "4h")
ORIGINAL_REQUEST_INTERVAL_SECONDS = 0.52
PUBLIC_PATHS = frozenset({
    "/fapi/v1/ping", "/fapi/v1/time", "/fapi/v1/exchangeInfo",
    "/fapi/v1/ticker/price", "/fapi/v1/ticker/bookTicker",
    "/fapi/v1/ticker/24hr", "/fapi/v1/klines",
    "/api/v3/depth", "/api/v3/ticker/price",
})


class PublicMarketData:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.cache: OrderedDict[tuple, tuple[float, httpx.Response]] = OrderedDict()
        self.inflight: dict[tuple, asyncio.Task] = {}
        self.condition = asyncio.Condition()
        self.original_request_pacer = asyncio.Lock()
        self.next_original_request_at = 0.0
        self.waiters: list[tuple[int, int]] = []
        self.sequence = 0
        self.active = 0
        self.retry_at = 0.0
        self.last_rejection = 429
        self.original_symbols = set(ORIGINAL_SYMBOLS)
        self.counts: Counter[str] = Counter()
        self.source_counts: Counter[str] = Counter()
        self.source_weights: Counter[str] = Counter()
        self.metrics_at = clock()
        self.used_weight_1m: int | None = None

    def set_original_symbols(self, symbols: list[str]) -> None:
        if (
            not isinstance(symbols, list)
            or len(symbols) > 100
            or any(not isinstance(symbol, str) or not symbol.isascii() or not symbol.isalnum()
                   or not symbol.isupper() or not symbol.endswith("USDT") for symbol in symbols)
            or len(set(symbols)) != len(symbols)
        ):
            raise ValueError("Invalid Original V2 market symbols")
        self.original_symbols = set(symbols)

    @asynccontextmanager
    async def slot(self, priority: bool):
        async with self.condition:
            self.sequence += 1
            ticket = (0 if priority else 1, self.sequence)
            self.waiters.append(ticket)
            try:
                await self.condition.wait_for(lambda: self.active < 2 and ticket == min(self.waiters))
            finally:
                self.waiters.remove(ticket)
                self.condition.notify_all()
            self.active += 1
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def blocked(self, path: str) -> httpx.Response | None:
        global_block = BINANCE_GLOBAL_BAN.synthetic_response(path)
        if global_block is not None:
            self.counts["global_ban_hits"] += 1
            return global_block
        remaining = math.ceil(self.retry_at - self.clock())
        if remaining <= 0:
            return None
        self.counts["backoff_hits"] += 1
        return httpx.Response(
            self.last_rejection, headers={"Retry-After": str(remaining)},
            json={"code": -1003, "msg": "Binance public market-data cooldown active."},
            request=httpx.Request("GET", f"https://fapi.binance.com{path}"),
        )

    def observe(self, path: str, response: httpx.Response, *, source: str = "other") -> None:
        self.counts[f"upstream:{path}:{response.status_code}"] += 1
        weight = response.headers.get("X-MBX-USED-WEIGHT-1M", "")
        if weight.isdigit():
            self.used_weight_1m = int(weight)
        if response.status_code in {418, 429}:
            delay = retry_after_seconds(response) or 30
            self.retry_at = max(self.retry_at, self.clock() + delay)
            self.last_rejection = response.status_code
            if source != "other":
                BINANCE_GLOBAL_BAN.record(
                    response,
                    source=source,
                    egress_route="render_direct",
                )
        self.report_metrics()

    def report_metrics(self) -> None:
        if self.clock() - self.metrics_at >= 60:
            logger.info("PUBLIC_MARKET_METRICS transport=direct window_seconds=%.1f used_weight_1m=%s cooldown_seconds=%s counts=%s sources=%s estimated_weights=%s",
                        self.clock() - self.metrics_at, self.used_weight_1m,
                        max(0, math.ceil(self.retry_at - self.clock())), dict(self.counts),
                        dict(self.source_counts), dict(self.source_weights))
            self.counts.clear()
            self.source_counts.clear()
            self.source_weights.clear()
            self.metrics_at = self.clock()

    @staticmethod
    def estimated_request_weight(path: str, values: dict[str, Any]) -> int:
        if path == "/fapi/v1/klines":
            limit = int(values.get("limit", 500))
            if limit < 100:
                return 1
            if limit < 500:
                return 2
            if limit <= 1000:
                return 5
            return 10
        if path == "/fapi/v1/ticker/24hr":
            return 1 if values.get("symbol") else 40
        if path in {"/fapi/v1/ticker/price", "/fapi/v1/ticker/bookTicker"}:
            return 1 if values.get("symbol") else 2
        return 1

    async def get(
        self, path: str, params: dict[str, Any] | None,
        fetch: Callable[[str, dict[str, Any]], Awaitable[httpx.Response]],
        *,
        source: str = "public_api",
    ) -> httpx.Response:
        if path not in PUBLIC_PATHS or any(key in (params or {}) for key in ("signature", "timestamp", "recvWindow")):
            raise ValueError("Market-data transport only accepts public read-only endpoints.")
        values = dict(params or {})
        limit = None
        if path == "/fapi/v1/klines":
            limit = int(values.get("limit", 500))
            cache_floor = 260 if source in {"original_live", "original_warmer"} else 500
            values["limit"] = max(cache_floor, limit)
            values["symbol"] = str(values.get("symbol", "")).upper()
        key = (path, tuple(sorted(values.items())))
        cached = self.cache.get(key)
        if cached and cached[0] > self.clock():
            self.cache.move_to_end(key)
            self.counts["cache_hits"] += 1
            response = cached[1]
        else:
            blocked = self.blocked(path)
            if blocked is not None:
                return blocked
            task = self.inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._fetch(key, path, values, fetch, source))
                self.inflight[key] = task
                task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            else:
                self.counts["shared_waiters"] += 1
            response = await asyncio.shield(task)
        if limit is not None and response.is_success:
            rows = response.json()
            if isinstance(rows, list):
                # Historical pages retain the oldest requested rows; live reads
                # retain the newest rows, including Binance's open candle.
                rows = rows[:limit] if "startTime" in values else rows[-limit:]
                headers = {name: value for name, value in response.headers.items() if name not in {"content-length", "content-encoding"}}
                return httpx.Response(response.status_code, json=rows, headers=headers, request=response.request)
        return response

    async def _fetch(self, key, path, values, fetch, source: str) -> httpx.Response:
        try:
            async with self.slot(values.get("symbol") in self.original_symbols):
                blocked = self.blocked(path)
                if blocked is not None:
                    return blocked
                if source == "original_live":
                    async with self.original_request_pacer:
                        delay = self.next_original_request_at - self.clock()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        blocked = self.blocked(path)
                        if blocked is not None:
                            return blocked
                        self.next_original_request_at = self.clock() + ORIGINAL_REQUEST_INTERVAL_SECONDS
                frame = values.get("interval", "bulk") if path.endswith("klines") else "bulk"
                source_key = f"{source}:GET:{path}:{frame}"
                self.source_counts[source_key] += 1
                self.source_weights[source_key] += self.estimated_request_weight(path, values)
                response = await fetch(path, values)
                self.observe(path, response, source=source)
                if response.is_success:
                    ttl = 600 if path.endswith("exchangeInfo") else 60 if path.endswith("klines") else 15 if path.endswith("24hr") else 0 if path.endswith("time") else 1
                    if path.endswith("klines") and "endTime" not in values and "startTime" not in values:
                        seconds = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}.get(values.get("interval"))
                        if seconds:
                            until_close = seconds - time.time() % seconds
                            ttl = (
                                max(1, until_close)
                                if source in {"original_live", "original_warmer"}
                                else max(1, min(ttl, until_close))
                            )
                    self.cache[key] = (self.clock() + ttl, response)
                    self.cache.move_to_end(key)
                elif response.status_code not in {418, 429}:
                    self.cache[key] = (self.clock() + 15, response)
                while len(self.cache) > 2048:
                    self.cache.popitem(last=False)
                return response
        except httpx.RequestError:
            self.retry_at = max(self.retry_at, self.clock() + 15)
            self.last_rejection = 503
            raise
        finally:
            self.inflight.pop(key, None)
