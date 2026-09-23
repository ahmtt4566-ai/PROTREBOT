import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
MAIN = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
EXECUTION = (ROOT / "backend" / "app" / "v25_execution.py").read_text(encoding="utf-8")
CREDENTIALS = (ROOT / "backend" / "app" / "credential_store.py").read_text(encoding="utf-8")
FRONTEND = (ROOT / "frontend" / "src" / "TestnetFirstApp.tsx").read_text(encoding="utf-8")
ENTRY = (ROOT / "frontend" / "src" / "main.tsx").read_text(encoding="utf-8")
RENDER = (ROOT / "render.yaml").read_text(encoding="utf-8")


class V26TestnetFirstContracts(unittest.TestCase):
    def test_testnet_is_primary_and_paper_is_disabled_by_default(self):
        self.assertIn('EXECUTION_MODE = "TESTNET_FIRST"', MAIN)
        self.assertIn('env_flag("PROTREBOT_PAPER_ENABLED", default=False)', MAIN)
        self.assertIn("Paper motoru V27 Testnet-First s├╝r├╝m├╝nde devre d─▒┼ş─▒d─▒r", MAIN)

    def test_new_shell_exposes_separate_testnet_live_and_setup_tabs(self):
        self.assertIn("<TestnetFirstApp/>", ENTRY)
        for label in ("TESTNET KOMUTA", "OPERASYON & KANIT", "CANLI HAZIRLIK", "YAYIN KAPILARI"):
            self.assertIn(label, FRONTEND)
        self.assertIn("Paper devre d─▒┼ş─▒", FRONTEND)

    def test_render_declares_demo_and_live_secrets_without_values(self):
        for key in (
            "BINANCE_DEMO_API_KEY", "BINANCE_DEMO_SECRET_KEY",
            "BINANCE_LIVE_API_KEY", "BINANCE_LIVE_SECRET_KEY",
        ):
            self.assertIn(f"- key: {key}\n        sync: false", RENDER)

    def test_live_channel_is_fail_closed_until_explicit_gates(self):
        self.assertIn("BINANCE_LIVE_API_KEY", CREDENTIALS)
        self.assertIn("BINANCE_LIVE_SECRET_KEY", CREDENTIALS)
        self.assertIn("CANLI ─░┼ŞLEM R─░SK─░N─░ 24 SAAT KABUL ED─░YORUM", EXECUTION)
        self.assertIn("LIVE_ARM_SECONDS = 5 * 60", EXECUTION)
        self.assertIn('"web_consent": {"accepted_at": None', EXECUTION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
