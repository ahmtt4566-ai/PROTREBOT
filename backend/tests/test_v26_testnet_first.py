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
    def test_testnet_is_primary_and_deployment_disables_paper(self):
        self.assertIn('EXECUTION_MODE = "TESTNET_FIRST"', MAIN)
        self.assertIn('env_flag("PROTREBOT_PAPER_ENABLED", default=True)', MAIN)
        self.assertIn('- key: PROTREBOT_PAPER_ENABLED\n        value: "false"', RENDER)

    def test_new_shell_exposes_separate_testnet_live_and_setup_tabs(self):
        self.assertIn("<TestnetFirstApp/>", ENTRY)
        for label in ("TESTNET KOMUTA", "OPERASYON & KANIT", "CANLI HAZIRLIK", "BORSA BAĞLANTILARI"):
            self.assertIn(label, FRONTEND)
        self.assertIn("Paper devre dışı", FRONTEND)
        self.assertIn("view === 'setup'", FRONTEND)
        self.assertIn("<ExchangeConnections/>", FRONTEND)

    def test_render_keeps_exchange_secrets_in_the_member_vault(self):
        for key in (
            "BINANCE_DEMO_API_KEY", "BINANCE_DEMO_SECRET_KEY",
            "BINANCE_LIVE_API_KEY", "BINANCE_LIVE_SECRET_KEY",
        ):
            self.assertNotIn(f"- key: {key}", RENDER)
        self.assertIn("ensure_exchange_vault", MAIN)
        self.assertIn("session_credentials_for_request", MAIN)

    def test_live_channel_is_fail_closed_until_explicit_gates(self):
        self.assertIn("BINANCE_LIVE_API_KEY", CREDENTIALS)
        self.assertIn("BINANCE_LIVE_SECRET_KEY", CREDENTIALS)
        self.assertIn("CANLI İŞLEM RİSKİNİ 24 SAAT KABUL EDİYORUM", EXECUTION)
        self.assertIn("LIVE_ARM_SECONDS = 24 * 60 * 60", EXECUTION)
        self.assertIn('"web_consent": {"accepted_at": None', EXECUTION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
