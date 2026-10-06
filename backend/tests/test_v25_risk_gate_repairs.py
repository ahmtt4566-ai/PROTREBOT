import asyncio
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).parents[1]))

from app import binance_demo as demo, execution_core as core, v25_execution as live  # noqa: E402
from app.v21_demo import performance_payload  # noqa: E402
from app.liquidation_risk import isolated_liquidation_risk  # noqa: E402
import test_v25_auto_symbol_scope as scope_tests  # noqa: E402
from test_v25_partial_fills import PartialFillClient, make_plan  # noqa: E402


def snapshot(**overrides):
    return {
        "wallet_balance": 100, "available_balance": 100,
        "positions": [], "open_orders": [], "hedge_mode": False,
        "multi_assets_mode": False, **overrides,
    }


def entry_gate(positions, *, candidate=100):
    return core.evaluate_entry_gates(
        symbol="ETHUSDT",
        signal={"direction": "LONG", "confidence": 95, "radar": {"trap_score": 0}},
        snapshot=snapshot(positions=positions),
        policy=core.sanitize_execution_policy({}), daily={},
        spread_bps=1, armed=True, candidate_notional_usdt=candidate,
    )


def stop_plan():
    return {
        "symbol": "BTCUSDT", "direction": "LONG", "stop_loss": "99",
        "stop_client_id": "PTBLV_SL_offline", "stop_algo_id": 17,
    }


def stop_row(**overrides):
    return {
        "symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET",
        "status": "NEW", "client_algo_id": "PTBLV_SL_offline", "algo_id": 17,
        "trigger_price": "99", "close_position": True,
        "working_type": "MARK_PRICE", **overrides,
    }


def maintenance_brackets(**overrides):
    return [{
        "symbol": "BTCUSDT",
        "brackets": [{
            "notionalFloor": 0, "notionalCap": 1000,
            "maintMarginRatio": "0.004", "cum": 0, "initialLeverage": 125,
        }],
        **overrides,
    }]


def liquidation_spec(direction="LONG", stop="99"):
    return {
        "symbol": "BTCUSDT", "direction": direction, "entry_price": "100",
        "quantity": "3", "leverage": 30, "stop_loss": stop,
    }


class RiskGateRepairTests(unittest.TestCase):
    def test_normalized_position_notional_blocks_500_plus_100(self):
        result = entry_gate([{
            "symbol": "BTCUSDT", "direction": "LONG",
            "quantity": 5, "mark_price": 100, "entry_price": 80,
        }])
        exposure = next(row for row in result["gates"] if row["key"] == "exposure")
        self.assertFalse(exposure["passed"])
        self.assertEqual(exposure["detail"], "600.00 / 350.00 USDT")

    def test_signed_quantity_and_stale_notional_use_current_mark_price(self):
        result = entry_gate([{
            "symbol": "BTCUSDT", "direction": "SHORT",
            "quantity": -5, "mark_price": 100, "notional": 1,
        }])
        exposure = next(row for row in result["gates"] if row["key"] == "exposure")
        self.assertFalse(exposure["passed"])
        self.assertEqual(exposure["detail"], "600.00 / 350.00 USDT")

    def test_exposure_unknown_is_fail_closed(self):
        for row in [
            {"quantity": 1}, {"quantity": 1, "mark_price": 0},
            {"quantity": 1, "mark_price": "NaN"},
            {"quantity": "Infinity", "mark_price": 100},
        ]:
            with self.subTest(row=row), self.assertLogs("app.execution_core", level="ERROR"):
                result = entry_gate([{"symbol": "BTCUSDT", "direction": "LONG", **row}])
                self.assertFalse(next(gate for gate in result["gates"] if gate["key"] == "exposure")["passed"])

    def test_newest_first_losses_are_the_current_streak(self):
        now = datetime(2026, 10, 6, 13, tzinfo=timezone.utc)
        rows = [{
            "kind": "LIVE_POSITION_CLOSED",
            "created_at": f"2026-10-06T{hour}:00:00+00:00",
            "realized_pnl": pnl,
        } for hour, pnl in [("12", -1), ("11", -1), ("10", -1), ("09", 1)]]
        for events in [rows, list(reversed(rows))]:
            self.assertEqual(core.daily_execution_metrics(events, now)["consecutive_losses"], 3)
        rows.insert(0, {"kind": "LIVE_POSITION_CLOSED", "created_at": now.isoformat(), "realized_pnl": 1})
        self.assertEqual(core.daily_execution_metrics(rows, now)["consecutive_losses"], 0)

    def test_exact_stop_matches_every_required_parameter(self):
        state, matched, reason = live.classify_plan_protection(stop_plan(), [stop_row()])
        self.assertEqual((state, reason), ("MATCHED", "EXACT_IDENTITY"))
        self.assertEqual(matched, [stop_row()])

    def test_exact_identity_with_wrong_stop_parameters_is_unknown(self):
        cases = [
            {"trigger_price": "90", "close_position": False},
            {"trigger_price": "NaN"}, {"close_position": False},
            {"side": "BUY"}, {"type": "TAKE_PROFIT_MARKET"},
            {"working_type": "CONTRACT_PRICE"}, {"working_type": None},
            {"symbol": "ETHUSDT"}, {"status": "CANCELED"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                status, _, reason = live.classify_plan_protection(stop_plan(), [stop_row(**overrides)])
                self.assertEqual(status, "UNKNOWN")
                self.assertEqual(reason, "EXACT_PARAMETERS_MISMATCH")

    def test_short_only_start_uses_an_allowed_direction(self):
        state = live.initial_state()
        state.update({
            "real_trading_locked": False, "recovery_ready": True,
            "armed_until": time.time() + 300, "snapshot": snapshot(),
        })
        state["policy"].update({"allow_long": False, "allow_short": True})
        with patch.object(live, "readiness", return_value={"ready": True}):
            allowed, reason = live.live_auto_start_gate(SimpleNamespace(), state)
        self.assertTrue(allowed, reason)

    def test_live_journal_keeps_direction_and_live_performance_label(self):
        now = datetime.now(timezone.utc).isoformat()
        plans = {
            direction: {
                "id": direction, "symbol": "BTCUSDT", "direction": direction,
                "status": "KAPANDI", "pnl_verified": True, "closed_at": now,
                "realized_pnl": pnl, "initial_risk_usdt": 2,
            } for direction, pnl in [("LONG", 4), ("SHORT", -2)]
        }
        journal = live.verified_live_journal({"plans": plans})
        self.assertEqual([row["direction"] for row in journal], ["LONG", "SHORT"])
        result = performance_payload({"journal": journal}, demo_only=False)
        self.assertFalse(result["demo_only"])
        self.assertEqual(result["directional"]["LONG"]["trades"], 1)
        self.assertEqual(result["directional"]["SHORT"]["trades"], 1)
        self.assertEqual(result["avg_r"], 0.5)
        self.assertTrue(performance_payload({"journal": journal})["demo_only"])
        application = SimpleNamespace(state=SimpleNamespace(v25_execution={**live.initial_state(), "plans": plans}))
        with patch.object(live, "consent_status", return_value={}), \
                patch.object(live, "readiness", return_value={"ready": False}):
            status = live.public_status(application)
        for key in ["performance", "daily_performance"]:
            self.assertFalse(status[key]["demo_only"])
            self.assertEqual(status[key]["directional"]["LONG"]["trades"], 1)
            self.assertEqual(status[key]["directional"]["SHORT"]["trades"], 1)

    def test_policy_api_accepts_the_shared_exposure_ceiling(self):
        parsed = live.PolicyUpdate(max_total_exposure_usdt=core.HARD_MAX_TOTAL_EXPOSURE_USDT)
        self.assertEqual(parsed.max_total_exposure_usdt, 350)
        self.assertEqual(live.initial_state()["policy"]["max_total_exposure_usdt"], 350)

    def test_demo_certificate_requirement_is_configurable_with_legacy_default(self):
        application = SimpleNamespace(state=SimpleNamespace(v21_demo={"offline": True}))
        with patch.object(live, "certificate_payload", return_value={"status": "KANIT TOPLUYOR"}):
            with patch.dict(os.environ, {}, clear=True):
                self.assertTrue(live.demo_certificate(application)["live_allowed"])
            with patch.dict(os.environ, {"PROTREBOT_LIVE_REQUIRE_DEMO_CERTIFICATE": "true"}):
                certificate = live.demo_certificate(application)
                self.assertFalse(certificate["live_allowed"])
                gates = core.release_gates(
                    credentials=True, consent_active=True, connected=True,
                    one_way=True, policy_acknowledged=True, demo_certificate=certificate,
                )
                self.assertFalse(core.release_ready(gates))
                self.assertFalse(next(gate for gate in gates if gate["key"] == "demo_certificate")["passed"])
        with patch.object(live, "certificate_payload", return_value={"status": "DEMO SERTİFİKALI"}), \
                patch.dict(os.environ, {"PROTREBOT_LIVE_REQUIRE_DEMO_CERTIFICATE": "true"}):
            self.assertTrue(live.demo_certificate(application)["live_allowed"])

    def test_cross_margin_rejection_does_not_fall_back(self):
        client = SimpleNamespace(signed=AsyncMock(side_effect=live.LiveExchangeError("cross-only", exchange_code=-4168)))
        with self.assertRaises(live.LiveExchangeError):
            asyncio.run(live.set_live_isolated_margin(client, "BTCUSDT"))
        client.signed.assert_awaited_once()


class LiquidationGateTests(unittest.TestCase):
    def test_long_and_short_stop_before_conservative_liquidation(self):
        policy = core.sanitize_execution_policy({})
        for direction, stop in [("LONG", "99"), ("SHORT", "101")]:
            with self.subTest(direction=direction):
                audit = isolated_liquidation_risk(liquidation_spec(direction, stop), maintenance_brackets(), policy)
                self.assertTrue(audit["verified"])
                self.assertEqual(audit["buffer_pct"], 0.5)
                self.assertGreaterEqual(float(audit["available_buffer"]), 0.5)
                self.assertFalse(audit["funding_included"])

    def test_stop_at_or_beyond_liquidation_and_inside_buffer_is_rejected(self):
        policy = core.sanitize_execution_policy({})
        for direction, stop in [("LONG", "97"), ("LONG", "97.5"), ("SHORT", "103"), ("SHORT", "102.5")]:
            with self.subTest(direction=direction, stop=stop), self.assertRaisesRegex(ValueError, "required buffer"):
                isolated_liquidation_risk(liquidation_spec(direction, stop), maintenance_brackets(), policy)

    def test_exact_buffer_boundary_and_policy_adjustment(self):
        policy = core.sanitize_execution_policy({"fee_bps_per_side": 0})
        spec = {**liquidation_spec(), "quantity": "1", "leverage": 2, "stop_loss": "50.5"}
        payload = maintenance_brackets()
        payload[0]["brackets"][0]["maintMarginRatio"] = 0
        audit = isolated_liquidation_risk(spec, payload, policy)
        self.assertEqual(float(audit["liquidation_price_estimate"]), 50)
        self.assertEqual(float(audit["available_buffer"]), 0.5)
        with self.assertRaises(ValueError):
            isolated_liquidation_risk(spec, payload, {**policy, "liquidation_buffer_pct": 0.6})

    def test_liquidation_uses_tier_at_liquidation_not_only_entry_tier(self):
        payload = maintenance_brackets(brackets=[
            {"notionalFloor": 0, "notionalCap": 75, "maintMarginRatio": "0.004", "cum": 0, "initialLeverage": 125},
            {"notionalFloor": 75, "notionalCap": 1000, "maintMarginRatio": "0.01", "cum": "0.45", "initialLeverage": 125},
        ])
        spec = {**liquidation_spec(), "quantity": "1", "leverage": 2, "stop_loss": "90"}
        audit = isolated_liquidation_risk(spec, payload, core.sanitize_execution_policy({}))
        self.assertEqual(audit["maintenance_rate"], "0.004")
        self.assertEqual(audit["maintenance_deduction"], "0")

    def test_missing_invalid_or_custom_brackets_fail_closed(self):
        policies = core.sanitize_execution_policy({})
        cases = [
            [], {}, maintenance_brackets(symbol="ETHUSDT"), maintenance_brackets(brackets=[]),
            maintenance_brackets(notionalCoef=1.5),
            maintenance_brackets(brackets=[{"notionalFloor": 0}]),
            maintenance_brackets(brackets=[{
                "notionalFloor": 0, "notionalCap": 1000, "maintMarginRatio": "NaN", "cum": 0, "initialLeverage": 125,
            }]),
            maintenance_brackets(brackets=[{
                "notionalFloor": 0, "notionalCap": 1000, "maintMarginRatio": "-0.0001", "cum": 0, "initialLeverage": 125,
            }]),
            maintenance_brackets(brackets=[{
                "notionalFloor": 0, "notionalCap": 1000, "maintMarginRatio": "0.004", "cum": 1, "initialLeverage": 125,
            }]),
            maintenance_brackets(brackets=[
                {"notionalFloor": 0, "notionalCap": 500, "maintMarginRatio": "0.004", "cum": 0, "initialLeverage": 125},
                {"notionalFloor": 400, "notionalCap": 1000, "maintMarginRatio": "0.004", "cum": 0, "initialLeverage": 125},
            ]),
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                isolated_liquidation_risk(liquidation_spec(), payload, policies)

    def test_invalid_inputs_do_not_produce_an_estimate(self):
        policy = core.sanitize_execution_policy({})
        for override in [
            {"quantity": 0}, {"quantity": True}, {"entry_price": "Infinity"},
            {"direction": "BEKLE"}, {"leverage": 126}, {"leverage": 0.5},
            {"leverage": 1.5}, {"stop_loss": "101"},
        ]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                isolated_liquidation_risk({**liquidation_spec(), **override}, maintenance_brackets(), policy)

    def execute(self, *, multi_assets=False, stop="99.2", margin_type="isolated", payload=None):
        application, state = scope_tests.application_for(["BTCUSDT"])
        state["policy"]["max_leverage"] = 50
        spec = {
            **scope_tests.spec_for("BTCUSDT"),
            "quantity": "2.5", "notional_usdt": 250, "margin_usdt": 5,
            "leverage": 50, "stop_loss": stop,
            "estimated_stop_loss_usdt": 2,
        }
        client = SimpleNamespace(signed=AsyncMock(return_value=payload if payload is not None else maintenance_brackets()))
        submit = AsyncMock(return_value={"orderId": 42, "status": "FILLED"})
        body = live.LiveOrderRequest(
            symbol="BTCUSDT", direction="LONG", margin_usdt=5, leverage=50,
            stop_loss=float(stop), tp1=102, tp2=104, tp3=106,
        )
        with patch.multiple(
            live,
            readiness_for=lambda *args, **kwargs: {"ready": True},
            client_for_with_credentials=lambda *args, **kwargs: client,
            account_snapshot=AsyncMock(return_value=snapshot(multi_assets_mode=multi_assets)),
            spread_bps=AsyncMock(return_value=1),
            build_live_spec=AsyncMock(return_value=spec),
            set_live_isolated_margin=AsyncMock(return_value="ISOLATED"),
            apply_live_verified_leverage=AsyncMock(return_value={"applied_leverage": 50, "margin_type": margin_type}),
            fresh_auto_submission_credentials=AsyncMock(return_value=scope_tests.CREDENTIALS),
            submit_entry=submit,
            install_protection=AsyncMock(),
            persist_state=lambda *args: None,
        ):
            try:
                result = asyncio.run(live.execute_live_order(
                    application, body, source="V25_AUTO", credentials=scope_tests.CREDENTIALS,
                ))
            except live.HTTPException as exc:
                return exc, submit, client, state
        return result, submit, client, state

    def test_entry_with_unsafe_liquidation_buffer_never_reaches_submit(self):
        result, submit, client, state = self.execute(stop="98.8")
        self.assertIsInstance(result, live.HTTPException)
        self.assertEqual(result.status_code, 409)
        submit.assert_not_awaited()
        self.assertEqual(state["plans"], {})
        client.signed.assert_awaited_once_with("GET", "/fapi/v1/leverageBracket", {"symbol": "BTCUSDT"})

    def test_safe_entry_records_liquidation_proof_without_rewriting_initial_risk(self):
        result, submit, client, state = self.execute()
        self.assertTrue(result["ok"])
        submit.assert_awaited_once()
        self.assertTrue(result["plan"]["liquidation_guard"]["verified"])
        self.assertEqual(result["plan"]["initial_risk_usdt"], 2)
        intent = next(iter(state["intents"].values()))
        self.assertEqual(intent["spec"]["initial_risk_usdt"], 2)
        self.assertEqual(intent["spec"]["liquidation_guard"], result["plan"]["liquidation_guard"])
        restored = live.recover_plan_from_intent(next(iter(state["intents"])), intent, {"orderId": 42, "status": "FILLED"})
        self.assertEqual(restored["liquidation_guard"], result["plan"]["liquidation_guard"])
        client.signed.assert_awaited_once()

    def test_multi_assets_unknown_mode_and_crossed_configuration_never_submit(self):
        for overrides in [{"multi_assets": True}, {"multi_assets": None}, {"margin_type": "crossed"}]:
            with self.subTest(overrides=overrides):
                result, submit, client, state = self.execute(**overrides)
                self.assertIsInstance(result, live.HTTPException)
                self.assertEqual(result.status_code, 409)
                submit.assert_not_awaited()
                client.signed.assert_not_awaited()
                self.assertEqual(state["intents"], {})

    def test_unproven_brackets_never_submit(self):
        result, submit, _, state = self.execute(payload=[])
        self.assertIsInstance(result, live.HTTPException)
        submit.assert_not_awaited()
        self.assertEqual(state["plans"], {})

    def test_isolated_policy_cannot_be_disabled_for_live_entry(self):
        with self.assertRaises(live.LiveExchangeError):
            live.validate_live_isolated_snapshot(snapshot(), {"require_isolated": False})


class ProtectionStatusRepairTests(unittest.TestCase):
    def setUp(self):
        persistence = patch.object(live, "persist_state")
        persistence.start()
        self.addCleanup(persistence.stop)

    def test_below_minimum_partial_tp_is_explicitly_unprotected(self):
        plan = make_plan()
        client = PartialFillClient(plan, quantity="0.001")
        state = live.initial_state()
        asyncio.run(live.install_protection(client, state, plan))
        self.assertEqual(plan["tp_protection_state"], "UNPROTECTED")
        self.assertIn("TP KORUMASIZ", plan["status"])
        self.assertEqual(live.live_plan_execution_outcome(plan)[0], "TP_UNPROTECTED")
        self.assertTrue(plan["stop_algo_id"])
        self.assertTrue(any("TP KORUMASIZ" in row["message"] for row in state["events"]))

    def test_tp3_missing_acceptance_id_is_not_reported_as_protected(self):
        plan = make_plan(entry_order_status="FILLED")
        client = PartialFillClient(plan, quantity="0.010")
        real_signed = client.signed

        async def signed(method, path, params=None):
            if method == "POST" and path == "/fapi/v1/algoOrder" and (params or {}).get("clientAlgoId") == live.client_id_for("TP3", plan["intent_id"]):
                return {}
            return await real_signed(method, path, params)

        with patch.object(client, "signed", new=signed):
            asyncio.run(live.install_protection(client, live.initial_state(), plan))
        self.assertIn("TP3", plan["monitoring_targets"])
        self.assertEqual(plan["tp_protection_state"], "UNPROTECTED")

    def test_combined_tp_does_not_claim_missing_tp3_is_exchange_backed(self):
        plan = {
            "id": "offline", "symbol": "BTCUSDT", "intent_id": "offline-intent",
            "stop_algo_id": 1, "monitoring_targets": ["TP3"],
        }
        rows = [{"symbol": "BTCUSDT", "client_algo_id": live.client_id_for("TP12", plan["intent_id"])}]
        live.reconcile_monitoring_targets(live.initial_state(), plan, rows)
        self.assertFalse(plan["monitoring_targets_exchange_backed"])
        self.assertEqual(plan["tp_protection_state"], "UNPROTECTED")
        self.assertIn("TP KORUMASIZ", plan["status"])

    def test_plan_outcomes_never_claim_failed_or_pending_protection_succeeded(self):
        cases = [
            ({"status": "GÜVENLİK İÇİN KAPATILDI", "stop_install_failed": True}, "SAFETY_CLOSE_REQUESTED"),
            ({"stop_install_failed": True, "safety_close_status": "NO_POSITION"}, "SAFETY_CLOSE_NO_POSITION"),
            ({"status": "DOLUM BEKLİYOR"}, "PROTECTION_PENDING"),
            ({"protection_state": "UNKNOWN", "provenance_state": "BROKEN"}, "PROTECTION_UNKNOWN"),
            ({"stop_algo_id": 1, "stop_verified_at": "offline", "tp_protection_state": "UNPROTECTED"}, "TP_UNPROTECTED"),
            ({"stop_algo_id": 1, "stop_verified_at": "offline", "tp_protection_state": "EXCHANGE_BACKED"}, "PROTECTED_ENTRY"),
        ]
        for plan, expected in cases:
            with self.subTest(expected=expected):
                outcome, message = live.live_plan_execution_outcome({"symbol": "BTCUSDT", **plan})
                self.assertEqual(outcome, expected)
                if expected != "PROTECTED_ENTRY":
                    self.assertNotIn("TP emirleri borsada kuruldu", message)

    def test_tp_monitoring_requires_active_exact_price_and_full_close(self):
        plan = make_plan(entry_order_status="FILLED")
        plan.update({"stop_algo_id": 101, "monitoring_targets": ["TP3"]})
        row = {
            "symbol": "BTCUSDT", "side": "SELL", "type": "TAKE_PROFIT_MARKET",
            "status": "NEW", "algo_id": 104, "close_position": True,
            "working_type": "MARK_PRICE", "trigger_price": "53000",
            "client_algo_id": live.client_id_for("TP3", plan["intent_id"]),
        }
        live.reconcile_monitoring_targets(live.initial_state(), plan, [row])
        self.assertTrue(plan["monitoring_targets_exchange_backed"])
        for overrides in [
            {"status": "CANCELED"}, {"trigger_price": "52000"},
            {"close_position": False}, {"side": "BUY"}, {"working_type": "CONTRACT_PRICE"},
        ]:
            with self.subTest(overrides=overrides):
                live.reconcile_monitoring_targets(live.initial_state(), plan, [{**row, **overrides}])
                self.assertFalse(plan["monitoring_targets_exchange_backed"])
                self.assertEqual(plan["tp_protection_state"], "UNPROTECTED")

    def test_wrong_periodic_stop_locks_without_mutating_exchange(self):
        state = live.initial_state()
        state.update({"real_trading_locked": False, "live_auto_trade": True})
        state["auto"].update({"enabled": True, "session_until": time.time() + 300})
        plan = {**stop_plan(), "id": "offline"}
        live.record_protection_ownership_uncertain(state, plan, "EXACT_PARAMETERS_MISMATCH")
        self.assertTrue(state["real_trading_locked"])
        self.assertTrue(state["reconciliation_required"])
        self.assertFalse(state["auto"]["enabled"])
        self.assertEqual(state["execution_state"], "UNKNOWN")

    def test_combined_tp_requires_reduce_only_and_exact_rounded_quantity(self):
        plan = make_plan(entry_order_status="FILLED")
        plan.update({"stop_algo_id": 101, "monitoring_targets": ["TP1", "TP2"]})
        row = {
            "symbol": "BTCUSDT", "side": "SELL", "type": "TAKE_PROFIT_MARKET",
            "status": "NEW", "algo_id": 103, "close_position": False,
            "working_type": "MARK_PRICE", "trigger_price": "51000",
            "quantity": "0.006", "reduce_only": True,
            "client_algo_id": live.client_id_for("TP12", plan["intent_id"]),
        }
        live.reconcile_monitoring_targets(live.initial_state(), plan, [row])
        self.assertTrue(plan["monitoring_targets_exchange_backed"])
        for overrides in [{"reduce_only": False}, {"quantity": "0.010"}]:
            with self.subTest(overrides=overrides):
                live.reconcile_monitoring_targets(live.initial_state(), plan, [{**row, **overrides}])
                self.assertFalse(plan["monitoring_targets_exchange_backed"])


class SnapshotNormalizationRepairTests(unittest.TestCase):
    def client(self, account, mode):
        payloads = {
            "/fapi/v3/account": account,
            "/fapi/v3/positionRisk": [{
                "symbol": "BTCUSDT", "positionAmt": "-5", "entryPrice": "80",
                "markPrice": "100", "leverage": "30", "marginType": "isolated",
            }],
            "/fapi/v1/openOrders": [],
            "/fapi/v1/openAlgoOrders": [{
                "symbol": "BTCUSDT", "algoId": 17, "clientAlgoId": "PTBLV_SL_offline",
                "side": "BUY", "orderType": "STOP_MARKET", "algoStatus": "NEW",
                "type": "STOP_MARKET", "triggerPrice": "101", "workingType": "MARK_PRICE",
                "closePosition": True,
            }],
            "/fapi/v1/positionSide/dual": {"dualSidePosition": False},
            "/fapi/v1/symbolConfig": [],
            "/fapi/v1/multiAssetsMargin": mode,
        }

        async def signed(method, path, params=None):
            self.assertEqual(method, "GET")
            self.assertIn(path, payloads)
            return payloads[path]

        return SimpleNamespace(signed=AsyncMock(side_effect=signed))

    def test_shared_snapshot_keeps_notional_working_type_and_explicit_asset_mode(self):
        for raw, expected in [("false", False), (False, False), ("true", True), (True, True)]:
            with self.subTest(raw=raw):
                client = self.client({"multiAssetsMargin": raw}, {})
                result = asyncio.run(demo._account_snapshot(client))
                self.assertEqual(result["positions"][0]["notional"], 500)
                self.assertEqual(result["positions"][0]["mark_price"], 100)
                self.assertEqual(result["open_algo_orders"][0]["working_type"], "MARK_PRICE")
                self.assertIs(result["multi_assets_mode"], expected)
                self.assertNotIn("/fapi/v1/multiAssetsMargin", [call.args[1] for call in client.signed.await_args_list])

    def test_missing_mode_is_not_normalized_to_safe_false_and_live_verifies_separately(self):
        result = asyncio.run(demo._account_snapshot(self.client({}, {})))
        self.assertIsNone(result["multi_assets_mode"])
        client = self.client({}, {"multiAssetsMargin": False})
        result = asyncio.run(live.account_snapshot(client))
        self.assertIs(result["multi_assets_mode"], False)
        self.assertEqual(client.signed.await_args_list[-1].args, ("GET", "/fapi/v1/multiAssetsMargin"))

    def test_live_unknown_asset_mode_is_an_explicit_error(self):
        with self.assertLogs("app.v25_execution", level="ERROR"), self.assertRaises(live.LiveExchangeError):
            asyncio.run(live.account_snapshot(self.client({}, {})))


if __name__ == "__main__":
    unittest.main()
