import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

MASTER = ROOT / "MasterTrade.tsx"
DECISION = ROOT / "masterTradeDecision.ts"


class MasterTradeSafetyTests(unittest.TestCase):
    def test_master_trade_is_exposed_and_live_first(self):
        source = MASTER.read_text(encoding="utf-8")
        decision_source = DECISION.read_text(encoding="utf-8")
        self.assertIn("MASTER TRADE", source)
        self.assertIn("LIVE ACCOUNT", source)
        self.assertIn("LIVE ORDER", source)
        self.assertIn("/v25/status", source)
        self.assertNotIn("DEMO / TESTNET", source)
        self.assertNotIn("binance-demo", source)
        self.assertIn("SETUP CONFIRMED", decision_source)
        self.assertIn("NO ORDER SENT", decision_source)
        self.assertNotIn("Demo order", source)

    def test_master_trade_component_exists_for_persistent_history_and_live_execution(self):
        self.assertTrue(MASTER.exists())
        source = MASTER.read_text(encoding="utf-8")
        self.assertIn("localStorage", source)
        self.assertIn("LIVE ACCOUNT SNAPSHOT", source)
        self.assertIn("trade history", source.lower())
        self.assertIn("/v25/position/close", source)

    def test_master_trade_local_cors_allowlist_is_explicit_and_non_wildcard(self):
        main_source = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('"http://127.0.0.1:4173"', main_source)
        self.assertIn('"http://localhost:4173"', main_source)
        self.assertNotIn('allow_origins=["*"]', main_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
