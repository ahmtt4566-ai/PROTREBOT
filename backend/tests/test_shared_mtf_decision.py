import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
backend_root = Path(__file__).resolve().parents[1]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from backend.app import main as main_module
from backend.app.main import canonical_historical_decision, shared_mtf_decision


class SharedMTFDecisionTests(unittest.TestCase):
    def _historical_candles(self, count=221):
        return [{"time": index * 900, "open": 100 + index * 0.02, "high": 101 + index * 0.02, "low": 99 + index * 0.02, "close": 100.5 + index * 0.02, "volume": 1000.0} for index in range(count)]

    def _aligned_frames(self, decision_time=10_000_000):
        durations = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        frames = {}
        for interval, duration in durations.items():
            count = 220 if interval == "15m" else 50
            rows = [{"time": decision_time - (count - index) * duration, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000} for index in range(count)]
            forming_time = decision_time if interval == "15m" else decision_time - 900
            rows.append({"time": forming_time, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000})
            frames[interval] = rows
        return frames, decision_time

    def test_higher_timeframe_forming_candles_are_ignored(self):
        for interval in ("1h", "4h", "1d"):
            frames, decision_time = self._aligned_frames()
            changed = {name: list(rows) for name, rows in frames.items()}
            changed[interval][-1] = {**changed[interval][-1], "close": 9999, "high": 10000, "low": 100}

            def analyze_stub(rows):
                direction = "SHORT" if any(row["close"] > 5000 for row in rows) else "LONG"
                return {"direction": direction, "confidence": 80, "trend": direction, "radar": {"trap_score": 10, "breakout_quality": 80, "trap_level": "LOW"}, "entry": 100, "stop_loss": 99, "tp1": 102}

            with patch.object(main_module, "analyze", side_effect=analyze_stub):
                first = canonical_historical_decision("BTCUSDT", frames, decision_time)
                second = canonical_historical_decision("BTCUSDT", changed, decision_time)
            self.assertEqual(first["decision"], second["decision"], interval)

    def test_higher_timeframe_close_boundary_is_included(self):
        frames, decision_time = self._aligned_frames()
        observed = []

        def analyze_stub(rows):
            observed.append(rows[-1]["time"])
            return {"direction": "LONG", "confidence": 80, "trend": "LONG", "radar": {"trap_score": 10, "breakout_quality": 80, "trap_level": "LOW"}, "entry": 100, "stop_loss": 99, "tp1": 102}

        with patch.object(main_module, "analyze", side_effect=analyze_stub):
            result = canonical_historical_decision("BTCUSDT", frames, decision_time)
        self.assertEqual(result["latest_closed_timestamps"]["1h"], decision_time - 3600)
        self.assertIn(decision_time - 3600, observed)

    def test_canonical_decision_uses_closed_candles_only(self):
        candles = self._historical_candles()
        decision_time = candles[-1]["time"]
        changed_forming = [*candles[:-1], {**candles[-1], "close": 10_000, "high": 10_100, "low": 100}]
        first = canonical_historical_decision("BTCUSDT", {"15m": candles}, decision_time, required_intervals=("15m",))
        second = canonical_historical_decision("BTCUSDT", {"15m": changed_forming}, decision_time, required_intervals=("15m",))
        self.assertEqual(first, second)
        self.assertEqual(first["signal_timestamp"], candles[-2]["time"])

    def test_canonical_decision_returns_wait_for_insufficient_or_invalid_data(self):
        candles = self._historical_candles(20)
        result = canonical_historical_decision("BTCUSDT", {"15m": candles}, candles[-1]["time"], required_intervals=("15m",))
        self.assertEqual(result["decision"], "WAIT")
        malformed = [*self._historical_candles(), {"time": 5, "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1}]
        result = canonical_historical_decision("BTCUSDT", {"15m": malformed}, malformed[-1]["time"] + 1, required_intervals=("15m",))
        self.assertEqual(result["decision"], "WAIT")

    def test_canonical_decision_with_valid_stop_distance_allows_entry(self):
        frames, decision_time = self._aligned_frames()
        signal = {
            "direction": "LONG", "confidence": 85, "trend": "LONG",
            "radar": {"trap_score": 10, "breakout_quality": 80, "trap_level": "LOW"},
            "entry": 100.0, "stop_loss": 98.0, "tp1": 104.0,  # 2.0% stop distance <= 2.5% max
        }
        policy = {"max_stop_distance_pct": 2.5, "max_loss_per_trade": 3.0, "max_margin_per_trade": 25.0, "max_leverage": 2}
        with patch.object(main_module, "analyze", return_value=signal):
            result = canonical_historical_decision("BTCUSDT", frames, decision_time, policy=policy)
        self.assertEqual(result["decision"], "BUY")
        self.assertTrue(result["entry_eligible"])
        self.assertEqual(result["reason"], "READY")
        self.assertIsNotNone(result["risk"])
        self.assertAlmostEqual(result["risk"]["stop_distance_pct"], 2.0)

    def test_canonical_decision_default_thresholds_remain_78_and_55(self):
        frames, decision_time = self._aligned_frames()
        signal = {
            "direction": "LONG", "confidence": 78.0, "trend": "LONG",
            "radar": {"trap_score": 10, "breakout_quality": 55.0, "trap_level": "LOW"},
            "entry": 100.0, "stop_loss": 97.5, "tp1": 105.0,
        }
        with patch.object(main_module, "analyze", return_value=signal):
            result = canonical_historical_decision("BTCUSDT", frames, decision_time)
        self.assertTrue(result["entry_eligible"])
        self.assertEqual(result["decision"], "BUY")

    def test_canonical_decision_fails_closed_when_higher_timeframes_are_still_warming_up(self):
        primary = [{
            "time": idx * 900,
            "open": 100.0 + idx * 0.05,
            "high": 101.0 + idx * 0.05,
            "low": 99.0 + idx * 0.05,
            "close": 100.5 + idx * 0.05,
            "volume": 1000.0,
        } for idx in range(220)]
        decision_time = primary[-1]["time"] + 900
        one_h = [{
            "time": idx * 3600,
            "open": 100.0 + idx * 0.10,
            "high": 101.0 + idx * 0.10,
            "low": 99.0 + idx * 0.10,
            "close": 100.5 + idx * 0.10,
            "volume": 5000.0,
        } for idx in range(49)]
        four_h = [{
            "time": idx * 14400,
            "open": 100.0 + idx * 0.15,
            "high": 101.0 + idx * 0.15,
            "low": 99.0 + idx * 0.15,
            "close": 100.5 + idx * 0.15,
            "volume": 20000.0,
        } for idx in range(12)]
        frames = {"15m": primary, "1h": one_h, "4h": four_h, "1d": [{"time": 0, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 50000}]}
        signal = {
            "direction": "LONG", "confidence": 85.0, "trend": "LONG",
            "radar": {"trap_score": 10, "breakout_quality": 80.0, "trap_level": "LOW"},
            "entry": 100.0, "stop_loss": 97.5, "tp1": 105.0,
        }
        with patch.object(main_module, "analyze", return_value=signal):
            result = canonical_historical_decision("BTCUSDT", frames, decision_time)
        self.assertEqual(result["decision"], "WAIT")
        self.assertFalse(result["entry_eligible"])
        self.assertEqual(result["reason"], "INSUFFICIENT_CLOSED_CANDLES")
        self.assertIn("1h: 49/50 closed candles", result["reasons"][0])

    def test_canonical_decision_fails_closed_when_only_4h_is_insufficient(self):
        primary = [{
            "time": idx * 900,
            "open": 100.0 + idx * 0.05,
            "high": 101.0 + idx * 0.05,
            "low": 99.0 + idx * 0.05,
            "close": 100.5 + idx * 0.05,
            "volume": 1000.0,
        } for idx in range(220)]
        decision_time = primary[-1]["time"] + 900
        one_h = [{
            "time": decision_time - (50 - idx) * 3600,
            "open": 100.0 + idx * 0.10,
            "high": 101.0 + idx * 0.10,
            "low": 99.0 + idx * 0.10,
            "close": 100.5 + idx * 0.10,
            "volume": 5000.0,
        } for idx in range(50)]
        four_h = [{
            "time": decision_time - (49 - idx) * 14400,
            "open": 100.0 + idx * 0.15,
            "high": 101.0 + idx * 0.15,
            "low": 99.0 + idx * 0.15,
            "close": 100.5 + idx * 0.15,
            "volume": 20000.0,
        } for idx in range(49)]
        one_d = [{
            "time": decision_time - (50 - idx) * 86400,
            "open": 100.0 + idx * 0.20,
            "high": 101.0 + idx * 0.20,
            "low": 99.0 + idx * 0.20,
            "close": 100.5 + idx * 0.20,
            "volume": 50000.0,
        } for idx in range(50)]
        frames = {"15m": primary, "1h": one_h, "4h": four_h, "1d": one_d}
        signal = {
            "direction": "LONG", "confidence": 85.0, "trend": "LONG",
            "radar": {"trap_score": 10, "breakout_quality": 80.0, "trap_level": "LOW"},
            "entry": 100.0, "stop_loss": 97.5, "tp1": 105.0,
        }
        with patch.object(main_module, "analyze", return_value=signal):
            result = canonical_historical_decision("BTCUSDT", frames, decision_time)
        self.assertEqual(result["decision"], "WAIT")
        self.assertFalse(result["entry_eligible"])
        self.assertEqual(result["reason"], "INSUFFICIENT_CLOSED_CANDLES")
        self.assertIn("4h: 49/50 closed candles", result["reasons"][0])

    def test_canonical_decision_threshold_override_uses_candidate_values(self):
        frames, decision_time = self._aligned_frames()
        signal = {
            "direction": "LONG", "confidence": 80.0, "trend": "LONG",
            "radar": {"trap_score": 10, "breakout_quality": 58.0, "trap_level": "LOW"},
            "entry": 100.0, "stop_loss": 97.5, "tp1": 105.0,
        }
        with patch.object(main_module, "analyze", return_value=signal):
            baseline = canonical_historical_decision("BTCUSDT", frames, decision_time, historical_policy_override={"confidence_threshold": 78, "breakout_quality_threshold": 55})
            strict = canonical_historical_decision("BTCUSDT", frames, decision_time, historical_policy_override={"confidence_threshold": 82, "breakout_quality_threshold": 60})
        self.assertTrue(baseline["entry_eligible"])
        self.assertFalse(strict["entry_eligible"])

    def test_historical_policy_override_rejects_unsafe_keys(self):
        frames, decision_time = self._aligned_frames()
        signal = {
            "direction": "LONG", "confidence": 80.0, "trend": "LONG",
            "radar": {"trap_score": 10, "breakout_quality": 58.0, "trap_level": "LOW"},
            "entry": 100.0, "stop_loss": 97.5, "tp1": 105.0,
        }
        with patch.object(main_module, "analyze", return_value=signal):
            with self.assertRaises(ValueError):
                canonical_historical_decision(
                    "BTCUSDT",
                    frames,
                    decision_time,
                    historical_policy_override={"confidence_threshold": 80, "breakout_quality_threshold": 55, "max_stop_distance_pct": 5.0},
                )

    def test_canonical_decision_with_excessive_stop_distance_rejects_without_raising(self):
        frames, decision_time = self._aligned_frames()
        signal = {
            "direction": "LONG", "confidence": 85, "trend": "LONG",
            "radar": {"trap_score": 10, "breakout_quality": 80, "trap_level": "LOW"},
            "entry": 100.0, "stop_loss": 97.0, "tp1": 106.0,  # 3.0% stop distance > 2.5% max
        }
        policy = {"max_stop_distance_pct": 2.5, "max_loss_per_trade": 3.0, "max_margin_per_trade": 25.0, "max_leverage": 2}
        with patch.object(main_module, "analyze", return_value=signal):
            result = canonical_historical_decision("BTCUSDT", frames, decision_time, policy=policy)
        self.assertEqual(result["decision"], "WAIT")
        self.assertFalse(result["entry_eligible"])
        self.assertEqual(result["reason"], "RISK_GATE_REJECTED")
        self.assertIn("Stop mesafesi %3.00", result["reasons"][0])
        self.assertIsNone(result["risk"])

    def test_canonical_decision_flags_entry_equal_stop_as_invariant_violation(self):
        frames, decision_time = self._aligned_frames()
        signal = {
            "direction": "LONG", "confidence": 85, "trend": "LONG",
            "radar": {"trap_score": 10, "breakout_quality": 80, "trap_level": "LOW"},
            "entry": 100.0, "stop_loss": 100.0, "tp1": 106.0,
        }
        policy = {"max_stop_distance_pct": 2.5, "max_loss_per_trade": 3.0, "max_margin_per_trade": 25.0, "max_leverage": 2}
        with patch.object(main_module, "analyze", return_value=signal):
            result = canonical_historical_decision("BTCUSDT", frames, decision_time, policy=policy)
        self.assertEqual(result["decision"], "WAIT")
        self.assertFalse(result["entry_eligible"])
        self.assertIn("ANALYSIS_INVARIANT_VIOLATION", result["reasons"])
        self.assertNotEqual(result["reason"], "RISK_GATE_REJECTED")
        self.assertIsNone(result["risk"])

    def test_canonical_decision_flags_non_positive_entry_or_stop_as_invariant_violation(self):
        frames, decision_time = self._aligned_frames()
        policy = {"max_stop_distance_pct": 2.5, "max_loss_per_trade": 3.0, "max_margin_per_trade": 25.0, "max_leverage": 2}
        for entry, stop in ((0.0, 97.5), (100.0, 0.0), (-5.0, 97.5)):
            signal = {
                "direction": "LONG", "confidence": 85, "trend": "LONG",
                "radar": {"trap_score": 10, "breakout_quality": 80, "trap_level": "LOW"},
                "entry": entry, "stop_loss": stop, "tp1": 106.0,
            }
            with patch.object(main_module, "analyze", return_value=signal):
                result = canonical_historical_decision("BTCUSDT", frames, decision_time, policy=policy)
            self.assertEqual(result["decision"], "WAIT", (entry, stop))
            self.assertFalse(result["entry_eligible"], (entry, stop))
            self.assertIn("ANALYSIS_INVARIANT_VIOLATION", result["reasons"], (entry, stop))

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
        for i in range(500):
            ts = (i - 300) * 3600
            mtf_1h.append({"time": ts, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0})
        for i in range(1000):
            ts = (i - 300) * 14400
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
