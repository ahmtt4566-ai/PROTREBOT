"""Process-local Binance REST rate-limit coordination."""

from __future__ import annotations

import asyncio
import os
import time
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
RATE_LIMIT_FALLBACK_SECONDS = int(os.getenv("BINANCE_RATE_LIMIT_FALLBACK_SECONDS", "10"))


def retry_after_seconds(response: Any) -> int | None:
    value = getattr(response, "headers", {}).get("Retry-After", "").strip()
    if not value.isdigit():
        return None
    return max(1, int(value))


@dataclass
class _HostState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cooldown_until: float = 0.0
    used_weight_1m: int | None = None


class RateLimitPermit:
    def __init__(self, limiter: "BinanceRateLimiter", host: str, state: _HostState) -> None:
        self._limiter = limiter
        self.host = host
        self._state = state

    def observe(self, response: Any, *, exchange_code: int | str | None = None) -> None:
        self._limiter.observe(self.host, response, exchange_code=exchange_code)


class BinanceRateLimiter:
    """Serialize Binance calls per host and coordinate process-local cooldowns."""

    def __init__(self) -> None:
        self._states: dict[str, _HostState] = {}

    def _state_for(self, host: str) -> _HostState:
        return self._states.setdefault(host, _HostState())

    async def _wait(self, state: _HostState) -> None:
        if not RATE_LIMIT_AWARE_BACKOFF_ENABLED:
            return
        remaining = state.cooldown_until - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
            state.cooldown_until = 0.0

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[RateLimitPermit]:
        state = self._state_for(host)
        async with state.lock:
            await self._wait(state)
            yield RateLimitPermit(self, host, state)

    def observe(self, host: str, response: Any, *, exchange_code: int | str | None = None) -> None:
        state = self._state_for(host)
        headers = getattr(response, "headers", {})
        used_weight = headers.get("X-MBX-USED-WEIGHT-1M", "")
        if used_weight.isdigit():
            state.used_weight_1m = int(used_weight)

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
