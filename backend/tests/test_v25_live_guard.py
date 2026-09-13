import asyncio
import sys
import unittest
from datetime import datetime, timezone
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
    credential_fingerprint,
    daily_execution_metrics,
    evaluate_entry_gates,
    policy_digest,
    release_gates,
    release_ready,
    risk_sized_order,
    sanitize_execution_policy,
)
from app.v25_execution import rank_market_tickers  # noqa: E402


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


class V25LiveGuardCoreTests(unittest.TestCase):
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

    def test_risk_sizing_respects_loss_margin_and_leverage(self):
        policy = sanitize_execution_policy({"max_margin_per_trade": 25, "max_loss_per_trade": 3, "max_leverage": 2})
        order = risk_sized_order(100, 98, policy)
        self.assertLessEqual(order["margin_usdt"], 25)
        self.assertLessEqual(order["notional_usdt"], 50)
        self.assertLessEqual(order["estimated_stop_loss_usdt"], 3)
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
        ready = release_gates(credentials=True, consent_active=True, connected=True, one_way=True, policy_acknowledged=True, demo_certificate={"status": "DEMO SERTİFİKALI", "score": 100})
        self.assertTrue(release_ready(ready))

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
        first = credential_fingerprint("live-api-key-123456")
        self.assertEqual(first, credential_fingerprint("live-api-key-123456"))
        self.assertNotIn("live-api-key", first or "")

class V25LiveGuardIntegrationContractTests(unittest.TestCase):
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
        self.assertEqual(len(ranked), 100)
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
        self.assertIn("LIVE_ARM_SECONDS = 5 * 60", EXECUTION_SOURCE)
        self.assertIn('if not is_armed(state)', EXECUTION_SOURCE)
        self.assertIn('if not readiness(application, state)["ready"]', EXECUTION_SOURCE)
        self.assertIn("30 gün / 100 Demo işlem kanıtı", CORE_SOURCE)
        self.assertIn("LIVE_AUTO_SESSION_SECONDS = 60 * 60", EXECUTION_SOURCE)
        self.assertIn('state["policy"]["scan_seconds"]', EXECUTION_SOURCE)

    def test_automatic_execution_uses_dynamic_top_three_not_btc_policy_defaults(self):
        self.assertIn("DEEP_ANALYSIS_LIMIT = 100", EXECUTION_SOURCE)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
