import asyncio
import logging
import sys
import unittest
import httpx
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

    async def test_protection_request_overtakes_browser_reads_without_bypassing_cooldown(self):
        occupied = asyncio.Event()
        release = asyncio.Event()
        order = []

        async def blocker():
            async with BINANCE_RATE_LIMITER.slot(self.HOST):
                occupied.set()
                await release.wait()

        async def reader(name, priority=False):
            async with BINANCE_RATE_LIMITER.slot(self.HOST, priority=priority):
                order.append(name)

        with patch("app.binance_rate_limit.BINANCE_MAX_CONCURRENT_REQUESTS_PER_HOST", 1):
            task = asyncio.create_task(blocker())
            await occupied.wait()
            browser = asyncio.create_task(reader("browser"))
            await asyncio.sleep(0)
            protection = asyncio.create_task(reader("stop_tp", True))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(task, browser, protection)
        self.assertEqual(order, ["stop_tp", "browser"])

    async def test_priority_is_preserved_after_shared_retry_after_wait(self):
        release = asyncio.Event()
        waiting = asyncio.Event()
        wait_calls = []
        order = []
        clock = [100.0]
        BINANCE_RATE_LIMITER._state_for(self.HOST).cooldown_until = 280.0

        async def elapsed(seconds):
            wait_calls.append(seconds)
            if len(wait_calls) == 2:
                waiting.set()
            await release.wait()
            clock[0] = 280.0

        async def reader(name, priority=False):
            async with BINANCE_RATE_LIMITER.slot(self.HOST, priority=priority):
                order.append(name)

        with patch("app.binance_rate_limit.time.monotonic", side_effect=lambda: clock[0]), patch("app.binance_rate_limit.asyncio.sleep", new=elapsed):
            browser = asyncio.create_task(reader("browser"))
            protection = asyncio.create_task(reader("stop_tp", True))
            await asyncio.wait_for(waiting.wait(), timeout=1)
            self.assertEqual(order, [])
            self.assertEqual(wait_calls, [180.0, 180.0])
            release.set()
            await asyncio.gather(browser, protection)
        self.assertEqual(order, ["stop_tp", "browser"])

    async def test_cancelled_waiter_does_not_block_protection_queue(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocker():
            async with BINANCE_RATE_LIMITER.slot(self.HOST):
                started.set()
                await release.wait()

        async def waiter():
            async with BINANCE_RATE_LIMITER.slot(self.HOST):
                return True

        with patch("app.binance_rate_limit.BINANCE_MAX_CONCURRENT_REQUESTS_PER_HOST", 1):
            task = asyncio.create_task(blocker())
            await started.wait()
            cancelled = asyncio.create_task(waiter())
            await asyncio.sleep(0)
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            release.set()
            await task
            self.assertTrue(await waiter())
        state = BINANCE_RATE_LIMITER._state_for(self.HOST)
        self.assertEqual(state.pending, [])
        self.assertEqual(state.active, 0)

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

    async def test_retry_after_above_sixty_seconds_is_honored(self):
        with patch("app.binance_rate_limit.time.monotonic", return_value=100.0):
            await self._request(Response(429, {"Retry-After": "180"}))
        self.assertEqual(BINANCE_RATE_LIMITER.snapshot(self.HOST)["cooldown_until"], 280.0)

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
            BINANCE_RATE_LIMITER._state_for(self.HOST).cooldown_until = 0

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
        async def elapsed(seconds):
            BINANCE_RATE_LIMITER._state_for(self.HOST).cooldown_until = 0
        with patch("app.binance_rate_limit.asyncio.sleep", new=elapsed):
            await self._request(Response(200))
        self.assertEqual(BINANCE_RATE_LIMITER.snapshot(self.HOST)["cooldown_until"], 0.0)

    async def test_concurrent_requests_keep_rate_limit_state_consistent(self):
        responses = [Response(429, {"Retry-After": "3"}), Response(200)]
        async def elapsed(seconds):
            BINANCE_RATE_LIMITER._state_for(self.HOST).cooldown_until = 0
        with patch("app.binance_rate_limit.asyncio.sleep", new=elapsed):
            await asyncio.gather(*(self._request(response) for response in responses))
        self.assertEqual(BINANCE_RATE_LIMITER.snapshot(self.HOST)["used_weight_1m"], None)

    async def test_normal_response_does_not_clear_weight_observation(self):
        await self._request(Response(headers={"X-MBX-USED-WEIGHT-1M": "100"}))
        await self._request(Response(headers={"X-MBX-USED-WEIGHT-1M": "101"}))
        self.assertEqual(BINANCE_RATE_LIMITER.snapshot(self.HOST)["used_weight_1m"], 101)

    async def test_logs_never_contain_credentials(self):
        with self.assertNoLogs("app.binance_rate_limit", level=logging.DEBUG):
            await self._request(Response(429, {"Retry-After": "2"}, {"code": -1003}))

    async def test_endpoint_metrics_are_per_host_and_exclude_signatures(self):
        first = httpx.Response(200, request=httpx.Request("GET", self.HOST + "/fapi/v3/account?signature=private-signature"))
        other_host = "https://api.binance.com"
        second = httpx.Response(200, request=httpx.Request("GET", other_host + "/sapi/v1/account/apiRestrictions"))
        BINANCE_RATE_LIMITER.observe(self.HOST, first)
        BINANCE_RATE_LIMITER.observe(other_host, second)
        BINANCE_RATE_LIMITER._state_for(self.HOST).metrics_at -= 60
        with self.assertLogs("app.binance_rate_limit", level="INFO") as logs:
            BINANCE_RATE_LIMITER.observe(self.HOST, first)
        text = " ".join(logs.output)
        self.assertIn("GET:/fapi/v3/account:200", text)
        self.assertNotIn("apiRestrictions", text)
        self.assertNotIn("private-signature", text)
        self.assertEqual(BINANCE_RATE_LIMITER._state_for(other_host).counts["GET:/sapi/v1/account/apiRestrictions:200"], 1)


if __name__ == "__main__":
    unittest.main()
