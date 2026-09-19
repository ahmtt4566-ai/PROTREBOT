import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import v21_demo  # noqa: E402
from app import binance_demo
from app.binance_demo import MANUAL_MAX_LEVERAGE, MAX_NOTIONAL_USDT, BinanceDemoError, DemoOrderRequest, ensure_one_way_position_mode, recover_pending_entry_intents, validate_entry_risk


ROOT_FRONTEND = Path(__file__).parents[2] / "BinanceDemo.tsx"
ROOT_FRONTEND_CSS = Path(__file__).parents[2] / "binance-demo.css"


class V21ScannerDashboardTests(unittest.TestCase):
    def test_invalid_allowed_symbols_do_not_abort_universe_selection(self):
        settings = v21_demo.initial_state()["settings"]
        settings["allowed_symbols"] = ["BTC/USDT", "BTCUSDT PERPETUAL", "ETHUSDT"]

        self.assertEqual(v21_demo._effective_allowed_symbols(settings), {"BTCUSDT", "ETHUSDT"})

    def test_auto_trade_dashboard_mount_is_visible_outside_legacy_wrapper(self):
        source = ROOT_FRONTEND.read_text(encoding="utf-8")
        css = ROOT_FRONTEND_CSS.read_text(encoding="utf-8")
        self.assertIn('aria-label="Auto Trade Bot"', source)
        self.assertIn(".binanceDemoDeck{display:grid", css)

    def test_manual_leverage_choices_are_bounded_and_default_ui_is_10x(self):
        for leverage in (1, 2, 3, 5, 10, 20, 30, 40, 50):
            body = DemoOrderRequest(symbol="BTCUSDT", direction="LONG", margin_usdt=5, leverage=leverage, stop_loss=99, tp1=101, tp2=102, tp3=103)
            self.assertEqual(body.leverage, leverage)
        with self.assertRaises(ValueError):
            DemoOrderRequest(symbol="BTCUSDT", direction="LONG", margin_usdt=5, leverage=51, stop_loss=99, tp1=101, tp2=102, tp3=103)
        self.assertEqual(MANUAL_MAX_LEVERAGE, 50)
        source = ROOT_FRONTEND.read_text(encoding="utf-8")
        self.assertIn("leverage:'10'", source)
        self.assertIn("['AUTO','1','2','3','5','10','15','20','25','30','40','50','CUSTOM']", source)
        self.assertIn("customLeverage", source)

    def test_manual_long_and_short_leverage_payloads_keep_notional_cap(self):
        settings = dict(v21_demo.DEFAULT_SETTINGS)
        snapshot = {"positions": [], "open_orders": [], "available_balance": 1_000}
        for direction, stop, targets in (("LONG", 99, (101, 102, 103)), ("SHORT", 101, (99, 98, 97))):
            body = DemoOrderRequest(symbol="BTCUSDT", direction=direction, margin_usdt=20, leverage=10, stop_loss=stop, tp1=targets[0], tp2=targets[1], tp3=targets[2])
            validate_entry_risk(snapshot, body, {"notional_usdt": 200, "current_price": 100, "stop_loss": str(stop)}, settings, daily_realized_pnl=0)
        self.assertEqual(float(MAX_NOTIONAL_USDT), 200.0)
        with self.assertRaises(BinanceDemoError):
            validate_entry_risk(snapshot, DemoOrderRequest(symbol="BTCUSDT", direction="LONG", margin_usdt=20, leverage=10, stop_loss=99, tp1=101, tp2=102, tp3=103), {"notional_usdt": 201, "current_price": 100, "stop_loss": "99"}, settings, daily_realized_pnl=0)

    def test_order_desk_uses_real_symbol_source_and_selected_analysis_endpoint(self):
        source = ROOT_FRONTEND.read_text(encoding="utf-8")
        self.assertIn("markets:MarketOption[]", source)
        self.assertIn("onSymbolChange", source)
        self.assertIn("/analysis/${symbol}?interval=15m", source)
        self.assertNotIn("/analysis/BTCUSDT?interval=15m", source)

    def test_root_frontend_arm_uses_user_confirmation(self):
        source = ROOT_FRONTEND.read_text(encoding="utf-8")
        self.assertIn("confirmation:armText", source)
        self.assertIn("demoConfirmationBackdrop", source)
        self.assertNotIn("window.prompt", source)

    def test_performance_aggregation_uses_real_closed_journal_events(self):
        state = v21_demo.initial_state()
        state["journal"] = [
            {"kind": "POSITION_CLOSED", "created_at": "2026-09-01T10:00:00+00:00", "realized_pnl": 12.0, "verified_realized": True},
            {"kind": "FILL", "reduce_only": True, "created_at": "2026-08-30T10:00:00+00:00", "realized_pnl": -4.0, "verified_realized": True},
            {"kind": "AUTO_ORDER", "created_at": "2026-09-01T10:01:00+00:00", "realized_pnl": 999.0},
        ]
        result = v21_demo.performance_payload(state, "all")
        self.assertEqual(result["total_trades"], 2)
        self.assertEqual(result["total_profit"], 12.0)
        self.assertEqual(result["total_loss"], -4.0)
        self.assertEqual(result["net_profit"], 8.0)
        self.assertEqual(result["wins"], 1)
        self.assertEqual(result["losses"], 1)
        self.assertEqual(result["profit_factor"], 3.0)

    def test_unverified_close_and_unrealized_pnl_are_excluded_from_daily_metrics(self):
        state = v21_demo.initial_state()
        current_day = datetime.now(timezone.utc).date().isoformat()
        state["journal"] = [
            {"kind": "POSITION_MISSING", "created_at": f"{current_day}T10:00:00+00:00", "realized_pnl": 99.0},
            {"kind": "FILL", "created_at": f"{current_day}T10:01:00+00:00", "realized_pnl": 4.0, "verified_realized": True},
        ]
        self.assertEqual(v21_demo.daily_metrics(state)["realized_pnl"], 4.0)

    def test_verified_performance_exposes_streaks_equity_and_directional_results(self):
        state = v21_demo.initial_state()
        state["journal"] = [
            {"id":"a", "kind":"FILL", "side":"BUY", "created_at":"2026-09-01T10:00:00+00:00", "realized_pnl":10.0, "verified_realized":True},
            {"id":"b", "kind":"FILL", "side":"SELL", "created_at":"2026-09-01T11:00:00+00:00", "realized_pnl":-4.0, "verified_realized":True},
            {"id":"c", "kind":"FILL", "side":"BUY", "created_at":"2026-09-01T12:00:00+00:00", "realized_pnl":6.0, "verified_realized":True},
        ]
        result = v21_demo.performance_payload(state, "all")
        self.assertEqual(result["average_win"], 8.0)
        self.assertEqual(result["average_loss"], -4.0)
        self.assertEqual(result["winning_streak"], 1)
        self.assertEqual(result["losing_streak"], 1)
        self.assertEqual([point["equity"] for point in result["equity_curve"]], [10.0, 6.0, 12.0])
        self.assertEqual(result["directional"]["LONG"]["trades"], 2)
        self.assertEqual(result["directional"]["SHORT"]["realized_pnl"], -4.0)

    def test_verified_performance_deduplicates_same_event_id(self):
        state = v21_demo.initial_state()
        state["journal"] = [
            {"id":"same", "kind":"FILL", "created_at":"2026-09-01T10:00:00+00:00", "realized_pnl":10.0, "verified_realized":True},
            {"id":"same", "kind":"FILL", "created_at":"2026-09-01T10:00:00+00:00", "realized_pnl":10.0, "verified_realized":True},
        ]
        self.assertEqual(v21_demo.performance_payload(state, "all")["total_trades"], 1)

    def test_stream_close_event_is_idempotent_and_verified_once(self):
        state = v21_demo.initial_state()
        payload = {"e": "ORDER_TRADE_UPDATE", "T": 123, "o": {"s": "BTCUSDT", "X": "FILLED", "x": "TRADE", "i": 7, "S": "SELL", "R": True, "rp": "2.5", "ap": "100"}}
        self.assertTrue(v21_demo.process_stream_event(state, payload))
        self.assertFalse(v21_demo.process_stream_event(state, payload))
        verified = [item for item in state["journal"] if item.get("verified_realized")]
        self.assertEqual(len(verified), 1)
        self.assertEqual(v21_demo.daily_metrics(state)["realized_pnl"], 2.5)

    def test_missing_position_never_records_last_unrealized_pnl(self):
        state = v21_demo.initial_state()
        previous = {"positions": [{"symbol": "BTCUSDT", "direction": "LONG", "mark_price": 100, "quantity": 1, "unrealized_pnl": -40}]}
        self.assertTrue(v21_demo.reconcile_positions(state, None, previous))
        self.assertTrue(v21_demo.reconcile_positions(state, previous, {"positions": []}))
        missing = [item for item in state["journal"] if item["kind"] == "POSITION_MISSING"]
        self.assertEqual(len(missing), 1)
        self.assertIsNone(missing[0].get("realized_pnl"))
        self.assertEqual(v21_demo.daily_metrics(state)["realized_pnl"], 0)

    def test_shared_risk_gate_rejects_excess_loss_and_pending_exposure(self):
        settings = dict(v21_demo.DEFAULT_SETTINGS)
        body = DemoOrderRequest(symbol="BTCUSDT", direction="LONG", margin_usdt=50, leverage=2, stop_loss=90, tp1=105, tp2=110, tp3=115)
        spec = {"notional_usdt": 100, "current_price": 100, "stop_loss": "90"}
        snapshot = {"positions": [], "open_orders": [], "available_balance": 1000}
        with self.assertRaises(BinanceDemoError):
            validate_entry_risk(snapshot, body, spec, settings, daily_realized_pnl=0)
        settings["max_positions"] = 1
        body = DemoOrderRequest(symbol="BTCUSDT", direction="LONG", margin_usdt=5, leverage=1, stop_loss=99, tp1=101, tp2=102, tp3=103)
        spec["notional_usdt"] = 5
        with self.assertRaises(BinanceDemoError):
            validate_entry_risk({"positions": [], "open_orders":[{"symbol":"ETHUSDT","reduce_only":False}], "available_balance":1000}, body, spec, settings, daily_realized_pnl=0)

    def test_minimum_order_size_is_rejected_without_inflating_risk(self):
        sizing = v21_demo.risk_size_values(100, 40, 5, 2, 50)
        self.assertLess(sizing["margin_usdt"], 5)
        source = (BACKEND / "app" / "v21_demo.py").read_text(encoding="utf-8")
        self.assertIn('"MINIMUM_ORDER_SIZE"', source)

    def test_pending_intent_recovers_by_client_order_id_without_new_submission(self):
        state = {"plans": {"intent": {"symbol":"BTCUSDT", "status":"ENTRY_INTENT_PENDING", "entry_client_order_id":"PTB_ENTRY_123"}}}
        class FakeClient:
            async def signed(self, method, path, params=None):
                self.last = (method, path, params)
                if path == "/fapi/v1/order": return {"orderId": 42, "clientOrderId":"PTB_ENTRY_123"}
                raise AssertionError((method, path, params))
        with patch.object(v21_demo, "persist_runtime"):
            changed = asyncio.run(recover_pending_entry_intents(FakeClient(), state))
        self.assertTrue(changed)
        self.assertEqual(state["plans"]["intent"]["entry_order_id"], 42)
        self.assertEqual(state["plans"]["intent"]["status"], "DOLUM BEKLİYOR")

    def test_one_way_mode_does_not_touch_protected_other_symbol_on_failed_transition(self):
        class FakeClient:
            def __init__(self): self.calls = []
            async def signed(self, method, path, params=None):
                self.calls.append((method, path, params))
                if path == "/fapi/v1/positionSide/dual" and method == "GET": return {"dualSidePosition": True}
                if path == "/fapi/v3/positionRisk": return [{"symbol":"ETHUSDT", "positionAmt":"1"}]
                if path == "/fapi/v1/openOrders": return []
                if path == "/fapi/v1/openAlgoOrders": return [{"symbol":"ETHUSDT", "algoId":77, "orderType":"STOP_MARKET"}]
                raise AssertionError((method, path, params))
        client = FakeClient()
        with self.assertRaises(BinanceDemoError):
            asyncio.run(ensure_one_way_position_mode(client))
        self.assertFalse(any(method == "DELETE" for method, _, _ in client.calls))

    def test_protection_failure_is_explicitly_critical_and_retries(self):
        state = {"plans": {"p": {"symbol":"BTCUSDT", "status":"DOLUM BEKLİYOR", "position_status":"PENDING", "stop_loss":"90", "direction":"LONG", "targets":["105","110","115"], "step":"0.001", "min_qty":"0.001"}}}
        plan = state["plans"]["p"]
        class FakeClient:
            async def signed(self, method, path, params=None):
                if path == "/fapi/v3/positionRisk": return [{"symbol":"BTCUSDT", "positionAmt":"1"}]
                raise AssertionError((method, path, params))
        with patch.object(binance_demo, "post_algo", new=AsyncMock(side_effect=BinanceDemoError("stop unavailable"))), \
                patch.object(binance_demo, "close_symbol_position", new=AsyncMock(return_value=None)), \
                patch.object(binance_demo, "persist_runtime"):
            with self.assertRaises(BinanceDemoError):
                asyncio.run(binance_demo.install_protection(FakeClient(), state, plan))
        self.assertEqual(plan["status"], "CRITICAL / UNPROTECTED")
        self.assertEqual(plan["protection_status"], "CRITICAL / UNPROTECTED")
        self.assertEqual(plan["recovery_attempts"], 1)
        self.assertEqual(plan["position_status"], "OPEN")

    def _automation_app(self, candidate):
        state = v21_demo.initial_state()
        state["auto"].update({"enabled": True, "user_confirmed": True})
        state["settings"]["allowed_symbols"] = ["BTCUSDT"]
        app = SimpleNamespace(state=SimpleNamespace(v21_demo=state, binance_demo={}, http=object()))
        return state, app, candidate

    def test_automation_rejects_candidate_outside_allowed_symbols(self):
        candidate = {"symbol": "ETHUSDT", "direction": "LONG", "status": "SELECTED", "entry": 100.0,
                     "stop_loss": 99.0, "tp1": 101.0, "tp2": 102.0, "tp3": 103.0, "score": 95,
                     "opportunity_score": 95, "confidence": "HIGH", "reasons": []}
        state, app, candidate = self._automation_app(candidate)
        state["scanner"]["top_candidates"] = [candidate]
        with patch.object(v21_demo, "armed", return_value=True), \
                patch.object(v21_demo, "client_for", return_value=object()), \
                patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value={"positions": [], "open_orders": []})), \
                patch.object(v21_demo, "scan_demo_universe", new=AsyncMock(return_value=[candidate])), \
                patch.object(v21_demo, "execute_demo_order", new=AsyncMock()) as order, \
                patch.object(v21_demo, "persist_state"):
            asyncio.run(v21_demo.automatic_cycle(app))
        order.assert_not_awaited()
        self.assertEqual(state["auto"]["rejection_gate"], "ALLOWED_SYMBOLS")
        self.assertIn("ETHUSDT", state["auto"]["rejection_reason"])

    def test_automation_reaches_demo_execution_when_all_gates_pass(self):
        candidate = {"symbol": "BTCUSDT", "direction": "LONG", "status": "SELECTED", "entry": 100.0,
                     "stop_loss": 99.0, "tp1": 101.0, "tp2": 102.0, "tp3": 103.0, "score": 95,
                     "opportunity_score": 95, "confidence": "HIGH", "reasons": ["test"]}
        state, app, candidate = self._automation_app(candidate)
        result = {"plan": {"entry_price": 100.0, "targets": [101.0, 102.0, 103.0], "stop_loss": 99.0,
                            "margin_usdt": 5.0, "leverage": 2, "status": "AÇIK"}}
        with patch.object(v21_demo, "armed", return_value=True), \
                patch.object(v21_demo, "client_for", return_value=object()), \
                patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value={"positions": [], "open_orders": []})), \
                patch.object(v21_demo, "scan_demo_universe", new=AsyncMock(return_value=[candidate])), \
                patch.object(v21_demo, "execute_demo_order", new=AsyncMock(return_value=result)) as order, \
                patch.object(v21_demo, "persist_state"):
            asyncio.run(v21_demo.automatic_cycle(app))
        order.assert_awaited_once()
        self.assertEqual(order.await_args.kwargs["source"], "AUTO_SCANNER")
        self.assertEqual(state["auto"]["rejection_reason"], None)

    def test_demo_smoke_test_creates_local_paper_position_without_exchange_order(self):
        state = v21_demo.initial_state()
        state["auto"].update({"enabled": True, "user_confirmed": True})
        state["settings"]["allowed_symbols"] = ["BTCUSDT"]
        app = SimpleNamespace(state=SimpleNamespace(v21_demo=state, binance_demo={}, http=object()))
        request = SimpleNamespace(app=app)
        candidate = {"symbol": "BTCUSDT", "direction": "LONG", "status": "SELECTED", "entry": 100.0,
                     "stop_loss": 99.0, "tp1": 101.0, "tp2": 102.0, "tp3": 103.0, "score": 95,
                     "opportunity_score": 95, "confidence": "HIGH", "reasons": ["test"]}
        state["scanner"]["top_candidates"] = [candidate]
        with patch.object(v21_demo, "credentials_configured", return_value=False), \
                patch.object(v21_demo, "execute_demo_order", new=AsyncMock()) as order, \
                patch.object(v21_demo, "persist_state"):
            result = asyncio.run(v21_demo.v21_demo_smoke_test(request))
        order.assert_not_awaited()
        self.assertEqual(result["position"]["symbol"], "BTCUSDT")
        self.assertEqual(result["position"]["stop_loss"], 99.0)
        self.assertEqual(result["position"]["targets"], [101.0, 102.0, 103.0])
        self.assertEqual(len(state["paper_positions"]), 1)

    def test_scanner_interval_and_candidate_score_contract(self):
        self.assertEqual(v21_demo.SCAN_INTERVAL_SECONDS, 900)
        candidates = v21_demo._enrich_scan_candidates([
            {"symbol": "LOWUSDT", "direction": "BEKLE", "opportunity_score": -10, "confidence": 20},
            {"symbol": "TOPUSDT", "direction": "LONG", "opportunity_score": 130, "confidence": 88,
             "trend": "Güçlü yükseliş", "mtf_trend": "UYUMLU LONG", "volume_ratio": 1.4,
             "rsi": 61, "macd_confirmation": True, "momentum": "Pozitif", "risk_reward": 3},
        ])
        self.assertEqual([item["symbol"] for item in candidates], ["TOPUSDT", "LOWUSDT"])
        self.assertEqual(candidates[0]["score"], 100)
        self.assertIn("MACD confirmation", candidates[0]["reasons"])
        self.assertIn("1h/4h trend: UYUMLU LONG", candidates[0]["reasons"])
        self.assertIn("Volume ortalamanın üzerinde", candidates[0]["reasons"])
        self.assertTrue(candidates[0]["macd_confirmation"])

    def test_summary_exposes_scanner_and_automation_history(self):
        state = v21_demo.initial_state()
        state["scanner"]["selected_symbols"] = ["BTCUSDT"]
        state["scanner"]["scan_duration_ms"] = 1250
        state["automation_trades"] = [{"symbol": "BTCUSDT", "scanner_rank": 1}]
        payload = v21_demo.summary_payload(state)
        self.assertEqual(payload["scanner"]["scan_interval_seconds"], 900)
        self.assertEqual(payload["scanner"]["selected_count"], 1)
        self.assertEqual(payload["scanner"]["scan_duration_seconds"], 1.25)
        self.assertEqual(payload["automation_trades"][0]["scanner_rank"], 1)

    def test_concurrent_manual_scans_are_serialized(self):
        state = v21_demo.initial_state()
        app = SimpleNamespace(state=SimpleNamespace(v21_demo=state))
        started = asyncio.Event()
        release = asyncio.Event()
        scan_calls = 0

        async def scan(_client, _occupied, _settings):
            nonlocal scan_calls
            scan_calls += 1
            started.set()
            await release.wait()
            return [{"symbol": "BTCUSDT", "direction": "LONG", "opportunity_score": 90, "confidence": 90,
                     "trend": "Güçlü yükseliş", "volume_ratio": 1.2, "rsi": 60, "momentum": "Pozitif",
                     "risk_reward": 3, "status": "SELECTED"}]

        async def exercise():
            with patch.object(v21_demo, "credentials_configured", return_value=False), \
                    patch.object(v21_demo, "market_client_for", return_value=object()), \
                    patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value={"positions": [], "open_orders": []})), \
                    patch.object(v21_demo, "scan_demo_universe", new=scan) as scanner, \
                    patch.object(v21_demo, "persist_state"):
                first = asyncio.create_task(v21_demo.run_scanner_cycle(app))
                await started.wait()
                second = asyncio.create_task(v21_demo.run_scanner_cycle(app))
                await asyncio.sleep(0)
                release.set()
                await asyncio.gather(first, second)
                return scan_calls

        scan_call_count = asyncio.run(exercise())
        self.assertEqual(scan_call_count, 1)

    def test_scanner_does_not_start_trade_when_auto_is_disabled(self):
        state = v21_demo.initial_state()
        app = SimpleNamespace(state=SimpleNamespace(v21_demo=state))
        with patch.object(v21_demo, "execute_demo_order", new=AsyncMock()) as order:
            asyncio.run(v21_demo.automatic_cycle(app))
        order.assert_not_awaited()
        self.assertIn("onayıyla", state["auto"]["last_decision"])

    def test_auto_start_triggers_immediate_scan_and_900_second_schedule(self):
        state = v21_demo.initial_state()
        state["settings"]["scan_seconds"] = 30
        app = SimpleNamespace(state=SimpleNamespace(v21_demo=state, binance_demo={}, http=object()))
        request = SimpleNamespace(app=app)

        async def fake_scan(_client, _occupied, _settings):
            return [{
                "symbol": "BTCUSDT", "direction": "LONG", "opportunity_score": 95, "confidence": 89,
                "score": 95, "trend": "Güçlü yükseliş", "volume_ratio": 1.2, "rsi": 62, "momentum": "Pozitif",
                "risk_reward": 3.2, "status": "SELECTED", "entry": 64000.0, "stop_loss": 63680.0,
                "tp1": 64500.0, "tp2": 65000.0, "tp3": 65500.0, "mtf_trend": "UYUMLU LONG",
                "macd_confirmation": True,
            }]

        with patch.object(v21_demo, "client_for", return_value=object()), \
                patch.object(v21_demo, "armed", return_value=True), \
                patch.object(v21_demo, "credentials_configured", return_value=True), \
                patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value={"positions": [], "open_orders": []})), \
                patch.object(v21_demo, "market_client_for", return_value=object()), \
                patch.object(v21_demo, "scan_demo_universe", new=fake_scan), \
                patch.object(v21_demo, "execute_demo_order", new=AsyncMock(return_value={"plan": {"entry_price": 64000.0, "margin_usdt": 50.0, "leverage": 2, "targets": [64500.0, 65000.0, 65500.0], "stop_loss": 63680.0, "status": "AÇIK"}})), \
                patch.object(v21_demo, "persist_state"):
            asyncio.run(v21_demo.v21_auto_start(request, v21_demo.AutoStartRequest(confirmation="DEMO OTOMATİK")))

        self.assertTrue(state["auto"]["enabled"])
        self.assertIsNotNone(state["scanner"]["last_scan_at"])
        self.assertIsNotNone(state["scanner"]["next_scan_at"])
        completion = __import__("datetime").datetime.fromisoformat(state["scanner"]["last_scan_at"].replace("Z", "+00:00"))
        next_scan = __import__("datetime").datetime.fromisoformat(state["scanner"]["next_scan_at"].replace("Z", "+00:00"))
        self.assertEqual((next_scan - completion).total_seconds(), 900)

    def test_scan_candidate_state_updates_when_new_response_arrives(self):
        state = v21_demo.initial_state()
        app = SimpleNamespace(state=SimpleNamespace(v21_demo=state, http=object()))
        first_payload = [{
            "symbol": "SOLUSDT", "direction": "LONG", "score": 91, "opportunity_score": 91, "confidence": 83,
            "trend": "Yükseliş", "volume_ratio": 1.35, "rsi": 58, "momentum": "Pozitif",
            "risk_reward": 2.7, "status": "SELECTED", "entry": 160.0, "stop_loss": 156.0,
            "tp1": 164.0, "tp2": 168.0, "tp3": 172.0, "mtf_trend": "UYUMLU LONG",
            "macd_confirmation": True,
        }]
        second_payload = [{
            "symbol": "ETHUSDT", "direction": "SHORT", "score": 88, "opportunity_score": 88, "confidence": 81,
            "trend": "Düşüş", "volume_ratio": 1.25, "rsi": 42, "momentum": "Negatif",
            "risk_reward": 2.4, "status": "SELECTED", "entry": 2450.0, "stop_loss": 2490.0,
            "tp1": 2410.0, "tp2": 2370.0, "tp3": 2330.0, "mtf_trend": "UYUMLU SHORT",
            "macd_confirmation": True,
        }]
        with patch.object(v21_demo, "credentials_configured", return_value=False), \
                patch.object(v21_demo, "market_client_for", return_value=object()), \
                patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value={"positions": [], "open_orders": []})), \
                patch.object(v21_demo, "scan_demo_universe", new=AsyncMock(side_effect=[first_payload, second_payload])), \
                patch.object(v21_demo, "persist_state"):
            asyncio.run(v21_demo.run_scanner_cycle(app))
            self.assertEqual(state["scanner"]["top_candidates"][0]["symbol"], "SOLUSDT")
            asyncio.run(v21_demo.run_scanner_cycle(app))
        self.assertEqual(state["scanner"]["top_candidates"][0]["symbol"], "ETHUSDT")
        self.assertEqual(state["scanner"]["selected_symbols"][0], "ETHUSDT")
        self.assertNotIn("SOLUSDT", state["scanner"]["selected_symbols"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
