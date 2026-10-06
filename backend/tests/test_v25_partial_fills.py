import asyncio
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).parents[1]))

from app import v25_execution as execution  # noqa: E402


def make_plan(**overrides):
    plan = {
        "id": "partial-plan", "intent_id": "partial-intent",
        "symbol": "BTCUSDT", "direction": "LONG", "order_type": "MARKET",
        "quantity": "0.010", "entry_price": "50000", "stop_loss": "49000",
        "targets": ["51000", "52000", "53000"],
        "step": "0.001", "min_qty": "0.001", "min_notional": "5",
        "entry_order_id": 42,
        "entry_client_order_id": execution.client_id_for("ENTRY", "partial-intent"),
        "entry_order_status": "PARTIALLY_FILLED",
        "provenance_state": "PROVISIONAL", "status": "DOLUM BEKLİYOR",
        "protection_ids": [],
    }
    plan.update(overrides)
    return plan


class PartialFillClient(execution.BinanceLiveClient):
    application = None
    time_offset_ms = 0

    def __init__(self, plan, quantity="0.005", final_quantity=None):
        self.plan = plan
        self.quantity = quantity
        self.final_quantity = final_quantity or quantity
        self.order = {
            "symbol": plan["symbol"], "side": "BUY" if plan["direction"] == "LONG" else "SELL", "type": plan["order_type"],
            "positionSide": "BOTH", "orderId": 42,
            "clientOrderId": plan["entry_client_order_id"], "origQty": "0.010",
            "status": plan["entry_order_status"],
        }
        self.calls = []
        self.algos = []
        self.cancel_error = None
        self.cancel_status = "CANCELED"
        self.position_symbol = plan["symbol"]

    async def signed(self, method, path, params=None):
        params = dict(params or {})
        self.calls.append((method, path, params))
        if (method, path) == ("GET", "/fapi/v3/positionRisk"):
            return [{"symbol": self.position_symbol, "positionAmt": self.quantity, "markPrice": "50000"}]
        if (method, path) == ("GET", "/fapi/v1/order"):
            return dict(self.order)
        if (method, path) == ("DELETE", "/fapi/v1/order"):
            if self.cancel_error:
                raise self.cancel_error
            self.order["status"] = self.cancel_status
            self.quantity = self.final_quantity
            return dict(self.order)
        if (method, path) == ("POST", "/fapi/v1/algoOrder"):
            self.algos.append(params)
            return {"algoId": 100 + len(self.algos)}
        if (method, path) == ("GET", "/fapi/v1/openAlgoOrders"):
            return [
                {**row, "algoId": 101 + index, "status": "NEW"}
                for index, row in enumerate(self.algos)
            ]
        raise AssertionError(f"Unexpected mock request: {method} {path}")

    def snapshot(self):
        return {
            "wallet_balance": 1000, "available_balance": 1000,
            "positions": [{
                "symbol": self.position_symbol,
                "direction": "LONG" if Decimal(self.quantity) > 0 else "SHORT",
                "quantity": execution.decimal_text(abs(Decimal(self.quantity))),
            }] if Decimal(self.quantity) != 0 else [],
            "open_orders": [], "open_algo_orders_available": True,
            "algo_orders_quality": "VALID_ROWS", "hedge_mode": False,
            "open_algo_orders": [
                {**row, "algo_id": 101 + index, "client_algo_id": row["clientAlgoId"], "status": "NEW"}
                for index, row in enumerate(self.algos)
            ],
        }


class PartialFillTests(unittest.TestCase):
    def setUp(self):
        self.persistence = patch.object(execution, "persist_state")
        self.persistence.start()
        self.addCleanup(self.persistence.stop)
        retry_delay = patch.object(execution, "STOP_RETRY_BASE_SECONDS", 0)
        retry_delay.start()
        self.addCleanup(retry_delay.stop)

    def install(self, client, plan):
        state = execution.initial_state()
        state["plans"] = {plan["id"]: plan}
        asyncio.run(execution.install_protection(client, state, plan))
        return state

    def reconcile(self, client, state):
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        with patch.object(execution, "client_for_with_credentials", return_value=client), \
                patch.object(execution, "account_snapshot", AsyncMock(side_effect=lambda *_: client.snapshot())), \
                patch.object(execution, "recover_orphan_plans", AsyncMock(return_value=0)), \
                patch.object(execution, "cleanup_orphan_protection_orders", AsyncMock()):
            asyncio.run(execution.reconcile(application))

    def test_required_quantity_cases(self):
        for actual, accepted in [("0.005", True), ("0.010", True), ("0.012", False), ("0", False)]:
            with self.subTest(actual=actual):
                plan = make_plan()
                result = execution.confirm_live_plan_provenance(
                    plan, {"symbol": "BTCUSDT", "direction": "LONG", "quantity": actual},
                )
                self.assertEqual(result, accepted)
                if accepted:
                    self.assertEqual(Decimal(plan["quantity"]), Decimal(actual))
                    self.assertEqual(Decimal(plan["requested_quantity"]), Decimal("0.010"))
                    self.assertEqual(plan["provenance_state"], "CONFIRMED")
                else:
                    self.assertEqual(plan["quantity"], "0.010")
                    self.assertEqual(plan["provenance_state"], "PROVISIONAL")

    def test_adopted_and_read_only_positions_are_outside_entry_cancellation_scope(self):
        for overrides in [{"source": "external_adoption"}, {"mutation_policy": "READ_ONLY_EXTERNAL"}]:
            with self.subTest(overrides=overrides):
                self.assertFalse(execution.entry_remainder_needs_settlement({
                    **overrides, "entry_remainder_pending": True,
                }))

    def test_identity_and_invalid_quantities_remain_rejected(self):
        cases = [
            {"symbol": "ETHUSDT"}, {"direction": "SHORT"},
            {"quantity": "-0.005"}, {"quantity": "NaN"}, {"quantity": "Infinity"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                plan = make_plan()
                position = {"symbol": "BTCUSDT", "direction": "LONG", "quantity": "0.005", **overrides}
                self.assertFalse(execution.confirm_live_plan_provenance(plan, position))
                self.assertEqual(plan["quantity"], "0.010")
        for overrides in [
            {"entry_order_id": None}, {"entry_client_order_id": None},
            {"provenance_state": "BROKEN"}, {"provenance_state": "NO_PROVENANCE"},
            {"mutation_policy": "READ_ONLY_EXTERNAL"},
        ]:
            with self.subTest(plan=overrides):
                plan = make_plan(**overrides)
                self.assertFalse(execution.confirm_live_plan_provenance(
                    plan, {"symbol": "BTCUSDT", "direction": "LONG", "quantity": "0.005"},
                ))
                self.assertEqual(plan["quantity"], "0.010")

    def test_partial_fill_installs_stop_then_cancels_owned_remainder_and_sizes_tp(self):
        plan = make_plan()
        client = PartialFillClient(plan)
        self.install(client, plan)
        self.assertEqual(plan["provenance_state"], "CONFIRMED")
        self.assertEqual(Decimal(plan["quantity"]), Decimal("0.005"))
        self.assertEqual(plan["entry_cancellation_state"], "CANCELLED")
        self.assertEqual(plan["protection_state"], "MATCHED")
        stop, partial_tp, final_tp = client.algos
        self.assertEqual(stop["type"], "STOP_MARKET")
        self.assertEqual(stop["closePosition"], "true")
        self.assertNotIn("quantity", stop)
        self.assertEqual(Decimal(partial_tp["quantity"]), Decimal("0.003"))
        self.assertEqual(partial_tp["reduceOnly"], "true")
        self.assertEqual(final_tp["closePosition"], "true")
        deletes = [call for call in client.calls if call[0] == "DELETE"]
        self.assertEqual(deletes, [("DELETE", "/fapi/v1/order", {"symbol": "BTCUSDT", "orderId": 42})])
        stop_index = next(index for index, call in enumerate(client.calls) if call[0] == "POST")
        cancel_index = client.calls.index(deletes[0])
        self.assertLess(stop_index, cancel_index)

    def test_full_fill_does_not_cancel_and_uses_full_actual_quantity(self):
        plan = make_plan(entry_order_status="FILLED")
        client = PartialFillClient(plan, "0.010")
        self.install(client, plan)
        self.assertFalse(any(call[0] == "DELETE" for call in client.calls))
        self.assertEqual(Decimal(client.algos[1]["quantity"]), Decimal("0.006"))

    def test_short_partial_fill_uses_buy_reduce_only_protections(self):
        plan = make_plan(direction="SHORT", stop_loss="51000", targets=["49000", "48000", "47000"])
        client = PartialFillClient(plan, "-0.005")
        self.install(client, plan)
        self.assertEqual(plan["provenance_state"], "CONFIRMED")
        self.assertEqual(Decimal(plan["quantity"]), Decimal("0.005"))
        self.assertTrue(all(row["side"] == "BUY" for row in client.algos))
        self.assertEqual(Decimal(client.algos[1]["quantity"]), Decimal("0.003"))
        self.assertEqual(client.algos[1]["reduceOnly"], "true")

    def test_tp_rounding_and_minimum_notional_boundary(self):
        for quantity, min_notional, expected in [("0.009", "5", "0.005"), ("0.005", "150", "0.003")]:
            with self.subTest(quantity=quantity, min_notional=min_notional):
                plan = make_plan(min_notional=min_notional)
                client = PartialFillClient(plan, quantity)
                self.install(client, plan)
                self.assertEqual(Decimal(client.algos[1]["quantity"]), Decimal(expected))
        plan = make_plan(min_qty="0.002")
        client = PartialFillClient(plan, "0.003")
        self.install(client, plan)
        self.assertEqual(len(client.algos), 2)
        self.assertEqual(plan["monitoring_targets"], ["TP1", "TP2"])

    def test_new_entry_with_partial_position_also_cancels_remainder(self):
        plan = make_plan(entry_order_status="NEW")
        client = PartialFillClient(plan)
        self.install(client, plan)
        self.assertEqual(plan["entry_cancellation_state"], "CANCELLED")
        self.assertEqual(Decimal(client.algos[1]["quantity"]), Decimal("0.003"))

    def test_provenance_retains_original_bound_across_multiple_fill_updates(self):
        plan = make_plan()
        for quantity, accepted in [("0.005", True), ("0.007", True), ("0.012", False)]:
            self.assertEqual(execution.confirm_live_plan_provenance(
                plan, {"symbol": "BTCUSDT", "direction": "LONG", "quantity": quantity},
            ), accepted)
        self.assertEqual(Decimal(plan["quantity"]), Decimal("0.007"))
        self.assertEqual(Decimal(plan["requested_quantity"]), Decimal("0.010"))

    def test_overfill_or_zero_after_cancellation_keeps_stop_but_blocks_tp(self):
        for final_quantity in ["0.012", "0"]:
            with self.subTest(final_quantity=final_quantity):
                plan = make_plan()
                client = PartialFillClient(plan, final_quantity=final_quantity)
                with self.assertRaises(execution.LiveExchangeError):
                    self.install(client, plan)
                self.assertEqual(plan["provenance_state"], "BROKEN")
                self.assertEqual(len(client.algos), 1)
                self.assertEqual(client.algos[0]["type"], "STOP_MARKET")

    def test_cancel_fill_race_rechecks_position_against_original_requested_quantity(self):
        for final_quantity, status in [("0.007", "CANCELED"), ("0.010", "FILLED")]:
            with self.subTest(final_quantity=final_quantity):
                plan = make_plan()
                client = PartialFillClient(plan, final_quantity=final_quantity)
                client.cancel_status = status
                self.install(client, plan)
                self.assertEqual(Decimal(plan["quantity"]), Decimal(final_quantity))
                self.assertEqual(Decimal(plan["requested_quantity"]), Decimal("0.010"))
                self.assertEqual(
                    Decimal(client.algos[1]["quantity"]),
                    execution.floor_step(Decimal(final_quantity) * Decimal("0.60"), Decimal("0.001")),
                )

    def test_overfill_and_wrong_position_identity_never_install_protection(self):
        for quantity, symbol in [("0.012", "BTCUSDT"), ("0.005", "ETHUSDT"), ("-0.005", "BTCUSDT")]:
            with self.subTest(quantity=quantity, symbol=symbol):
                plan = make_plan()
                client = PartialFillClient(plan, quantity)
                client.position_symbol = symbol
                self.install(client, plan)
                self.assertEqual(plan["provenance_state"], "BROKEN")
                self.assertEqual(client.algos, [])
                self.assertFalse(any(call[0] == "DELETE" for call in client.calls))

    def test_minimum_qty_and_notional_keep_stop_and_skip_invalid_partial_tp(self):
        for overrides, quantity in [({"min_notional": "151"}, "0.005"), ({}, "0.001")]:
            with self.subTest(overrides=overrides, quantity=quantity):
                plan = make_plan(**overrides)
                client = PartialFillClient(plan, quantity)
                state = self.install(client, plan)
                self.assertEqual(len(client.algos), 2)
                self.assertEqual(client.algos[0]["type"], "STOP_MARKET")
                self.assertEqual(client.algos[1]["closePosition"], "true")
                self.assertEqual(plan["monitoring_targets"], ["TP1", "TP2"])
                self.assertTrue(any(event["kind"] == "TP_MONITORING_PERSISTED" for event in state["events"]))

    def test_cancel_failure_is_explicit_and_already_installed_stop_is_kept(self):
        plan = make_plan()
        client = PartialFillClient(plan)
        client.cancel_error = execution.LiveExchangeError("Mock cancellation timeout", unknown_execution=True)
        with self.assertRaises(execution.LiveExchangeError):
            self.install(client, plan)
        self.assertEqual(len(client.algos), 1)
        self.assertEqual(client.algos[0]["type"], "STOP_MARKET")
        self.assertEqual(plan["entry_cancellation_state"], "UNKNOWN")
        self.assertTrue(plan["entry_remainder_pending"])

    def test_reconcile_retries_uncertain_cancellation_without_duplicate_stop(self):
        plan = make_plan()
        client = PartialFillClient(plan)
        client.cancel_error = execution.LiveExchangeError("Mock cancellation timeout", unknown_execution=True)
        state = execution.initial_state()
        state["plans"] = {plan["id"]: plan}
        with self.assertRaises(execution.LiveExchangeError):
            asyncio.run(execution.install_protection(client, state, plan))
        self.assertTrue(state["reconciliation_required"])
        self.assertTrue(state["real_trading_locked"])
        client.cancel_error = None
        self.reconcile(client, state)
        self.assertEqual(plan["entry_cancellation_state"], "CANCELLED")
        self.assertFalse(plan["entry_remainder_pending"])
        self.assertEqual(len([row for row in client.algos if row["type"] == "STOP_MARKET"]), 1)
        self.assertEqual(Decimal(client.algos[1]["quantity"]), Decimal("0.003"))

    def test_nonterminal_cancel_response_never_reports_success(self):
        plan = make_plan()
        client = PartialFillClient(plan)
        client.cancel_status = "PARTIALLY_FILLED"
        with self.assertRaises(execution.LiveExchangeError):
            self.install(client, plan)
        self.assertEqual(plan["entry_cancellation_state"], "UNKNOWN")
        self.assertEqual(len(client.algos), 1)

    def test_stop_failure_cancels_remainder_before_existing_reduce_only_close(self):
        plan = make_plan()
        client = PartialFillClient(plan, final_quantity="0.007")
        closed_quantities = []

        async def close_position(*_):
            closed_quantities.append(client.quantity)
            return {"orderId": 99}

        with patch.object(execution, "post_algo", AsyncMock(side_effect=execution.LiveExchangeError("Mock stop rejected"))), \
                patch.object(execution, "close_tracked_symbol", AsyncMock(side_effect=close_position)):
            self.install(client, plan)
        self.assertEqual(closed_quantities, ["0.007"])
        self.assertEqual(Decimal(plan["quantity"]), Decimal("0.007"))
        self.assertEqual(plan["entry_cancellation_state"], "CANCELLED")
        self.assertEqual(plan["status"], "GÜVENLİK İÇİN KAPATILDI")
        self.assertFalse(plan["entry_remainder_pending"])
        self.assertEqual(plan["stop_install_attempts"], 3)
        self.assertEqual(len([call for call in client.calls if call[:2] == ("GET", "/fapi/v1/openAlgoOrders")]), 3)

    def test_stop_and_cancel_failure_still_attempts_close_but_remains_unknown(self):
        plan = make_plan()
        client = PartialFillClient(plan)
        client.cancel_error = execution.LiveExchangeError("Mock cancellation timeout", unknown_execution=True)
        closer = AsyncMock(return_value={"orderId": 99})
        with patch.object(execution, "post_algo", AsyncMock(side_effect=execution.LiveExchangeError("Mock stop rejected"))), \
                patch.object(execution, "close_tracked_symbol", closer):
            with self.assertRaises(execution.LiveExchangeError):
                self.install(client, plan)
        closer.assert_awaited_once()
        self.assertTrue(plan["entry_remainder_pending"])
        self.assertEqual(plan["entry_cancellation_state"], "UNKNOWN")

    def test_zero_position_does_not_settle_closed_plan_while_entry_remainder_is_open(self):
        plan = make_plan(
            provenance_state="CONFIRMED", quantity="0.005", requested_quantity="0.010",
            entry_remainder_pending=True, status="CLOSING", protection_cleanup_state="CLEAN",
        )
        client = PartialFillClient(plan, "0")
        client.cancel_error = execution.LiveExchangeError("Mock cancellation timeout", unknown_execution=True)
        state = execution.initial_state()
        state["plans"] = {plan["id"]: plan}
        settler = AsyncMock()
        with patch.object(execution, "settle_closed_plan", settler), \
                patch.object(execution, "cancel_owned_algos_for_symbol", AsyncMock(return_value=0)):
            with self.assertRaises(execution.LiveExchangeError):
                self.reconcile(client, state)
            settler.assert_not_awaited()
            self.assertTrue(plan["entry_remainder_pending"])
            client.cancel_error = None
            self.reconcile(client, state)
            settler.assert_awaited_once()
            self.assertFalse(plan["entry_remainder_pending"])
            self.assertEqual(plan["entry_cancellation_state"], "CANCELLED")

    def test_late_fill_after_empty_snapshot_is_protected_not_settled_as_closed(self):
        plan = make_plan(
            provenance_state="CONFIRMED", quantity="0.005", requested_quantity="0.010",
            entry_remainder_pending=True, status="CLOSING",
        )
        client = PartialFillClient(plan, "0", final_quantity="0.005")
        state = execution.initial_state()
        state["plans"] = {plan["id"]: plan}
        with patch.object(execution, "settle_closed_plan", AsyncMock()) as settler:
            self.reconcile(client, state)
        settler.assert_not_awaited()
        self.assertEqual(plan["status"], "KORUMA AKTİF")
        self.assertEqual(len(client.algos), 3)
        self.assertEqual(Decimal(client.algos[1]["quantity"]), Decimal("0.003"))
        self.assertFalse(plan["entry_remainder_pending"])

    def test_existing_exact_stop_and_targets_are_not_duplicated_during_entry_recovery(self):
        plan = make_plan(
            provenance_state="CONFIRMED", quantity="0.005", requested_quantity="0.010",
            entry_remainder_pending=True, entry_order_status="CANCELED",
        )
        client = PartialFillClient(plan)
        client.algos = [
            {"symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET", "closePosition": "true", "triggerPrice": "49000", "clientAlgoId": execution.client_id_for("SL", plan["intent_id"])},
            {"symbol": "BTCUSDT", "side": "SELL", "type": "TAKE_PROFIT_MARKET", "quantity": "0.003", "reduceOnly": "true", "triggerPrice": "51000", "clientAlgoId": execution.client_id_for("TP12", plan["intent_id"])},
            {"symbol": "BTCUSDT", "side": "SELL", "type": "TAKE_PROFIT_MARKET", "closePosition": "true", "triggerPrice": "53000", "clientAlgoId": execution.client_id_for("TP3", plan["intent_id"])},
        ]
        plan.update({"stop_algo_id": 101, "stop_client_id": client.algos[0]["clientAlgoId"]})
        state = execution.initial_state()
        state["plans"] = {plan["id"]: plan}
        self.reconcile(client, state)
        self.assertFalse(any(call[0] in {"POST", "DELETE"} for call in client.calls))
        self.assertEqual(plan["protection_ids"], [101, 102, 103])
        self.assertFalse(plan["entry_remainder_pending"])

    def test_foreign_or_mismatched_entry_is_never_cancelled(self):
        for overrides in [{"clientOrderId": "MANUAL_ORDER"}, {"orderId": 99}, {"origQty": "0.012"}]:
            with self.subTest(overrides=overrides):
                plan = make_plan()
                client = PartialFillClient(plan)
                client.order.update(overrides)
                with self.assertRaises(execution.LiveExchangeError):
                    self.install(client, plan)
                self.assertFalse(any(call[0] == "DELETE" for call in client.calls))

    def test_reconcile_cancels_partial_remainder_even_when_stop_is_already_matched(self):
        plan = make_plan()
        client = PartialFillClient(plan)
        client.algos = [{
            "symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET",
            "closePosition": "true", "triggerPrice": "49000",
            "clientAlgoId": execution.client_id_for("SL", plan["intent_id"]),
        }]
        plan.update({"stop_algo_id": 101, "stop_client_id": client.algos[0]["clientAlgoId"]})
        state = execution.initial_state()
        state["plans"] = {plan["id"]: plan}
        self.reconcile(client, state)
        self.assertEqual(plan["entry_cancellation_state"], "CANCELLED")
        self.assertEqual(Decimal(plan["quantity"]), Decimal("0.005"))
        self.assertEqual(len([row for row in client.algos if row["type"] == "STOP_MARKET"]), 1)
        self.assertEqual(Decimal(client.algos[1]["quantity"]), Decimal("0.003"))

    def test_recovered_intent_preserves_quantity_bound_and_exchange_notional_rule(self):
        original = make_plan()
        client = PartialFillClient(original)
        recovered = execution.recover_plan_from_intent(
            original["intent_id"],
            {"spec": original, "client_order_id": original["entry_client_order_id"]},
            client.order,
        )
        self.assertIsNotNone(recovered)
        self.assertEqual(Decimal(recovered["requested_quantity"]), Decimal("0.010"))
        self.assertEqual(recovered["min_notional"], "5")
        self.assertEqual(recovered["entry_order_status"], "PARTIALLY_FILLED")

    def test_legacy_plan_fetches_missing_notional_rule_without_unprotected_fallback(self):
        plan = make_plan()
        del plan["min_notional"]
        client = PartialFillClient(plan)
        rules = {"step": Decimal("0.001"), "min_qty": Decimal("0.001"), "min_notional": Decimal("151")}
        with patch.object(execution, "live_symbol_rules", AsyncMock(return_value=rules)) as fetch_rules:
            self.install(client, plan)
        fetch_rules.assert_awaited_once_with(client, "BTCUSDT", "MARKET")
        self.assertEqual(plan["min_notional"], "151")
        self.assertEqual(plan["monitoring_targets"], ["TP1", "TP2"])

    def test_limit_entry_tp_uses_market_lot_rules(self):
        plan = make_plan(order_type="LIMIT")
        client = PartialFillClient(plan)
        rules = {"step": Decimal("0.002"), "min_qty": Decimal("0.002"), "min_notional": Decimal("5")}
        with patch.object(execution, "live_symbol_rules", AsyncMock(return_value=rules)):
            self.install(client, plan)
        self.assertEqual(Decimal(client.algos[1]["quantity"]), Decimal("0.002"))

    def test_live_spec_includes_minimum_notional_for_durable_recovery(self):
        client = PartialFillClient(make_plan())
        rules = {
            "step": Decimal("0.001"), "min_qty": Decimal("0.001"),
            "max_qty": Decimal("100"), "min_notional": Decimal("5"), "tick": Decimal("0.01"),
        }
        order = execution.LiveOrderRequest(
            symbol="BTCUSDT", direction="LONG", order_type="MARKET",
            margin_usdt=25, leverage=2, stop_loss=49000, tp1=55000, tp2=60000, tp3=65000,
        )
        with patch.object(execution, "ticker_price", AsyncMock(return_value=Decimal("50000"))), \
                patch.object(execution, "live_symbol_rules", AsyncMock(return_value=rules)):
            spec = asyncio.run(execution.build_live_spec(client, order, execution.initial_state()["policy"]))
        self.assertEqual(spec["min_notional"], Decimal("5"))


if __name__ == "__main__":
    unittest.main()
