import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from backend.app import main as main_module
from backend.app.main import shared_mtf_decision


class SharedMTFDecisionTests(unittest.TestCase):
    def _payload(self, entry_direction, confidence_15m, one_h=None, four_h=None):
        return {
            "1h": {"direction": one_h if one_h is not None else entry_direction, "confidence": 80.0},
            "4h": {"direction": four_h if four_h is not None else entry_direction, "confidence": 82.0},
        }, confidence_15m

    def test_long_all_aligned_allowed(self):
        timeframe_results, confidence_15m = self._payload("LONG", 78.0)
        result = shared_mtf_decision("BTCUSDT", "LONG", confidence_15m, timeframe_results)
        self.assertEqual(result["direction"], "LONG")
        self.assertTrue(result["entry_permission"])
        self.assertFalse(result["blocked_by_short_filter"])
        self.assertEqual(result["verdict"], "GÜÇLÜ ONAY")

    def test_consensus_uses_closed_candles_for_all_timeframes(self):
        from backend.app.main import consensus_from_candles

        candles = {
            interval: [{"time": index, "open": 1, "high": 2, "low": 0.5, "close": 1, "volume": 10} for index in range(4)]
            for interval in ("15m", "1h", "4h", "1d")
        }
        observed_lengths = []

        def analyze_stub(rows):
            observed_lengths.append(len(rows))
            return {"direction": "LONG", "confidence": 80, "trend": "UP", "radar": {"trap_level": "LOW"}}

        with patch.object(main_module, "analyze", side_effect=analyze_stub):
            result = asyncio.run(consensus_from_candles("BTCUSDT", candles))
        self.assertEqual(observed_lengths, [3, 3, 3, 3])
        self.assertEqual(result["direction"], "LONG")

    def test_consensus_fails_safe_with_insufficient_closed_candles(self):
        from backend.app.main import consensus_from_candles

        candles = {interval: [{"time": 1}] for interval in ("15m", "1h", "4h", "1d")}
        result = asyncio.run(consensus_from_candles("BTCUSDT", candles))
        self.assertFalse(result["entry_permission"])
        self.assertEqual(result["direction"], "BEKLE")

    def test_forming_candle_changes_do_not_change_consensus(self):
        from backend.app.main import consensus_from_candles

        base = [{"time": index, "open": 1, "high": 2, "low": 0.5, "close": 1, "volume": 10} for index in range(4)]
        changed = [*base[:-1], {**base[-1], "close": 999, "high": 1000, "low": 998}]
        first = {interval: list(base) for interval in ("15m", "1h", "4h", "1d")}
        second = {interval: list(changed) for interval in ("15m", "1h", "4h", "1d")}
        signal = {"direction": "LONG", "confidence": 80, "trend": "UP", "radar": {"trap_level": "LOW"}}
        with patch.object(main_module, "analyze", return_value=signal):
            self.assertEqual(asyncio.run(consensus_from_candles("BTCUSDT", first)), asyncio.run(consensus_from_candles("BTCUSDT", second)))

    def test_short_all_aligned_below_threshold_allowed(self):
        timeframe_results, confidence_15m = self._payload("SHORT", 72.0)
        result = shared_mtf_decision("BTCUSDT", "SHORT", confidence_15m, timeframe_results)
        self.assertEqual(result["direction"], "SHORT")
        self.assertTrue(result["entry_permission"])
        self.assertFalse(result["blocked_by_short_filter"])

    def test_short_all_aligned_above_threshold_blocked(self):
        timeframe_results, confidence_15m = self._payload("SHORT", 90.0)
        result = shared_mtf_decision("BTCUSDT", "SHORT", confidence_15m, timeframe_results, short_filter=True, short_alignment_max=80.0)
        self.assertEqual(result["direction"], "SHORT")
        self.assertFalse(result["entry_permission"])
        self.assertTrue(result["blocked_by_short_filter"])
        self.assertIn("SHORT", result["verdict"])

    def test_long_with_1h_disagreement_blocked(self):
        timeframe_results, confidence_15m = self._payload("LONG", 81.0, one_h="SHORT")
        result = shared_mtf_decision("BTCUSDT", "LONG", confidence_15m, timeframe_results)
        self.assertEqual(result["direction"], "BEKLE")
        self.assertFalse(result["entry_permission"])
        self.assertEqual(result["verdict"], "UYUMSUZ")
        self.assertIn("1h", result["reason"])

    def test_short_with_4h_disagreement_blocked(self):
        timeframe_results, confidence_15m = self._payload("SHORT", 83.0, four_h="LONG")
        result = shared_mtf_decision("BTCUSDT", "SHORT", confidence_15m, timeframe_results)
        self.assertEqual(result["direction"], "BEKLE")
        self.assertFalse(result["entry_permission"])
        self.assertEqual(result["verdict"], "UYUMSUZ")
        self.assertIn("4h", result["reason"])

    def test_invalid_entry_direction_blocked(self):
        timeframe_results, confidence_15m = self._payload("BEKLE", 70.0)
        result = shared_mtf_decision("BTCUSDT", "BEKLE", confidence_15m, timeframe_results)
        self.assertEqual(result["direction"], "BEKLE")
        self.assertFalse(result["entry_permission"])
        self.assertEqual(result["verdict"], "UYUMSUZ")

    def test_alignment_uses_25_35_40_weights(self):
        timeframe_results = {
            "1h": {"direction": "LONG", "confidence": 80.0},
            "4h": {"direction": "LONG", "confidence": 90.0},
        }
        result = shared_mtf_decision("BTCUSDT", "LONG", 70.0, timeframe_results)
        expected = round(70.0 * 0.25 + 80.0 * 0.35 + 90.0 * 0.40)
        self.assertEqual(result["alignment"], expected)
        self.assertEqual(result["timeframes"]["15m"]["weight"], 0.25)
        self.assertEqual(result["timeframes"]["1h"]["weight"], 0.35)
        self.assertEqual(result["timeframes"]["4h"]["weight"], 0.40)

    def test_simulate_strategy_uses_shared_mtf_decision(self):
        candles = []
        for i in range(260):
            ts = i * 900
            candles.append({
                "time": ts,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5 + (i % 5) * 0.1,
                "volume": 1000.0,
            })
        base_signal = {
            "direction": "LONG",
            "confidence": 80.0,
            "entry": 100.0,
            "stop_loss": 95.0,
            "tp1": 108.0,
            "radar": {"trap_score": 10, "breakout_quality": 80, "trap_level": "LOW"},
            "volume_ratio": 1.0,
            "trend": "UP",
        }
        mtf_1h = []
        mtf_4h = []
        for i in range(200):
            ts = i * 3600
            mtf_1h.append({"time": ts, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0})
        for i in range(200):
            ts = i * 14400
            mtf_4h.append({"time": ts, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0})

        with patch.object(main_module, "analyze", return_value=base_signal), patch.object(main_module, "shared_mtf_decision", wraps=main_module.shared_mtf_decision) as mocked:
            result = main_module.simulate_strategy(candles, mtf=True, mtf_candles={"1h": mtf_1h, "4h": mtf_4h}, symbol="BTCUSDT", max_results=5)
        self.assertTrue(mocked.called)
        self.assertIn("trade_log", result)

    def test_live_automatic_cycle_blocks_on_missing_mtf_data(self):
        from backend.app import v25_execution as v25

        app = type("App", (), {})()
        app.state = type("State", (), {})()
        app.state.v25_execution = {
            "auto": {"last_scan": None, "busy": False, "last_decision": ""},
            "policy": {"allowed_symbols": ["BTCUSDT"], "interval": "15m", "scan_seconds": 30},
            "intents": {},
            "duplicate_blocks": 0,
        }
        with patch.object(v25, "auto_session_active", return_value=True), \
             patch.object(v25, "readiness", return_value={"ready": True}), \
             patch.object(v25, "client_for", return_value=object()), \
             patch.object(v25, "account_snapshot", AsyncMock(return_value={})), \
             patch.object(v25, "live_daily_metrics", return_value={}), \
             patch.object(v25, "live_candles", AsyncMock(side_effect=[([{"time": 0, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1} for _ in range(220)], 1), ([{"time": 0, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1} for _ in range(10)], 1), ([{"time": 0, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1} for _ in range(10)], 1)])), \
             patch.object(v25, "analyze", return_value={"direction": "LONG", "confidence": 82.0, "entry": 100.0, "stop_loss": 95.0, "tp1": 110.0}), \
             patch.object(v25, "spread_bps", AsyncMock(return_value=10.0)), \
             patch.object(v25, "evaluate_entry_gates", return_value={"passed": True}), \
             patch.object(v25, "risk_sized_order", return_value={"margin_usdt": 50.0, "leverage": 1}), \
             patch.object(v25, "persist_state", lambda *args, **kwargs: None), \
             patch.object(v25, "add_event", lambda *args, **kwargs: None), \
             patch.object(v25, "execute_live_order", AsyncMock()) as execute_mock:
            import asyncio
            asyncio.run(v25.automatic_cycle(app))
        self.assertFalse(execute_mock.called)
        self.assertNotIn("MTF", app.state.v25_execution["auto"]["last_decision"])
        self.assertEqual(app.state.v25_execution["auto"]["last_decision"], "Canlı otomasyon turu güvenli biçimde durduruldu.")


if __name__ == "__main__":
    unittest.main()
