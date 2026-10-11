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

from .binance_rate_limit import retry_after_seconds

logger = logging.getLogger(__name__)
ORIGINAL_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT")
ORIGINAL_INTERVALS = ("15m", "1h", "4h")
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
        self.waiters: list[tuple[int, int]] = []
        self.sequence = 0
        self.active = 0
        self.retry_at = 0.0
        self.last_rejection = 429
        self.original_until = 0.0
        self.warmer_task: asyncio.Task | None = None
        self.counts: Counter[str] = Counter()
        self.metrics_at = clock()
        self.used_weight_1m: int | None = None

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
        remaining = math.ceil(self.retry_at - self.clock())
        if remaining <= 0:
            return None
        self.counts["backoff_hits"] += 1
        return httpx.Response(
            self.last_rejection, headers={"Retry-After": str(remaining)},
            json={"code": -1003, "msg": "Binance public market-data cooldown active."},
            request=httpx.Request("GET", f"https://fapi.binance.com{path}"),
        )

    def observe(self, path: str, response: httpx.Response) -> None:
        self.counts[f"upstream:{path}:{response.status_code}"] += 1
        weight = response.headers.get("X-MBX-USED-WEIGHT-1M", "")
        if weight.isdigit():
            self.used_weight_1m = int(weight)
        if response.status_code in {418, 429}:
            delay = retry_after_seconds(response) or 30
            self.retry_at = max(self.retry_at, self.clock() + delay)
            self.last_rejection = response.status_code
        self.report_metrics()

    def report_metrics(self) -> None:
        if self.clock() - self.metrics_at >= 60:
            logger.info("PUBLIC_MARKET_METRICS transport=direct window_seconds=%.1f used_weight_1m=%s cooldown_seconds=%s counts=%s",
                        self.clock() - self.metrics_at, self.used_weight_1m,
                        max(0, math.ceil(self.retry_at - self.clock())), dict(self.counts))
            self.counts.clear()
            self.metrics_at = self.clock()

    async def get(
        self, path: str, params: dict[str, Any] | None,
        fetch: Callable[[str, dict[str, Any]], Awaitable[httpx.Response]],
    ) -> httpx.Response:
        if path not in PUBLIC_PATHS or any(key in (params or {}) for key in ("signature", "timestamp", "recvWindow")):
            raise ValueError("Market-data transport only accepts public read-only endpoints.")
        values = dict(params or {})
        limit = None
        if path == "/fapi/v1/klines":
            limit = int(values.get("limit", 500))
            values["limit"] = max(500, limit)
            values["symbol"] = str(values.get("symbol", "")).upper()
            if values["symbol"] in ORIGINAL_SYMBOLS and "endTime" not in values and "startTime" not in values \
                    and asyncio.current_task() is not self.warmer_task:
                self.original_until = self.clock() + 90
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
                task = asyncio.create_task(self._fetch(key, path, values, fetch))
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

    async def _fetch(self, key, path, values, fetch) -> httpx.Response:
        try:
            async with self.slot(values.get("symbol") in ORIGINAL_SYMBOLS):
                blocked = self.blocked(path)
                if blocked is not None:
                    return blocked
                response = await fetch(path, values)
                if response.is_success:
                    ttl = 600 if path.endswith("exchangeInfo") else 15 if path.endswith(("klines", "24hr")) else 0 if path.endswith("time") else 1
                    if path.endswith("klines") and "endTime" not in values and "startTime" not in values:
                        seconds = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}.get(values.get("interval"))
                        if seconds:
                            ttl = min(ttl, seconds - time.time() % seconds)
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


async def original_market_warm_loop(application, request) -> None:
    while True:
        service = getattr(application.state, "public_market_data", None)
        if service is not None:
            service.report_metrics()
            service.warmer_task = asyncio.current_task()
            for symbol in ORIGINAL_SYMBOLS:
                for interval in ORIGINAL_INTERVALS:
                    try:
                        response = await request(application, "/fapi/v1/klines",
                                                 {"symbol": symbol, "interval": interval, "limit": 260})
                        response.raise_for_status()
                    except httpx.HTTPError as exc:
                        failed_response = exc.response if isinstance(exc, httpx.HTTPStatusError) else None
                        logger.warning("ORIGINAL_MARKET_WARM unavailable symbol=%s interval=%s error=%s status=%s retry_after_seconds=%s",
                                       symbol, interval, type(exc).__name__,
                                       failed_response.status_code if failed_response is not None else None,
                                       retry_after_seconds(failed_response) if failed_response is not None else None)
                        break
                if service.retry_at > service.clock():
                    break
        await asyncio.sleep(15)
