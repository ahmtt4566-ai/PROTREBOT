"""Process-local Binance REST rate-limit coordination."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import Counter
from email.utils import parsedate_to_datetime
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


RATE_LIMIT_AWARE_BACKOFF_ENABLED = os.getenv(
    "BINANCE_RATE_LIMIT_AWARE_BACKOFF_ENABLED",
    os.getenv("V25_RATE_LIMIT_AWARE_BACKOFF_ENABLED", "true"),
).strip().lower() not in {"0", "false", "no", "off"}
BINANCE_REQUEST_WEIGHT_LIMIT_1M = int(os.getenv("BINANCE_REQUEST_WEIGHT_LIMIT_1M", "2400"))
BINANCE_REQUEST_WEIGHT_THRESHOLD_RATIO = float(os.getenv("BINANCE_REQUEST_WEIGHT_THRESHOLD_RATIO", "0.8"))
RATE_LIMIT_PROACTIVE_WAIT_SECONDS = float(os.getenv("BINANCE_RATE_LIMIT_PROACTIVE_WAIT_SECONDS", "1"))
RATE_LIMIT_MAX_WAIT_SECONDS = max(1, int(os.getenv("BINANCE_RATE_LIMIT_MAX_WAIT_SECONDS", "60")))
RATE_LIMIT_FALLBACK_SECONDS = min(
    max(1, int(os.getenv("BINANCE_RATE_LIMIT_FALLBACK_SECONDS", "10"))),
    RATE_LIMIT_MAX_WAIT_SECONDS,
)
BINANCE_MAX_CONCURRENT_REQUESTS_PER_HOST = max(
    1, int(os.getenv("BINANCE_MAX_CONCURRENT_REQUESTS_PER_HOST", "8"))
)
logger = logging.getLogger(__name__)


def retry_after_seconds(response: Any) -> int | None:
    value = getattr(response, "headers", {}).get("Retry-After", "").strip()
    if not value:
        return None
    try:
        delay = int(value) if value.isdigit() else int(parsedate_to_datetime(value).timestamp() - time.time()) + 1
    except (ValueError, TypeError, OverflowError):
        logger.warning("Invalid Binance Retry-After header; using bounded fallback.")
        return None
    if delay < 0 or delay > 31_536_000:
        logger.warning("Invalid Binance Retry-After delay; using bounded fallback.")
        return None
    return max(1, delay)


@dataclass
class _HostState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    semaphore: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(BINANCE_MAX_CONCURRENT_REQUESTS_PER_HOST)
    )
    cooldown_until: float = 0.0
    used_weight_1m: int | None = None
    counts: Counter[str] = field(default_factory=Counter)
    metrics_at: float = field(default_factory=time.monotonic)


class RateLimitPermit:
    def __init__(self, limiter: "BinanceRateLimiter", host: str, state: _HostState) -> None:
        self._limiter = limiter
        self.host = host
        self._state = state

    def observe(self, response: Any, *, exchange_code: int | str | None = None) -> None:
        self._limiter.observe(self.host, response, exchange_code=exchange_code)


class BinanceRateLimiter:
    """Bound Binance calls per host and coordinate process-local cooldowns."""

    def __init__(self) -> None:
        self._states: dict[str, _HostState] = {}

    def _state_for(self, host: str) -> _HostState:
        return self._states.setdefault(host, _HostState())

    async def _wait(self, state: _HostState) -> None:
        if not RATE_LIMIT_AWARE_BACKOFF_ENABLED:
            return
        remaining = state.cooldown_until - time.monotonic()
        while remaining > 0:
            await asyncio.sleep(remaining)
            remaining = state.cooldown_until - time.monotonic()
        state.cooldown_until = 0.0

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[RateLimitPermit]:
        state = self._state_for(host)
        async with state.semaphore:
            async with state.lock:
                await self._wait(state)
            yield RateLimitPermit(self, host, state)

    def observe(self, host: str, response: Any, *, exchange_code: int | str | None = None) -> None:
        state = self._state_for(host)
        try:
            request = response.request
            state.counts[f"{request.method}:{request.url.path}:{response.status_code}"] += 1
        except (AttributeError, RuntimeError):
            state.counts[f"response:{getattr(response, 'status_code', 0)}"] += 1
        headers = getattr(response, "headers", {})
        used_weight = headers.get("X-MBX-USED-WEIGHT-1M", "")
        if used_weight.isdigit():
            state.used_weight_1m = int(used_weight)
        if time.monotonic() - state.metrics_at >= 60:
            logger.info("BINANCE_ACCOUNT_METRICS host=%s window_seconds=%.1f used_weight_1m=%s counts=%s",
                        host, time.monotonic() - state.metrics_at, state.used_weight_1m, dict(state.counts))
            state.counts.clear()
            state.metrics_at = time.monotonic()

        status_code = int(getattr(response, "status_code", 0) or 0)
        code = exchange_code
        try:
            payload = response.json()
        except (AttributeError, ValueError, TypeError):
            payload = None
        if code is None and isinstance(payload, dict):
            code = payload.get("code")
        try:
            is_rate_limited_code = int(code) == -1003
        except (TypeError, ValueError):
            is_rate_limited_code = False
        if not RATE_LIMIT_AWARE_BACKOFF_ENABLED:
            return
        if status_code in {429, 418} or is_rate_limited_code:
            delay = retry_after_seconds(response) or max(1, RATE_LIMIT_FALLBACK_SECONDS)
            state.cooldown_until = max(state.cooldown_until, time.monotonic() + delay)
        elif (
            state.used_weight_1m is not None
            and state.used_weight_1m >= BINANCE_REQUEST_WEIGHT_LIMIT_1M * BINANCE_REQUEST_WEIGHT_THRESHOLD_RATIO
        ):
            state.cooldown_until = max(
                state.cooldown_until,
                time.monotonic() + RATE_LIMIT_PROACTIVE_WAIT_SECONDS,
            )

    def snapshot(self, host: str) -> dict[str, int | float | None]:
        state = self._state_for(host)
        return {
            "cooldown_until": state.cooldown_until,
            "used_weight_1m": state.used_weight_1m,
        }

    def reset(self) -> None:
        self._states.clear()


BINANCE_RATE_LIMITER = BinanceRateLimiter()
