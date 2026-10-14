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

from app.binance_rate_limit import BINANCE_GLOBAL_BAN, BINANCE_RATE_LIMITER, BinanceGlobalBan  # noqa: E402


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

    async def test_ban_starting_after_queue_entry_blocks_dispatch_on_every_host(self):
        real_sleep = asyncio.sleep
        for status in (418, 429):
            for host, priority in ((self.HOST, False), ("https://fapi.binance.com", True), ("https://api.binance.com", False)):
                with self.subTest(status=status, host=host, priority=priority):
                    BINANCE_RATE_LIMITER.reset()
                    BINANCE_GLOBAL_BAN.reset(clear_persistent=True)
                    clock = [1_000.0]
                    occupied, release_slot = asyncio.Event(), asyncio.Event()
                    ban_waiting, release_ban, dispatched = asyncio.Event(), asyncio.Event(), asyncio.Event()

                    async def blocker():
                        async with BINANCE_RATE_LIMITER.slot(host):
                            occupied.set()
                            await release_slot.wait()

                    async def reader():
                        async with BINANCE_RATE_LIMITER.slot(host, priority=priority):
                            dispatched.set()

                    async def elapsed(seconds):
                        ban_waiting.set()
                        await release_ban.wait()
                        clock[0] += seconds

                    with patch("app.binance_rate_limit.BINANCE_MAX_CONCURRENT_REQUESTS_PER_HOST", 1), \
                         patch("app.binance_rate_limit.time.time", side_effect=lambda: clock[0]), \
                         patch("app.binance_rate_limit.asyncio.sleep", new=elapsed):
                        first = asyncio.create_task(blocker())
                        await occupied.wait()
                        queued = asyncio.create_task(reader())
                        await real_sleep(0)
                        self.assertEqual(len(BINANCE_RATE_LIMITER._state_for(host).pending), 1)
                        BINANCE_GLOBAL_BAN.record(httpx.Response(
                            status, headers={"Retry-After": "60"}, json={"code": -1003},
                            request=httpx.Request("GET", "https://fapi.binance.com/fapi/v1/klines"),
                        ), source="original_live", egress_route="render_direct")
                        signals = [
                            asyncio.create_task(ban_waiting.wait()),
                            asyncio.create_task(dispatched.wait()),
                        ]
                        try:
                            release_slot.set()
                            await asyncio.wait_for(asyncio.wait(signals, return_when=asyncio.FIRST_COMPLETED), timeout=1)
                            self.assertFalse(dispatched.is_set())
                            self.assertTrue(ban_waiting.is_set())
                        finally:
                            release_slot.set()
                            release_ban.set()
                            for signal in signals:
                                signal.cancel()
                            await asyncio.gather(*signals, return_exceptions=True)
                            await asyncio.gather(first, queued)
                        self.assertTrue(dispatched.is_set())
                        self.assertFalse(BINANCE_GLOBAL_BAN.active())
                        self.assertEqual(BINANCE_RATE_LIMITER._state_for(host).active, 0)
                        self.assertEqual(BINANCE_RATE_LIMITER._state_for(host).pending, [])

    async def test_explicit_exchange_ban_is_recorded_even_when_adaptive_backoff_is_disabled(self):
        for status in (418, 429):
            with self.subTest(status=status):
                BINANCE_GLOBAL_BAN.reset(clear_persistent=True)
                with patch("app.binance_rate_limit.RATE_LIMIT_AWARE_BACKOFF_ENABLED", False):
                    BINANCE_RATE_LIMITER.observe(self.HOST, httpx.Response(
                        status, headers={"Retry-After": "60"}, json={"code": -1003},
                        request=httpx.Request("GET", self.HOST + "/fapi/v1/time"),
                    ), source="connection_server_time", egress_route="quotaguard")
                self.assertTrue(BINANCE_GLOBAL_BAN.active())

    async def test_http_429_and_418_create_cooldown(self):
        for status_code in (429, 418):
            BINANCE_RATE_LIMITER.reset()
            await self._request(Response(status_code=status_code))
            self.assertGreater(BINANCE_RATE_LIMITER.snapshot(self.HOST)["cooldown_until"], 0.0)

    async def test_global_ban_persists_across_restart_with_safe_metadata(self):
        path = BINANCE_GLOBAL_BAN.path
        response = httpx.Response(
            418,
            headers={"Retry-After": "4881", "X-MBX-USED-WEIGHT-1M": "1660"},
            json={"code": -1003, "msg": "IP banned"},
            request=httpx.Request("GET", "https://fapi.binance.com/fapi/v1/klines"),
        )
        with patch("app.binance_rate_limit.time.time", return_value=1_000.0):
            clock = BinanceGlobalBan(path)
            self.assertTrue(clock.record(
                response,
                source="original_warmer",
                egress_route="render_direct",
            ))
        with patch("app.binance_rate_limit.time.time", return_value=1_001.0):
            restored = BinanceGlobalBan(path)
            status = restored.public_status()
        self.assertTrue(status["active"])
        self.assertEqual(status["remaining_seconds"], 4880)
        self.assertEqual(status["status_code"], 418)
        self.assertEqual(status["exchange_code"], -1003)
        self.assertEqual(status["used_weight_1m"], 1660)
        self.assertEqual(status["source"], "original_warmer")
        self.assertEqual(status["egress_route"], "render_direct")
        self.assertNotIn("IP banned", str(status))

    async def test_global_ban_restores_from_durable_database(self):
        pool = AsyncMock()
        pool.fetchrow.return_value = {
            "payload": {
                "blocked_until_epoch": 5_881.0,
                "observed_at_epoch": 1_000.0,
                "status_code": 418,
                "exchange_code": -1003,
                "used_weight_1m": 1660,
                "source": "original_warmer",
                "egress_route": "render_direct",
            }
        }
        clock = BinanceGlobalBan(BINANCE_GLOBAL_BAN.path.with_name("database-restored.json"))
        with patch("app.binance_rate_limit.time.time", return_value=1_001.0):
            await clock.configure_persistence(pool)
            status = clock.public_status()
        self.assertTrue(status["active"])
        self.assertEqual(status["remaining_seconds"], 4880)
        self.assertEqual(status["source"], "original_warmer")
        self.assertEqual(status["egress_route"], "render_direct")
        pool.fetchrow.assert_awaited_once()

    async def test_global_ban_writes_new_deadline_to_durable_database(self):
        pool = AsyncMock()
        pool.fetchrow.return_value = None
        clock = BinanceGlobalBan(BINANCE_GLOBAL_BAN.path.with_name("database-written.json"))
        await clock.configure_persistence(pool)
        response = httpx.Response(
            429,
            headers={"Retry-After": "180"},
            json={"code": -1003},
            request=httpx.Request("GET", "https://fapi.binance.com/fapi/v1/time"),
        )
        self.assertTrue(clock.record(
            response,
            source="connection_server_time",
            egress_route="quotaguard",
        ))
        await clock.flush_persistence()
        self.assertTrue(any(
            "INSERT INTO binance_global_ban_state" in str(call.args[0])
            for call in pool.execute.await_args_list
        ))

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
