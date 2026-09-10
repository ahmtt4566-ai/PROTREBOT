import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

FRONTEND = ROOT / "TestnetFirstApp.tsx"
MASTER = ROOT / "MasterTrade.tsx"


class MasterTradeSafetyTests(unittest.TestCase):
    def test_master_trade_is_exposed_and_live_trading_is_locked(self):
        app_source = FRONTEND.read_text(encoding="utf-8")
        self.assertIn("MASTER TRADE", app_source)
        self.assertIn("LIVE TRADING LOCKED", app_source)
        self.assertIn("DEMO ONLY", app_source)
        self.assertIn("Risk preview", app_source)

    def test_master_trade_component_exists_for_persistent_history_and_locked_execution(self):
        self.assertTrue(MASTER.exists())
        source = MASTER.read_text(encoding="utf-8")
        self.assertIn("localStorage", source)
        self.assertIn("LIVE TRADING LOCKED", source)
        self.assertIn("trade history", source.lower())
        self.assertIn("disabled", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
