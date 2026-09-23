import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import binance_demo  # noqa: E402


class ProtectionIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    def plan(self, plan_id="plan-a", symbol="BTCUSDT"):
        return {
            "id": plan_id,
            "user_id": "user-a",
            "symbol": symbol,
            "direction": "LONG",
            "provenance_state": "CONFIRMED",
            "status": "DOLUM BEKLİYOR",
            "position_status": "PENDING",
            "stop_loss": "90",
            "targets": ["105", "110", "115"],
            "step": "0.1",
            "min_qty": "0.1",
            "quantity": "1",
            "entry_price": "100",
            "initial_quantity": "1",
            "remaining_quantity": "1",
        }

    async def test_same_plan_concurrent_install_runs_once(self):
        plan = self.plan()
        state = {"plans": {plan["id"]: plan}}
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fake_install(*args, **kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            plan["protection_ids"] = [101, 102, 103, 104]

        with patch.object(binance_demo, "_install_protection", new=fake_install):
            first = asyncio.create_task(binance_demo.install_protection(None, state, plan))
            await started.wait()
            second = asyncio.create_task(binance_demo.install_protection(None, state, plan))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)

        self.assertEqual(calls, 1)
        self.assertEqual(plan["protection_ids"], [101, 102, 103, 104])

    async def test_protection_completion_marks_plan_open(self):
        plan = self.plan()
        state = {"plans": {plan["id"]: plan}}

        class FakeClient:
            async def signed(self, method, path, params=None):
                if path == "/fapi/v3/positionRisk":
                    return [{"symbol": "BTCUSDT", "positionAmt": "1"}]
                if path == "/fapi/v1/openAlgoOrders":
                    return [
                        {"symbol": "BTCUSDT", "algoId": 101, "orderType": "STOP_MARKET", "side": "SELL", "algoStatus": "NEW"},
                        {"symbol": "BTCUSDT", "algoId": 102, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "algoStatus": "NEW"},
                        {"symbol": "BTCUSDT", "algoId": 103, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "algoStatus": "NEW"},
                        {"symbol": "BTCUSDT", "algoId": 104, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "algoStatus": "NEW"},
                    ]
                raise AssertionError((method, path, params))

        with patch.object(binance_demo, "post_algo", new=AsyncMock(side_effect=[
            {"algoId": 101, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"},
            {"algoId": 102, "symbol": "BTCUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
            {"algoId": 103, "symbol": "BTCUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
            {"algoId": 104, "symbol": "BTCUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
        ])):
            await binance_demo.install_protection(FakeClient(), state, plan)

        self.assertEqual(plan["status"], "OPEN")
        self.assertEqual(plan["position_status"], "OPEN")
        self.assertEqual(plan["protection_ids"], [101, 102, 103, 104])

    async def test_final_snapshot_runs_outside_passed_state_lock(self):
        plan = self.plan()
        state = {"plans": {plan["id"]: plan}}
        state_lock = asyncio.Lock()
        await state_lock.acquire()

        async def signed(method, path, params=None):
            if path == "/fapi/v3/positionRisk":
                return [{"symbol": "BTCUSDT", "positionAmt": "1"}]
            if path == "/fapi/v1/openAlgoOrders":
                self.assertFalse(state_lock.locked())
                return [
                    {"symbol": "BTCUSDT", "algoId": 101, "orderType": "STOP_MARKET", "side": "SELL", "algoStatus": "NEW"},
                    {"symbol": "BTCUSDT", "algoId": 102, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "algoStatus": "NEW"},
                    {"symbol": "BTCUSDT", "algoId": 103, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "algoStatus": "NEW"},
                    {"symbol": "BTCUSDT", "algoId": 104, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "algoStatus": "NEW"},
                ]
            raise AssertionError((method, path, params))

        client = SimpleNamespace(signed=signed)
        with patch.object(binance_demo, "post_algo", new=AsyncMock(side_effect=[
            {"algoId": 101, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"},
            {"algoId": 102, "symbol": "BTCUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
            {"algoId": 103, "symbol": "BTCUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
            {"algoId": 104, "symbol": "BTCUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
        ])), patch.object(binance_demo, "persist_runtime"):
            await binance_demo._install_protection(client, state, plan, state_lock=state_lock)

        self.assertTrue(state_lock.locked())
        state_lock.release()

    async def test_malformed_truthy_protection_ids_fail_closed(self):
        plan = self.plan()
        plan["protection_ids"] = ["not-an-id"]
        state = {"plans": {plan["id"]: plan}}

        with patch.object(binance_demo, "_install_protection", new=AsyncMock()) as install_mock:
            await binance_demo.install_protection(None, state, plan)

        install_mock.assert_not_awaited()

    async def test_different_plans_install_independently(self):
        first_plan = self.plan("plan-a", "BTCUSDT")
        second_plan = self.plan("plan-b", "ETHUSDT")
        started = []
        release = asyncio.Event()

        async def fake_install(_client, _state, plan, **_kwargs):
            started.append(plan["symbol"])
            await release.wait()
            plan["protection_ids"] = [len(started)]

        with patch.object(binance_demo, "_install_protection", new=fake_install):
            first = asyncio.create_task(binance_demo.install_protection(None, {}, first_plan))
            second = asyncio.create_task(binance_demo.install_protection(None, {}, second_plan))
            while len(started) < 2:
                await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)

        self.assertEqual(set(started), {"BTCUSDT", "ETHUSDT"})
        self.assertTrue(first_plan["protection_ids"])
        self.assertTrue(second_plan["protection_ids"])

    async def test_duplicate_client_id_recovers_matching_existing_order(self):
        params = {"symbol": "BTCUSDT", "clientAlgoId": "PTB_SL_existing", "type": "STOP_MARKET"}
        client = SimpleNamespace()
        client.signed = AsyncMock(side_effect=[
            binance_demo.BinanceDemoError("ClientOrderId is duplicated"),
            [{"symbol": "BTCUSDT", "clientAlgoId": "PTB_SL_existing", "algoId": 77}],
        ])

        result = await binance_demo.post_algo(client, params)

        self.assertEqual(result["algoId"], 77)
        self.assertEqual(client.signed.await_count, 2)
        self.assertEqual(client.signed.await_args_list[1].args[1], "/fapi/v1/openAlgoOrders")

    async def test_duplicate_client_id_without_match_preserves_failure(self):
        params = {"symbol": "BTCUSDT", "clientAlgoId": "PTB_SL_missing", "type": "STOP_MARKET"}
        client = SimpleNamespace()
        client.signed = AsyncMock(side_effect=[
            binance_demo.BinanceDemoError("ClientOrderId is duplicated"),
            [{"symbol": "ETHUSDT", "clientAlgoId": "PTB_SL_missing", "algoId": 77}],
        ])

        with self.assertRaisesRegex(binance_demo.BinanceDemoError, "duplicated"):
            await binance_demo.post_algo(client, params)

    async def test_duplicate_install_without_match_preserves_safety_close(self):
        plan = self.plan()
        state = {"plans": {plan["id"]: plan}}
        client = SimpleNamespace()
        position_calls = 0

        async def signed(method, path, params=None):
            nonlocal position_calls
            if path == "/fapi/v3/positionRisk":
                position_calls += 1
                return [{"symbol": "BTCUSDT", "positionAmt": "1"}] if position_calls == 1 else []
            if method == "POST" and path == "/fapi/v1/algoOrder":
                raise binance_demo.BinanceDemoError("ClientOrderId is duplicated")
            if method == "GET" and path == "/fapi/v1/openAlgoOrders":
                return []
            raise AssertionError((method, path, params))

        client.signed = signed
        with patch.object(binance_demo, "close_symbol_position", new=AsyncMock(return_value=None)), \
                patch.object(binance_demo, "persist_runtime"):
            with self.assertRaisesRegex(binance_demo.BinanceDemoError, "pozisyon güvenli biçimde kapatıldı"):
                await binance_demo.install_protection(client, state, plan)

        self.assertEqual(plan["status"], "GÜVENLİK İÇİN KAPATILDI")
        self.assertEqual(plan["protection_ids"], [])

    async def test_partial_protection_failure_cleans_created_orders(self):
        plan = self.plan()
        state = {"plans": {plan["id"]: plan}}
        client = SimpleNamespace()
        position_calls = 0
        delete_calls = []

        async def signed(method, path, params=None):
            nonlocal position_calls
            if path == "/fapi/v3/positionRisk":
                position_calls += 1
                return [{"symbol": "BTCUSDT", "positionAmt": "1"}] if position_calls == 1 else []
            if path == "/fapi/v1/openAlgoOrders":
                return [order for order in [
                    {"symbol": "BTCUSDT", "algoId": 101, "orderType": "STOP_MARKET", "side": "SELL", "algoStatus": "NEW"},
                    {"symbol": "BTCUSDT", "algoId": 102, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "algoStatus": "NEW"},
                ] if order["algoId"] not in delete_calls]
            if method == "DELETE" and path == "/fapi/v1/algoOrder":
                delete_calls.append(params["algoId"])
                return {}
            raise AssertionError((method, path, params))

        client.signed = signed
        post_results = iter([
            {"algoId": 101, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"},
            {"algoId": 102, "symbol": "BTCUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
            binance_demo.BinanceDemoError("TP2 unavailable"),
        ])

        async def post_algo(_client, _params):
            result = next(post_results)
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(binance_demo, "post_algo", new=post_algo), \
                patch.object(binance_demo, "close_symbol_position", new=AsyncMock(return_value=None)), \
                patch.object(binance_demo, "persist_runtime"):
            with self.assertRaises(binance_demo.BinanceDemoError):
                await binance_demo.install_protection(client, state, plan)

        self.assertEqual(delete_calls, [101, 102])
        self.assertEqual(plan["protection_ids"], [])
        self.assertEqual(plan["status"], "GÜVENLİK İÇİN KAPATILDI")

    async def test_cleanup_retains_failed_ids_for_symbol_scoped_retry(self):
        plan = self.plan(symbol="BTCUSDT")
        plan["provenance_state"] = "CONFIRMED"
        plan["protection_ids"] = [101, 202, 303]
        calls = []
        deleted = set()

        class Client:
            async def signed(self, method, path, params=None):
                calls.append((method, path, params.copy()))
                if path == "/fapi/v1/openAlgoOrders":
                    return [order for order in [
                        {"symbol": "BTCUSDT", "algoId": 101, "orderType": "STOP_MARKET", "side": "SELL", "algoStatus": "NEW"},
                        {"symbol": "BTCUSDT", "algoId": 202, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "algoStatus": "NEW"},
                        {"symbol": "BTCUSDT", "algoId": 303, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "algoStatus": "NEW"},
                    ] if order["algoId"] not in deleted]
                if params.get("algoId") == 202 and not any(
                    call[2].get("algoId") == 202 for call in calls[:-1]
                ):
                    raise binance_demo.BinanceDemoError("temporary cancellation failure", exchange_code=-1000)
                if params.get("algoId"):
                    deleted.add(params["algoId"])
                return {}

        client = Client()
        await binance_demo.cleanup_closed_plan(client, plan)

        self.assertEqual(plan["protection_ids"], [202])
        self.assertEqual({call[2]["symbol"] for call in calls}, {"BTCUSDT"})

        await binance_demo.cleanup_closed_plan(client, plan)

        self.assertEqual(plan["protection_ids"], [])
        self.assertEqual([
            call[2]["algoId"] for call in calls if call[0] == "DELETE"
        ], [101, 202, 303, 202])

    async def test_partial_cleanup_completes_before_emergency_close(self):
        plan = self.plan()
        state = {"plans": {plan["id"]: plan}}
        plan["protection_ids"] = [101, 102]
        order = []

        class Client:
            async def signed(self, method, path, params=None):
                if method == "DELETE":
                    order.append(("cleanup", params["algoId"]))
                    return {}
                if path == "/fapi/v3/positionRisk":
                    order.append(("verify", None))
                    return []
                if path == "/fapi/v1/openAlgoOrders":
                    return [
                        {"symbol": "BTCUSDT", "algoId": 101, "orderType": "STOP_MARKET", "side": "SELL", "algoStatus": "NEW"},
                        {"symbol": "BTCUSDT", "algoId": 102, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "algoStatus": "NEW"},
                    ]
                raise AssertionError((method, path, params))

        async def close_position(_client, _symbol):
            order.append(("close", None))
            return None

        with patch.object(binance_demo, "close_symbol_position", new=close_position), \
                patch.object(binance_demo, "persist_runtime"):
            with self.assertRaises(binance_demo.BinanceDemoError):
                await binance_demo._abort_protection_installation(
                    Client(), state, plan, binance_demo.BinanceDemoError("TP failed"),
                    "protection failed", "position closed",
                )

        self.assertEqual([item[0] for item in order], ["cleanup", "cleanup", "close", "verify"])

    async def test_protection_parameters_remain_unchanged(self):
        plan = self.plan()
        state = {"plans": {plan["id"]: plan}}
        client = SimpleNamespace()
        position_calls = 0
        params_seen = []

        async def signed(method, path, params=None):
            nonlocal position_calls
            if path == "/fapi/v3/positionRisk":
                position_calls += 1
                return [{"symbol": "BTCUSDT", "positionAmt": "1"}]
            if path == "/fapi/v1/openAlgoOrders":
                return [
                    {"symbol": "BTCUSDT", "algoId": 1, "orderType": "STOP_MARKET", "side": "SELL", "algoStatus": "NEW"},
                    {"symbol": "BTCUSDT", "algoId": 2, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "algoStatus": "NEW"},
                    {"symbol": "BTCUSDT", "algoId": 3, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "algoStatus": "NEW"},
                    {"symbol": "BTCUSDT", "algoId": 4, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "algoStatus": "NEW"},
                ]
            raise AssertionError((method, path, params))

        client.signed = signed

        async def post_algo(_client, params):
            params_seen.append(dict(params))
            return {
                "algoId": len(params_seen),
                "symbol": params["symbol"],
                "type": params["type"],
                "side": params["side"],
                "status": "NEW",
            }

        with patch.object(binance_demo, "post_algo", new=post_algo), \
                patch.object(binance_demo, "persist_runtime"):
            await binance_demo.install_protection(client, state, plan)

        self.assertEqual([item["type"] for item in params_seen], ["STOP_MARKET", "TAKE_PROFIT_MARKET", "TAKE_PROFIT_MARKET", "TAKE_PROFIT_MARKET"])
        self.assertEqual([item["triggerPrice"] for item in params_seen], ["90", "105", "110", "115"])
        self.assertEqual([item["quantity"] for item in params_seen[1:3]], ["0.3", "0.3"])
        self.assertEqual(params_seen[0]["closePosition"], "true")
        self.assertEqual(params_seen[3]["closePosition"], "true")
        self.assertEqual([item["reduceOnly"] for item in params_seen[1:3]], ["true", "true"])


if __name__ == "__main__":
    unittest.main()
