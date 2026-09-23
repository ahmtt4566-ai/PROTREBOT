import sys
import time
import io
import logging
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch


BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app.execution_core import HARD_MAX_POSITIONS, evaluate_entry_gates, sanitize_execution_policy  # noqa: E402
from app.v25_execution import Confirmation, automation_telemetry, automatic_cycle, execution_loop, initial_state, public_status, rank_market_tickers, v25_auto_start, v25_auto_stop  # noqa: E402


class MultiSymbolScannerTests(unittest.TestCase):
    def test_scanner_filters_contract_status_assets_liquidity_and_activity(self):
        exchange_info = {"symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "ETHUSDT", "status": "BREAK", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "USDCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "SOLUPUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "BNBUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        ]}
        tickers = [
            {"symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "20000000", "priceChangePercent": "3"},
            {"symbol": "ETHUSDT", "lastPrice": "100", "quoteVolume": "20000000", "priceChangePercent": "3"},
            {"symbol": "USDCUSDT", "lastPrice": "1", "quoteVolume": "20000000", "priceChangePercent": "3"},
            {"symbol": "SOLUPUSDT", "lastPrice": "1", "quoteVolume": "20000000", "priceChangePercent": "3"},
            {"symbol": "BNBUSDT", "lastPrice": "100", "quoteVolume": "100", "priceChangePercent": "3"},
        ]
        result = rank_market_tickers(exchange_info, tickers)
        self.assertEqual([item["symbol"] for item in result], ["BTCUSDT"])

    def test_scanner_selects_top_candidates_and_excludes_occupied_symbols(self):
        symbols = [f"COIN{index}USDT" for index in range(30)]
        exchange_info = {"symbols": [
            {"symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"}
            for symbol in symbols
        ]}
        tickers = [
            {"symbol": symbol, "lastPrice": "100", "quoteVolume": str(1_000_000 + index * 1_000_000), "priceChangePercent": str(index + 1)}
            for index, symbol in enumerate(symbols)
        ]
        result = rank_market_tickers(exchange_info, tickers, candidate_limit=25, excluded_symbols={"COIN29USDT"})
        self.assertEqual(len(result), 25)
        self.assertNotIn("COIN29USDT", {item["symbol"] for item in result})
        self.assertEqual(result[0]["symbol"], "COIN28USDT")

    def test_position_ceiling_is_five_and_gate_blocks_the_sixth(self):
        self.assertEqual(HARD_MAX_POSITIONS, 5)
        policy = sanitize_execution_policy({"max_positions": 5, "allowed_symbols": ["BTCUSDT"]})
        result = evaluate_entry_gates(
            symbol="BTCUSDT",
            signal={"direction": "LONG", "confidence": 95, "radar": {"trap_score": 10}},
            snapshot={"positions": [{"symbol": f"COIN{index}USDT"} for index in range(5)], "open_orders": [], "hedge_mode": False},
            policy=policy,
            daily={"entries": 0, "realized_pnl": 0, "unverified_closures": 0},
            spread_bps=1,
            armed=True,
            allowed_symbols=["BTCUSDT"],
        )
        failed = {item["key"] for item in result["gates"] if not item["passed"]}
        self.assertIn("positions", failed)

    def test_automatic_cycle_calls_scanner_when_automation_is_active(self):
        state = initial_state()
        state["auto"].update({"enabled": True, "session_until": time.time() + 3600})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(last_scan_eligible_count=7)
        with patch("app.v25_execution.readiness", return_value={"ready": True}), \
                patch("app.v25_execution.client_for", return_value=client), \
                patch("app.v25_execution.account_snapshot", new=AsyncMock(return_value={"positions": [], "open_orders": []})), \
                patch("app.v25_execution.live_daily_metrics", return_value={"entries": 0, "realized_pnl": 0, "unverified_closures": 0}), \
                patch("app.v25_execution.scan_market_candidates", new=AsyncMock(return_value=[])) as scan, \
                patch("app.v25_execution.persist_state"):
            import asyncio
            asyncio.run(automatic_cycle(application))
        scan.assert_awaited_once_with(client, {"positions": [], "open_orders": []})
        self.assertEqual(state["auto"]["last_scan_stats"]["deep_analysis_candidates"], 0)

    def test_automatic_cycle_deep_analyzes_all_candidates(self):
        import asyncio

        state = initial_state()
        state["auto"].update({"enabled": True, "session_until": time.time() + 3600})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(last_scan_eligible_count=3)
        candidates = [{"symbol": symbol} for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")]
        candles = [{"time": index, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1} for index in range(220)]
        with patch("app.v25_execution.readiness", return_value={"ready": True}), \
                patch("app.v25_execution.client_for", return_value=client), \
                patch("app.v25_execution.account_snapshot", new=AsyncMock(return_value={"positions": [], "open_orders": []})), \
                patch("app.v25_execution.live_daily_metrics", return_value={"entries": 0, "realized_pnl": 0, "unverified_closures": 0}), \
                patch("app.v25_execution.scan_market_candidates", new=AsyncMock(return_value=candidates)), \
                patch("app.v25_execution.live_candles", new=AsyncMock(side_effect=[(candles, index) for index in range(3)])) as candle_fetch, \
                patch("app.v25_execution.canonical_live_decision", new=AsyncMock(side_effect=[
                    {"decision": "WAIT", "entry_eligible": False},
                    {"decision": "BUY", "entry_eligible": True, "analysis": {"direction": "LONG", "confidence": 80}},
                    {"decision": "SELL", "entry_eligible": True, "analysis": {"direction": "SHORT", "confidence": 79}},
                ])) as canonical, \
                patch("app.v25_execution.persist_state"):
            asyncio.run(automatic_cycle(application))
        self.assertEqual([call.args[1] for call in candle_fetch.await_args_list], ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        self.assertEqual(canonical.await_count, 3)
        self.assertEqual(state["auto"]["last_scan_stats"]["deep_analysis_symbols"], ["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    def test_automatic_cycle_skips_timed_out_candidate_and_completes(self):
        import asyncio

        state = initial_state()
        state["auto"].update({"enabled": True, "session_until": time.time() + 3600})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        client = SimpleNamespace(last_scan_eligible_count=2)
        candidates = [{"symbol": symbol} for symbol in ("BTCUSDT", "ETHUSDT")]
        candles = [{"time": index, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1} for index in range(220)]

        async def timeout_then_success(_client, symbol, _interval):
            if symbol == "BTCUSDT":
                raise asyncio.TimeoutError
            return candles, 123

        with patch("app.v25_execution.readiness_for", return_value={"ready": True}), \
            patch("app.v25_execution.client_for_with_credentials", return_value=client), \
                patch("app.v25_execution.account_snapshot", new=AsyncMock(return_value={"positions": [], "open_orders": []})), \
                patch("app.v25_execution.live_daily_metrics", return_value={"entries": 0, "realized_pnl": 0, "unverified_closures": 0}), \
                patch("app.v25_execution.scan_market_candidates", new=AsyncMock(return_value=candidates)), \
                patch("app.v25_execution.live_candles", new=AsyncMock(side_effect=timeout_then_success)), \
                patch("app.v25_execution.canonical_live_decision", new=AsyncMock(return_value={"decision": "WAIT", "entry_eligible": False})), \
                patch("app.v25_execution.persist_state"):
            asyncio.run(automatic_cycle(application, credentials=("api-key-123456", "secret-key-123456")))

        self.assertEqual(state["auto"]["cycles"], 1)
        self.assertEqual(state["auto"]["last_cycle_stage"], "completed")
        self.assertFalse(state["auto"]["busy"])
        self.assertEqual(state["auto"]["last_scan_stats"]["analysis_timeout_symbols"], ["BTCUSDT"])

    def test_scanner_respects_multiple_policy_symbols(self):
        exchange_info = {"symbols": [
            {"symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"}
            for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
        ]}
        tickers = [
            {"symbol": symbol, "lastPrice": "100", "quoteVolume": "20000000", "priceChangePercent": "3"}
            for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
        ]
        result = rank_market_tickers(exchange_info, tickers, allowed_symbols={"BTCUSDT", "ETHUSDT", "SOLUSDT"})
        self.assertEqual({item["symbol"] for item in result}, {"BTCUSDT", "ETHUSDT", "SOLUSDT"})

    def test_execution_loop_calls_automatic_cycle_after_reconcile(self):
        import asyncio

        state = initial_state()
        state["lock"] = asyncio.Lock()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))

        async def stop_loop(_seconds):
            raise asyncio.CancelledError

        with patch("app.v25_execution.live_credentials_status", return_value=("api-key-123456", "secret-key-123456", "fingerprint")), \
                patch("app.v25_execution.reconcile", new=AsyncMock()), \
                patch("app.v25_execution.automatic_cycle", new=AsyncMock()) as cycle, \
                patch("app.v25_execution.automation_telemetry") as telemetry, \
                patch("app.v25_execution.asyncio.sleep", new=stop_loop):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(execution_loop(application))
        cycle.assert_awaited_once_with(application)
        telemetry.assert_any_call("AUTOMATION_LOOP running", reason="loop_running")

    def test_inactive_and_expired_sessions_emit_distinct_skip_reasons(self):
        import asyncio

        for enabled, session_until, reason in ((False, time.time() + 3600, "automation_inactive"), (True, time.time() - 1, "session_expired")):
            state = initial_state()
            state["auto"].update({"enabled": enabled, "session_until": session_until})
            application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
            with patch("app.v25_execution.automation_telemetry") as telemetry:
                asyncio.run(automatic_cycle(application))
            telemetry.assert_called_once_with(f"AUTOMATION_SKIP reason={reason}", reason=reason)

    def test_automation_telemetry_reaches_logging_stream_once_per_interval(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("app.v25_execution")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            from app import v25_execution
            v25_execution._automation_telemetry_at.clear()
            automation_telemetry("AUTOMATION_LOOP running", reason="logger_test")
            automation_telemetry("AUTOMATION_LOOP running", reason="logger_test")
        finally:
            logger.removeHandler(handler)
        self.assertEqual(stream.getvalue().count("AUTOMATION_LOOP running"), 1)

    def test_start_endpoint_activates_reset_state_with_future_session(self):
        import asyncio

        state = initial_state()
        state["auto"].update({"enabled": False, "session_until": 0.0})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        request = SimpleNamespace(app=application)
        with patch("app.v25_execution.execution_owner", return_value={"id": "TEST"}), \
                patch("app.v25_execution.is_armed", return_value=True), \
                patch("app.v25_execution.readiness", return_value={"ready": True}), \
                patch("app.v25_execution.persist_state"), \
                patch("app.v25_execution.public_status", return_value={}):
            asyncio.run(v25_auto_start(request, Confirmation(confirmation="CANLI OTOMATİK")))
        self.assertTrue(state["auto"]["enabled"])
        self.assertGreater(state["auto"]["session_until"], time.time())

    def test_stop_endpoint_disables_active_session(self):
        import asyncio

        state = initial_state()
        state["auto"].update({"enabled": True, "session_until": time.time() + 3600})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        request = SimpleNamespace(app=application)
        with patch("app.v25_execution.execution_owner", return_value={"id": "TEST"}), \
                patch("app.v25_execution.persist_state"), \
                patch("app.v25_execution.public_status", return_value={}):
            asyncio.run(v25_auto_stop(request))
        self.assertFalse(state["auto"]["enabled"])
        self.assertEqual(state["auto"]["session_until"], 0.0)

    def test_status_exposes_scanner_observation_summary(self):
        state = initial_state()
        state["auto"]["last_scan"] = "2026-08-30T17:00:00+00:00"
        state["auto"]["last_skip_reason"] = None
        state["auto"]["last_cycle_stage"] = "completed"
        state["auto"]["last_scan_stats"] = {
            "candidate_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            "deep_analysis_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            "candidate_count": 3,
            "deep_analysis_count": 3,
        }
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        with patch("app.v25_execution.consent_status", return_value={"fingerprint": None}), \
                patch("app.v25_execution.readiness", return_value={"ready": False, "score": 0, "gates": [], "demo_certificate": {}}), \
                patch("app.v25_execution.is_armed", return_value=False), \
                patch("app.v25_execution.auto_session_active", return_value=False):
            payload = public_status(application)
        self.assertEqual(payload["scanner"]["deep_analysis_symbols"], ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        self.assertEqual(payload["scanner"]["candidate_count"], 3)
        self.assertEqual(payload["scanner"]["last_cycle_stage"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)