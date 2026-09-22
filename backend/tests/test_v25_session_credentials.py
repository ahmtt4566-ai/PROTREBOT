import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import exchange_connections, v25_execution  # noqa: E402


class V25SessionCredentialTests(unittest.TestCase):
    def request(self, session: str):
        return SimpleNamespace(headers={"x-protrebot-session": session})

    def test_live_credentials_use_current_session_when_global_vault_is_empty(self):
        first = self.request("session-a")
        second = self.request("session-b")
        first_key = (exchange_connections.session_id(first), "LIVE")
        second_key = (exchange_connections.session_id(second), "LIVE")

        with patch.dict(exchange_connections._CACHE, {}, clear=True), \
             patch.dict(exchange_connections._META, {}, clear=True), \
             patch.dict(exchange_connections._SESSION_CACHE, {first_key: ("api-key-a", "secret-key-a")}, clear=True), \
             patch.dict(exchange_connections._SESSION_META, {first_key: {"active": True}}, clear=True):
            self.assertEqual(
                v25_execution.live_credentials_status(first),
                ("api-key-a", "secret-key-a", v25_execution.credential_fingerprint("api-key-a")),
            )
            self.assertEqual(v25_execution.live_credentials_status(second), ("", "", None))

    def test_missing_session_credential_is_unconfigured(self):
        request = self.request("session-missing")
        with patch.dict(exchange_connections._CACHE, {}, clear=True), \
             patch.dict(exchange_connections._META, {}, clear=True), \
             patch.dict(exchange_connections._SESSION_CACHE, {}, clear=True), \
             patch.dict(exchange_connections._SESSION_META, {}, clear=True):
            status = v25_execution.public_status(
                SimpleNamespace(state=SimpleNamespace(v25_execution=v25_execution.initial_state())),
                request,
            )
        self.assertFalse(status["credentials"]["configured"])
        self.assertEqual(status["credentials"]["storage"], "YOK")

    def test_status_exposes_snapshot_account_fields_without_private_material(self):
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=v25_execution.initial_state()))
        application.state.v25_execution["snapshot"] = {
            "wallet_balance": 1000.0,
            "available_balance": 800.0,
            "unrealized_pnl": 12.5,
            "positions": [{"symbol": "BTCUSDT", "quantity": 0.01, "mark_price": 50000}],
            "open_orders": [{"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "price": 49000, "quantity": 0.01, "status": "NEW"}],
        }
        status = v25_execution.public_status(application, self.request("session-a"))
        self.assertEqual(status["account"]["wallet_balance"], 1000.0)
        self.assertEqual(status["account"]["available_balance"], 800.0)
        self.assertEqual(status["account"]["unrealized_pnl"], 12.5)
        self.assertEqual(status["account"]["positions"][0]["symbol"], "BTCUSDT")
        self.assertEqual(status["account"]["open_orders"][0]["status"], "NEW")
        self.assertNotIn("api_key", status)
        self.assertNotIn("secret_key", status)


if __name__ == "__main__":
    unittest.main()