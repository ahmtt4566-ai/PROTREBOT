import asyncio
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import v21_demo  # noqa: E402
from app.binance_demo import DEMO_REST_BASE  # noqa: E402


class MultiSymbolDemoAutoTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(v21_demo.DEFAULT_SETTINGS)
        self.settings["allowed_symbols"] = list(v21_demo.AUTO_TRADE_SYMBOLS)

    def candidate(self, symbol, score, direction="LONG", **overrides):
        value = {
            "symbol": symbol,
            "direction": direction,
            "status": "SELECTED",
            "entry": 100.0,
            "stop_loss": 99.0 if direction == "LONG" else 101.0,
            "tp1": 101.0 if direction == "LONG" else 99.0,
            "tp2": 102.0 if direction == "LONG" else 98.0,
            "tp3": 103.0 if direction == "LONG" else 97.0,
            "risk_reward": 3.0,
            "score": score,
            "data_health": True,
            "signal_age_seconds": 60,
        }
        value.update(overrides)
        return value

    def test_multi_symbol_scan(self):
        self.assertEqual(len(v21_demo.AUTO_TRADE_SYMBOLS), 24)
        self.assertEqual(len(set(v21_demo.AUTO_TRADE_SYMBOLS)), 24)

    def test_multi_symbol_selects_best_candidates(self):
        ranked = [self.candidate(symbol, score) for symbol, score in (("XRPUSDT", 61), ("BTCUSDT", 87), ("SOLUSDT", 82), ("ETHUSDT", 78))]
        selected = v21_demo.select_auto_candidates(ranked, self.settings, set())
        self.assertEqual([item["symbol"] for item in selected], ["BTCUSDT", "SOLUSDT", "ETHUSDT"])

    def test_three_symbols_can_open(self):
        selected = v21_demo.select_auto_candidates([self.candidate(symbol, 90) for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")], self.settings, set())
        self.assertEqual(len(selected), 3)

    def test_fourth_symbol_blocked_at_max_three(self):
        selected = v21_demo.select_auto_candidates([self.candidate(symbol, 90) for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")], self.settings, set())
        self.assertEqual(len(selected), 3)
        self.assertNotIn("XRPUSDT", {item["symbol"] for item in selected})

    def test_duplicate_symbol_blocked(self):
        ranked = [self.candidate("BTCUSDT", 95), self.candidate("BTCUSDT", 94)]
        selected = v21_demo.select_auto_candidates(ranked, self.settings, set())
        self.assertEqual([item["symbol"] for item in selected], ["BTCUSDT"])

    def test_only_one_valid_signal_opens_one(self):
        ranked = [self.candidate("BTCUSDT", 95), self.candidate("ETHUSDT", 80, direction="BEKLE")]
        selected = v21_demo.select_auto_candidates(ranked, self.settings, set())
        self.assertEqual([item["symbol"] for item in selected], ["BTCUSDT"])

    def test_no_valid_signal_opens_none(self):
        selected = v21_demo.select_auto_candidates([self.candidate("BTCUSDT", 95, direction="BEKLE")], self.settings, set())
        self.assertEqual(selected, [])

    def test_stale_candidate_skipped(self):
        selected = v21_demo.select_auto_candidates([self.candidate("BTCUSDT", 99, signal_age_seconds=v21_demo.MAX_SIGNAL_AGE_SECONDS + 1)], self.settings, set())
        self.assertEqual(selected, [])

    def test_invalid_candidate_skipped(self):
        selected = v21_demo.select_auto_candidates([self.candidate("BTCUSDT", 99, stop_loss=101.0)], self.settings, set())
        self.assertEqual(selected, [])

    def test_risk_gate_skips_candidate(self):
        state = v21_demo.initial_state()
        state["risk"]["consecutive_losses"] = 3
        self.assertEqual(v21_demo.automatic_risk_block(state)[0], "CONSECUTIVE_LOSSES")

    def test_daily_loss_blocks_all_candidates(self):
        state = v21_demo.initial_state()
        state["journal"] = [{"kind": "FILL", "created_at": v21_demo.now_iso(), "realized_pnl": -31.0, "verified_realized": True}]
        self.assertLessEqual(v21_demo.daily_metrics(state)["realized_pnl"], -state["settings"]["daily_loss_limit"])

    def test_consecutive_loss_blocks_all_candidates(self):
        state = v21_demo.initial_state()
        state["risk"].update({"consecutive_losses": 3, "consecutive_loss_limit": 3})
        self.assertEqual(v21_demo.automatic_risk_block(state), ("CONSECUTIVE_LOSSES", "Ardışık Demo zarar koruması aktif; yeni otomatik giriş kilitli."))

    def test_kill_switch_blocks_all_candidates(self):
        state = v21_demo.initial_state()
        state["risk"]["kill_switch"] = True
        self.assertEqual(v21_demo.automatic_risk_block(state)[0], "KILL_SWITCH")

    def test_race_condition_does_not_duplicate_symbol(self):
        selected = v21_demo.select_auto_candidates([self.candidate("BTCUSDT", 95)], self.settings, {"BTCUSDT"})
        self.assertEqual(selected, [])
        self.assertIn('async with state["lock"]:', (BACKEND / "app" / "binance_demo.py").read_text(encoding="utf-8"))

    def test_race_condition_does_not_exceed_three_positions(self):
        selected = v21_demo.select_auto_candidates([self.candidate(symbol, 90) for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")], self.settings, set(), limit=4)
        self.assertEqual(len(selected), 3)

    def test_live_mode_rejected_for_all_symbols(self):
        self.assertTrue(all("LIVE" not in symbol for symbol in v21_demo.AUTO_TRADE_SYMBOLS))
        self.assertEqual(v21_demo.select_auto_candidates([self.candidate("BTCUSDT", 90)], self.settings, set())[0]["symbol"], "BTCUSDT")
        self.assertEqual(DEMO_REST_BASE, "https://demo-fapi.binance.com")

    def test_testnet_client_used_for_all_symbols(self):
        source = (BACKEND / "app" / "v21_demo.py").read_text(encoding="utf-8")
        self.assertIn("market_client_for(application)", source)
        self.assertIn("execute_demo_order(application, body, source=\"AUTO_SCANNER\", request=request)", source)
        self.assertIn("DEMO_REST_BASE", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
