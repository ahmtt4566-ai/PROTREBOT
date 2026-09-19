import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import v21_demo  # noqa: E402


class AutoTradeBotTests(unittest.TestCase):
    def candidate(self, score=85, opportunity=85, direction="LONG", **overrides):
        candidate = {
            "symbol": "TESTUSDT", "direction": direction, "status": "SELECTED",
            "score": score, "analysis_score": score, "opportunity_score": opportunity,
            "decision_status": v21_demo._decision_status(score),
            "opportunity_breakdown": {"liquidity_quality": 80, "mtf_confirmation": 80},
            "entry": 100, "stop_loss": 99, "tp1": 101, "tp2": 102, "tp3": 103,
            "risk_reward": 3, "data_health": True, "signal_age_seconds": 0,
        }
        candidate.update(overrides)
        return candidate

    def test_decision_status_thresholds(self):
        expected = ((59, "BEKLE"), (60, "ZAYIF / İŞLEM YOK"), (70, "İZLE"), (80, "GÜÇLÜ ADAY"), (85, "AUTO TRADE ADAYI"))
        for score, label in expected:
            self.assertEqual(v21_demo._decision_status(score), label)

    def test_high_analysis_score_with_low_opportunity_is_not_tradeable(self):
        self.assertFalse(v21_demo.candidate_is_tradeable(self.candidate(90, 65), v21_demo.DEFAULT_SETTINGS))

    def test_analysis_score_75_can_be_tradeable_without_changing_decision_band(self):
        candidate = self.candidate(75, 75)
        self.assertEqual(candidate["decision_status"], "İZLE")
        self.assertTrue(v21_demo.candidate_is_tradeable(candidate, v21_demo.DEFAULT_SETTINGS))

    def test_high_analysis_score_with_bad_risk_reward_is_not_tradeable(self):
        self.assertFalse(v21_demo.candidate_is_tradeable(self.candidate(90, 85, risk_reward=0), v21_demo.DEFAULT_SETTINGS))

    def test_candidate_gate_diagnostics_count_first_rejection_reason(self):
        diagnostics = {key: 0 for key in v21_demo.GATE_REJECTION_KEYS}
        cases = {
            "direction_rejected": self.candidate(direction="NEUTRAL"),
            "status_rejected": self.candidate(status="WATCH"),
            "analysis_score_rejected": self.candidate(score=74, analysis_score=74),
            "opportunity_score_rejected": self.candidate(opportunity=69),
            "data_freshness_rejected": self.candidate(data_health=False),
            "mtf_rejected": self.candidate(opportunity_breakdown={"liquidity_quality": 80, "mtf_confirmation": 49}),
            "liquidity_rejected": self.candidate(opportunity_breakdown={"liquidity_quality": 49, "mtf_confirmation": 80}),
            "sl_tp_rejected": self.candidate(stop_loss=101),
            "other_rejected": self.candidate(risk_reward=0),
        }

        for expected_reason, candidate in cases.items():
            self.assertFalse(v21_demo.candidate_is_tradeable(candidate, v21_demo.DEFAULT_SETTINGS, diagnostics))
            self.assertEqual(diagnostics[expected_reason], 1)

        self.assertEqual(sum(diagnostics.values()), len(cases))

    def test_selection_with_diagnostics_has_same_result(self):
        candidates = [
            self.candidate(95, 95, symbol="GOODUSDT"),
            self.candidate(90, 65, symbol="LOWOPPUSDT"),
            self.candidate(85, 85, symbol="BADRISKUSDT", risk_reward=0),
        ]
        without_diagnostics = v21_demo.select_auto_candidates(candidates, v21_demo.DEFAULT_SETTINGS, set(), limit=3)
        diagnostics = {key: 0 for key in v21_demo.GATE_REJECTION_KEYS}
        with_diagnostics = v21_demo.select_auto_candidates(candidates, v21_demo.DEFAULT_SETTINGS, set(), limit=3, diagnostics=diagnostics)

        self.assertEqual([item["symbol"] for item in with_diagnostics], [item["symbol"] for item in without_diagnostics])
        self.assertEqual(diagnostics["opportunity_score_rejected"], 1)
        self.assertEqual(diagnostics["other_rejected"], 1)

    def test_mtf_confirmation_improves_opportunity_ranking(self):
        weak = self.candidate(90, 0, opportunity_breakdown={"liquidity_quality": 80, "mtf_confirmation": 25})
        strong = self.candidate(90, 0, opportunity_breakdown={"liquidity_quality": 80, "mtf_confirmation": 100})
        weak_score, _ = v21_demo._quality_score(weak, 90)
        strong_score, _ = v21_demo._quality_score(strong, 90)
        self.assertGreater(strong_score, weak_score)

    def test_low_liquidity_and_stale_signal_reduce_opportunity(self):
        fresh = self.candidate(90, 0, volume_ratio=1.5, signal_age_seconds=0)
        stale = self.candidate(90, 0, volume_ratio=0.1, signal_age_seconds=v21_demo.MAX_SIGNAL_AGE_SECONDS)
        fresh_score, _ = v21_demo._quality_score(fresh, 90)
        stale_score, _ = v21_demo._quality_score(stale, 90)
        self.assertGreater(fresh_score, stale_score)

    def test_long_and_short_directions_are_preserved(self):
        self.assertEqual(v21_demo._enrich_scan_candidates([self.candidate(90, 90, "LONG")])[0]["direction"], "LONG")
        self.assertEqual(v21_demo._enrich_scan_candidates([self.candidate(90, 90, "SHORT")])[0]["direction"], "SHORT")

    def test_candidate_selection_keeps_three_position_and_duplicate_limits(self):
        candidates = [self.candidate(90, 90, symbol=f"COIN{index}USDT") for index in range(4)]
        candidates.append(self.candidate(95, 95, symbol="COIN0USDT"))
        selected = v21_demo.select_auto_candidates(candidates, v21_demo.DEFAULT_SETTINGS, set())
        self.assertEqual(len(selected), 3)
        self.assertEqual(len({item["symbol"] for item in selected}), 3)

    def test_scan_100_symbols_and_filter_invalid_symbols(self):
        symbols = []
        tickers = []
        for index in range(105):
            symbol = f"COIN{index}USDT"
            symbols.append({"symbol": symbol, "baseAsset": f"COIN{index}", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"})
            tickers.append({"symbol": symbol, "quoteVolume": str(1_000_000 + index), "lastPrice": "100", "priceChangePercent": "1"})
        symbols.extend([
            {"symbol": "USDCUSDT", "baseAsset": "USDC", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "BADUSDT", "baseAsset": "BAD", "status": "BREAK", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "BADUSD", "baseAsset": "BAD", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USD"},
        ])
        result = v21_demo.dynamic_auto_universe({"symbols": symbols}, tickers, v21_demo.DEFAULT_SETTINGS)
        self.assertEqual(len(result), 100)
        self.assertNotIn("USDCUSDT", result)
        self.assertNotIn("BADUSDT", result)
        self.assertNotIn("BADUSD", result)

    def test_default_universe_ignores_legacy_fixed_24_coin_allowlist(self):
        symbols = []
        tickers = []
        for index in range(30):
            symbol = f"COIN{index}USDT"
            symbols.append({"symbol": symbol, "baseAsset": f"COIN{index}", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"})
            tickers.append({"symbol": symbol, "quoteVolume": str(1_000_000 + index), "lastPrice": "100", "priceChangePercent": "1"})
        result = v21_demo.dynamic_auto_universe({"symbols": symbols}, tickers, v21_demo.DEFAULT_SETTINGS)
        self.assertEqual(len(result), 30)
        self.assertEqual(result[0], "COIN29USDT")
        self.assertTrue(all(symbol in result for symbol in ("COIN0USDT", "COIN1USDT", "COIN29USDT")))

    def test_daily_loss_thresholds_and_deduplication(self):
        state = v21_demo.initial_state()
        state["risk"]["daily_base_balance"] = 1000.0
        state["journal"] = [{"kind": "FILL", "created_at": v21_demo.now_iso(), "realized_pnl": -50.0, "verified_realized": True}]
        result = v21_demo.refresh_daily_risk_state(state, 1000.0)
        self.assertEqual(result["loss_pct"], 5.0)
        self.assertEqual(state["notifications"]["unread"], 1)
        v21_demo.refresh_daily_risk_state(state, 1000.0)
        self.assertEqual(state["notifications"]["unread"], 1)

    def test_daily_loss_20_pauses_without_closing_existing(self):
        state = v21_demo.initial_state()
        state["auto"].update({"enabled": True, "status": "ON"})
        state["risk"]["daily_base_balance"] = 1000.0
        state["journal"] = [{"kind": "FILL", "created_at": v21_demo.now_iso(), "realized_pnl": -200.0, "verified_realized": True}]
        result = v21_demo.refresh_daily_risk_state(state, 1000.0)
        self.assertTrue(result["paused"])
        self.assertEqual(state["auto"]["pause_reason"], "DAILY_LOSS_20")
        self.assertEqual(len(state["notifications"]["seen"]), 4)

    def test_rotation_keeps_losing_position_and_only_returns_auto_safe_symbols(self):
        snapshot = {"positions": [
            {"symbol": "BTCUSDT", "unrealized_pnl": -2.0},
            {"symbol": "ETHUSDT", "unrealized_pnl": 1.0},
        ]}
        plans = {
            "auto-btc": {"symbol": "BTCUSDT", "source": "AUTO_SCANNER", "position_status": "OPEN"},
            "auto-eth": {"symbol": "ETHUSDT", "source": "AUTO_SCANNER", "position_status": "OPEN"},
            "manual-sol": {"symbol": "SOLUSDT", "source": "MANUAL", "position_status": "OPEN"},
        }
        self.assertEqual(v21_demo.safe_rotation_symbols(snapshot, {"SOLUSDT"}, plans), ["ETHUSDT"])

    def test_live_mode_rejected_and_testnet_host_remains_only_execution_host(self):
        source = (BACKEND / "app" / "binance_demo.py").read_text(encoding="utf-8")
        self.assertIn('DEMO_REST_BASE = "https://demo-fapi.binance.com"', source)
        self.assertNotIn('https://fapi.binance.com', source)
        self.assertIn('source="AUTO_SCANNER"', (BACKEND / "app" / "v21_demo.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
