import asyncio
import httpx
import json
import sys
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.execution_core import (  # noqa: E402
    HARD_MAX_DAILY_LOSS_USDT,
    HARD_MAX_LEVERAGE,
    HARD_MAX_MARGIN_USDT,
    HARD_MAX_POSITIONS,
    HARD_MAX_TOTAL_EXPOSURE_USDT,
    credential_fingerprint,
    configured_mtf_allow_either_timeframe,
    configured_min_confidence,
    daily_execution_metrics,
    dynamic_stop_distance_pct,
    evaluate_entry_gates,
    policy_digest,
    release_gates,
    release_ready,
    risk_sized_order,
    sanitize_execution_policy,
)
from app import v25_execution  # noqa: E402
from app.binance_demo import BinanceDemoError, verify_symbol_configuration  # noqa: E402
from app.v25_execution import BinanceLiveClient, LiveExchangeError, LiveOrderRequest, classify_plan_protection, close_reason_for_client_id, close_reason_for_intent, client_id_for, confirm_live_plan_provenance, initial_state, live_auto_start_gate, lock_live_execution, owned_protection_rows, process_live_stream_event, prune_mtf_decision_history, rank_market_tickers, sanitized_state, submit_entry, summarize_mtf_relaxation, validate_protection_readiness  # noqa: E402


EXECUTION_SOURCE = (BACKEND / "app" / "v25_execution.py").read_text(encoding="utf-8")
CORE_SOURCE = (BACKEND / "app" / "execution_core.py").read_text(encoding="utf-8")
CREDENTIAL_SOURCE = (BACKEND / "app" / "credential_store.py").read_text(encoding="utf-8")
MAIN_SOURCE = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
FRONTEND_SOURCE = (ROOT / "ExecutionCenter.tsx").read_text(encoding="utf-8")
ACTIVE_COMMERCIAL_SOURCE = (ROOT / "CommercialHub.tsx").read_text(encoding="utf-8")
ACTIVE_EXECUTION_SOURCE = (ROOT / "ExecutionCenter.tsx").read_text(encoding="utf-8")
ACTIVE_API_SOURCE = (ROOT / "api.ts").read_text(encoding="utf-8")
VERCEL_SOURCE = (ROOT / "vercel.json").read_text(encoding="utf-8")
RENDER_SOURCE = (ROOT / "render.yaml").read_text(encoding="utf-8")


def complete_reconciliation_snapshot(**overrides):
    snapshot = {
        "wallet_balance": 100.0,
        "available_balance": 100.0,
        "positions": [],
        "open_orders": [],
        "open_algo_orders": [],
        "open_algo_orders_available": True,
        "algo_orders_quality": "VALID_EMPTY",
        "hedge_mode": False,
    }
    snapshot.update(overrides)
    return snapshot


class V25LiveGuardCoreTests(unittest.TestCase):
    def test_default_live_policy_raises_leverage_only(self):
        policy = initial_state()["policy"]
        self.assertEqual(policy["max_leverage"], 30)
        self.assertEqual(policy["max_loss_per_trade"], 3.0)
        self.assertEqual(policy["max_margin_per_trade"], 25.0)
        self.assertEqual(policy["max_stop_distance_pct"], 5.0)
        self.assertEqual(policy["atr_stop_multiplier"], 1.5)
        self.assertEqual(policy["max_total_exposure_usdt"], 350.0)

    def test_atr_stop_cap_narrows_and_widens_with_hard_bounds(self):
        policy = initial_state()["policy"]
        self.assertEqual(dynamic_stop_distance_pct(100, 0.5, policy), 1.0)
        self.assertEqual(dynamic_stop_distance_pct(100, 10, policy), 5.0)
        with self.assertRaisesRegex(ValueError, "izin verilen üst sınır %1.00"):
            risk_sized_order(100, 98, policy, atr=0.5)
        risk = risk_sized_order(100, 96, policy, atr=10)
        self.assertEqual(risk["max_stop_distance_pct"], 5.0)

    def test_tp_monitoring_reconciliation_survives_persisted_plan(self):
        state = initial_state()
        plan = {"id": "plan-1", "symbol": "BTCUSDT", "intent_id": "intent-123", "monitoring_targets": ["TP1", "TP2"], "monitoring_targets_exchange_backed": False}
        v25_execution.reconcile_monitoring_targets(state, plan, [])
        self.assertFalse(plan["monitoring_targets_exchange_backed"])
        self.assertTrue(plan["monitoring_targets_reconciled_at"])
        self.assertEqual(state["events"][0]["kind"], "TP_MONITORING_RECONCILED")
        restored = sanitized_state({"plans": {"plan-1": plan}})
        self.assertEqual(restored["plans"]["plan-1"]["monitoring_targets"], ["TP1", "TP2"])

    def test_risk_preview_is_no_side_effect_backend_calculation(self):
        state = initial_state()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        request = SimpleNamespace(app=application)
        body = v25_execution.RiskPreviewRequest(entry=100, stop_loss=99, margin_usdt=10, leverage=2, atr=0.5)
        with patch.object(v25_execution, "execution_owner", return_value={"id": "owner"}):
            preview = asyncio.run(v25_execution.v25_risk_preview(request, body))
        self.assertTrue(preview["ok"])
        self.assertFalse(preview["orders_created"])
        self.assertEqual(preview["max_stop_distance_pct"], 1.0)

    def test_live_account_snapshot_algo_orders_quality_contract(self):
        class FakeClient:
            def __init__(self, algo_orders):
                self.algo_orders = algo_orders

            async def signed(self, method, path, params=None):
                if path == "/fapi/v3/account":
                    return {}
                if path in {"/fapi/v3/positionRisk", "/fapi/v1/openOrders", "/fapi/v1/symbolConfig"}:
                    return []
                if path == "/fapi/v1/openAlgoOrders":
                    if isinstance(self.algo_orders, BaseException):
                        raise self.algo_orders
                    return self.algo_orders
                if path == "/fapi/v1/positionSide/dual":
                    return {"dualSidePosition": False}
                raise AssertionError(f"Unexpected snapshot path: {path}")

        cases = (
            ([{"symbol": "BTCUSDT", "algoId": 101}], "VALID_ORDERS"),
            ([], "VALID_EMPTY"),
            ({}, "UNKNOWN"),
            (BinanceDemoError("openAlgoOrders unavailable"), "UNKNOWN"),
        )
        for algo_orders, expected_quality in cases:
            snapshot = asyncio.run(v25_execution.account_snapshot(FakeClient(algo_orders)))
            self.assertEqual(snapshot["algo_orders_quality"], expected_quality)

    def test_live_cleanup_state_is_clean_after_successful_delete(self):
        plan = {"symbol": "BTCUSDT", "intent_id": "intent-1", "stop_client_id": "PTB_SL_owned", "provenance_state": "CONFIRMED"}
        rows = [{"symbol": "BTCUSDT", "client_algo_id": "PTB_SL_owned", "algo_id": 101}]

        class FakeClient:
            async def signed(self, method, path, params=None):
                return [] if method == "GET" and path == "/fapi/v1/openAlgoOrders" else {}

        asyncio.run(v25_execution.cancel_owned_algos_for_symbol(FakeClient(), rows, plan))
        self.assertEqual(plan["protection_cleanup_state"], "CLEAN")
        self.assertEqual(plan["protection_cleanup_pending_ids"], [])
        self.assertIsNone(plan["protection_cleanup_last_error"])
        self.assertIsNotNone(plan["protection_cleanup_attempted_at"])

    def test_live_cleanup_known_missing_delete_is_clean(self):
        plan = {"symbol": "BTCUSDT", "intent_id": "intent-1", "stop_client_id": "PTB_SL_owned", "provenance_state": "CONFIRMED"}
        rows = [{"symbol": "BTCUSDT", "client_algo_id": "PTB_SL_owned", "algo_id": 101}]

        for exchange_code in (-2011, -2013):
            class FakeClient:
                async def signed(self, method, path, params=None):
                    raise v25_execution.LiveExchangeError("already gone", exchange_code=exchange_code)

            asyncio.run(v25_execution.cancel_owned_algos_for_symbol(FakeClient(), rows, plan))
            self.assertEqual(plan["protection_cleanup_state"], "CLEAN")
            self.assertEqual(plan["protection_cleanup_pending_ids"], [])
            self.assertIsNone(plan["protection_cleanup_last_error"])

    def test_live_cleanup_unknown_delete_failure_requires_retry_state(self):
        plan = {"symbol": "BTCUSDT", "intent_id": "intent-1", "stop_client_id": "PTB_SL_owned", "provenance_state": "CONFIRMED"}
        rows = [{"symbol": "BTCUSDT", "client_algo_id": "PTB_SL_owned", "algo_id": 101}]

        class FakeClient:
            async def signed(self, method, path, params=None):
                raise v25_execution.LiveExchangeError("temporary delete failure")

        asyncio.run(v25_execution.cancel_owned_algos_for_symbol(FakeClient(), rows, plan))
        self.assertEqual(plan["protection_cleanup_state"], "UNKNOWN")
        self.assertEqual(plan["protection_cleanup_pending_ids"], ["101"])
        self.assertEqual(plan["protection_cleanup_last_error"], "temporary delete failure")

    def test_reconcile_settles_after_clean_cleanup(self):
        state = initial_state()
        plan = {"id": "plan-1", "symbol": "BTCUSDT", "status": "KORUMA AKTİF", "provenance_state": "CONFIRMED", "intent_id": "intent-1", "stop_client_id": "PTB_SL_owned"}
        state["plans"] = {plan["id"]: plan}
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(time_offset_ms=0, calls=[])
        settle = AsyncMock()

        async def signed(method, path, params=None):
            client.calls.append((method, path, params))
            return [] if method == "GET" and path == "/fapi/v1/openAlgoOrders" else {}

        client.signed = signed

        with patch.object(v25_execution, "client_for", return_value=client), \
                patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value={"positions": [], "open_algo_orders": [{"symbol": "BTCUSDT", "client_algo_id": "PTB_SL_owned", "algo_id": 101}]})), \
                patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
                patch.object(v25_execution, "settle_closed_plan", new=settle), \
                patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application))

        settle.assert_awaited_once_with(client, state, plan)
        self.assertEqual([call[0:2] for call in client.calls], [("DELETE", "/fapi/v1/algoOrder"), ("GET", "/fapi/v1/openAlgoOrders")])
        self.assertNotIn("settle_deferred_reason", plan)

    def test_reconcile_defers_unknown_cleanup_and_locks_live_execution(self):
        state = initial_state()
        plan = {"id": "plan-1", "symbol": "BTCUSDT", "status": "KORUMA AKTİF", "provenance_state": "CONFIRMED", "intent_id": "intent-1", "stop_client_id": "PTB_SL_owned"}
        state["plans"] = {plan["id"]: plan}
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(time_offset_ms=0, calls=[])
        settle = AsyncMock()

        async def signed(method, path, params=None):
            client.calls.append((method, path, params))
            return {} if method == "GET" and path == "/fapi/v1/openAlgoOrders" else {}

        client.signed = signed

        with patch.object(v25_execution, "client_for", return_value=client), \
                patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value={"positions": [], "open_algo_orders": [{"symbol": "BTCUSDT", "client_algo_id": "PTB_SL_owned", "algo_id": 101}]})), \
                patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
                patch.object(v25_execution, "settle_closed_plan", new=settle), \
                patch.object(v25_execution, "lock_live_execution", wraps=v25_execution.lock_live_execution) as lock, \
                patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application))

        settle.assert_not_awaited()
        self.assertEqual(plan["settle_deferred_reason"], "protection_cleanup_unknown")
        lock.assert_called_once_with(state, reason="protection cleanup unknown for BTCUSDT", unknown=True)
        self.assertTrue(state["reconciliation_required"])
        self.assertEqual([call[0:2] for call in client.calls], [("DELETE", "/fapi/v1/algoOrder"), ("GET", "/fapi/v1/openAlgoOrders")])

    def test_public_status_exposes_reconciliation_state(self):
        state = initial_state()
        state["reconciliation_required"] = True
        state["execution_state"] = "UNKNOWN"
        state["recovery_ready"] = False
        state["recovery_error"] = "PostgreSQL recovery pending."
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))

        with patch.object(v25_execution, "consent_status", return_value={}), \
            patch.object(v25_execution, "readiness", return_value={"ready": False, "score": 0, "gates": [], "demo_certificate": {}}):
            status = v25_execution.public_status(application)

        self.assertTrue(status["reconciliation_required"])
        self.assertEqual(status["execution_state"], "UNKNOWN")
        self.assertFalse(status["recovery_ready"])
        self.assertEqual(status["recovery_error"], "PostgreSQL recovery pending.")

    def test_public_status_does_not_disarm_live_arm_when_auto_trade_is_off(self):
        state = initial_state()
        state["armed_until"] = time.time() + 300
        state["real_trading_locked"] = False
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))

        with patch.object(v25_execution, "consent_status", return_value={"active": True, "fingerprint": "fingerprint"}), \
            patch.object(v25_execution, "readiness", return_value={"ready": True, "score": 1, "gates": [], "demo_certificate": {}}):
            status = v25_execution.public_status(application)

        self.assertTrue(status["armed"])
        self.assertFalse(status["real_trading_locked"])
        self.assertFalse(state["auto"]["enabled"])

    def test_live_stream_uses_supervised_vault_credentials(self):
        state = initial_state()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        credentials = ("api-key-123456", "secret-key-123456")

        with patch.object(v25_execution, "auto_session_credentials", new=AsyncMock(return_value=credentials)) as resolver:
            resolved = asyncio.run(v25_execution.live_stream_credentials(application, state))

        self.assertEqual(resolved, credentials)
        resolver.assert_awaited_once_with(application, state)

    def test_background_credentials_fall_back_to_connected_live_session(self):
        state = initial_state()
        state["live_session_authorization"] = {
            "session_id": "session-1", "user_id": "WEB_OWNER", "fingerprint": "fingerprint",
        }
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        credentials = ("api-key-123456", "secret-key-123456")

        with patch.object(v25_execution, "session_credentials_for_identity", new=AsyncMock(return_value=credentials)) as resolver, \
            patch.object(v25_execution, "consent_status", return_value={"active": True}):
            resolved = asyncio.run(v25_execution.auto_session_credentials(application, state))

        self.assertEqual(resolved, credentials)
        resolver.assert_awaited_once_with(application, "session-1", "WEB_OWNER", "LIVE", "fingerprint", force_refresh=False)

    def test_background_credentials_keep_protected_position_monitoring_after_consent_expires(self):
        state = initial_state()
        state["live_session_authorization"] = {
            "session_id": "session-1", "user_id": "WEB_OWNER", "fingerprint": "fingerprint",
        }
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        credentials = ("api-key-123456", "secret-key-123456")

        with patch.object(v25_execution, "session_credentials_for_identity", new=AsyncMock(return_value=credentials)), \
            patch.object(v25_execution, "consent_status", return_value={"active": False, "grace_active": False}):
            resolved = asyncio.run(v25_execution.auto_session_credentials(application, state))

        self.assertEqual(resolved, credentials)

    def test_public_status_exposes_only_sanitized_reconciliation_diagnostic_fields(self):
        state = initial_state()
        state["events"].insert(0, {
            "kind": "RECONCILIATION_FAILURE_DIAGNOSTIC",
            "exception_type": "LiveExchangeError",
            "exception_message": "api_key=abc123 secret=super-secret",
            "reconciliation_stage": "account_reconciliation",
            "source": "v25_execution.py",
            "function": "_request",
            "line": 700,
            "created_at": "2026-09-23T12:00:00+00:00",
            "authorization": "Bearer should-not-appear",
        })
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))

        with patch.object(v25_execution, "consent_status", return_value={}), \
            patch.object(v25_execution, "readiness", return_value={"ready": False, "score": 0, "gates": [], "demo_certificate": {}}):
            diagnostic = v25_execution.public_status(application)["reconciliation_diagnostic"]

        self.assertEqual(set(diagnostic), {"exception_type", "exception_message", "reconciliation_stage", "source", "function", "line", "timestamp"})
        self.assertEqual(diagnostic["exception_type"], "LiveExchangeError")
        self.assertEqual(diagnostic["reconciliation_stage"], "account_reconciliation")
        self.assertEqual(diagnostic["source"], "v25_execution.py")
        self.assertEqual(diagnostic["function"], "_request")
        self.assertEqual(diagnostic["line"], 700)
        self.assertEqual(diagnostic["timestamp"], "2026-09-23T12:00:00+00:00")
        self.assertIn("[REDACTED]", diagnostic["exception_message"])
        serialized = json.dumps(diagnostic)
        for secret in ("abc123", "super-secret", "Bearer should-not-appear"):
            self.assertNotIn(secret, serialized)

    def test_close_reason_mapping_covers_all_explicit_close_intents(self):
        self.assertEqual(close_reason_for_intent("manual-close-plan"), "MANUAL")
        self.assertEqual(close_reason_for_intent("protection-plan"), "STOP")
        self.assertEqual(close_reason_for_intent("emergency-close-plan"), "EMERGENCY")
        self.assertEqual(close_reason_for_intent("other-close-plan"), "UNKNOWN")
        plan = {"intent_id": "intent-1", "stop_client_id": "PTB_SL_owned"}
        self.assertEqual(close_reason_for_client_id(plan, "PTB_SL_owned"), "STOP")
        for index in range(1, 4):
            self.assertEqual(close_reason_for_client_id(plan, client_id_for(f"TP{index}", "intent-1")), f"TP{index}")
        self.assertEqual(close_reason_for_client_id(plan, "foreign-client"), "UNKNOWN")

    def test_tp_close_fill_sets_the_matching_close_reason(self):
        plan = {"intent_id": "intent-1", "symbol": "BTCUSDT", "stop_client_id": "PTB_SL_owned"}
        state = {"stream": {}, "events": [], "plans": {"plan-1": plan}}
        payload = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1720000000000,
            "o": {
                "s": "BTCUSDT", "c": client_id_for("TP2", "intent-1"), "R": True,
                "x": "TRADE", "X": "FILLED", "i": 42, "rp": "1.2", "ap": "102", "z": "0.1",
            },
        }
        self.assertTrue(process_live_stream_event(state, payload))
        self.assertEqual(plan["close_reason"], "TP2")

    def test_verified_plan_pnl_adds_exit_price_and_opened_at_without_changing_pnl(self):
        class FakeClient:
            async def signed(self, method, path, params=None):
                if path == "/fapi/v1/userTrades":
                    return [
                        {"orderId": 1, "side": "BUY", "price": "100", "qty": "3", "time": 1720000000000, "realizedPnl": "0", "commission": "0", "commissionAsset": "USDT"},
                        {"orderId": 2, "side": "SELL", "price": "110", "qty": "2", "time": 1720000060000, "realizedPnl": "4", "commission": "0.1", "commissionAsset": "USDT"},
                        {"orderId": 3, "side": "SELL", "price": "114", "qty": "1", "time": 1720000120000, "realizedPnl": "5", "commission": "0.1", "commissionAsset": "USDT"},
                    ]
                return []

        plan = {"symbol": "BTCUSDT", "direction": "LONG", "created_at": datetime.now(timezone.utc).isoformat(), "entry_order_id": 1, "exchange_order_ids": [2, 3]}
        result = asyncio.run(v25_execution.verified_plan_pnl(FakeClient(), plan))
        self.assertEqual(result["gross_realized_pnl"], 9.0)
        self.assertEqual(result["commission_usdt"], 0.2)
        self.assertEqual(result["realized_pnl"], 8.8)
        self.assertEqual(result["trade_count"], 3)
        self.assertAlmostEqual(result["exit_price"], 111.33333333)
        self.assertEqual(result["opened_at"], datetime.fromtimestamp(1720000000000 / 1000, timezone.utc).isoformat())

    def test_verified_plan_pnl_leaves_opened_at_null_without_entry_fill(self):
        class FakeClient:
            async def signed(self, method, path, params=None):
                if path == "/fapi/v1/userTrades":
                    return [{"orderId": 2, "side": "SELL", "price": "110", "qty": "1", "time": 1720000060000, "realizedPnl": "4", "commission": "0", "commissionAsset": "USDT"}]
                return []

        plan = {"symbol": "BTCUSDT", "direction": "LONG", "created_at": datetime.now(timezone.utc).isoformat(), "entry_order_id": 1, "exchange_order_ids": [2]}
        result = asyncio.run(v25_execution.verified_plan_pnl(FakeClient(), plan))
        self.assertIsNone(result["opened_at"])

    def test_auto_start_gate_rejects_missing_recovery_and_account_readiness(self):
        state = initial_state()
        state.update({"real_trading_locked": False, "connected": True, "armed_until": time.time() + 300})
        state["policy_ack_digest"] = policy_digest(state["policy"])
        state["snapshot"] = {"available_balance": 1000, "positions": [], "open_orders": [], "hedge_mode": False}
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, v21_demo=None))
        allowed, reason = live_auto_start_gate(application, state)
        self.assertFalse(allowed)
        self.assertIn("recovery", reason.lower())

    def test_auto_start_gate_rejects_unmanaged_external_position(self):
        state = initial_state()
        state.update({"real_trading_locked": False, "connected": True, "recovery_ready": True, "armed_until": time.time() + 300})
        state["policy_ack_digest"] = policy_digest(state["policy"])
        state["web_consent"] = {"key_fingerprint": "fingerprint", "expires_at_epoch": time.time() + 3600}
        state["snapshot"] = {
            "available_balance": 1000,
            "positions": [{"symbol": "RAYSOLUSDT", "direction": "LONG", "quantity": "1"}],
            "open_orders": [],
            "hedge_mode": False,
        }
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, v21_demo=None))
        with patch.object(v25_execution, "consent_status", return_value={"fingerprint": "fingerprint", "active": True}):
            allowed, reason = live_auto_start_gate(application, state)
        self.assertFalse(allowed)
        self.assertIn("V25 dışı", reason)

    def test_provenance_requires_exact_position_identity(self):
        plan = {"provenance_state": "PROVISIONAL", "symbol": "BTCUSDT", "direction": "LONG", "quantity": "0.010", "entry_order_id": 42, "entry_client_order_id": "PTB_ENTRY_exact"}
        self.assertFalse(confirm_live_plan_provenance(plan, {"symbol": "BTCUSDT", "direction": "LONG", "quantity": "0.011"}))
        self.assertEqual(plan["provenance_state"], "PROVISIONAL")
        self.assertTrue(confirm_live_plan_provenance(plan, {"symbol": "BTCUSDT", "direction": "LONG", "quantity": "0.010"}))
        self.assertEqual(plan["provenance_state"], "CONFIRMED")

    def test_protection_rows_ignore_same_symbol_foreign_client_ids(self):
        plan = {"symbol": "BTCUSDT", "intent_id": "intent-1", "stop_client_id": "PTB_SL_owned"}
        rows = [
            {"symbol": "BTCUSDT", "client_algo_id": "PTB_SL_owned"},
            {"symbol": "BTCUSDT", "client_algo_id": "PTB_FOREIGN"},
        ]
        self.assertEqual(owned_protection_rows(plan, rows), [rows[0]])

    def test_protection_classification_matches_exact_client_or_algo_identity(self):
        plan = {"symbol": "BTCUSDT", "direction": "LONG", "stop_client_id": "PTB_SL_owned", "stop_algo_id": 101}
        rows = [{"symbol": "BTCUSDT", "client_algo_id": "PTB_SL_owned", "algo_id": 101, "side": "SELL", "type": "STOP_MARKET", "status": "NEW"}]
        status, matched, reason = classify_plan_protection(plan, rows)
        self.assertEqual((status, reason), ("MATCHED", "EXACT_IDENTITY"))
        self.assertEqual(matched, rows)

    def test_protection_classification_marks_single_fallback_as_unknown(self):
        plan = {"symbol": "BTCUSDT", "direction": "LONG", "stop_client_id": "PTB_SL_owned"}
        rows = [{"symbol": "BTCUSDT", "client_algo_id": "EXTERNAL_STOP", "side": "SELL", "type": "STOP_MARKET", "status": "NEW"}]
        status, matched, reason = classify_plan_protection(plan, rows)
        self.assertEqual((status, reason), ("UNKNOWN", "FALLBACK_SYMBOL_DIRECTION_TYPE"))
        self.assertEqual(matched, rows)

    def test_protection_classification_marks_absent_stop_as_missing(self):
        plan = {"symbol": "BTCUSDT", "direction": "SHORT", "stop_client_id": "PTB_SL_owned"}
        status, matched, reason = classify_plan_protection(plan, [])
        self.assertEqual((status, matched, reason), ("MISSING", [], "REQUIRED_STOP_NOT_FOUND"))

    def test_live_is_locked_and_auto_trade_is_off_by_default_and_after_restart(self):
        self.assertTrue(initial_state()["real_trading_locked"])
        self.assertFalse(initial_state()["live_auto_trade"])
        restored = sanitized_state({"real_trading_locked": False, "live_auto_trade": True, "auto": {"enabled": True, "session_until": 9999999999}})
        self.assertTrue(restored["real_trading_locked"])
        self.assertFalse(restored["live_auto_trade"])
        self.assertFalse(restored["auto"]["enabled"])

    def test_mtf_history_rolls_and_or_gate_simulation_counts_rescued_signals(self):
        now = time.time()
        rows = [
            {"timestamp": "now", "timestamp_epoch": now, "symbol": "A", "confidence": 85, "entry_direction": "LONG", "1h_direction": "LONG", "4h_direction": "SHORT", "mtf_mismatch": True},
            {"timestamp": "now", "timestamp_epoch": now, "symbol": "B", "confidence": 79, "entry_direction": "SHORT", "1h_direction": "SHORT", "4h_direction": "LONG", "mtf_mismatch": True},
            {"timestamp": "old", "timestamp_epoch": now - 49 * 3600, "symbol": "C", "confidence": 90, "entry_direction": "LONG", "1h_direction": "LONG", "4h_direction": "SHORT", "mtf_mismatch": True},
        ]
        self.assertEqual(len(prune_mtf_decision_history(rows, now_epoch=now)), 2)
        restored = sanitized_state({"mtf_decision_history": rows})
        self.assertEqual(len(restored["mtf_decision_history"]), 2)
        summary = summarize_mtf_relaxation(restored["mtf_decision_history"])
        self.assertEqual(summary["high_confidence_signals"], 1)
        self.assertEqual(summary["strict_mtf_rejections"], 1)
        self.assertEqual(summary["rescued_by_or_gate"], 1)

    def test_web_consent_survives_restore_through_grace_only(self):
        active = {
            "accepted_at": "2026-09-23T18:00:00+00:00",
            "expires_at_epoch": time.time() + 3600,
            "key_fingerprint": "fingerprint-1",
        }
        restored = sanitized_state({"web_consent": active})
        self.assertEqual(restored["web_consent"], active)

        grace = dict(active, expires_at_epoch=time.time() - 1)
        grace_restored = sanitized_state({"web_consent": grace})
        self.assertEqual(grace_restored["web_consent"], grace)

        expired = dict(active, expires_at_epoch=time.time() - v25_execution.LIVE_CONSENT_GRACE_SECONDS - 1)
        expired_restored = sanitized_state({"web_consent": expired})
        self.assertEqual(expired_restored["web_consent"]["expires_at_epoch"], 0.0)
        self.assertIsNone(expired_restored["web_consent"]["key_fingerprint"])

    def test_consent_acknowledges_current_policy_for_its_24_hour_window(self):
        state = initial_state()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, v21_demo=None))
        request = SimpleNamespace(app=application)

        with patch.object(v25_execution, "execution_owner", return_value={"id": "owner"}), \
            patch.object(v25_execution, "live_credentials_status", return_value=("api-key-123456", "secret-key-123456", "fingerprint")), \
            patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.v25_web_consent(
                request,
                v25_execution.Confirmation(confirmation="CANLI İŞLEM RİSKİNİ 24 SAAT KABUL EDİYORUM"),
            ))

        self.assertEqual(state["policy_ack_digest"], policy_digest(state["policy"]))

    def test_policy_change_preserves_active_consent_ack_and_arm(self):
        state = initial_state()
        state["policy_ack_digest"] = policy_digest(state["policy"])
        state["armed_until"] = time.time() + 300
        state["real_trading_locked"] = False
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, v21_demo=None))
        request = SimpleNamespace(app=application)

        with patch.object(v25_execution, "execution_owner", return_value={"id": "owner"}), \
            patch.object(v25_execution, "consent_status", return_value={"active": True, "fingerprint": "fingerprint"}), \
            patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.v25_policy(
                request,
                v25_execution.PolicyUpdate(max_loss_per_trade=4),
            ))

        self.assertEqual(state["policy_ack_digest"], policy_digest(state["policy"]))
        self.assertGreater(state["armed_until"], time.time())
        self.assertFalse(state["auto"]["enabled"])

    def test_hard_total_exposure_and_active_plan_gates_fail_closed(self):
        self.assertEqual(HARD_MAX_TOTAL_EXPOSURE_USDT, 350.0)
        kwargs = dict(
            symbol="BTCUSDT",
            signal={"direction": "LONG", "confidence": 100, "radar": {"trap_score": 1}},
            snapshot={"positions": [{"symbol": "ETHUSDT", "notional": "90"}], "open_orders": [], "hedge_mode": False},
            policy=sanitize_execution_policy({"max_total_exposure_usdt": 100}),
            daily={"entries": 0, "realized_pnl": 0, "unverified_closures": 0},
            spread_bps=1,
            armed=True,
        )
        exposure = evaluate_entry_gates(candidate_notional_usdt=20, **kwargs)
        self.assertIn("exposure", {item["key"] for item in exposure["gates"] if not item["passed"]})
        active_plan = evaluate_entry_gates(active_plans=[{"symbol": "BTCUSDT", "direction": "LONG", "status": "KORUMA AKTİF"}], **{**kwargs, "snapshot": {"positions": [], "open_orders": [], "hedge_mode": False}})
        self.assertIn("active_plan", {item["key"] for item in active_plan["gates"] if not item["passed"]})

    def test_restore_sanitizer_cannot_exceed_hard_caps(self):
        policy = sanitize_execution_policy({
            "max_margin_per_trade": 9999,
            "max_leverage": 125,
            "max_positions": 999,
            "daily_loss_limit": 9999,
            "allowed_symbols": ["BTC/USDT", "ETHUSDT", "BAD-USD"],
        })
        self.assertEqual(policy["max_margin_per_trade"], HARD_MAX_MARGIN_USDT)
        self.assertEqual(policy["max_leverage"], HARD_MAX_LEVERAGE)
        self.assertEqual(policy["max_positions"], HARD_MAX_POSITIONS)
        self.assertEqual(policy["daily_loss_limit"], HARD_MAX_DAILY_LOSS_USDT)
        self.assertEqual(policy["allowed_symbols"], ["BTCUSDT", "ETHUSDT"])

    def test_policy_digest_changes_when_risk_changes(self):
        first = sanitize_execution_policy({})
        second = sanitize_execution_policy({"max_margin_per_trade": 30})
        self.assertNotEqual(policy_digest(first), policy_digest(second))

    def test_default_min_confidence_is_configurable(self):
        with patch.dict("os.environ", {"PROTREBOT_MIN_CONFIDENCE": "80"}, clear=False):
            self.assertEqual(configured_min_confidence(), 80)
            self.assertEqual(sanitize_execution_policy({})["min_confidence"], 80)
            self.assertEqual(v25_execution.initial_state()["policy"]["min_confidence"], 80)

        with patch.dict("os.environ", {"PROTREBOT_MIN_CONFIDENCE": "91"}, clear=False):
            self.assertEqual(configured_min_confidence(), 91)
            self.assertEqual(sanitize_execution_policy({})["min_confidence"], 91)
            self.assertEqual(sanitize_execution_policy({"min_confidence": 84})["min_confidence"], 84)

    def test_mtf_either_timeframe_mode_is_explicitly_configurable(self):
        with patch.dict("os.environ", {"PROTREBOT_MTF_ALLOW_EITHER_TIMEFRAME": "false"}, clear=False):
            self.assertFalse(configured_mtf_allow_either_timeframe())
            self.assertFalse(sanitize_execution_policy({})["mtf_allow_either_timeframe"])

        with patch.dict("os.environ", {"PROTREBOT_MTF_ALLOW_EITHER_TIMEFRAME": "true"}, clear=False):
            self.assertTrue(configured_mtf_allow_either_timeframe())
            self.assertTrue(sanitize_execution_policy({})["mtf_allow_either_timeframe"])
            self.assertFalse(sanitize_execution_policy({"mtf_allow_either_timeframe": False})["mtf_allow_either_timeframe"])

    def test_risk_sizing_respects_loss_margin_and_leverage(self):
        policy = sanitize_execution_policy({"max_margin_per_trade": 25, "max_loss_per_trade": 3, "max_leverage": 2})
        order = risk_sized_order(100, 98, policy)
        self.assertLessEqual(order["margin_usdt"], 25)
        self.assertLessEqual(order["notional_usdt"], 50)
        self.assertLessEqual(order["estimated_stop_loss_usdt"], 3)
        self.assertEqual(risk_sized_order(100, 95, policy)["stop_distance_pct"], 5.0)
        with self.assertRaises(ValueError):
            risk_sized_order(100, 90, policy)

    def test_entry_gate_fails_closed_without_arm_and_on_duplicate(self):
        policy = sanitize_execution_policy({"min_confidence": 85, "max_positions": 2})
        result = evaluate_entry_gates(
            symbol="BTCUSDT",
            signal={"direction": "LONG", "confidence": 92, "radar": {"trap_score": 20}},
            snapshot={"positions": [{"symbol": "BTCUSDT"}], "open_orders": [], "hedge_mode": False},
            policy=policy,
            daily={"entries": 0, "realized_pnl": 0},
            spread_bps=1.5,
            armed=False,
        )
        self.assertFalse(result["passed"])
        failed = {item["key"] for item in result["gates"] if not item["passed"]}
        self.assertIn("arm", failed)
        self.assertIn("duplicate", failed)

    def test_open_loss_uses_same_hard_daily_loss_circuit_breaker(self):
        policy = sanitize_execution_policy({"daily_loss_limit": 10})
        result = evaluate_entry_gates(
            symbol="BTCUSDT",
            signal={"direction": "LONG", "confidence": 95, "radar": {"trap_score": 10}},
            snapshot={"positions": [], "open_orders": [], "hedge_mode": False, "unrealized_pnl": -10.01},
            policy=policy,
            daily={"entries": 0, "realized_pnl": 0, "unverified_closures": 0},
            spread_bps=1,
            armed=True,
        )
        failed = {item["key"] for item in result["gates"] if not item["passed"]}
        self.assertIn("open_loss", failed)

    def test_release_requires_every_gate_and_demo_certificate(self):
        locked = release_gates(credentials=True, consent_active=True, connected=True, one_way=True, policy_acknowledged=True, demo_certificate={"status": "KANIT TOPLUYOR", "score": 75})
        self.assertFalse(release_ready(locked))
        waived = release_gates(credentials=True, consent_active=True, connected=True, one_way=True, policy_acknowledged=True, demo_certificate={"status": "KANIT TOPLUYOR", "score": 38, "live_allowed": True})
        self.assertTrue(release_ready(waived))
        self.assertTrue(next(item for item in waived if item["key"] == "demo_certificate")["passed"])
        for key in ("credentials", "local_consent", "read_only", "one_way", "policy"):
            gate_values = {name: True for name in ("credentials", "consent_active", "connected", "one_way", "policy_acknowledged")}
            gate_values[{"credentials": "credentials", "local_consent": "consent_active", "read_only": "connected", "one_way": "one_way", "policy": "policy_acknowledged"}[key]] = False
            blocked = release_gates(**gate_values, demo_certificate={"status": "KANIT TOPLUYOR", "score": 38, "live_allowed": True})
            self.assertFalse(release_ready(blocked), key)
            self.assertFalse(next(item for item in blocked if item["key"] == key)["passed"])

    def test_daily_metrics_count_only_live_entries_and_closed_realized(self):
        events = [
            {"kind": "LIVE_ENTRY", "created_at": "2026-08-08T10:00:00+00:00"},
            {"kind": "LIVE_POSITION_CLOSED", "created_at": "2026-08-08T11:00:00+00:00", "realized_pnl": -2.5},
            {"kind": "DEMO_ENTRY", "created_at": "2026-08-08T12:00:00+00:00"},
        ]
        metrics = daily_execution_metrics(events, datetime(2026, 8, 8, 15, tzinfo=timezone.utc))
        self.assertEqual(metrics["entries"], 1)
        self.assertEqual(metrics["realized_pnl"], -2.5)
        self.assertEqual(metrics["unverified_closures"], 0)

    def test_unverified_close_blocks_until_exchange_pnl_is_proven(self):
        events = [
            {"kind": "LIVE_POSITION_CLOSED_UNVERIFIED", "plan_id": "plan-1", "created_at": "2026-08-08T10:00:00+00:00"},
        ]
        pending = daily_execution_metrics(events, datetime(2026, 8, 8, 15, tzinfo=timezone.utc))
        self.assertEqual(pending["unverified_closures"], 1)
        events.append({"kind": "LIVE_POSITION_CLOSED", "plan_id": "plan-1", "realized_pnl": -1.25, "created_at": "2026-08-08T10:01:00+00:00"})
        verified = daily_execution_metrics(events, datetime(2026, 8, 8, 15, tzinfo=timezone.utc))
        self.assertEqual(verified["unverified_closures"], 0)

    def test_key_fingerprint_is_one_way_and_stable(self):
        first = credential_fingerprint("LIVE_KEY_PLACEHOLDER")
        self.assertEqual(first, credential_fingerprint("LIVE_KEY_PLACEHOLDER"))
        self.assertNotIn("LIVE_KEY_PLACEHOLDER", first or "")

class V25LiveGuardIntegrationContractTests(unittest.TestCase):
    def _live_spec(self):
        return {
            "symbol": "BTCUSDT", "side": "BUY", "order_type": "MARKET", "quantity": "0.500",
        }

    def _matching_order(self):
        return {
            "orderId": 7001, "clientOrderId": "V25_ENTRY_test-intent", "symbol": "BTCUSDT",
            "side": "BUY", "positionSide": "BOTH", "type": "MARKET", "origQty": "0.500", "status": "NEW",
        }

    def test_unknown_transport_failures_have_zero_retry_and_safe_recovery(self):
        class FakeClient:
            def __init__(self, post_result):
                self.calls = []
                self.post_result = post_result

            async def signed(self, method, path, params=None):
                self.calls.append((method, path))
                if method == "GET":
                    return None
                if isinstance(self.post_result, BaseException):
                    raise self.post_result
                return self.post_result

        for failure in (
            LiveExchangeError("timeout", unknown_execution=True),
            LiveExchangeError("5xx", unknown_execution=True),
            {},
        ):
            client = FakeClient(failure)
            with self.assertRaises(LiveExchangeError) as raised:
                asyncio.run(submit_entry(client, self._live_spec(), "V25_ENTRY_test-intent", test_only=False))
            self.assertTrue(raised.exception.unknown_execution)
            self.assertEqual(sum(1 for method, path in client.calls if method == "POST" and path == "/fapi/v1/order"), 1)
            self.assertEqual(sum(1 for method, path in client.calls if method == "POST"), 1)

    def test_exact_reconciliation_recovers_without_duplicate_post(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            async def signed(self, method, path, params=None):
                self.calls.append((method, path))
                if method == "GET" and len([item for item in self.calls if item[0] == "GET"]) == 1:
                    return None
                if method == "POST":
                    raise LiveExchangeError("timeout", unknown_execution=True)
                return self._matching_order()

            def _matching_order(self):
                return {**V25LiveGuardIntegrationContractTests()._matching_order(), "clientOrderId": "V25_ENTRY_test-intent"}

        client = FakeClient()
        result = asyncio.run(submit_entry(client, self._live_spec(), "V25_ENTRY_test-intent", test_only=False))
        self.assertTrue(result["recovered"])
        second = asyncio.run(submit_entry(client, self._live_spec(), "V25_ENTRY_test-intent", test_only=False))
        self.assertTrue(second["recovered"])
        self.assertEqual(sum(1 for method, path in client.calls if method == "POST" and path == "/fapi/v1/order"), 1)

    def test_ambiguous_and_foreign_reconciliation_are_not_owned(self):
        class FakeClient:
            def __init__(self, response):
                self.response = response
                self.calls = []

            async def signed(self, method, path, params=None):
                self.calls.append((method, path))
                return self.response

        for response in (
            [self._matching_order(), {**self._matching_order(), "orderId": 7002}],
            {**self._matching_order(), "clientOrderId": "FOREIGN_ORDER"},
        ):
            client = FakeClient(response)
            with self.assertRaises(LiveExchangeError) as raised:
                asyncio.run(submit_entry(client, self._live_spec(), "V25_ENTRY_test-intent", test_only=False))
            self.assertTrue(raised.exception.unknown_execution)
            self.assertEqual(sum(1 for method, path in client.calls if method == "POST" and path == "/fapi/v1/order"), 0)

    def test_emergency_unknown_and_recovery_incomplete_states_reject(self):
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=initial_state()))
        state = application.state.v25_execution
        state["lock"] = asyncio.Lock()
        state.update({"recovery_ready": True, "live_auto_trade": True})
        state["auto"].update({"enabled": True, "session_until": time.time() + 300})
        state["emergency"]["active"] = True
        with patch.object(v25_execution, "readiness", return_value={"ready": True}):
            with self.assertRaises(v25_execution.HTTPException) as emergency_error:
                asyncio.run(v25_execution.execute_live_order(application, LiveOrderRequest(symbol="BTCUSDT", direction="LONG", margin_usdt=5, leverage=1, stop_loss=98, tp1=102, tp2=104, tp3=106), source="V25_AUTO"))
        self.assertEqual(emergency_error.exception.status_code, 423)
        state["emergency"]["active"] = False
        state["execution_state"] = "UNKNOWN"
        state["reconciliation_required"] = True
        with patch.object(v25_execution, "readiness", return_value={"ready": True}):
            with self.assertRaises(v25_execution.HTTPException):
                asyncio.run(v25_execution.execute_live_order(application, LiveOrderRequest(symbol="BTCUSDT", direction="LONG", margin_usdt=5, leverage=1, stop_loss=98, tp1=102, tp2=104, tp3=106), source="V25_AUTO"))
        state["recovery_ready"] = False
        state["execution_state"] = "LOCKED"
        state["reconciliation_required"] = False
        with self.assertRaises(v25_execution.HTTPException):
            asyncio.run(v25_execution.execute_live_order(application, LiveOrderRequest(symbol="BTCUSDT", direction="LONG", margin_usdt=5, leverage=1, stop_loss=98, tp1=102, tp2=104, tp3=106), source="V25_AUTO"))

    def test_kill_switch_is_persisted_and_audit_is_secret_free(self):
        state = initial_state()
        lock_live_execution(state, "NETWORK_AMBIGUOUS", unknown=True, symbol="BTCUSDT", client_id="V25_ENTRY_secret-value")
        restored = sanitized_state(state)
        self.assertTrue(restored["real_trading_locked"])
        self.assertFalse(restored["live_auto_trade"])
        self.assertEqual(restored["execution_state"], "UNKNOWN")
        self.assertTrue(restored["reconciliation_required"])
        self.assertTrue(restored["emergency"]["active"])
        audit = state["events"][0]
        self.assertNotIn("secret-value", json.dumps(audit))
        self.assertNotIn("api_key", json.dumps(audit).lower())

    def test_request_timeout_and_5xx_are_unknown_without_real_transport(self):
        class FakeHttp:
            def __init__(self, result):
                self.result = result

            async def request(self, *args, **kwargs):
                if isinstance(self.result, BaseException):
                    raise self.result
                return self.result

        class Response:
            def __init__(self, status_code):
                self.status_code = status_code

            def json(self):
                return {"code": -1000, "msg": "ambiguous"}

        for result in (httpx.TimeoutException("timeout"), httpx.ConnectError("reset"), Response(500), Response(503)):
            client = BinanceLiveClient(FakeHttp(result), "TEST_KEY_PLACEHOLDER", "TEST_SECRET_PLACEHOLDER")
            with self.assertRaises(LiveExchangeError) as raised:
                asyncio.run(client._request("POST", "/fapi/v1/order", {}, signed=False))
            self.assertTrue(raised.exception.unknown_execution)

    def test_request_network_error_records_persistent_diagnostic_metadata(self):
        class FakeHttp:
            timeout = httpx.Timeout(30, connect=10, read=30, write=10, pool=30)

            async def request(self, *args, **kwargs):
                raise httpx.ConnectError("reset")

        state = initial_state()
        state["connection"]["retry_count"] = 2
        client = BinanceLiveClient(
            FakeHttp(),
            "TEST_KEY_PLACEHOLDER",
            "TEST_SECRET_PLACEHOLDER",
            diagnostic_state=state,
        )
        with self.assertRaises(LiveExchangeError):
            asyncio.run(client._request("GET", "/fapi/v3/account", {}, signed=True))

        diagnostic = next(event for event in state["events"] if event["kind"] == "LIVE_REQUEST_ERROR")
        self.assertEqual(diagnostic["endpoint"], "/fapi/v3/account")
        self.assertEqual(diagnostic["exception_type"], "LiveExchangeError")
        self.assertEqual(diagnostic["httpx_error_type"], "ConnectError")
        self.assertEqual(diagnostic["retry_count"], 2)
        self.assertEqual(diagnostic["request_attempt"], 1)
        self.assertEqual(diagnostic["timeout_seconds"], {"connect": 10, "read": 30, "write": 10, "pool": 30})

    def test_transient_market_data_get_failure_is_recoverable_but_order_paths_are_not(self):
        class FakeHttp:
            timeout = httpx.Timeout(30, connect=10, read=30, write=10, pool=30)

            async def request(self, method, url, **kwargs):
                raise httpx.RemoteProtocolError("server disconnected")

        state = initial_state()
        client = BinanceLiveClient(
            FakeHttp(),
            "TEST_KEY_PLACEHOLDER",
            "TEST_SECRET_PLACEHOLDER",
            diagnostic_state=state,
        )
        with self.assertRaises(LiveExchangeError) as market_data_error:
            asyncio.run(client._request("GET", "/fapi/v1/klines", {}, signed=False))
        self.assertFalse(market_data_error.exception.unknown_execution)
        self.assertTrue(market_data_error.exception.transient_read_failure)
        self.assertTrue(next(event for event in state["events"] if event["kind"] == "LIVE_REQUEST_ERROR")["transient_read_failure"])

        with self.assertRaises(LiveExchangeError) as order_read_error:
            asyncio.run(client._request("GET", "/fapi/v1/order", {}, signed=True))
        self.assertTrue(order_read_error.exception.unknown_execution)
        self.assertFalse(order_read_error.exception.transient_read_failure)

    def test_successful_safe_get_recovers_transient_market_data_failure_without_rearm(self):
        class Response:
            status_code = 200

            def json(self):
                return {"serverTime": 1}

        class FakeHttp:
            async def request(self, *args, **kwargs):
                return Response()

        state = initial_state()
        state["auto"].update({"enabled": True, "session_until": time.time() + 300})
        state["real_trading_locked"] = False
        state["live_auto_trade"] = True
        failure = LiveExchangeError(
            "temporary market data failure",
            transient_read_failure=True,
            request_method="GET",
            request_path="/fapi/v1/klines",
        )
        self.assertTrue(v25_execution.mark_transient_market_data_failure(state, failure))
        self.assertTrue(state["real_trading_locked"])
        client = BinanceLiveClient(
            FakeHttp(),
            "TEST_KEY_PLACEHOLDER",
            "TEST_SECRET_PLACEHOLDER",
            diagnostic_state=state,
        )
        asyncio.run(client._request("GET", "/fapi/v1/klines", {}, signed=False))
        self.assertFalse(state["real_trading_locked"])
        self.assertTrue(state["live_auto_trade"])
        self.assertTrue(state["auto"]["enabled"])
        self.assertIsNone(state["transient_market_data"])
        self.assertTrue(any(event["kind"] == "AUTO_RECOVERED_FROM_TRANSIENT_MARKET_DATA" for event in state["events"]))

    def test_rate_limit_retry_after_uses_header_and_records_endpoint_weight(self):
        class Response:
            def __init__(self):
                self.status_code = 429
                self.headers = {"Retry-After": "7", "X-MBX-USED-WEIGHT-1M": "1900"}

            def json(self):
                return {"code": -1003, "msg": "Too many requests"}

        class FakeHttp:
            async def request(self, *args, **kwargs):
                return Response()

        client = BinanceLiveClient(FakeHttp(), "TEST_KEY_PLACEHOLDER", "TEST_SECRET_PLACEHOLDER")
        with self.assertRaises(v25_execution.LiveRateLimitError) as raised:
            asyncio.run(client._request("GET", "/fapi/v1/openOrders", {}, signed=False))
        self.assertEqual(raised.exception.retry_after, 7)
        self.assertEqual(raised.exception.exchange_code, -1003)
        self.assertEqual(client.last_used_weight_1m, 1900)

    def test_rate_limit_without_retry_after_falls_back_to_generic_backoff(self):
        class Response:
            status_code = 418
            headers = {}

            def json(self):
                return {"code": -1003, "msg": "Too many requests"}

        class FakeHttp:
            async def request(self, *args, **kwargs):
                return Response()

        client = BinanceLiveClient(FakeHttp(), "TEST_KEY_PLACEHOLDER", "TEST_SECRET_PLACEHOLDER")
        with self.assertRaises(v25_execution.LiveRateLimitError) as raised:
            asyncio.run(client._request("GET", "/fapi/v1/openOrders", {}, signed=False))
        self.assertIsNone(raised.exception.retry_after)
        self.assertEqual(v25_execution.rate_limit_backoff_seconds(raised.exception, 5), 5)
        self.assertEqual(v25_execution.rate_limit_backoff_seconds(v25_execution.LiveRateLimitError("limited", retry_after=7), 5), 7)

    def test_used_weight_threshold_waits_before_next_request(self):
        class Response:
            status_code = 200

            def __init__(self):
                self.headers = {"X-MBX-USED-WEIGHT-1M": "1920"}

            def json(self):
                return {}

        class FakeHttp:
            async def request(self, *args, **kwargs):
                return Response()

        client = BinanceLiveClient(FakeHttp(), "TEST_KEY_PLACEHOLDER", "TEST_SECRET_PLACEHOLDER")
        with patch.object(v25_execution.asyncio, "sleep", new=AsyncMock()) as sleep:
            asyncio.run(client._request("GET", "/fapi/v1/openOrders", {}, signed=False))
            asyncio.run(client._request("GET", "/fapi/v1/openOrders", {}, signed=False))
        sleep.assert_awaited_once()
        self.assertAlmostEqual(sleep.await_args.args[0], v25_execution.RATE_LIMIT_PROACTIVE_WAIT_SECONDS, places=2)

    def test_unexpected_submit_exception_locks_unknown_and_disables_auto(self):
        state = initial_state()
        state.update({"recovery_ready": True, "real_trading_locked": False, "live_auto_trade": True})
        state["auto"].update({"enabled": True, "session_until": time.time() + 300})
        state["lock"] = asyncio.Lock()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=None))
        spec = {
            "symbol": "BTCUSDT", "direction": "LONG", "side": "BUY", "order_type": "MARKET",
            "margin_usdt": 5.0, "leverage": 1, "notional_usdt": 5.0, "quantity": "0.050",
            "entry_price": "100.00", "stop_loss": "98.00", "targets": ["102.00", "104.00", "106.00"],
            "step": Decimal("0.001"), "min_qty": Decimal("0.001"), "estimated_stop_loss_usdt": 0.1,
        }
        with patch.multiple(
            v25_execution,
            client_for=lambda application, request=None: object(),
            account_snapshot=AsyncMock(return_value={"available_balance": 100, "positions": [], "open_orders": [], "hedge_mode": False}),
            spread_bps=AsyncMock(return_value=1.0),
            readiness=lambda application, state, request=None: {"ready": True},
            auto_session_credentials=AsyncMock(return_value=("TEST_KEY_PLACEHOLDER", "TEST_SECRET_PLACEHOLDER")),
            build_live_spec=AsyncMock(return_value=spec),
            set_live_isolated_margin=AsyncMock(),
            apply_live_verified_leverage=AsyncMock(return_value={"applied_leverage": 1, "margin_type": "isolated"}),
            submit_entry=AsyncMock(side_effect=RuntimeError("synthetic submit failure")),
            persist_state=lambda state: None,
        ):
            with self.assertRaises(v25_execution.HTTPException) as raised:
                asyncio.run(v25_execution.execute_live_order(
                    application,
                    LiveOrderRequest(symbol="BTCUSDT", direction="LONG", margin_usdt=5, leverage=1, stop_loss=98, tp1=102, tp2=104, tp3=106, intent_id="test-intent-1"),
                    source="V25_AUTO",
                ))
        self.assertEqual(raised.exception.status_code, 502)
        self.assertTrue(state["real_trading_locked"])
        self.assertFalse(state["live_auto_trade"])
        self.assertEqual(state["execution_state"], "UNKNOWN")
        self.assertTrue(state["reconciliation_required"])

    def test_reconciliation_failure_records_sanitized_diagnostic_before_order_submission(self):
        state = initial_state()
        state["recovery_loaded"] = True
        state["lock"] = asyncio.Lock()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=None))
        submit = AsyncMock()

        async def fail_reconcile(*args, **kwargs):
            raise RuntimeError("api_key=abc123 secret=super-secret signature=deadbeef")

        async def stop_after_failure(_seconds):
            raise asyncio.CancelledError

        with patch.multiple(
            v25_execution,
            auto_session_credentials=AsyncMock(return_value=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")),
            reconcile=fail_reconcile,
            submit_entry=submit,
            persist_state=lambda current: None,
            automation_telemetry=lambda *args, **kwargs: None,
        ), patch.object(v25_execution.asyncio, "sleep", new=stop_after_failure):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(v25_execution.execution_loop(application))

        self.assertTrue(state["real_trading_locked"])
        self.assertTrue(state["reconciliation_required"])
        self.assertEqual(state["execution_state"], "LOCKED")
        self.assertTrue(state["emergency"]["active"])
        self.assertEqual(state["emergency"]["reason"], "RECONCILIATION_FAILURE")
        self.assertFalse(any(event["kind"] == "LIVE_UNKNOWN_EXECUTION" for event in state["events"]))
        self.assertTrue(any(event.get("reason") == "RECONCILIATION_FAILURE" for event in state["events"]))
        diagnostic = next(event for event in state["events"] if event["kind"] == "RECONCILIATION_FAILURE_DIAGNOSTIC")
        self.assertEqual(diagnostic["exception_type"], "RuntimeError")
        self.assertEqual(diagnostic["reconciliation_stage"], "account_reconciliation")
        self.assertTrue(diagnostic["source"])
        self.assertTrue(diagnostic["function"])
        self.assertGreater(diagnostic["line"], 0)
        self.assertIn("[REDACTED]", diagnostic["exception_message"])
        serialized = json.dumps(sanitized_state(state))
        for secret in ("abc123", "super-secret", "deadbeef"):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, state["connection"]["last_error"])
        submit.assert_not_awaited()

    def test_clean_reconciliation_clears_stale_technical_unknown_only(self):
        state = initial_state()
        state.update({
            "execution_state": "UNKNOWN",
            "reconciliation_required": True,
            "real_trading_locked": True,
            "live_auto_trade": True,
            "armed_until": time.time() + 300,
        })
        state["auto"].update({"enabled": True, "session_until": time.time() + 300})
        state["emergency"].update({"active": True, "reason": "RECONCILIATION_FAILURE"})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(time_offset_ms=0)

        with patch.object(v25_execution, "client_for_with_credentials", return_value=client), \
                patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=complete_reconciliation_snapshot())), \
                patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
                patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application, credentials=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")))

        self.assertEqual(state["execution_state"], "LOCKED")
        self.assertFalse(state["reconciliation_required"])
        self.assertTrue(state["real_trading_locked"])
        self.assertFalse(state["live_auto_trade"])
        self.assertEqual(state["armed_until"], 0.0)
        self.assertFalse(state["emergency"]["active"])
        self.assertTrue(any(event["kind"] == "RECONCILIATION_CLEAN" for event in state["events"]))

    def test_clean_reconciliation_auto_recovers_active_session_within_window(self):
        state = initial_state()
        state["auto"].update({"enabled": True, "session_until": time.time() + 300})
        state["live_auto_trade"] = True
        v25_execution.lock_reconciliation_failure(state)
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(time_offset_ms=0)

        with patch.object(v25_execution, "client_for_with_credentials", return_value=client), \
                patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=complete_reconciliation_snapshot())), \
                patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
                patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application, credentials=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")))

        self.assertFalse(state["real_trading_locked"])
        self.assertTrue(state["live_auto_trade"])
        self.assertTrue(state["auto"]["enabled"])
        self.assertFalse(state["reconciliation_required"])
        self.assertTrue(any(event["kind"] == "AUTO_RECOVERED_FROM_TRANSIENT_RECONCILIATION" for event in state["events"]))

    def test_clean_reconciliation_does_not_auto_recover_after_window(self):
        state = initial_state()
        state["auto"].update({"enabled": True, "session_until": time.time() + 300})
        state["live_auto_trade"] = True
        v25_execution.lock_reconciliation_failure(state)
        state["transient_reconciliation"]["failed_at_epoch"] -= v25_execution.TRANSIENT_RECONCILIATION_RECOVERY_SECONDS + 1
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(time_offset_ms=0)

        with patch.object(v25_execution, "client_for_with_credentials", return_value=client), \
                patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=complete_reconciliation_snapshot())), \
                patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
                patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application, credentials=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")))

        self.assertTrue(state["real_trading_locked"])
        self.assertFalse(state["live_auto_trade"])
        self.assertFalse(state["auto"]["enabled"])
        self.assertTrue(any(event["kind"] == "RECONCILIATION_CLEAN" for event in state["events"]))
        self.assertFalse(any(event["kind"] == "AUTO_RECOVERED_FROM_TRANSIENT_RECONCILIATION" for event in state["events"]))

    def test_genuine_unknown_execution_remains_locked_after_clean_snapshot(self):
        state = initial_state()
        state.update({"execution_state": "UNKNOWN", "reconciliation_required": True})
        lock_live_execution(state, "UNKNOWN_ORDER_STATE", unknown=True, client_id="V25_ENTRY_unknown")
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(time_offset_ms=0)

        with patch.object(v25_execution, "client_for_with_credentials", return_value=client), \
                patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=complete_reconciliation_snapshot())), \
                patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
                patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application, credentials=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")))

        self.assertEqual(state["execution_state"], "UNKNOWN")
        self.assertTrue(state["reconciliation_required"])
        self.assertTrue(state["emergency"]["active"])
        self.assertTrue(state["real_trading_locked"])
        self.assertFalse(any(event["kind"] == "RECONCILIATION_CLEAN" for event in state["events"]))

    def test_incomplete_algo_snapshot_does_not_clear_stale_unknown(self):
        state = initial_state()
        state.update({"execution_state": "UNKNOWN", "reconciliation_required": True})
        state["emergency"].update({"active": True, "reason": "RECONCILIATION_FAILURE"})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(time_offset_ms=0)

        with patch.object(v25_execution, "client_for_with_credentials", return_value=client), \
                patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=complete_reconciliation_snapshot(open_algo_orders_available=False, algo_orders_quality="UNKNOWN"))), \
                patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
                patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application, credentials=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")))

        self.assertEqual(state["execution_state"], "UNKNOWN")
        self.assertTrue(state["reconciliation_required"])
        self.assertTrue(state["emergency"]["active"])

    def test_malformed_open_order_does_not_clear_stale_unknown(self):
        state = initial_state()
        state.update({"execution_state": "UNKNOWN", "reconciliation_required": True})
        state["emergency"].update({"active": True, "reason": "RECONCILIATION_FAILURE"})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(time_offset_ms=0)

        with patch.object(v25_execution, "client_for_with_credentials", return_value=client), \
                patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=complete_reconciliation_snapshot(open_orders=[None]))), \
                patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
                patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application, credentials=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")))

        self.assertEqual(state["execution_state"], "UNKNOWN")
        self.assertTrue(state["reconciliation_required"])
        self.assertTrue(state["emergency"]["active"])

    def test_malformed_position_does_not_clear_stale_unknown(self):
        state = initial_state()
        state.update({"execution_state": "UNKNOWN", "reconciliation_required": True})
        state["emergency"].update({"active": True, "reason": "RECONCILIATION_FAILURE"})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(time_offset_ms=0)

        with patch.object(v25_execution, "client_for_with_credentials", return_value=client), \
                patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=complete_reconciliation_snapshot(positions=[{}]))), \
                patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
                patch.object(v25_execution, "persist_state"):
            with self.assertRaises(KeyError):
                asyncio.run(v25_execution.reconcile(application, credentials=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")))

        self.assertEqual(state["execution_state"], "UNKNOWN")
        self.assertTrue(state["reconciliation_required"])
        self.assertTrue(state["emergency"]["active"])

    def test_malformed_algo_order_does_not_clear_stale_unknown(self):
        state = initial_state()
        state.update({"execution_state": "UNKNOWN", "reconciliation_required": True})
        state["emergency"].update({"active": True, "reason": "RECONCILIATION_FAILURE"})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(time_offset_ms=0)

        with patch.object(v25_execution, "client_for_with_credentials", return_value=client), \
                patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=complete_reconciliation_snapshot(open_algo_orders=[{}], algo_orders_quality="VALID_ORDERS"))), \
                patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
                patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application, credentials=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")))

        self.assertEqual(state["execution_state"], "UNKNOWN")
        self.assertTrue(state["reconciliation_required"])
        self.assertTrue(state["emergency"]["active"])

    def test_invalid_or_missing_wallet_balance_does_not_clear_stale_unknown(self):
        for overrides in ({"wallet_balance": None}, {"wallet_balance": "100.0"}, {"available_balance": None}):
            with self.subTest(overrides=overrides):
                state = initial_state()
                state.update({"execution_state": "UNKNOWN", "reconciliation_required": True})
                state["emergency"].update({"active": True, "reason": "RECONCILIATION_FAILURE"})
                application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
                client = SimpleNamespace(time_offset_ms=0)

                with patch.object(v25_execution, "client_for_with_credentials", return_value=client), \
                        patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=complete_reconciliation_snapshot(**overrides))), \
                        patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
                        patch.object(v25_execution, "persist_state"):
                    asyncio.run(v25_execution.reconcile(application, credentials=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")))

                self.assertEqual(state["execution_state"], "UNKNOWN")
                self.assertTrue(state["reconciliation_required"])
                self.assertTrue(state["emergency"]["active"])

    def test_open_v25_client_order_identity_does_not_clear_stale_unknown(self):
        state = initial_state()
        state.update({"execution_state": "UNKNOWN", "reconciliation_required": True})
        state["emergency"].update({"active": True, "reason": "RECONCILIATION_FAILURE"})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(time_offset_ms=0)

        with patch.object(v25_execution, "client_for_with_credentials", return_value=client), \
                patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=complete_reconciliation_snapshot(open_orders=[{"clientOrderId": "PTBLV_ENTRY_unresolved"}]))), \
                patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
                patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application, credentials=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")))

        self.assertEqual(state["execution_state"], "UNKNOWN")
        self.assertTrue(state["reconciliation_required"])
        self.assertTrue(state["emergency"]["active"])

    def test_manual_emergency_is_never_cleared_by_clean_reconciliation(self):
        state = initial_state()
        state.update({"execution_state": "UNKNOWN", "reconciliation_required": True})
        state["emergency"].update({"active": True, "reason": "MANUAL_EMERGENCY_STOP"})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(time_offset_ms=0)

        with patch.object(v25_execution, "client_for_with_credentials", return_value=client), \
                patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=complete_reconciliation_snapshot())), \
                patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
                patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application, credentials=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")))

        self.assertEqual(state["execution_state"], "UNKNOWN")
        self.assertTrue(state["reconciliation_required"])
        self.assertTrue(state["emergency"]["active"])
        self.assertFalse(state["live_auto_trade"])
        self.assertEqual(state["armed_until"], 0.0)

    def test_exact_orphan_recovery_keeps_unknown_order_reconciled_behavior(self):
        state = initial_state()
        state.update({"execution_state": "UNKNOWN", "reconciliation_required": True})
        state["emergency"].update({"active": True, "reason": "RECONCILIATION_FAILURE"})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(time_offset_ms=0)

        with patch.object(v25_execution, "client_for_with_credentials", return_value=client), \
                patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=complete_reconciliation_snapshot())), \
                patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=1)), \
                patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application, credentials=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")))

        self.assertEqual(state["execution_state"], "LOCKED")
        self.assertFalse(state["reconciliation_required"])
        self.assertFalse(state["emergency"]["active"])
        self.assertEqual(state["emergency"]["reason"], "UNKNOWN_ORDER_RECONCILED")
        self.assertTrue(any(event["kind"] == "UNKNOWN_ORDER_RECONCILED" for event in state["events"]))
        self.assertFalse(any(event["kind"] == "RECONCILIATION_CLEAN" for event in state["events"]))

    def test_credential_resolution_failure_records_stage(self):
        state = initial_state()
        state["recovery_loaded"] = True
        state["lock"] = asyncio.Lock()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=None))

        async def fail_credentials(*args, **kwargs):
            raise RuntimeError("credential lookup failed")

        async def stop_after_failure(_seconds):
            raise asyncio.CancelledError

        with patch.multiple(
            v25_execution,
            auto_session_credentials=fail_credentials,
            persist_state=lambda current: None,
            automation_telemetry=lambda *args, **kwargs: None,
        ), patch.object(v25_execution.asyncio, "sleep", new=stop_after_failure):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(v25_execution.execution_loop(application))

        diagnostic = next(event for event in state["events"] if event["kind"] == "RECONCILIATION_FAILURE_DIAGNOSTIC")
        self.assertEqual(diagnostic["reconciliation_stage"], "credential_resolution")

    def test_automatic_cycle_failure_records_stage(self):
        state = initial_state()
        state["recovery_loaded"] = True
        state["lock"] = asyncio.Lock()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=None))

        async def fail_cycle(*args, **kwargs):
            raise RuntimeError("synthetic automatic cycle failure")

        async def stop_after_failure(_seconds):
            raise asyncio.CancelledError

        with patch.multiple(
            v25_execution,
            auto_session_credentials=AsyncMock(return_value=("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")),
            reconcile=AsyncMock(),
            automatic_cycle=fail_cycle,
            persist_state=lambda current: None,
            automation_telemetry=lambda *args, **kwargs: None,
        ), patch.object(v25_execution.asyncio, "sleep", new=stop_after_failure):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(v25_execution.execution_loop(application))

        diagnostic = next(event for event in state["events"] if event["kind"] == "RECONCILIATION_FAILURE_DIAGNOSTIC")
        self.assertEqual(diagnostic["reconciliation_stage"], "automatic_cycle")

    def test_transient_reconciliation_recovery_reopens_mocked_auto_order_path(self):
        state = initial_state()
        state.update({
            "recovery_ready": True,
            "connected": True,
            "snapshot": complete_reconciliation_snapshot(),
        })
        state["auto"].update({"enabled": True, "session_until": time.time() + 300, "last_scan": None})
        state["live_auto_trade"] = True
        state["lock"] = asyncio.Lock()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=None))
        state["_app"] = application
        session_until_before_failure = state["auto"]["session_until"]
        v25_execution.lock_reconciliation_failure(state)

        self.assertTrue(v25_execution.clear_clean_reconciliation_state(state, complete_reconciliation_snapshot()))
        self.assertTrue(any(event["kind"] == "AUTO_RECOVERED_FROM_TRANSIENT_RECONCILIATION" for event in state["events"]))
        self.assertFalse(state["real_trading_locked"])
        self.assertEqual(state["auto"]["session_until"], session_until_before_failure)

        class FakeClient:
            time_offset_ms = 0

        snapshot = complete_reconciliation_snapshot()
        snapshot["multi_assets_mode"] = True
        candles = [{"time": index, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000} for index in range(220)]
        spec = {
            "symbol": "BTCUSDT", "direction": "LONG", "side": "BUY", "close_side": "SELL", "order_type": "MARKET",
            "margin_usdt": 25.0, "leverage": 2, "notional_usdt": 50.0, "quantity": "0.500",
            "entry_price": "100.00", "stop_loss": "98.00", "targets": ["102.00", "104.00", "106.00"],
            "step": Decimal("0.001"), "min_qty": Decimal("0.001"), "estimated_stop_loss_usdt": 1.0,
        }
        analysis = {"direction": "LONG", "confidence": 90, "radar": {"trap_score": 1}, "entry": 100, "stop_loss": 98, "tp1": 102, "tp2": 104, "tp3": 106}
        ready = {"ready": True, "score": 100, "gates": []}
        submit = AsyncMock(return_value={"status": "NEW", "orderId": 12345})
        isolated_margin = AsyncMock()
        with patch.multiple(
            v25_execution,
            client_for_with_credentials=lambda application, credentials, request=None: FakeClient(),
            account_snapshot=AsyncMock(return_value=snapshot),
            scan_market_candidates=AsyncMock(return_value=[{"symbol": "BTCUSDT", "opportunity_score": 99}]),
            live_candles=AsyncMock(return_value=(candles, 123)),
            canonical_live_decision=AsyncMock(return_value={"decision": "BUY", "entry_eligible": True, "analysis": analysis}),
            spread_bps=AsyncMock(return_value=1.0),
            readiness_for=lambda *args, **kwargs: ready,
            auto_session_credentials=AsyncMock(return_value=("TEST_KEY_PLACEHOLDER", "TEST_SECRET_PLACEHOLDER")),
            evaluate_entry_gates=lambda **kwargs: {"passed": True, "gates": []},
            build_live_spec=AsyncMock(return_value=spec),
            set_live_isolated_margin=isolated_margin,
            apply_live_verified_leverage=AsyncMock(return_value={"applied_leverage": 2, "margin_type": "isolated"}),
            submit_entry=submit,
            install_protection=AsyncMock(),
            persist_state=lambda current: None,
        ):
            asyncio.run(v25_execution.automatic_cycle(application, credentials=("TEST_KEY_PLACEHOLDER", "TEST_SECRET_PLACEHOLDER")))

        submit.assert_awaited_once()
        isolated_margin.assert_not_awaited()
        self.assertEqual(state["auto"]["last_scan_stats"]["executed_symbols"], ["BTCUSDT"])

    def test_auto_trade_dry_run_reaches_submit_boundary_without_exchange_mutation(self):
        class FakeTransport:
            def __init__(self):
                self.calls = []
                self.submit_boundary_calls = 0
                self.real_exchange_requests = 0
                self.real_orders = 0
                self.real_positions = 0
                self.real_protection_orders = 0
                self.testnet_mutations = 0
                self.production_mutations = 0

            async def signed(self, method, path, params=None):
                self.calls.append((method, path, dict(params or {})))
                if method == "POST" and path == "/fapi/v1/order":
                    self.submit_boundary_calls += 1
                    return {"status": "DRY_RUN_ACCEPTED", "dry_run": True, "orderId": 777001}
                if method == "GET" and path == "/fapi/v1/order":
                    return {}
                return {}

        async def fake_install_protection(client, state, plan):
            state["dry_run_protection_checks"] = int(state.get("dry_run_protection_checks") or 0) + 1
            plan["dry_run_protection_ready"] = True

        state = initial_state()
        state["policy"]["max_leverage"] = 2
        state.update({
            "recovery_ready": True,
            "connected": True,
            "real_trading_locked": False,
            "live_auto_trade": True,
            "armed_until": time.time() + 300,
        })
        state["auto"].update({"enabled": True, "session_until": time.time() + 300, "last_scan": None})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=None))
        state["_app"] = application
        state["lock"] = asyncio.Lock()
        transport = FakeTransport()
        snapshot = {"available_balance": 1000, "positions": [], "open_orders": [], "hedge_mode": False, "unrealized_pnl": 0}
        spec = {
            "symbol": "BTCUSDT", "direction": "LONG", "side": "BUY", "close_side": "SELL", "order_type": "MARKET",
            "margin_usdt": 25.0, "leverage": 2, "notional_usdt": 50.0, "quantity": "0.500",
            "entry_price": "100.00", "stop_loss": "98.00", "targets": ["102.00", "104.00", "106.00"],
            "step": Decimal("0.001"), "min_qty": Decimal("0.001"), "estimated_stop_loss_usdt": 1.0,
        }
        candles = [{"time": index, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000} for index in range(220)]
        analysis = {"direction": "LONG", "confidence": 90, "radar": {"trap_score": 1}, "entry": 100, "stop_loss": 98, "tp1": 102, "tp2": 104, "tp3": 106}
        ready = {"ready": True, "score": 100, "gates": []}
        with patch.multiple(
            v25_execution,
            client_for=lambda application, request=None: transport,
            account_snapshot=AsyncMock(return_value=snapshot),
            scan_market_candidates=AsyncMock(return_value=[{"symbol": "BTCUSDT", "opportunity_score": 99}]),
            live_candles=AsyncMock(return_value=(candles, 123)),
            canonical_live_decision=AsyncMock(return_value={"decision": "BUY", "entry_eligible": True, "analysis": analysis}),
            spread_bps=AsyncMock(return_value=1.0),
            readiness=lambda application, state, request=None: ready,
            auto_session_credentials=AsyncMock(return_value=("TEST_KEY_PLACEHOLDER", "TEST_SECRET_PLACEHOLDER")),
            build_live_spec=AsyncMock(return_value=spec),
            set_live_isolated_margin=AsyncMock(),
            apply_live_verified_leverage=AsyncMock(return_value={"applied_leverage": 2, "margin_type": "isolated"}),
            install_protection=fake_install_protection,
            persist_state=lambda state: None,
        ):
            asyncio.run(v25_execution.automatic_cycle(application))
            state["auto"]["last_scan"] = None
            asyncio.run(v25_execution.automatic_cycle(application))

        audit_events = [item for item in state["events"] if item["kind"] == "LIVE_DECISION_AUDIT"]
        self.assertTrue(audit_events, state["auto"])
        audit = audit_events[0]["audit_snapshot"]
        self.assertEqual(transport.submit_boundary_calls, 1)
        self.assertEqual(transport.real_exchange_requests, 0)
        self.assertEqual(transport.real_orders, 0)
        self.assertEqual(transport.real_positions, 0)
        self.assertEqual(transport.real_protection_orders, 0)
        self.assertEqual(transport.testnet_mutations, 0)
        self.assertEqual(transport.production_mutations, 0)
        self.assertEqual(state.get("dry_run_protection_checks"), 1)
        self.assertEqual(state["auto"]["last_scan_stats"]["executed_symbols_count"], 0)
        rejection_counts = state["auto"]["last_scan_stats"]["rejection_reason_counts"]
        self.assertEqual(set(rejection_counts), {"signal_wait_or_invalid", "entry_ineligible", "stop_distance", "spread"})
        self.assertEqual(state["auto"]["last_scan_stats"]["rejection_reason_breakdown"], {
            "signal_wait_or_invalid": {},
            "entry_ineligible": {},
            "stop_distance": {},
            "spread": {},
        })
        self.assertEqual(state["auto"]["last_scan_stats"]["signal_thresholds"]["min_confidence"], state["policy"]["min_confidence"])
        self.assertEqual(state["auto"]["last_scan_stats"]["signal_thresholds"]["max_stop_distance_pct"], state["policy"]["max_stop_distance_pct"])
        self.assertEqual(audit["symbol"], "BTCUSDT")
        self.assertEqual(audit["side"], "BUY")
        self.assertEqual(audit["position_side"], "BOTH")
        self.assertEqual(audit["quantity"], "0.500")
        self.assertEqual(audit["exposure_usdt"], 50.0)
        self.assertFalse(audit["active_plan_conflict"])
        self.assertTrue(audit["protection_readiness"])
        self.assertFalse(audit["lock_state"])
        self.assertFalse(any("secret" in str(key).lower() or "token" in str(key).lower() for key in audit))

    def test_confidence_rejections_expose_exact_scores_and_distribution(self):
        state = initial_state()
        state.update({"recovery_ready": True, "connected": True, "snapshot": {"available_balance": 100.0, "positions": [], "open_orders": [], "hedge_mode": False}})
        state["real_trading_locked"] = False
        state["live_auto_trade"] = True
        state["armed_until"] = time.time() + 300
        state["auto"].update({"enabled": True, "session_until": time.time() + 300, "last_scan": None})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=None))
        state["_app"] = application
        symbols_and_confidence = {"AAAUSDT": 72.5, "BBBUSDT": 77.5, "CCCUSDT": 83.0}
        candles = [{"time": index, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000} for index in range(220)]

        async def canonical_for_symbol(_application, _client, symbol, _interval, _candles, _policy):
            return {
                "decision": "WAIT",
                "entry_eligible": False,
                "analysis": {
                    "direction": "LONG",
                    "confidence": symbols_and_confidence[symbol],
                    "radar": {"trap_score": 1, "breakout_quality": 100},
                },
                "mtf": {"higher_timeframe_confirmation": True},
            }

        with patch.multiple(
            v25_execution,
            client_for_with_credentials=lambda *args, **kwargs: SimpleNamespace(last_scan_eligible_count=3),
            account_snapshot=AsyncMock(return_value=state["snapshot"]),
            scan_market_candidates=AsyncMock(return_value=[{"symbol": symbol, "opportunity_score": 99} for symbol in symbols_and_confidence]),
            live_candles=AsyncMock(return_value=(candles, 123)),
            canonical_live_decision=canonical_for_symbol,
            readiness_for=lambda *args, **kwargs: {"ready": True},
            auto_session_credentials=AsyncMock(return_value=("TEST_KEY_PLACEHOLDER", "TEST_SECRET_PLACEHOLDER")),
            persist_state=lambda current: None,
        ):
            asyncio.run(v25_execution.automatic_cycle(application, credentials=("TEST_KEY_PLACEHOLDER", "TEST_SECRET_PLACEHOLDER")))

        stats = state["auto"]["last_scan_stats"]
        self.assertEqual(stats["confidence_below_min_scores"], [
            {"symbol": "AAAUSDT", "confidence": 72.5, "threshold": 80.0},
            {"symbol": "BBBUSDT", "confidence": 77.5, "threshold": 80.0},
        ])
        self.assertEqual(stats["confidence_below_min_distribution"], {
            "below_70": 0,
            "70-75": 1,
            "75-80": 1,
            "80-86": 0,
        })
        self.assertEqual(state["auto"]["confidence_rejection_history"][-1]["cycle"], 1)

    def test_mock_100_symbol_universe_ranks_unique_top_three_without_btc_fallback(self):
        symbols = [f"COIN{index}USDT" for index in range(120)]
        symbols[0] = "BTCUSDT"
        exchange_info = {"symbols": [
            {"symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"}
            for symbol in symbols
        ]}
        tickers = [{
            "symbol": symbol, "quoteVolume": str(10_000_000 + index * 100_000),
            "priceChangePercent": str(1 + index / 100), "lastPrice": "10",
        } for index, symbol in enumerate(symbols)]
        tickers[0]["quoteVolume"] = "100"
        tickers.extend([
            {"symbol": "COINUPUSDT", "quoteVolume": "5000000", "priceChangePercent": "2", "lastPrice": "10"},
            {"symbol": "COINDOWNUSDT", "quoteVolume": "5000000", "priceChangePercent": "2", "lastPrice": "10"},
        ])
        ranked = rank_market_tickers(exchange_info, tickers)
        self.assertEqual(len(ranked), 50)
        top_three = [item["symbol"] for item in ranked[:3]]
        self.assertEqual(len(top_three), len(set(top_three)))
        self.assertNotIn("BTCUSDT", top_three)
        self.assertNotIn("COINUPUSDT", [item["symbol"] for item in ranked])
        self.assertNotIn("COINDOWNUSDT", [item["symbol"] for item in ranked])

    def test_live_transport_is_separate_and_official_host_allowlisted(self):
        self.assertIn('LIVE_REST_BASE = "https://fapi.binance.com"', EXECUTION_SOURCE)
        self.assertIn('LIVE_WS_BASE = "wss://fstream.binance.com/private"', EXECUTION_SOURCE)
        self.assertIn("PRIVATE_PATHS", EXECUTION_SOURCE)
        for forbidden in ("withdraw", "deposit", "transfer"):
            self.assertNotIn(f'/sapi/v1/{forbidden}', EXECUTION_SOURCE.casefold())

    def test_unknown_execution_is_queried_not_blindly_retried(self):
        self.assertIn("unknown_execution", EXECUTION_SOURCE)
        self.assertIn("origClientOrderId", EXECUTION_SOURCE)
        self.assertIn("find_order", EXECUTION_SOURCE)
        self.assertNotIn("for attempt in", EXECUTION_SOURCE)

    def test_live_order_timeout_and_unknown_state_fail_closed(self):
        self.assertIn("except httpx.TimeoutException", EXECUTION_SOURCE)
        self.assertIn("unknown_execution=True", EXECUTION_SOURCE)
        self.assertIn('"UNKNOWN_ORDER_STATE"', EXECUTION_SOURCE)
        self.assertIn('state["real_trading_locked"] = True', EXECUTION_SOURCE)
        self.assertIn('manuel uzlaştırma gerekiyor', EXECUTION_SOURCE)

    def test_live_order_has_immutable_audit_and_hard_pre_submit_controls(self):
        self.assertIn("MappingProxyType", EXECUTION_SOURCE)
        self.assertIn('"mode": "LIVE"', EXECUTION_SOURCE)
        self.assertIn('"estimated_notional_usdt"', EXECUTION_SOURCE)
        self.assertIn('"risk_amount_usdt"', EXECUTION_SOURCE)
        self.assertIn("active_plans=list(state.get(\"plans\", {}).values())", EXECUTION_SOURCE)
        self.assertIn("candidate_notional_usdt=body.margin_usdt * body.leverage", EXECUTION_SOURCE)
        self.assertIn('filters.get("MIN_NOTIONAL", {}) or filters.get("NOTIONAL", {})', EXECUTION_SOURCE)
        self.assertIn('row.get("contractType") != "PERPETUAL"', EXECUTION_SOURCE)

    def test_protection_readiness_is_required_before_live_submission(self):
        spec = {"direction": "LONG", "entry_price": "100", "stop_loss": "98", "targets": ["102", "104", "106"], "quantity": "0.1"}
        validate_protection_readiness(spec, sanitize_execution_policy({}))

    def test_invalid_protection_values_fail_closed_before_live_submission(self):
        invalid_specs = [
            {"direction": "LONG", "entry_price": "NaN", "stop_loss": "98", "targets": ["102", "104", "106"], "quantity": "0.1"},
            {"direction": "LONG", "entry_price": "100", "stop_loss": "101", "targets": ["102", "104", "106"], "quantity": "0.1"},
        ]
        for spec in invalid_specs:
            with self.assertRaises(LiveExchangeError):
                validate_protection_readiness(spec, sanitize_execution_policy({}))

    def test_unexpected_live_exception_halts_auto_trade(self):
        self.assertIn('state["live_auto_trade"] = False', EXECUTION_SOURCE)
        self.assertIn('lock_live_execution(state, "AUTO_EXCEPTION")', EXECUTION_SOURCE)

    def test_consecutive_loss_gate_blocks_at_limit_and_resets_after_profit(self):
        events = [
            {"kind": "LIVE_POSITION_CLOSED", "realized_pnl": -1, "created_at": "2026-09-12T10:00:00+00:00"},
            {"kind": "LIVE_POSITION_CLOSED", "realized_pnl": -1, "created_at": "2026-09-12T11:00:00+00:00"},
            {"kind": "LIVE_POSITION_CLOSED", "realized_pnl": -1, "created_at": "2026-09-12T12:00:00+00:00"},
        ]
        metrics = daily_execution_metrics(events, datetime(2026, 9, 12, 13, tzinfo=timezone.utc))
        self.assertEqual(metrics["consecutive_losses"], 3)
        policy = sanitize_execution_policy({"consecutive_loss_limit": 3})
        kwargs = dict(symbol="BTCUSDT", signal={"direction": "LONG", "confidence": 100, "radar": {"trap_score": 1}}, snapshot={"positions": [], "open_orders": [], "hedge_mode": False}, policy=policy, spread_bps=1, armed=True, allowed_symbols=["BTCUSDT"])
        blocked = evaluate_entry_gates(daily=metrics, **kwargs)
        self.assertFalse(blocked["passed"])
        reset_metrics = daily_execution_metrics(events + [{"kind": "LIVE_POSITION_CLOSED", "realized_pnl": 5, "created_at": "2026-09-12T14:00:00+00:00"}], datetime(2026, 9, 12, 15, tzinfo=timezone.utc))
        self.assertEqual(reset_metrics["consecutive_losses"], 0)
        self.assertTrue(evaluate_entry_gates(daily=reset_metrics, **kwargs)["passed"])

    def test_v25_recovery_restores_snapshot_and_missing_db_fails_closed(self):
        from app.v25_execution import restore_v25_state

        class Pool:
            async def fetchrow(self, query, *args):
                return {"payload": {"plans": {"plan-a": {"status": "DOLUM BEKLİYOR"}}, "intents": {}}}

        application = SimpleNamespace(state=SimpleNamespace(db_pool=Pool(), v25_execution={"plans": {}, "intents": {}, "policy": {}, "events": [], "recovery_ready": False, "recovery_error": "pending"}))
        self.assertTrue(asyncio.run(restore_v25_state(application)))
        self.assertTrue(application.state.v25_execution["recovery_loaded"])
        self.assertFalse(application.state.v25_execution["recovery_ready"])
        self.assertIn("plan-a", application.state.v25_execution["plans"])
        unavailable = SimpleNamespace(state=SimpleNamespace(db_pool=None, v25_execution={"recovery_ready": False, "recovery_error": None}))
        self.assertFalse(asyncio.run(restore_v25_state(unavailable)))
        self.assertFalse(unavailable.state.v25_execution["recovery_ready"])

    def test_v25_recovery_failure_and_missing_snapshot_do_not_authorize_entries(self):
        from app.v25_execution import execute_live_order, restore_v25_state
        from fastapi import HTTPException

        class MissingPool:
            async def fetchrow(self, query, *args):
                return None

        missing = SimpleNamespace(state=SimpleNamespace(db_pool=MissingPool(), v25_execution={"plans": {}, "intents": {}, "policy": {}, "events": [], "recovery_ready": False, "recovery_loaded": False, "recovery_error": None}))
        self.assertTrue(asyncio.run(restore_v25_state(missing)))
        self.assertTrue(missing.state.v25_execution["recovery_loaded"])
        with self.assertRaises(HTTPException):
            asyncio.run(execute_live_order(missing, SimpleNamespace(), source="V25_AUTO"))

        class FailedPool:
            async def fetchrow(self, query, *args):
                raise RuntimeError("database unavailable")

        failed = SimpleNamespace(state=SimpleNamespace(db_pool=FailedPool(), v25_execution={"recovery_ready": False, "recovery_loaded": False, "recovery_error": None}))
        self.assertFalse(asyncio.run(restore_v25_state(failed)))
        self.assertFalse(failed.state.v25_execution["recovery_loaded"])

    def test_live_decision_adapter_uses_canonical_closed_candle_contract(self):
        from app.v25_execution import canonical_live_decision

        candles = [{"time": index * 900, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1} for index in range(220)]
        app = SimpleNamespace(state=SimpleNamespace())
        from app import main as app_main
        with patch("app.v25_execution.live_candles", new=AsyncMock(side_effect=[(candles, 1), (candles, 1)])), patch.object(app_main, "canonical_historical_decision", return_value={"decision": "WAIT"}) as canonical:
            import asyncio
            result = asyncio.run(canonical_live_decision(app, object(), "BTCUSDT", "15m", candles))
        self.assertEqual(result["decision"], "WAIT")
        canonical.assert_called_once()

    def _v25_alignment_frames(self, decision_time: int, *, one_hour_close: bool = False, four_hour_close: bool = False):
        def row(timestamp, close=100.0):
            return {"time": timestamp, "open": 100.0, "high": max(101.0, close), "low": min(99.0, close), "close": close, "volume": 1000.0}

        primary = [row(decision_time - (220 - index) * 900) for index in range(221)]
        one_hour_open = decision_time - (3600 if one_hour_close else 900)
        four_hour_open = decision_time - (14400 if four_hour_close else 7200)
        one_hour = [row(one_hour_open - (50 - index) * 3600) for index in range(50)] + [row(one_hour_open)]
        four_hour = [row(four_hour_open - (50 - index) * 14400) for index in range(50)] + [row(four_hour_open)]
        return primary, one_hour, four_hour

    def test_v25_adapter_ignores_forming_one_hour_candle(self):
        from app.v25_execution import canonical_live_decision
        from app import main as app_main

        decision_time = 10 * 3600 + 15 * 60
        primary, one_hour, four_hour = self._v25_alignment_frames(decision_time)
        changed_one_hour = [*one_hour[:-1], {**one_hour[-1], "close": 9999.0, "high": 10000.0, "low": 100.0}]
        analysis = {"direction": "LONG", "confidence": 80, "trend": "LONG", "radar": {"trap_score": 10, "breakout_quality": 80, "trap_level": "LOW"}, "entry": 100, "stop_loss": 99, "tp1": 102}
        async def candles(client, symbol, interval, limit=260):
            return ({"15m": primary, "1h": one_hour, "4h": four_hour}[interval], 1)
        with patch.object(app_main, "analyze", return_value=analysis), patch("app.v25_execution.live_candles", new=candles):
            first = asyncio.run(canonical_live_decision(SimpleNamespace(state=SimpleNamespace()), object(), "BTCUSDT", "15m", primary))
        async def changed_candles(client, symbol, interval, limit=260):
            return ({"15m": primary, "1h": changed_one_hour if interval == "1h" else one_hour, "4h": four_hour}[interval], 1)
        with patch.object(app_main, "analyze", return_value=analysis), patch("app.v25_execution.live_candles", new=changed_candles):
            second = asyncio.run(canonical_live_decision(SimpleNamespace(state=SimpleNamespace()), object(), "BTCUSDT", "15m", primary))
        self.assertEqual(first, second)

    def test_v25_adapter_accepts_exact_one_hour_close_boundary(self):
        from app.v25_execution import canonical_live_decision
        from app import main as app_main

        decision_time = 11 * 3600
        primary, one_hour, four_hour = self._v25_alignment_frames(decision_time, one_hour_close=True)
        analysis = {"direction": "LONG", "confidence": 80, "trend": "LONG", "radar": {"trap_score": 10, "breakout_quality": 80, "trap_level": "LOW"}, "entry": 100, "stop_loss": 99, "tp1": 102}
        async def candles(client, symbol, interval, limit=260):
            return ({"1h": one_hour, "4h": four_hour}[interval], 1)
        with patch.object(app_main, "analyze", return_value=analysis), patch("app.v25_execution.live_candles", new=candles):
            result = asyncio.run(canonical_live_decision(SimpleNamespace(state=SimpleNamespace()), object(), "BTCUSDT", "15m", primary))
        self.assertEqual(result["latest_closed_timestamps"]["1h"], one_hour[-1]["time"])

    def test_v25_adapter_ignores_forming_four_hour_candle(self):
        from app.v25_execution import canonical_live_decision
        from app import main as app_main

        decision_time = 10 * 3600 + 15 * 60
        primary, one_hour, four_hour = self._v25_alignment_frames(decision_time)
        changed_four_hour = [*four_hour[:-1], {**four_hour[-1], "close": 9999.0, "high": 10000.0, "low": 100.0}]
        analysis = {"direction": "LONG", "confidence": 80, "trend": "LONG", "radar": {"trap_score": 10, "breakout_quality": 80, "trap_level": "LOW"}, "entry": 100, "stop_loss": 99, "tp1": 102}
        async def candles(client, symbol, interval, limit=260):
            return ({"1h": one_hour, "4h": four_hour}[interval], 1)
        with patch.object(app_main, "analyze", return_value=analysis), patch("app.v25_execution.live_candles", new=candles):
            first = asyncio.run(canonical_live_decision(SimpleNamespace(state=SimpleNamespace()), object(), "BTCUSDT", "15m", primary))
        async def changed_candles(client, symbol, interval, limit=260):
            return ({"1h": one_hour, "4h": changed_four_hour if interval == "4h" else four_hour}[interval], 1)
        with patch.object(app_main, "analyze", return_value=analysis), patch("app.v25_execution.live_candles", new=changed_candles):
            second = asyncio.run(canonical_live_decision(SimpleNamespace(state=SimpleNamespace()), object(), "BTCUSDT", "15m", primary))
        self.assertEqual(first, second)

    def test_v25_adapter_accepts_exact_four_hour_close_boundary(self):
        from app.v25_execution import canonical_live_decision
        from app import main as app_main

        decision_time = 12 * 3600
        primary, one_hour, four_hour = self._v25_alignment_frames(decision_time, four_hour_close=True)
        analysis = {"direction": "LONG", "confidence": 80, "trend": "LONG", "radar": {"trap_score": 10, "breakout_quality": 80, "trap_level": "LOW"}, "entry": 100, "stop_loss": 99, "tp1": 102}
        async def candles(client, symbol, interval, limit=260):
            return ({"1h": one_hour, "4h": four_hour}[interval], 1)
        with patch.object(app_main, "analyze", return_value=analysis), patch("app.v25_execution.live_candles", new=candles):
            result = asyncio.run(canonical_live_decision(SimpleNamespace(state=SimpleNamespace()), object(), "BTCUSDT", "15m", primary))
        self.assertEqual(result["latest_closed_timestamps"]["4h"], four_hour[-1]["time"])

    def test_live_decision_adapter_fails_closed_for_non_15m_policy_intervals(self):
        from app.v25_execution import canonical_live_decision
        import asyncio

        app = SimpleNamespace(state=SimpleNamespace())
        for interval in ("1m", "5m", "1h", "4h"):
            result = asyncio.run(canonical_live_decision(app, object(), "BTCUSDT", interval, []))
            self.assertEqual(result["decision"], "WAIT")
            self.assertFalse(result["entry_eligible"])

    def test_crash_window_persists_full_intent_and_recovers_orphan_plan(self):
        self.assertIn('"spec": serializable_spec', EXECUTION_SOURCE)
        self.assertIn("recover_plan_from_intent", EXECUTION_SOURCE)
        self.assertIn("recover_orphan_plans", EXECUTION_SOURCE)
        self.assertIn("await recover_orphan_plans(client, state, positions)", EXECUTION_SOURCE)
        self.assertIn("Persist exchange acceptance before any later API call", EXECUTION_SOURCE)

    def test_private_user_stream_and_verified_pnl_fallback_exist(self):
        self.assertIn("live_user_stream_loop", EXECUTION_SOURCE)
        self.assertIn('("POST", "/fapi/v1/listenKey")', EXECUTION_SOURCE)
        self.assertIn('"/fapi/v1/userTrades"', EXECUTION_SOURCE)
        self.assertIn("LIVE_POSITION_CLOSED_UNVERIFIED", EXECUTION_SOURCE)
        self.assertIn("funding_included", EXECUTION_SOURCE)

    def test_closed_pnl_is_attributed_by_exact_order_identity(self):
        self.assertIn('"/fapi/v1/allOrders"', EXECUTION_SOURCE)
        self.assertIn('"/fapi/v1/allAlgoOrders"', EXECUTION_SOURCE)
        self.assertIn("actualOrderId", EXECUTION_SOURCE)
        self.assertIn("expected_normal_clients", EXECUTION_SOURCE)
        self.assertIn("in known_ids", EXECUTION_SOURCE)

    def test_real_entries_require_readiness_and_short_lived_arm(self):
        self.assertIn("LIVE_ARM_SECONDS = 24 * 60 * 60", EXECUTION_SOURCE)
        self.assertIn('if not is_armed(state)', EXECUTION_SOURCE)
        self.assertIn('if not readiness_for(application, state, credentials=credentials)["ready"]', EXECUTION_SOURCE)
        self.assertIn("30 gün / 100 Demo işlem kanıtı", CORE_SOURCE)
        self.assertIn("LIVE_AUTO_SESSION_SECONDS = 60 * 60", EXECUTION_SOURCE)
        self.assertIn('state["policy"]["scan_seconds"]', EXECUTION_SOURCE)

    def test_automatic_execution_uses_dynamic_top_three_not_btc_policy_defaults(self):
        self.assertIn("DEEP_ANALYSIS_LIMIT = 50", EXECUTION_SOURCE)
        self.assertIn("candidates = await scan_market_candidates(client, snapshot)", EXECUTION_SOURCE)
        self.assertIn("selected = signals[:3]", EXECUTION_SOURCE)
        self.assertIn('"scanned_symbol_count": len(candidates)', EXECUTION_SOURCE)
        self.assertIn('"selected_symbols": scan_stats.get', EXECUTION_SOURCE)
        self.assertIn('"selected_symbols_count": len(selected_symbols)', EXECUTION_SOURCE)
        self.assertIn('"executed_symbols"', EXECUTION_SOURCE)
        self.assertIn('"/fapi/v1/ticker/24hr"', EXECUTION_SOURCE)
        self.assertIn('"executed_symbols_count": scan_stats.get', EXECUTION_SOURCE)
        self.assertNotIn("scan_market_candidates(client, snapshot, state[\"policy\"][\"allowed_symbols\"])", EXECUTION_SOURCE)

    def test_stop_failure_closes_with_reduce_only_and_emergency_is_scoped(self):
        self.assertIn('"reduceOnly": "true"', EXECUTION_SOURCE)
        self.assertIn("STOP BAŞARISIZ · KAPATILIYOR", EXECUTION_SOURCE)
        self.assertIn("startswith(LIVE_CLIENT_PREFIX)", EXECUTION_SOURCE)
        self.assertIn("active_symbols", EXECUTION_SOURCE)
        self.assertIn("cancel_owned_algos_for_symbol", EXECUTION_SOURCE)
        self.assertIn("PROTECTION_CLEANUP", EXECUTION_SOURCE)

    def test_credentials_are_dpapi_only_and_never_browser_inputs(self):
        self.assertIn("LIVE_VAULT_PATH", CREDENTIAL_SOURCE)
        self.assertIn("CryptProtectData", CREDENTIAL_SOURCE)
        lowered = (FRONTEND_SOURCE + ACTIVE_COMMERCIAL_SOURCE).casefold()
        self.assertNotIn("secret_key", lowered)
        self.assertNotIn("api_key", lowered)
        self.assertIn("protrebot-v25-session", ACTIVE_COMMERCIAL_SOURCE)
        self.assertIn("secret_inputs_in_browser", EXECUTION_SOURCE)

    def test_v25_router_and_frontend_center_are_integrated(self):
        self.assertIn('version="25.0.0"', MAIN_SOURCE)
        self.assertIn("httpx.Timeout(30, connect=10, read=30, write=10, pool=30)", MAIN_SOURCE)
        self.assertIn("max_connections=40", MAIN_SOURCE)
        self.assertIn("v25_execution_router", MAIN_SOURCE)
        for route in ('"/connect/read-only"', '"/market/candles"', '"/policy"', '"/order/test"', '"/arm"', '"/order"', '"/auto/start"', '"/emergency"'):
            self.assertIn(route, EXECUTION_SOURCE)
        for label in ("Canlı Kasa & Otonom Emir Merkezi", "Canlı Risk Politikası", "Canlı Yayın Kapısı", "MARKET / LIMIT Emir Bileti", "Canlı Seviye Grafiği", "ACİL DURDUR"):
            self.assertIn(label, FRONTEND_SOURCE)

    def test_active_frontend_loading_has_timeout_error_and_retry_paths(self):
        for source in (ACTIVE_COMMERCIAL_SOURCE, ACTIVE_EXECUTION_SOURCE):
            self.assertIn("API_TIMEOUT_MS = 15000", source)
            self.assertIn("controller.abort()", source)
            self.assertIn("finally { window.clearTimeout(timeout) }", source)
            self.assertIn("TEKRAR DENE", source)
        self.assertIn("setLoadError", ACTIVE_COMMERCIAL_SOURCE)
        self.assertIn("setLoadError", ACTIVE_EXECUTION_SOURCE)
        self.assertIn("V25 Live Guard geçersiz yanıt döndürdü", ACTIVE_EXECUTION_SOURCE)

    def test_active_v25_requests_forward_the_verified_owner_access_header(self):
        self.assertIn("'/api/v22', '/api/v24'", ACTIVE_API_SOURCE)
        self.assertNotIn("'/api/v25'", ACTIVE_API_SOURCE)
        self.assertIn("X-ProTreBot-Owner", ACTIVE_API_SOURCE)
        self.assertIn("ownerAccessToken()", ACTIVE_API_SOURCE)

    def test_first_admin_bootstrap_forwards_owner_access_but_other_v22_routes_do_not(self):
        self.assertIn("path === '/api/v22/bootstrap'", ACTIVE_API_SOURCE)
        self.assertIn("return !['/api/v22', '/api/v24']", ACTIVE_API_SOURCE)
        self.assertIn("X-ProTreBot-Owner':ownerAccessToken()", ACTIVE_COMMERCIAL_SOURCE)

    def test_vercel_build_targets_current_render_api(self):
        self.assertIn('"VITE_API_URL": "https://protrebot.onrender.com"', VERCEL_SOURCE)
        self.assertNotIn("tradebt8.onrender.com", VERCEL_SOURCE)

    def test_render_manifest_matches_production_service(self):
        self.assertIn("name: tradebt15", RENDER_SOURCE)
        self.assertIn("startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT", RENDER_SOURCE)

    def test_manual_live_order_requires_second_explicit_confirmation(self):
        self.assertIn("class ManualLiveOrderRequest", EXECUTION_SOURCE)
        self.assertIn("CANLI EMİR GÖNDER", EXECUTION_SOURCE)
        self.assertIn("confirmation:phrase", FRONTEND_SOURCE)


class V25AutoAuthorizationRaceTests(unittest.TestCase):
    API_KEY = "TEST_KEY_PLACEHOLDER"
    SECRET_KEY = "TEST_SECRET_PLACEHOLDER"
    SESSION_ID = "authorized-session"
    USER_ID = "authorized-user"

    def _context(self):
        credentials = (self.API_KEY, self.SECRET_KEY)
        fingerprint = credential_fingerprint(self.API_KEY)
        expires_at = time.time() + 300
        state = initial_state()
        state.update({
            "recovery_ready": True,
            "real_trading_locked": False,
            "auto_authorization": {
                "session_id": self.SESSION_ID,
                "user_id": self.USER_ID,
                "fingerprint": fingerprint,
                "expires_at_epoch": expires_at,
            },
        })
        state["auto"].update({"enabled": True, "session_until": expires_at})
        state["web_consent"] = {
            "accepted_at": datetime.now(timezone.utc).isoformat(),
            "expires_at_epoch": expires_at,
            "key_fingerprint": fingerprint,
        }
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=None))
        return application, state, credentials

    def _resolve(self, application, result):
        return patch.multiple(
            v25_execution,
            session_credentials_for_identity=AsyncMock(return_value=result),
            readiness=lambda application, state, request=None: {"ready": True},
        )

    def test_a_deactivated_credential_blocks_submission(self):
        application, state, _ = self._context()
        with self._resolve(application, ("", "")):
            result = asyncio.run(v25_execution.fresh_auto_submission_credentials(application, state))
        self.assertEqual(result, ("", ""))

    def test_b_changed_fingerprint_blocks_submission(self):
        application, state, _ = self._context()
        with self._resolve(application, ("DIFFERENT_KEY_PLACEHOLDER", self.SECRET_KEY)):
            result = asyncio.run(v25_execution.fresh_auto_submission_credentials(application, state))
        self.assertEqual(result, ("", ""))

    def test_c_expired_session_blocks_submission(self):
        application, state, credentials = self._context()
        state["auto"]["session_until"] = time.time() - 1
        with self._resolve(application, credentials):
            result = asyncio.run(v25_execution.fresh_auto_submission_credentials(application, state))
        self.assertEqual(result, ("", ""))

    def test_d_expired_auto_authorization_blocks_submission(self):
        application, state, credentials = self._context()
        state["auto_authorization"]["expires_at_epoch"] = time.time() - 1
        with self._resolve(application, credentials):
            result = asyncio.run(v25_execution.fresh_auto_submission_credentials(application, state))
        self.assertEqual(result, ("", ""))

    def test_e_expired_consent_blocks_submission(self):
        application, state, credentials = self._context()
        state["web_consent"]["expires_at_epoch"] = time.time() - 1
        with self._resolve(application, credentials):
            result = asyncio.run(v25_execution.fresh_auto_submission_credentials(application, state))
        self.assertEqual(result, ("", ""))

    def test_f_user_or_session_mismatch_blocks_submission(self):
        application, state, credentials = self._context()
        state["auto_authorization"]["user_id"] = "different-user"
        with self._resolve(application, ("", "")):
            result = asyncio.run(v25_execution.fresh_auto_submission_credentials(application, state))
        self.assertEqual(result, ("", ""))

    def test_g_all_final_validations_allow_mocked_submission_boundary(self):
        application, state, credentials = self._context()
        with self._resolve(application, credentials):
            result = asyncio.run(v25_execution.fresh_auto_submission_credentials(application, state))
        self.assertEqual(result, credentials)

    def test_h_plaintext_credentials_do_not_enter_state_events_or_output(self):
        application, state, credentials = self._context()
        with self._resolve(application, credentials):
            resolved = asyncio.run(v25_execution.fresh_auto_submission_credentials(application, state))
        state_text = json.dumps(state, default=str)
        events_text = json.dumps(state["events"], default=str)
        self.assertEqual(resolved, credentials)
        self.assertNotIn(self.API_KEY, state_text)
        self.assertNotIn(self.SECRET_KEY, state_text)
        self.assertNotIn(self.API_KEY, events_text)
        self.assertNotIn(self.SECRET_KEY, events_text)

    def test_background_credential_refresh_retry_recovers_once(self):
        state = initial_state()
        state["recovery_loaded"] = True
        state["lock"] = asyncio.Lock()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=None))
        credentials = ("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")

        async def stop_after_cycle(_seconds):
            raise asyncio.CancelledError

        resolver = AsyncMock(side_effect=[("", ""), credentials])
        with patch.object(v25_execution, "auto_session_credentials", new=resolver), \
                patch.object(v25_execution, "reconcile", new=AsyncMock()), \
                patch.object(v25_execution, "automatic_cycle", new=AsyncMock()), \
                patch.object(v25_execution.asyncio, "sleep", new=stop_after_cycle):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(v25_execution.execution_loop(application))

        self.assertEqual(resolver.await_count, 2)
        self.assertEqual(resolver.await_args_list[0].kwargs, {})
        self.assertEqual(resolver.await_args_list[1].kwargs, {"force_refresh": True})
        self.assertTrue(any(event["kind"] == "CREDENTIAL_REFRESH_RETRY_SUCCEEDED" for event in state["events"]))

    def test_background_credential_refresh_retry_still_fails_closed(self):
        state = initial_state()
        state["recovery_loaded"] = True
        state["lock"] = asyncio.Lock()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=None))

        async def stop_after_skip(_seconds):
            raise asyncio.CancelledError

        resolver = AsyncMock(return_value=("", ""))
        with patch.object(v25_execution, "auto_session_credentials", new=resolver), \
                patch.object(v25_execution, "reconcile", new=AsyncMock()) as reconcile, \
                patch.object(v25_execution.asyncio, "sleep", new=stop_after_skip):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(v25_execution.execution_loop(application))

        reconcile.assert_not_awaited()
        self.assertEqual(resolver.await_count, 2)
        self.assertEqual(resolver.await_args_list[1].kwargs, {"force_refresh": True})
        self.assertTrue(any(event["kind"] == "CREDENTIAL_REFRESH_RETRY_FAILED" for event in state["events"]))
        self.assertEqual(state["auto"]["last_skip_reason"], "no_credentials")

    def test_automatic_cycle_blocks_new_entries_during_consent_grace(self):
        state = initial_state()
        state["auto"].update({"enabled": True, "session_until": time.time() + 300})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        credentials = ("KEY_PLACEHOLDER", "SECRET_PLACEHOLDER")
        with patch.object(v25_execution, "consent_status", return_value={"active": False, "grace_active": True}), \
                patch.object(v25_execution, "readiness_for") as readiness:
            asyncio.run(v25_execution.automatic_cycle(application, credentials=credentials))

        readiness.assert_not_called()
        self.assertTrue(state["auto"]["enabled"])
        self.assertEqual(state["auto"]["last_skip_reason"], "consent_reauthorization_required")
        self.assertTrue(any(event["kind"] == "LIVE_CONSENT_REAUTH_REQUIRED" for event in state["events"]))

    def test_background_client_preserves_activated_live_credential_pair(self):
        application = SimpleNamespace(state=SimpleNamespace(http=object()))
        client = v25_execution.client_for_with_credentials(
            application,
            (self.API_KEY, self.SECRET_KEY),
        )
        self.assertEqual(client.api_key, self.API_KEY)
        self.assertEqual(client.secret_key, self.SECRET_KEY)


class V25MarginModeCompatibilityTests(unittest.TestCase):
    def test_multi_assets_configuration_accepts_crossed_margin(self):
        result = verify_symbol_configuration(
            [{"symbol": "BTCUSDT", "leverage": 2, "marginType": "CROSSED"}],
            "BTCUSDT",
            2,
            expected_margin_type="CROSSED",
        )
        self.assertEqual(result["margin_type"], "crossed")

    def test_multi_assets_margin_error_is_treated_as_non_applicable(self):
        client = SimpleNamespace(
            signed=AsyncMock(side_effect=LiveExchangeError("multi-assets", exchange_code=-4168))
        )
        result = asyncio.run(v25_execution.set_live_isolated_margin(client, "BTCUSDT"))
        self.assertEqual(result, "CROSSED")

    def test_unknown_margin_error_still_fails_closed(self):
        client = SimpleNamespace(
            signed=AsyncMock(side_effect=LiveExchangeError("unexpected", exchange_code=-2019))
        )
        with self.assertRaises(LiveExchangeError):
            asyncio.run(v25_execution.set_live_isolated_margin(client, "BTCUSDT"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
