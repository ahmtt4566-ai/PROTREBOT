import asyncio
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.binance_rate_limit import BINANCE_RATE_LIMITER  # noqa: E402


class Response:
    def __init__(self, status_code=200, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class BinanceRateLimitTests(unittest.IsolatedAsyncioTestCase):
    HOST = "https://demo-fapi.binance.com"

    def setUp(self):
        BINANCE_RATE_LIMITER.reset()

    async def _request(self, response):
        async with BINANCE_RATE_LIMITER.slot(self.HOST) as permit:
            permit.observe(response)
            return response

    async def test_normal_request_passes_without_cooldown(self):
        response = Response()
        await self._request(response)
        self.assertEqual(BINANCE_RATE_LIMITER.snapshot(self.HOST)["cooldown_until"], 0.0)

    async def test_http_429_and_418_create_cooldown(self):
        for status_code in (429, 418):
            BINANCE_RATE_LIMITER.reset()
            await self._request(Response(status_code=status_code))
            self.assertGreater(BINANCE_RATE_LIMITER.snapshot(self.HOST)["cooldown_until"], 0.0)

    async def test_binance_minus_1003_creates_cooldown(self):
        await self._request(Response(payload={"code": -1003, "msg": "Too many requests"}))
        self.assertGreater(BINANCE_RATE_LIMITER.snapshot(self.HOST)["cooldown_until"], 0.0)

    async def test_retry_after_is_used(self):
        with patch("app.binance_rate_limit.time.monotonic", return_value=100.0):
            await self._request(Response(429, {"Retry-After": "7"}))
        self.assertEqual(BINANCE_RATE_LIMITER.snapshot(self.HOST)["cooldown_until"], 107.0)

    async def test_missing_retry_after_uses_safe_fallback(self):
        with patch("app.binance_rate_limit.time.monotonic", return_value=100.0):
            await self._request(Response(429))
        self.assertEqual(BINANCE_RATE_LIMITER.snapshot(self.HOST)["cooldown_until"], 110.0)

    async def test_weight_threshold_creates_proactive_cooldown(self):
        with patch("app.binance_rate_limit.time.monotonic", return_value=100.0):
            await self._request(Response(headers={"X-MBX-USED-WEIGHT-1M": "1920"}))
        state = BINANCE_RATE_LIMITER.snapshot(self.HOST)
        self.assertEqual(state["used_weight_1m"], 1920)
        self.assertGreater(state["cooldown_until"], 100.0)

    async def test_second_request_waits_and_only_runs_after_cooldown(self):
        calls = []
        sleep_started = asyncio.Event()
        release_sleep = asyncio.Event()

        async def delayed_sleep(seconds):
            sleep_started.set()
            await release_sleep.wait()

        async def call(response):
            async with BINANCE_RATE_LIMITER.slot(self.HOST) as permit:
                calls.append(response.status_code)
                permit.observe(response)

        await call(Response(429))
        with patch("app.binance_rate_limit.asyncio.sleep", new=delayed_sleep):
            second = asyncio.create_task(call(Response(200)))
            await asyncio.wait_for(sleep_started.wait(), timeout=1)
            self.assertEqual(calls, [429])
            release_sleep.set()
            await second
        self.assertEqual(calls, [429, 200])

    async def test_request_after_cooldown_can_run(self):
        await self._request(Response(429, {"Retry-After": "1"}))
        with patch("app.binance_rate_limit.asyncio.sleep", new=AsyncMock()):
            await self._request(Response(200))
        self.assertEqual(BINANCE_RATE_LIMITER.snapshot(self.HOST)["cooldown_until"], 0.0)

    async def test_concurrent_requests_keep_rate_limit_state_consistent(self):
        responses = [Response(429, {"Retry-After": "3"}), Response(200)]
        with patch("app.binance_rate_limit.asyncio.sleep", new=AsyncMock()):
            await asyncio.gather(*(self._request(response) for response in responses))
        self.assertEqual(BINANCE_RATE_LIMITER.snapshot(self.HOST)["used_weight_1m"], None)

    async def test_normal_response_does_not_clear_weight_observation(self):
        await self._request(Response(headers={"X-MBX-USED-WEIGHT-1M": "100"}))
        await self._request(Response(headers={"X-MBX-USED-WEIGHT-1M": "101"}))
        self.assertEqual(BINANCE_RATE_LIMITER.snapshot(self.HOST)["used_weight_1m"], 101)

    async def test_logs_never_contain_credentials(self):
        with self.assertNoLogs("app.binance_rate_limit", level=logging.DEBUG):
            await self._request(Response(429, {"Retry-After": "2"}, {"code": -1003}))


if __name__ == "__main__":
    unittest.main()
