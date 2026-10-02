import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.analyst_credits import AnalystCredits, CreditConfig, CreditStore


class AnalystCreditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 100000.0
        application = SimpleNamespace(state=SimpleNamespace(db_pool=None))
        self.store = CreditStore(application, path=Path(self.directory.name) / "credits.sqlite3")
        self.credits = AnalystCredits(self.store, CreditConfig(), clock=lambda: self.now)
        self.producer = AsyncMock(return_value={"symbol": "BTCUSDT", "entry": 98765.4321})
        self.environment = patch.dict("os.environ", {"PROTREBOT_DURABLE_AUTH_REQUIRED": "false"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def buy(self, user="a", key="request-000000001", symbol="BTCUSDT", premium=False):
        return await self.credits.consume(user, premium, symbol, "15m", key, self.producer)

    async def test_user_isolation_and_persistent_budget(self):
        self.assertIsNone((await self.credits.credits("a", False))["resetsAt"])
        first, status = await self.buy()
        self.assertEqual(status, 200)
        self.assertEqual(first["remaining"], 99)
        self.assertEqual((await self.credits.credits("b", False))["remaining"], 100)
        restored = AnalystCredits(self.store, clock=lambda: self.now)
        self.assertEqual((await restored.credits("a", False))["remaining"], 99)
        await self.buy(user="b")
        self.assertEqual((await self.credits.credits("a", False))["remaining"], 99)

    async def test_window_rolls_over_only_after_24_hours_and_no_rollover(self):
        await self.buy()
        self.now += 24 * 3600 - 1
        self.assertEqual((await self.credits.credits("a", False))["remaining"], 99)
        self.now += 1
        snapshot = await self.credits.credits("a", False)
        self.assertEqual(snapshot["remaining"], 100)
        self.assertIsNone(snapshot["resetsAt"])
        response, _ = await self.buy(key="request-000000002")
        self.assertEqual(response["remaining"], 99)
        self.assertIsNotNone(response["resetsAt"])

    async def test_cache_is_per_user_symbol_timeframe_and_expires(self):
        await self.buy()
        self.now += 899
        result, _ = await self.buy(key="request-000000002")
        self.assertTrue(result["cached"])
        self.assertEqual(result["remaining"], 99)
        self.producer.assert_awaited_once()
        self.now += 1
        result, _ = await self.buy(key="request-000000003")
        self.assertFalse(result["cached"])
        self.assertEqual(result["remaining"], 98)
        self.assertEqual(self.producer.await_count, 2)
        result, _ = await self.credits.consume("a", False, "BTCUSDT", "1h", "request-000000004", self.producer)
        self.assertEqual(result["remaining"], 97)

    async def test_concurrent_keys_and_replays_do_not_double_charge(self):
        responses = await asyncio.gather(*(self.buy(key=f"request-{index:09}") for index in range(8)))
        self.assertTrue(all(status == 200 for _, status in responses))
        self.assertEqual((await self.credits.credits("a", False))["remaining"], 99)
        self.producer.assert_awaited_once()
        result, _ = await self.buy(key="request-000000000")
        self.assertTrue(result["replayed"])
        self.assertEqual(result["remaining"], 99)
        _, status = await self.buy(key="request-000000000", symbol="ETHUSDT")
        self.assertEqual(status, 409)

    async def test_failure_is_refunded_once_and_failure_replay_is_idempotent(self):
        self.producer.side_effect = RuntimeError("test analysis failure")
        result, status = await self.buy()
        self.assertEqual(status, 502)
        self.assertEqual(result["remaining"], 100)
        result, status = await self.buy()
        self.assertEqual(status, 502)
        self.assertEqual(result["remaining"], 100)
        self.producer.assert_awaited_once()

    async def test_exhaustion_has_reset_and_cached_analysis_is_still_free(self):
        self.credits.config = CreditConfig(total=1)
        await self.buy()
        result, status = await self.buy(key="request-000000002", symbol="ETHUSDT")
        self.assertEqual(status, 429)
        self.assertEqual(result["remaining"], 0)
        self.assertIsNotNone(result["resetsAt"])
        result, status = await self.buy(key="request-000000003")
        self.assertEqual(status, 200)
        self.assertTrue(result["cached"])

    async def test_premium_is_unlimited_and_rate_limit_still_applies(self):
        result, _ = await self.buy(premium=True)
        self.assertIsNone(result["remaining"])
        self.assertTrue(result["unlimited"])
        self.assertEqual((await self.credits.credits("a", False))["remaining"], 100)
        for index in range(29):
            await self.buy(premium=True, key=f"premium-{index:09}")
        result, status = await self.buy(premium=True, key="premium-000000030")
        self.assertEqual(status, 429)
        self.assertIn("retryAfter", result)

    async def test_cancellation_rolls_back_charge(self):
        self.producer.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.buy()
        self.assertEqual((await self.credits.credits("a", False))["remaining"], 100)
