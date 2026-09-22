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


if __name__ == "__main__":
    unittest.main()