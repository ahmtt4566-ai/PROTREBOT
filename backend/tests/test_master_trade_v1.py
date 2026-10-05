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
        panel = (ROOT / "frontend" / "src" / "LiveTradingPanel.tsx").read_text(encoding="utf-8")
        self.assertIn("LIVE ORDER", panel)
        self.assertIn("<LiveTradingPanel", source)
        self.assertIn("sharedStatus={liveStatus}", source)
        self.assertIn("/v25/status", source)
        self.assertNotIn("DEMO / TESTNET", source)
        self.assertNotIn("binance-demo", source)
        self.assertIn("SETUP CONFIRMED", decision_source)
        self.assertIn("NO ORDER SENT", decision_source)
        self.assertNotIn("Demo order", source)

    def test_master_trade_component_exists_for_persistent_history_and_live_execution(self):
        self.assertTrue(MASTER.exists())
        source = MASTER.read_text(encoding="utf-8")
        self.assertNotIn("localStorage", source)
        self.assertIn("accountPayload.journal", source)
        self.assertNotIn("/v21/journal", source)
        self.assertNotIn("/v21/performance", source)
        self.assertIn("item.verified_realized === true", source)
        self.assertIn("LIVE ACCOUNT SNAPSHOT", source)
        self.assertIn("trade history", source.lower())
        self.assertIn("/v25/position/close", source)

    def test_master_trade_cors_allowlist_is_trusted_production_and_non_wildcard(self):
        from app import main
        from starlette.middleware.cors import CORSMiddleware

        main_source = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('PRODUCTION_WEB_ORIGIN', main_source)
        self.assertIn('WEB_CORS_ORIGIN_REGEX = VERCEL_PREVIEW_ORIGIN_RE.pattern', main_source)
        self.assertNotIn('"http://127.0.0.1:4173"', main_source)
        self.assertNotIn('"http://localhost:4173"', main_source)
        self.assertNotIn('allow_origins=["*"]', main_source)
        cors = CORSMiddleware(
            app=main.app,
            allow_origins=main.WEB_CORS_ORIGINS,
            allow_origin_regex=main.WEB_CORS_ORIGIN_REGEX,
            allow_credentials=True,
        )
        for origin in ("https://kaistrade.com", "https://frontend-nu-two-18.vercel.app"):
            self.assertTrue(cors.is_allowed_origin(origin), origin)
        for origin in ("*", "http://kaistrade.com", "https://kaistrade.com.evil.example",
                       "https://attacker.vercel.app", "http://localhost:4173", "null"):
            self.assertFalse(cors.is_allowed_origin(origin), origin)


if __name__ == "__main__":
    unittest.main(verbosity=2)
