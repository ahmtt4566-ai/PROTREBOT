import asyncio
import sys
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
backend_root = Path(__file__).resolve().parents[1]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from backend.app import main as main_module
from backend.app.main import candle_close_gate


def _candles(count=260):
    return [{"time": index * 900, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0} for index in range(count)]


class CandleCloseGateThresholdTests(unittest.TestCase):
    def setUp(self):
        main_module.GATE_CACHE.clear()

    def _run(self, current_confidence, current_breakout_quality, previous_confidence=75.0, trap_score=10):
        main_module.GATE_CACHE.clear()  # each call must observe fresh thresholds, not a cached prior result
        current = {"direction": "LONG", "confidence": current_confidence, "radar": {"trap_score": trap_score, "breakout_quality": current_breakout_quality}}
        previous = {"direction": "LONG", "confidence": previous_confidence, "radar": {"trap_score": trap_score, "breakout_quality": current_breakout_quality}}
        with patch.object(main_module, "fetch_candles", new=AsyncMock(return_value=_candles())), \
             patch.object(main_module, "analyze", side_effect=[current, previous]):
            return asyncio.run(candle_close_gate("BTCUSDT", "15m"))

    def test_breakout_quality_between_50_and_54_passes_with_canonical_threshold(self):
        gate = self._run(current_confidence=78, current_breakout_quality=52)
        self.assertTrue(gate["entry_allowed"])

    def test_breakout_quality_below_50_still_fails(self):
        gate = self._run(current_confidence=78, current_breakout_quality=49)
        self.assertFalse(gate["entry_allowed"])

    def test_confidence_boundary_remains_78(self):
        below = self._run(current_confidence=77, current_breakout_quality=80)
        at_boundary = self._run(current_confidence=78, current_breakout_quality=80)
        self.assertFalse(below["entry_allowed"])
        self.assertTrue(at_boundary["entry_allowed"])

    def test_uses_same_historical_policy_default_thresholds_as_canonical_decision(self):
        confidence_threshold = main_module.HISTORICAL_POLICY_DEFAULT["confidence_threshold"]
        breakout_quality_threshold = main_module.HISTORICAL_POLICY_DEFAULT["breakout_quality_threshold"]
        passing = self._run(current_confidence=confidence_threshold, current_breakout_quality=breakout_quality_threshold)
        self.assertTrue(passing["entry_allowed"])
        failing = self._run(current_confidence=confidence_threshold, current_breakout_quality=breakout_quality_threshold - 1)
        self.assertFalse(failing["entry_allowed"])


if __name__ == "__main__":
    unittest.main()
