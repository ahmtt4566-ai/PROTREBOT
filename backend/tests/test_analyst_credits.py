import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.analyst_credits import (
    ANALYSIS_COST,
    AnalystCredits,
    CreditConfig,
    CreditStore,
    CreditTransaction,
)


class AnalystCreditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 100000.0
        application = SimpleNamespace(state=SimpleNamespace(db_pool=None))
        self.store = CreditStore(application, path=Path(self.directory.name) / "credits.sqlite3")
        self.credits = AnalystCredits(self.store, CreditConfig(), clock=lambda: self.now)
        self.producer = AsyncMock(return_value={"symbol": "BTCUSDT", "entry": 98765.4321})
        self.environment = patch.dict("os.environ", {"PROTREBOT_DURABLE_AUTH_REQUIRED": "false", "ANALYST_COST": str(ANALYSIS_COST)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def buy(self, user="a", key="request-000000001", symbol="BTCUSDT", premium=False):
        return await self.credits.consume(user, premium, symbol, "15m", key, self.producer)

    def test_default_cost_is_shared_and_environment_override_is_preserved(self):
        self.assertEqual(ANALYSIS_COST, 10)
        self.assertEqual(CreditConfig().cost, ANALYSIS_COST)
        with patch.dict("os.environ", {}, clear=True):
            config = CreditConfig.from_environment()
            self.assertEqual(config.cost, ANALYSIS_COST)
            self.assertEqual(config.total, 100)
            with patch.dict("os.environ", {"ANALYST_COST": "7"}):
                self.assertEqual(CreditConfig.from_environment().cost, 7)

    async def test_snapshot_reports_the_configured_cost_for_all_members(self):
        self.credits.config = CreditConfig(cost=7)
        for premium in (False, True):
            snapshot = await self.credits.credits("a", premium)
            self.assertEqual(snapshot["analysis_cost"], 7)
            self.assertEqual(snapshot["total"], 100)

    async def test_user_isolation_and_persistent_budget(self):
        self.assertIsNone((await self.credits.credits("a", False))["resetsAt"])
        first, status = await self.buy()
        self.assertEqual(status, 200)
        self.assertEqual(first["remaining"], 90)
        self.assertEqual(first["analysis_cost"], 10)
        self.assertEqual((await self.credits.credits("b", False))["remaining"], 100)
        restored = AnalystCredits(self.store, clock=lambda: self.now)
        self.assertEqual((await restored.credits("a", False))["remaining"], 90)
        await self.buy(user="b")
        self.assertEqual((await self.credits.credits("a", False))["remaining"], 90)

    async def test_window_rolls_over_only_after_24_hours_and_no_rollover(self):
        await self.buy()
        self.now += 24 * 3600 - 1
        self.assertEqual((await self.credits.credits("a", False))["remaining"], 90)
        self.now += 1
        snapshot = await self.credits.credits("a", False)
        self.assertEqual(snapshot["remaining"], 100)
        self.assertIsNone(snapshot["resetsAt"])
        response, _ = await self.buy(key="request-000000002")
        self.assertEqual(response["remaining"], 90)
        self.assertIsNotNone(response["resetsAt"])

    async def test_cache_is_per_user_symbol_timeframe_and_expires(self):
        await self.buy()
        self.now += 899
        result, _ = await self.buy(key="request-000000002")
        self.assertTrue(result["cached"])
        self.assertEqual(result["remaining"], 90)
        self.producer.assert_awaited_once()
        self.now += 1
        result, _ = await self.buy(key="request-000000003")
        self.assertFalse(result["cached"])
        self.assertEqual(result["remaining"], 80)
        self.assertEqual(self.producer.await_count, 2)
        result, _ = await self.credits.consume("a", False, "BTCUSDT", "1h", "request-000000004", self.producer)
        self.assertEqual(result["remaining"], 70)

    async def test_concurrent_keys_and_replays_do_not_double_charge(self):
        responses = await asyncio.gather(*(self.buy(key=f"request-{index:09}") for index in range(8)))
        self.assertTrue(all(status == 200 for _, status in responses))
        self.assertEqual((await self.credits.credits("a", False))["remaining"], 90)
        self.producer.assert_awaited_once()
        result, _ = await self.buy(key="request-000000000")
        self.assertTrue(result["replayed"])
        self.assertEqual(result["remaining"], 90)
        _, status = await self.buy(key="request-000000000", symbol="ETHUSDT")
        self.assertEqual(status, 409)

    async def test_failure_is_refunded_once_and_failure_replay_is_idempotent(self):
        await self.buy()
        self.now += self.credits.config.cache_minutes * 60
        self.producer.side_effect = RuntimeError("test analysis failure")
        balances = []
        original_execute = CreditTransaction.execute

        async def observe_balance(transaction, sql, *args):
            if sql.startswith("UPDATE analyst_credits SET credits_remaining="):
                balances.append(args[0])
            await original_execute(transaction, sql, *args)

        with patch.object(CreditTransaction, "execute", observe_balance):
            result, status = await self.buy(key="failed-request-000000001")
            self.assertEqual(status, 502)
            self.assertEqual(result["remaining"], 90)
            self.assertEqual(result["analysis_cost"], 10)
            result, status = await self.buy(key="failed-request-000000001")
            self.assertEqual(status, 502)
            self.assertTrue(result["replayed"])
            self.assertEqual(result["remaining"], 90)
        self.assertEqual(balances, [80, 90])
        self.assertEqual(self.producer.await_count, 2)

    async def test_exhaustion_has_reset_and_cached_analysis_is_still_free(self):
        self.credits.config = CreditConfig(total=ANALYSIS_COST)
        await self.buy()
        result, status = await self.buy(key="request-000000002", symbol="ETHUSDT")
        self.assertEqual(status, 429)
        self.assertEqual(result["remaining"], 0)
        self.assertIsNotNone(result["resetsAt"])
        result, status = await self.buy(key="request-000000003")
        self.assertEqual(status, 200)
        self.assertTrue(result["cached"])
        self.assertEqual(result["remaining"], 0)
        self.producer.assert_awaited_once()

    async def test_hundred_credits_allow_ten_fresh_analyses_and_reject_eleventh(self):
        for index in range(10):
            result, status = await self.buy(key=f"fresh-request-{index:09}")
            self.assertEqual(status, 200)
            self.assertFalse(result["cached"])
            self.assertEqual(result["remaining"], 100 - (index + 1) * 10)
            self.assertEqual(result["analysis_cost"], 10)
            self.now += self.credits.config.cache_minutes * 60
        result, status = await self.buy(key="fresh-request-000000010")
        self.assertEqual(status, 429)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(result["total"], 100)
        self.assertEqual(result["analysis_cost"], 10)
        self.assertIsNotNone(result["resetsAt"])
        self.assertEqual(self.producer.await_count, 10)

    async def test_balance_below_cost_is_rejected_but_cache_remains_free(self):
        self.credits.config = CreditConfig(total=15)
        result, status = await self.buy()
        self.assertEqual(status, 200)
        self.assertEqual(result["remaining"], 5)
        result, status = await self.buy(key="request-000000002", symbol="ETHUSDT")
        self.assertEqual(status, 429)
        self.assertEqual(result["remaining"], 5)
        result, status = await self.buy(key="request-000000003")
        self.assertEqual(status, 200)
        self.assertTrue(result["cached"])
        self.assertEqual(result["remaining"], 5)
        self.producer.assert_awaited_once()

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
