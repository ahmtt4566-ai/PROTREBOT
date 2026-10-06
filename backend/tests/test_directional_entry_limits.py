import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).parents[1]))

from app import binance_demo, v25_execution as execution  # noqa: E402
from app.execution_core import evaluate_entry_gates, policy_digest, sanitize_execution_policy  # noqa: E402
from test_v25_auto_symbol_scope import CREDENTIALS, SNAPSHOT, application_for, order_for, spec_for  # noqa: E402


def gate_for(symbol: str = "CCCUSDT", direction: str = "LONG", *,
             snapshot: dict[str, Any] | None = None, policy: dict[str, Any] | None = None,
             **kwargs: Any) -> dict[str, Any]:
    return evaluate_entry_gates(
        symbol=symbol,
        signal={"direction": direction, "confidence": 100, "radar": {"trap_score": 0}},
        snapshot=snapshot or {"positions": [], "open_orders": []},
        policy=policy or sanitize_execution_policy({"allowed_symbols": ["AAAUSDT", "BBBUSDT", "CCCUSDT"]}),
        daily={"entries": 0, "realized_pnl": 0, "unverified_closures": 0},
        spread_bps=1, armed=True,
        candidate_notional_usdt=kwargs.pop("candidate_notional_usdt", 10), **kwargs,
    )


def failed_keys(result: dict[str, Any]) -> set[str]:
    return {gate["key"] for gate in result["gates"] if not gate["passed"]}


def exposure_policy(cap: float) -> dict[str, Any]:
    return sanitize_execution_policy({
        "allowed_symbols": ["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        "max_direction_exposure_usdt": cap,
    })


class DirectionalEntryLimitTests(unittest.TestCase):
    def test_third_long_is_rejected_at_default_limit(self):
        result = gate_for(snapshot={
            "positions": [
                {"symbol": "AAAUSDT", "direction": "LONG", "notional_usdt": 10},
                {"symbol": "BBBUSDT", "direction": "LONG", "notional_usdt": 10},
            ],
            "open_orders": [],
        })
        self.assertFalse(result["passed"])
        self.assertIn("same_direction_positions", {
            gate["key"] for gate in result["gates"] if not gate["passed"]
        })

    def test_three_same_round_long_candidates_only_allow_two(self):
        reservations = []
        accepted = []
        with self.assertLogs("app.execution_core", level="INFO") as logs:
            for symbol in ["AAAUSDT", "BBBUSDT", "CCCUSDT"]:
                result = gate_for(symbol, cycle_candidates=reservations)
                if result["passed"]:
                    accepted.append(symbol)
                    reservations.append({"symbol": symbol, "direction": "LONG", "notional_usdt": 10})
        self.assertEqual(accepted, ["AAAUSDT", "BBBUSDT"])
        self.assertIn("DIRECTIONAL_ENTRY_BLOCKED", "\n".join(logs.output))
        self.assertIn("same_direction_positions", "\n".join(logs.output))
        self.assertIn("3 / 2", result["reason"])

    def test_long_and_short_capacities_are_independent(self):
        reservations = [
            {"symbol": "AAAUSDT", "direction": "LONG", "notional_usdt": 10},
            {"symbol": "BBBUSDT", "direction": "LONG", "notional_usdt": 10},
        ]
        self.assertTrue(gate_for(direction="SHORT", cycle_candidates=reservations)["passed"])
        self.assertFalse(gate_for(direction="LONG", cycle_candidates=reservations)["passed"])

    def test_pending_buy_and_sell_count_in_their_direction(self):
        for direction, side in [("LONG", "BUY"), ("SHORT", "SELL")]:
            with self.subTest(direction=direction):
                orders = [{"symbol": symbol, "side": side, "quantity": 1, "price": 10, "status": "NEW"}
                          for symbol in ["AAAUSDT", "BBBUSDT"]]
                self.assertIn("same_direction_positions", failed_keys(gate_for(
                    direction=direction, snapshot={"positions": [], "open_orders": orders},
                )))

    def test_position_and_remaining_entry_share_slot_but_sum_exposure(self):
        snapshot = {
            "positions": [{"symbol": "AAAUSDT", "direction": "LONG", "quantity": 0.2, "mark_price": 100}],
            "open_orders": [{"symbol": "AAAUSDT", "side": "BUY", "quantity": 0.3,
                             "executed_quantity": 0.2, "price": 100, "status": "PARTIALLY_FILLED"}],
        }
        self.assertTrue(gate_for(snapshot=snapshot, policy=exposure_policy(40))["passed"])
        failed = failed_keys(gate_for(snapshot=snapshot, policy=exposure_policy(39)))
        self.assertEqual(failed, {"direction_exposure"})

    def test_snapshot_and_reservation_do_not_double_count(self):
        snapshot = {
            "positions": [{"symbol": "AAAUSDT", "direction": "LONG", "quantity": 0.1, "mark_price": 100}],
            "open_orders": [{"symbol": "AAAUSDT", "side": "BUY", "quantity": 0.3,
                             "executed_quantity": 0.1, "price": 100}],
        }
        reservation = [{"symbol": "AAAUSDT", "direction": "LONG", "notional_usdt": 30}]
        self.assertTrue(gate_for(snapshot=snapshot, policy=exposure_policy(40), cycle_candidates=reservation)["passed"])
        self.assertIn("direction_exposure", failed_keys(gate_for(
            snapshot=snapshot, policy=exposure_policy(39), cycle_candidates=reservation,
        )))

    def test_stale_snapshot_keeps_larger_reservation_exposure(self):
        snapshot = {"positions": [{"symbol": "AAAUSDT", "direction": "LONG", "notional_usdt": 5}], "open_orders": []}
        reservations = [{"symbol": "AAAUSDT", "direction": "LONG", "notional_usdt": 20}]
        self.assertIn("direction_exposure", failed_keys(gate_for(
            snapshot=snapshot, policy=exposure_policy(29), cycle_candidates=reservations,
        )))

    def test_snapshot_larger_than_reservation_is_not_under_counted(self):
        snapshot = {"positions": [{"symbol": "AAAUSDT", "direction": "LONG", "notional_usdt": 30}], "open_orders": []}
        reservations = [{"symbol": "AAAUSDT", "direction": "LONG", "notional_usdt": 10}]
        self.assertIn("direction_exposure", failed_keys(gate_for(
            snapshot=snapshot, policy=exposure_policy(39), cycle_candidates=reservations,
        )))

    def test_closing_and_terminal_orders_do_not_consume_entry_capacity(self):
        for flag in ["reduce_only", "reduceOnly", "close_position", "closePosition"]:
            for value in [True, "true"]:
                with self.subTest(flag=flag, value=value):
                    orders = [{"symbol": symbol, "side": "SELL", flag: value, "quantity": 1, "price": 100}
                              for symbol in ["AAAUSDT", "BBBUSDT"]]
                    self.assertTrue(gate_for(direction="SHORT", snapshot={"positions": [], "open_orders": orders},
                                             policy=exposure_policy(10))["passed"])
        for status in ["FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"]:
            with self.subTest(status=status):
                orders = [{"symbol": symbol, "side": "BUY", "quantity": 1, "price": 100, "status": status}
                          for symbol in ["AAAUSDT", "BBBUSDT"]]
                self.assertTrue(gate_for(snapshot={"positions": [], "open_orders": orders},
                                         policy=exposure_policy(10))["passed"])

    def test_false_closing_flags_still_count_entry_orders(self):
        orders = [{"symbol": symbol, "side": "BUY", "reduceOnly": "false", "closePosition": False}
                  for symbol in ["AAAUSDT", "BBBUSDT"]]
        self.assertIn("same_direction_positions", failed_keys(gate_for(
            snapshot={"positions": [], "open_orders": orders},
        )))

    def test_fully_executed_pending_rows_and_zero_positions_do_not_count(self):
        snapshot = {
            "positions": [{"symbol": "AAAUSDT", "direction": "LONG", "quantity": 0}],
            "open_orders": [{"symbol": "BBBUSDT", "side": "BUY", "quantity": 1, "executed_quantity": 1}],
        }
        self.assertTrue(gate_for(snapshot=snapshot, policy=exposure_policy(10))["passed"])

    def test_market_pending_uses_plan_entry_price_and_remaining_quantity(self):
        orders = [{"symbol": "AAAUSDT", "side": "BUY", "origQty": "0.3", "executedQty": "0.2", "price": "0"}]
        plans = [{"symbol": "AAAUSDT", "direction": "LONG", "entry_price": "100", "status": "OPEN"}]
        self.assertTrue(gate_for(snapshot={"positions": [], "open_orders": orders}, active_plans=plans,
                                 policy=exposure_policy(20))["passed"])
        self.assertIn("direction_exposure", failed_keys(gate_for(
            snapshot={"positions": [], "open_orders": orders}, active_plans=plans, policy=exposure_policy(19),
        )))

    def test_enabled_exposure_limit_fails_closed_for_unknown_or_invalid_values(self):
        for row in [
            {"symbol": "AAAUSDT", "direction": "LONG"},
            {"symbol": "AAAUSDT", "direction": "LONG", "quantity": 1},
            {"symbol": "AAAUSDT", "direction": "LONG", "quantity": 1, "mark_price": float("nan")},
            {"symbol": "AAAUSDT", "direction": "LONG", "quantity": 1, "notional_usdt": 0},
        ]:
            with self.subTest(row=row):
                snapshot = {"positions": [row], "open_orders": []}
                result = gate_for(snapshot=snapshot, policy=exposure_policy(100))
                self.assertIn("direction_exposure", failed_keys(result))
                self.assertIn("doğrulanamadı", result["reason"])
                self.assertTrue(gate_for(snapshot=snapshot)["passed"])
        orders = [{"symbol": "AAAUSDT", "side": "BUY", "quantity": 0.3, "price": 100,
                   "executed_quantity": "invalid"}]
        self.assertIn("direction_exposure", failed_keys(gate_for(
            snapshot={"positions": [], "open_orders": orders}, policy=exposure_policy(100),
        )))

    def test_unknown_direction_is_conservatively_counted(self):
        snapshot = {"positions": [{"symbol": symbol} for symbol in ["AAAUSDT", "BBBUSDT"]], "open_orders": []}
        for direction in ["LONG", "SHORT"]:
            with self.subTest(direction=direction):
                self.assertIn("same_direction_positions", failed_keys(gate_for(direction=direction, snapshot=snapshot)))

    def test_same_exposure_cap_is_applied_separately_to_both_directions(self):
        snapshot = {"positions": [
            {"symbol": "AAAUSDT", "direction": "LONG", "notional_usdt": 20},
            {"symbol": "BBBUSDT", "direction": "SHORT", "notional_usdt": 30},
        ], "open_orders": []}
        self.assertTrue(gate_for(direction="LONG", snapshot=snapshot, policy=exposure_policy(30))["passed"])
        self.assertIn("direction_exposure", failed_keys(gate_for(
            direction="SHORT", snapshot=snapshot, policy=exposure_policy(30),
        )))

    def test_policy_defaults_digest_and_validation(self):
        defaults = sanitize_execution_policy({})
        self.assertEqual(defaults["max_same_direction_positions"], 2)
        self.assertIsNone(defaults["max_direction_exposure_usdt"])
        self.assertNotEqual(policy_digest(defaults), policy_digest({**defaults, "max_same_direction_positions": 1}))
        self.assertNotEqual(policy_digest(defaults), policy_digest({**defaults, "max_direction_exposure_usdt": 20}))
        for invalid in [0, -1, 351, float("nan"), float("inf"), "bad"]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                sanitize_execution_policy({"max_direction_exposure_usdt": invalid})

    def test_custom_count_limit_is_applied(self):
        reservations = [
            {"symbol": "AAAUSDT", "direction": "LONG", "notional_usdt": 10},
            {"symbol": "BBBUSDT", "direction": "LONG", "notional_usdt": 10},
        ]
        policy = sanitize_execution_policy({"max_same_direction_positions": 3})
        self.assertTrue(gate_for(policy=policy, allowed_symbols=["CCCUSDT"], cycle_candidates=reservations)["passed"])
        policy["max_same_direction_positions"] = 1
        self.assertIn("same_direction_positions", failed_keys(gate_for(
            policy=policy, allowed_symbols=["CCCUSDT"], cycle_candidates=reservations[:1],
        )))

    def test_raw_signed_position_amount_is_counted_in_correct_direction(self):
        snapshot = {"positions": [{"symbol": "AAAUSDT", "positionAmt": "-0.2", "mark_price": 100}], "open_orders": []}
        self.assertTrue(gate_for(direction="LONG", snapshot=snapshot, policy=exposure_policy(10))["passed"])
        self.assertIn("direction_exposure", failed_keys(gate_for(
            direction="SHORT", snapshot=snapshot, policy=exposure_policy(29),
        )))


class PolicyChangingClient(execution.BinanceLiveClient):
    def __init__(self, state: dict[str, Any], update: dict[str, Any]) -> None:
        self.state = state
        self.update = update
        self.calls: list[tuple[str, str]] = []

    async def signed(self, method, path, params=None):
        self.calls.append((method, path))
        if method == "GET" and path == "/fapi/v1/order":
            self.state["policy"].update(self.update)
            return {}
        raise AssertionError(f"Unexpected mock request: {method} {path}")


class DirectionalLiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        persistence = patch.object(execution, "persist_state")
        persistence.start()
        self.addCleanup(persistence.stop)

    def cycle(self, directions, *, snapshot=SNAPSHOT, cap=None, submit_effect=None):
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        application, state = application_for(symbols)
        state["policy"]["max_direction_exposure_usdt"] = cap
        state["policy"]["max_leverage"] = 1
        state["policy"]["max_margin_per_trade"] = 10
        decisions = [{
            "decision": "BUY" if direction == "LONG" else "SELL", "entry_eligible": True,
            "analysis": {"direction": direction, "confidence": 95, "radar": {"trap_score": 0},
                         "entry": 100, "stop_loss": 98 if direction == "LONG" else 102,
                         "tp1": 102 if direction == "LONG" else 98,
                         "tp2": 104 if direction == "LONG" else 96,
                         "tp3": 106 if direction == "LONG" else 94},
        } for direction in directions]
        submit = AsyncMock(side_effect=submit_effect) if submit_effect else AsyncMock(return_value={})
        with patch.multiple(
            execution,
            readiness_for=lambda *args, **kwargs: {"ready": True},
            consent_status=lambda *args, **kwargs: {"grace_active": False},
            client_for_with_credentials=lambda *args, **kwargs: SimpleNamespace(),
            account_snapshot=AsyncMock(return_value=snapshot),
            scan_market_candidates=AsyncMock(return_value=[{"symbol": symbol} for symbol in symbols]),
            live_candles=AsyncMock(return_value=([{"close": 100}] * 220, 123)),
            canonical_live_decision=AsyncMock(side_effect=decisions),
            spread_bps=AsyncMock(return_value=1),
            execute_live_order=submit,
        ):
            asyncio.run(execution.automatic_cycle(application, credentials=CREDENTIALS))
        return application, state, submit

    def test_automatic_cycle_with_stale_empty_snapshot_submits_only_two_longs(self):
        with self.assertLogs("app.execution_core", level="INFO") as logs:
            application, state, submit = self.cycle(["LONG"] * 3)
        self.assertEqual([call.args[1].symbol for call in submit.await_args_list], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(len(submit.await_args_list[0].kwargs["cycle_candidates"]), 0)
        self.assertEqual(len(submit.await_args_list[1].kwargs["cycle_candidates"]), 1)
        self.assertEqual(state["auto"]["last_scan_stats"]["executed_symbols_count"], 2)
        self.assertIn("3 / 2", state["auto"]["last_decision"])
        self.assertEqual(state["auto"]["last_scan_stats"]["rejection_reason_breakdown"]["entry_ineligible"]["SAME_DIRECTION_POSITIONS"], 1)
        self.assertTrue(any(row["kind"] == "LIVE_DIRECTIONAL_ENTRY_BLOCKED" for row in state["events"]))
        self.assertIn("DIRECTIONAL_ENTRY_BLOCKED", "\n".join(logs.output))
        self.assertFalse(any(row["kind"] == "AUTO_ERROR" for row in state["events"]))
        with patch.object(execution, "readiness", return_value={"ready": True}), \
                patch.object(execution, "consent_status", return_value={"fingerprint": None}):
            status = execution.public_status(application)
        self.assertEqual(status["policy"]["max_same_direction_positions"], 2)
        self.assertEqual(status["auto"]["last_decision"], state["auto"]["last_decision"])

    def test_automatic_cycle_accepts_third_candidate_in_opposite_direction(self):
        _, state, submit = self.cycle(["LONG", "LONG", "SHORT"])
        self.assertEqual(submit.await_count, 3)
        self.assertEqual(state["auto"]["last_scan_stats"]["executed_symbols_count"], 3)

    def test_automatic_cycle_uses_sized_candidate_for_exposure_gate(self):
        _, state, submit = self.cycle(["LONG"] * 3, cap=15)
        self.assertEqual(submit.await_count, 1)
        self.assertIn("maruziyet sınırı", state["auto"]["last_decision"])

    def test_partial_fill_reservation_uses_actual_filled_notional(self):
        async def submit(*args, **kwargs):
            return {"plan": {"quantity": "0.05", "entry_price": "100"}}

        _, _, submitted = self.cycle(["LONG", "LONG", "SHORT"], cap=15, submit_effect=submit)
        self.assertEqual(submitted.await_count, 3)
        self.assertEqual(submitted.await_args_list[1].kwargs["cycle_candidates"][0]["notional_usdt"], 5)

    def test_failed_submission_does_not_reserve_directional_capacity(self):
        calls = 0

        async def submit(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise execution.LiveExchangeError("Mock known rejection", http_status=409)
            return {}

        _, state, submitted = self.cycle(["LONG"] * 3, submit_effect=submit)
        self.assertEqual(submitted.await_count, 3)
        self.assertEqual(state["auto"]["last_scan_stats"]["executed_symbols_count"], 2)

    def test_final_directional_rejection_is_visible_not_generic_cycle_crash(self):
        async def submit(*args, **kwargs):
            raise execution.HTTPException(409, "Canlı yön risk kapısı: LONG aynı yön pozisyon sınırı: 3 / 2")

        _, state, _ = self.cycle(["LONG"] * 3, submit_effect=submit)
        self.assertEqual(state["auto"]["last_cycle_stage"], "completed")
        self.assertIn("3 / 2", state["auto"]["last_decision"])
        self.assertFalse(any(row["kind"] == "AUTO_ERROR" for row in state["events"]))

    def test_execute_enforces_same_round_reservations_before_building_or_submitting(self):
        application, state = application_for(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        reservations = [{"symbol": symbol, "direction": "LONG", "notional_usdt": 10}
                        for symbol in ["ETHUSDT", "SOLUSDT"]]
        with patch.multiple(
            execution,
            readiness_for=lambda *args, **kwargs: {"ready": True},
            client_for_with_credentials=lambda *args, **kwargs: SimpleNamespace(),
            account_snapshot=AsyncMock(return_value=SNAPSHOT),
            spread_bps=AsyncMock(return_value=1),
        ), patch.object(execution, "build_live_spec", new=AsyncMock()) as build, \
                patch.object(execution, "submit_entry", new=AsyncMock()) as submit:
            with self.assertRaises(execution.HTTPException) as caught:
                asyncio.run(execution.execute_live_order(
                    application, order_for("BTCUSDT"), source="V25_AUTO",
                    credentials=CREDENTIALS, cycle_candidates=reservations,
                ))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("3 / 2", caught.exception.detail)
        build.assert_not_awaited()
        submit.assert_not_awaited()
        self.assertTrue(any(row["kind"] == "LIVE_ORDER_BLOCKED" for row in state["events"]))

    def test_policy_updates_preserve_owner_guard_and_explicit_null_clears_cap(self):
        application, state = application_for(["BTCUSDT"])
        request = SimpleNamespace(app=application)
        with patch.object(execution, "execution_owner", return_value={"id": "offline-owner"}) as owner, \
                patch.object(execution, "public_status", side_effect=lambda app, req: {"policy": app.state.v25_execution["policy"]}):
            asyncio.run(execution.v25_policy(request, execution.PolicyUpdate(
                max_same_direction_positions=1, max_direction_exposure_usdt=30,
            )))
            self.assertEqual(state["policy"]["max_same_direction_positions"], 1)
            self.assertEqual(state["policy"]["max_direction_exposure_usdt"], 30)
            asyncio.run(execution.v25_policy(request, execution.PolicyUpdate(scan_seconds=60)))
            self.assertEqual(state["policy"]["max_direction_exposure_usdt"], 30)
            restored = execution.sanitized_state({"policy": state["policy"]})
            self.assertEqual(restored["policy"]["max_direction_exposure_usdt"], 30)
            asyncio.run(execution.v25_policy(request, execution.PolicyUpdate(max_direction_exposure_usdt=None)))
            self.assertIsNone(state["policy"]["max_direction_exposure_usdt"])
        self.assertEqual(owner.call_count, 3)
        self.assertFalse(state["auto"]["enabled"])
        self.assertFalse(state["live_auto_trade"])
        self.assertIsNone(state["policy_ack_digest"])
        before = dict(state["policy"])
        with patch.object(execution, "execution_owner", side_effect=execution.HTTPException(403, "owner required")):
            with self.assertRaises(execution.HTTPException):
                asyncio.run(execution.v25_policy(request, execution.PolicyUpdate(max_same_direction_positions=5)))
        self.assertEqual(state["policy"], before)

    def test_api_rejects_invalid_limits(self):
        from pydantic import ValidationError

        for value in [0, -1, 6]:
            with self.subTest(count=value), self.assertRaises(ValidationError):
                execution.PolicyUpdate(max_same_direction_positions=value)
        for value in [0, -1, 351, float("inf"), float("nan")]:
            with self.subTest(cap=value), self.assertRaises(ValidationError):
                execution.PolicyUpdate(max_direction_exposure_usdt=value)

    def test_policy_tightening_during_entry_lookup_cannot_reach_post(self):
        for update, reservations, expected in [
            ({"max_same_direction_positions": 1},
             [{"symbol": "ETHUSDT", "direction": "LONG", "notional_usdt": 10}], "2 / 1"),
            ({"max_direction_exposure_usdt": 4}, [], "5.00 / 4.00"),
        ]:
            with self.subTest(update=update):
                application, state = application_for(["BTCUSDT", "ETHUSDT"])
                client = PolicyChangingClient(state, update)
                with patch.multiple(
                    execution,
                    readiness_for=lambda *args, **kwargs: {"ready": True},
                    client_for_with_credentials=lambda *args, **kwargs: client,
                    account_snapshot=AsyncMock(return_value=SNAPSHOT),
                    spread_bps=AsyncMock(return_value=1),
                    build_live_spec=AsyncMock(return_value=spec_for("BTCUSDT")),
                    set_live_isolated_margin=AsyncMock(return_value="ISOLATED"),
                    apply_live_verified_leverage=AsyncMock(return_value={"applied_leverage": 1, "margin_type": "isolated"}),
                    fresh_auto_submission_credentials=AsyncMock(return_value=CREDENTIALS),
                    install_protection=AsyncMock(),
                ):
                    with self.assertRaises(execution.HTTPException) as caught:
                        asyncio.run(execution.execute_live_order(
                            application, order_for("BTCUSDT"), source="V25_AUTO",
                            credentials=CREDENTIALS, cycle_candidates=reservations,
                        ))
                self.assertEqual(caught.exception.status_code, 409)
                self.assertIn(expected, caught.exception.detail)
                self.assertEqual(client.calls, [("GET", "/fapi/v1/order")])
                self.assertEqual(state["plans"], {})
                self.assertFalse(state.get("unknown_order_state", False))

    def test_snapshot_preserves_close_position_flag(self):
        payloads = {
            "/fapi/v3/account": {}, "/fapi/v3/positionRisk": [],
            "/fapi/v1/openOrders": [
                {"symbol": "AAAUSDT", "side": "SELL", "closePosition": True},
                {"symbol": "BBBUSDT", "side": "BUY", "closePosition": "false"},
            ],
            "/fapi/v1/openAlgoOrders": [], "/fapi/v1/positionSide/dual": {}, "/fapi/v1/symbolConfig": [],
        }

        async def snapshot_request(client, path, request_id):
            return payloads[path]

        with patch.object(binance_demo, "snapshot_request", new=AsyncMock(side_effect=snapshot_request)):
            snapshot = asyncio.run(binance_demo._account_snapshot(SimpleNamespace()))
        self.assertTrue(snapshot["open_orders"][0]["close_position"])
        self.assertFalse(snapshot["open_orders"][1]["close_position"])


if __name__ == "__main__":
    unittest.main()
