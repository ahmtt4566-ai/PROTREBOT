import asyncio
from copy import deepcopy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import binance_demo, v21_demo  # noqa: E402


class P0P2ProtectionOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def plan(self, *, protection_ids=None, stop_algo_id=None):
        plan = {
            "id": "plan-a",
            "user_id": "user-a",
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "provenance_state": "CONFIRMED",
            "status": "OPEN",
            "position_status": "OPEN",
            "stop_loss": "90",
            "initial_stop_loss": "90",
            "protection_ids": protection_ids,
        }
        if stop_algo_id is not None:
            plan["stop_algo_id"] = stop_algo_id
        return plan

    def snapshot(self, orders, *, available=True):
        return {
            "open_algo_orders_available": available,
            "open_algo_orders": orders,
        }

    def algo(self, algo_id, *, order_type="STOP_MARKET", symbol="BTCUSDT", side="SELL", trigger_price=90):
        return {
            "symbol": symbol,
            "algo_id": algo_id,
            "type": order_type,
            "side": side,
            "status": "NEW",
            "trigger_price": trigger_price,
        }

    def application(self, plan, *, settings=None):
        demo_state = {"plans": {plan["id"]: plan}, "protection_repairs": 0}
        v21_state = {
            "settings": {
                "breakeven_enabled": True,
                "trailing_enabled": False,
                "breakeven_trigger_r": 0,
                "trailing_trigger_r": 1,
                "trailing_distance_r": 0.5,
                **(settings or {}),
            },
            "protection_repairs": 0,
        }
        return SimpleNamespace(state=SimpleNamespace(
            binance_demo=demo_state,
            v21_demo=v21_state,
        ))

    def initial_install_plan(self):
        plan = self.plan(protection_ids=[])
        plan.update({
            "targets": ["105", "110", "115"],
            "step": "0.1",
            "min_qty": "0.1",
            "quantity": "1",
            "entry_price": "100",
            "initial_quantity": "1",
            "remaining_quantity": "1",
        })
        return plan

    def ownership_snapshot(self, plan):
        fields = (
            "protection_ids", "stop_algo_id", "stop_client_id", "stop_loss",
            "tp1_algo_id", "tp1_client_id", "tp1_quantity",
            "tp2_algo_id", "tp2_client_id", "tp2_quantity",
            "tp3_algo_id", "tp3_client_id", "protection_status",
        )
        return {
            field: (field in plan, deepcopy(plan.get(field)))
            for field in fields
        }

    async def run_initial_install_with_responses(self, responses):
        plan = self.initial_install_plan()
        state = {"plans": {plan["id"]: plan}, "events": []}
        position_calls = 0
        response_index = 0
        before_invalid = None

        async def signed(method, path, params=None):
            nonlocal position_calls
            if path == "/fapi/v3/positionRisk":
                position_calls += 1
                return [{"symbol": "BTCUSDT", "positionAmt": "1"}] if position_calls == 1 else []
            if path == "/fapi/v1/openAlgoOrders":
                return []
            raise AssertionError((method, path, params))

        client = SimpleNamespace(signed=signed)
        async def post_algo(_client, _params):
            nonlocal response_index, before_invalid
            before_invalid = self.ownership_snapshot(plan)
            response = responses[response_index]
            response_index += 1
            return response

        async def abort_installation(*_args, **_kwargs):
            raise binance_demo.BinanceDemoError("test abort")

        with patch.object(binance_demo, "post_algo", new=post_algo), \
                patch.object(binance_demo, "_abort_protection_installation", new=abort_installation), \
                patch.object(binance_demo, "close_symbol_position", new=AsyncMock()), \
                patch.object(binance_demo, "persist_runtime"):
            with self.assertRaises(binance_demo.BinanceDemoError):
                await binance_demo._install_protection(client, state, plan)
        return plan, before_invalid

    async def test_initial_install_wrong_status_does_not_mutate_ownership(self):
        plan, before = await self.run_initial_install_with_responses([
            {"algoId": 201, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "CANCELED"},
        ])
        self.assertEqual(self.ownership_snapshot(plan), before)

    async def test_initial_install_invalid_stop_response_does_not_mutate_ownership(self):
        plan, before = await self.run_initial_install_with_responses([
            {"algoId": 201, "symbol": "ETHUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"},
        ])
        self.assertEqual(self.ownership_snapshot(plan), before)

    async def test_initial_install_invalid_tp1_response_does_not_claim_tp1(self):
        plan, before = await self.run_initial_install_with_responses([
            {"algoId": 201, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"},
            {"algoId": 202, "symbol": "ETHUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
        ])
        self.assertEqual(self.ownership_snapshot(plan), before)

    async def test_initial_install_invalid_tp2_response_does_not_claim_tp2(self):
        plan, before = await self.run_initial_install_with_responses([
            {"algoId": 201, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"},
            {"algoId": 202, "symbol": "BTCUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
            {"algoId": 203, "symbol": "ETHUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
        ])
        self.assertEqual(self.ownership_snapshot(plan), before)

    async def test_initial_install_invalid_tp3_response_does_not_claim_tp3(self):
        plan, before = await self.run_initial_install_with_responses([
            {"algoId": 201, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"},
            {"algoId": 202, "symbol": "BTCUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
            {"algoId": 203, "symbol": "BTCUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
            {"algoId": 204, "symbol": "ETHUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
        ])
        self.assertEqual(self.ownership_snapshot(plan), before)

    def test_exact_owned_stop_is_matched(self):
        plan = self.plan(protection_ids=[101], stop_algo_id=101)
        result = binance_demo._protection_classification(
            plan, self.snapshot([self.algo(101)])
        )
        self.assertEqual(result, ("MATCHED", [101], []))

    def test_missing_expected_id_with_foreign_same_symbol_stop_is_missing(self):
        plan = self.plan(protection_ids=[101], stop_algo_id=101)
        result = binance_demo._protection_classification(
            plan, self.snapshot([self.algo(202)])
        )
        self.assertEqual(result, ("MISSING", [], [101]))

    def test_same_symbol_foreign_stop_without_plan_id_is_unknown(self):
        plan = self.plan(protection_ids=[])
        result = binance_demo._protection_classification(
            plan, self.snapshot([self.algo(202)])
        )
        self.assertEqual(result[0], "UNKNOWN")

    def test_authoritative_absent_exact_id_is_missing(self):
        plan = self.plan(protection_ids=[101], stop_algo_id=101)
        result = binance_demo._protection_classification(plan, self.snapshot([]))
        self.assertEqual(result, ("MISSING", [], [101]))

    def test_unavailable_snapshot_is_unknown(self):
        plan = self.plan(protection_ids=[101], stop_algo_id=101)
        result = binance_demo._protection_classification(
            plan, self.snapshot([], available=False)
        )
        self.assertEqual(result[0], "UNKNOWN")

    def test_malformed_and_duplicate_plan_ids_are_unknown(self):
        for protection_ids in ([True], [101, 101], ["bad"]):
            plan = self.plan(protection_ids=protection_ids, stop_algo_id=101)
            result = binance_demo._protection_classification(
                plan, self.snapshot([self.algo(101)])
            )
            self.assertEqual(result[0], "UNKNOWN")

    def test_duplicate_exchange_algo_id_is_unknown(self):
        plan = self.plan(protection_ids=[101], stop_algo_id=101)
        result = binance_demo._protection_classification(
            plan, self.snapshot([self.algo(101), self.algo(101)])
        )
        self.assertEqual(result[0], "UNKNOWN")

    def test_shared_algo_id_across_active_plans_is_unknown(self):
        first = self.plan(protection_ids=[123], stop_algo_id=123)
        second = self.plan(protection_ids=[123], stop_algo_id=123)
        second["id"] = "plan-b"
        result = binance_demo._protection_classification(
            first,
            self.snapshot([self.algo(123)]),
            plans=[first, second],
        )
        self.assertEqual(result[0], "UNKNOWN")

    def test_malformed_active_plan_ownership_is_unknown(self):
        plan = self.plan(protection_ids=[123], stop_algo_id=123)
        malformed = self.plan(protection_ids=["not-an-id"], stop_algo_id=456)
        malformed["id"] = "plan-b"
        result = binance_demo._protection_classification(
            plan,
            self.snapshot([self.algo(123)]),
            plans=[plan, malformed],
        )
        self.assertEqual(result[0], "UNKNOWN")

    async def test_dynamic_replacement_delete_failure_preserves_ambiguity(self):
        plan = self.plan(protection_ids=[101], stop_algo_id=101)
        application = self.application(plan)
        snapshot = {
            **self.snapshot([self.algo(101)]),
            "positions": [{
                "symbol": "BTCUSDT", "direction": "LONG",
                "entry_price": 100, "mark_price": 120,
            }],
        }
        delete_error = binance_demo.BinanceDemoError("delete failed")
        client = SimpleNamespace(signed=AsyncMock(side_effect=delete_error))
        response = {"algoId": 303, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"}
        with patch.object(v21_demo, "symbol_rules", new=AsyncMock(return_value={"tick": 1})), \
                patch.object(v21_demo, "post_algo", new=AsyncMock(return_value=response)) as post_algo, \
                patch.object(v21_demo, "persist_runtime"):
            with self.assertRaises(binance_demo.BinanceDemoError):
                await v21_demo.improve_dynamic_stops(
                    application, snapshot, client=client
                )
        post_algo.assert_awaited_once()
        client.signed.assert_awaited_once_with(
            "DELETE", "/fapi/v1/algoOrder", {"symbol": "BTCUSDT", "algoId": 101}
        )
        self.assertEqual(plan["stop_algo_id"], 101)
        self.assertEqual(set(plan["protection_ids"]), {101, 303})
        self.assertEqual(plan["protection_status"], "CRITICAL / AMBIGUOUS")

    async def test_manual_cancel_shared_algo_id_does_not_mutate_or_delete(self):
        first = self.plan(protection_ids=[123], stop_algo_id=123)
        second = self.plan(protection_ids=[123], stop_algo_id=123)
        second["id"] = "plan-b"
        before_first = first.copy()
        before_second = second.copy()
        state = {"plans": {"plan-a": first, "plan-b": second}, "events": []}
        client = SimpleNamespace(signed=AsyncMock(return_value={"algoId": 123}))
        request = SimpleNamespace()
        body = binance_demo.CancelAlgoRequest(symbol="BTCUSDT", algo_id=123)
        snapshot = self.snapshot([self.algo(123)])
        with patch.object(binance_demo, "state_for", return_value=state), \
                patch.object(binance_demo, "client_for", return_value=client), \
                patch.object(binance_demo, "_fresh_protection_snapshot", new=AsyncMock(return_value=snapshot)):
            with self.assertRaises(binance_demo.HTTPException):
                await binance_demo.demo_cancel_algo(request, body)
        client.signed.assert_not_awaited()
        self.assertEqual(first, before_first)
        self.assertEqual(second, before_second)

    async def test_dynamic_stop_cannot_adopt_foreign_stop(self):
        plan = self.plan(protection_ids=[101], stop_algo_id=101)
        application = self.application(plan)
        snapshot = {
            **self.snapshot([self.algo(202, trigger_price=110)]),
            "positions": [{"symbol": "BTCUSDT", "direction": "LONG", "entry_price": 100, "mark_price": 120}],
        }
        client = SimpleNamespace()
        with patch.object(v21_demo, "symbol_rules", new=AsyncMock(return_value={"tick": 1})), \
                patch.object(v21_demo, "post_algo", new=AsyncMock()) as post_algo:
            changed = await v21_demo.improve_dynamic_stops(application, snapshot, client=client)
        self.assertFalse(changed)
        self.assertEqual(plan["stop_algo_id"], 101)
        post_algo.assert_not_awaited()

    async def test_unknown_stop_does_not_repair(self):
        plan = self.plan(protection_ids=[])
        application = self.application(plan)
        snapshot = {
            **self.snapshot([self.algo(202)]),
            "positions": [{"symbol": "BTCUSDT", "direction": "LONG"}],
        }
        with patch.object(v21_demo, "post_algo", new=AsyncMock()) as post_algo:
            changed = await v21_demo.ensure_stop_protection(application, snapshot, client=SimpleNamespace())
        self.assertFalse(changed)
        post_algo.assert_not_awaited()
        self.assertEqual(plan["protection_ids"], [])

    async def test_missing_stop_with_unknown_ownership_does_not_repair(self):
        plan = self.plan(protection_ids=[])
        application = self.application(plan)
        snapshot = {
            **self.snapshot([]),
            "positions": [{"symbol": "BTCUSDT", "direction": "LONG"}],
        }
        before = plan.copy()
        with patch.object(v21_demo, "post_algo", new=AsyncMock(return_value={"algoId": 303, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"})) as post_algo, \
            patch.object(v21_demo, "persist_runtime") as persist_runtime:
            changed = await v21_demo.ensure_stop_protection(application, snapshot, client=SimpleNamespace())
        self.assertFalse(changed)
        post_algo.assert_not_awaited()
        persist_runtime.assert_not_called()
        self.assertEqual(plan, before)

    async def test_v21_repair_rejects_invalid_response_identity(self):
        responses = [
            {"algoId": 303, "symbol": "ETHUSDT"},
            {"algoId": 303, "type": "TAKE_PROFIT_MARKET"},
            {"algoId": 303, "side": "BUY"},
            {"symbol": "BTCUSDT"},
            {"algoId": "not-an-id"},
        ]
        for response in responses:
            with self.subTest(response=response):
                plan = self.plan(protection_ids=[999], stop_algo_id=999)
                application = self.application(plan)
                snapshot = {
                    **self.snapshot([]),
                    "positions": [{"symbol": "BTCUSDT", "direction": "LONG"}],
                }
                before = plan.copy()
                with patch.object(v21_demo, "post_algo", new=AsyncMock(return_value=response)) as post_algo, \
                        patch.object(v21_demo, "persist_runtime"):
                    changed = await v21_demo.ensure_stop_protection(
                        application, snapshot, client=SimpleNamespace()
                    )
                self.assertFalse(changed)
                post_algo.assert_awaited_once()
                self.assertEqual(plan, before)

    async def test_v21_repair_rejects_id_referenced_by_another_active_plan(self):
        plan = self.plan(protection_ids=[999], stop_algo_id=999)
        other = self.plan(protection_ids=[303], stop_algo_id=303)
        other["id"] = "plan-b"
        application = self.application(plan)
        application.state.binance_demo["plans"][other["id"]] = other
        snapshot = {
            **self.snapshot([]),
            "positions": [{"symbol": "BTCUSDT", "direction": "LONG"}],
        }
        with patch.object(v21_demo, "post_algo", new=AsyncMock(return_value={"algoId": 303, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"})) as post_algo, \
                patch.object(v21_demo, "persist_runtime"):
            changed = await v21_demo.ensure_stop_protection(
                application, snapshot, client=SimpleNamespace()
            )
        self.assertFalse(changed)
        post_algo.assert_awaited_once()
        self.assertEqual(plan["protection_ids"], [999])
        self.assertEqual(plan["stop_algo_id"], 999)

    async def test_v21_repair_replaces_only_missing_id(self):
        plan = self.plan(protection_ids=[999, 777, 888], stop_algo_id=999)
        application = self.application(plan)
        snapshot = {
            **self.snapshot([]),
            "positions": [{"symbol": "BTCUSDT", "direction": "LONG"}],
        }
        with patch.object(v21_demo, "post_algo", new=AsyncMock(return_value={"algoId": 456, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"})) as post_algo, \
                patch.object(v21_demo, "persist_runtime"):
            changed = await v21_demo.ensure_stop_protection(
                application, snapshot, client=SimpleNamespace()
            )
        self.assertTrue(changed)
        post_algo.assert_awaited_once()
        self.assertEqual(set(plan["protection_ids"]), {456, 777, 888})
        self.assertEqual(plan["stop_algo_id"], 456)

    async def test_dynamic_replacement_rejects_invalid_response_without_delete(self):
        responses = [
            {"algoId": 303, "symbol": "ETHUSDT"},
            {"algoId": 303, "type": "TAKE_PROFIT_MARKET"},
            {"algoId": 303, "side": "BUY"},
            {"algoId": "bad"},
        ]
        for response in responses:
            with self.subTest(response=response):
                plan = self.plan(protection_ids=[101], stop_algo_id=101)
                application = self.application(plan)
                snapshot = {
                    **self.snapshot([self.algo(101)]),
                    "positions": [{
                        "symbol": "BTCUSDT", "direction": "LONG",
                        "entry_price": 100, "mark_price": 120,
                    }],
                }
                client = SimpleNamespace(signed=AsyncMock())
                with patch.object(v21_demo, "symbol_rules", new=AsyncMock(return_value={"tick": 1})), \
                        patch.object(v21_demo, "post_algo", new=AsyncMock(return_value=response)) as post_algo, \
                        patch.object(v21_demo, "persist_runtime"):
                    changed = await v21_demo.improve_dynamic_stops(
                        application, snapshot, client=client
                    )
                self.assertFalse(changed)
                post_algo.assert_awaited_once()
                client.signed.assert_not_awaited()
                self.assertEqual(plan["protection_ids"], [101])
                self.assertEqual(plan["stop_algo_id"], 101)

    async def test_dynamic_replacement_rejects_shared_response_id(self):
        plan = self.plan(protection_ids=[101], stop_algo_id=101)
        other = self.plan(protection_ids=[303], stop_algo_id=303)
        other["id"] = "plan-b"
        other["symbol"] = "ETHUSDT"
        application = self.application(plan)
        application.state.binance_demo["plans"][other["id"]] = other
        snapshot = {
            **self.snapshot([self.algo(101)]),
            "positions": [{
                "symbol": "BTCUSDT", "direction": "LONG",
                "entry_price": 100, "mark_price": 120,
            }],
        }
        client = SimpleNamespace(signed=AsyncMock())
        response = {"algoId": 303, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL"}
        with patch.object(v21_demo, "symbol_rules", new=AsyncMock(return_value={"tick": 1})), \
                patch.object(v21_demo, "post_algo", new=AsyncMock(return_value=response)) as post_algo, \
                patch.object(v21_demo, "persist_runtime"):
            changed = await v21_demo.improve_dynamic_stops(application, snapshot, client=client)
        self.assertFalse(changed)
        post_algo.assert_awaited_once()
        client.signed.assert_not_awaited()
        self.assertEqual(plan["protection_ids"], [101])
        self.assertEqual(plan["stop_algo_id"], 101)

    async def test_dynamic_replacement_accepts_verified_response_and_deletes_old_id(self):
        plan = self.plan(protection_ids=[101], stop_algo_id=101)
        application = self.application(plan)
        snapshot = {
            **self.snapshot([self.algo(101)]),
            "positions": [{
                "symbol": "BTCUSDT", "direction": "LONG",
                "entry_price": 100, "mark_price": 120,
            }],
        }
        client = SimpleNamespace(signed=AsyncMock(return_value={}))
        response = {"algoId": 303, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"}
        with patch.object(v21_demo, "symbol_rules", new=AsyncMock(return_value={"tick": 1})), \
                patch.object(v21_demo, "post_algo", new=AsyncMock(return_value=response)) as post_algo, \
                patch.object(v21_demo, "persist_runtime"):
            changed = await v21_demo.improve_dynamic_stops(application, snapshot, client=client)
        self.assertTrue(changed)
        post_algo.assert_awaited_once()
        client.signed.assert_awaited_once_with(
            "DELETE", "/fapi/v1/algoOrder", {"symbol": "BTCUSDT", "algoId": 101}
        )
        self.assertEqual(plan["protection_ids"], [303])
        self.assertEqual(plan["stop_algo_id"], 303)

    async def test_partial_missing_repairs_without_adopting_foreign_stop(self):
        plan = self.plan(protection_ids=[999], stop_algo_id=999)
        snapshot = self.snapshot([self.algo(123)])
        client = SimpleNamespace()
        with patch.object(binance_demo, "post_algo", new=AsyncMock(return_value={"algoId": 303, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"})) as post_algo, \
                patch.object(binance_demo, "persist_runtime"):
            changed = await binance_demo._repair_missing_stop_protection(
                client,
                {"plans": {plan["id"]: plan}},
                plan,
                snapshot,
                plans=[plan],
            )
        self.assertTrue(changed)
        post_algo.assert_awaited_once()
        self.assertEqual(plan["protection_ids"], [303])
        self.assertEqual(plan["stop_algo_id"], 303)
        self.assertNotIn(123, plan["protection_ids"])

    async def test_cleanup_missing_does_not_delete(self):
        plan = self.plan(protection_ids=[101], stop_algo_id=101)
        client = SimpleNamespace(signed=AsyncMock())
        await binance_demo.cleanup_closed_plan(
            client, plan, snapshot=self.snapshot([self.algo(202)])
        )
        client.signed.assert_not_awaited()
        self.assertEqual(plan["protection_ids"], [])

    async def test_cleanup_matched_deletes_exact_id(self):
        plan = self.plan(protection_ids=[101], stop_algo_id=101)
        client = SimpleNamespace(signed=AsyncMock(side_effect=[{}, []]))
        await binance_demo.cleanup_closed_plan(
            client, plan, snapshot=self.snapshot([self.algo(101)])
        )
        self.assertEqual(client.signed.await_args_list[0].args,
            (
            "DELETE", "/fapi/v1/algoOrder", {"symbol": "BTCUSDT", "algoId": 101}
            )
        )
        self.assertEqual(client.signed.await_count, 2)
        self.assertEqual(plan["protection_ids"], [])


if __name__ == "__main__":
    unittest.main()
