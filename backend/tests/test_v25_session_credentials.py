import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import exchange_connections, v22_commercial, v25_execution  # noqa: E402
from app.commercial_core import issue_token  # noqa: E402


class V25SessionCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tokens = {}
        self.secret = b"offline-session-fixture-signing-key"

    def request(self, session: str, application=None, *, method="GET", bind=False):
        if session not in self.tokens:
            self.tokens[session] = issue_token("owner", "OWNER", self.secret, token_version=1)
        user = {"id": "owner", "role": "OWNER", "active": True, "email_verified": True, "auth_version": 1}
        application = application or SimpleNamespace(state=SimpleNamespace(v25_execution=v25_execution.initial_state()))
        application.state.v22_commercial = {"secret": self.secret, "state": {"users": [user]}}
        application.state.db_pool = SimpleNamespace(fetchrow=AsyncMock(return_value={
            "auth_version": 1, "security": {"active": True, "role": "OWNER", "email_verified": True},
        }))
        request = SimpleNamespace(
            app=application, method=method, state=SimpleNamespace(),
            headers={"x-protrebot-session": self.tokens[session]}, cookies={},
        )
        request.state.member = asyncio.run(v22_commercial.authenticated_user_async(request))
        if bind:
            v25_execution.guard_execution_ownership(request, claim=True)
        return request

    def live_metadata(self):
        return {
            "user_id": "owner", "active": True, "configured": True,
            "account": {"api_permissions": {"withdrawal_check": "VERIFIED", "enableWithdrawals": False}},
        }

    def test_live_credentials_use_current_session_when_global_vault_is_empty(self):
        first = self.request("session-a")
        second = self.request("session-b")
        first_key = (exchange_connections.session_id(first), "LIVE")
        second_key = (exchange_connections.session_id(second), "LIVE")

        with patch.dict(exchange_connections._CACHE, {}, clear=True), \
             patch.dict(exchange_connections._META, {}, clear=True), \
             patch.dict(exchange_connections._SESSION_CACHE, {first_key: ("api-key-a", "secret-key-a")}, clear=True), \
             patch.dict(exchange_connections._SESSION_META, {first_key: self.live_metadata()}, clear=True):
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
                request.app,
                request,
            )
        self.assertFalse(status["credentials"]["configured"])
        self.assertEqual(status["credentials"]["storage"], "YOK")

    def test_status_exposes_snapshot_account_fields_without_private_material(self):
        state = v25_execution.initial_state()
        state["lock"] = asyncio.Lock()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, v22_commercial={"authenticated": True}))
        request = self.request("session-a", application, bind=True)
        application.state.v25_execution["snapshot"] = {
            "wallet_balance": 1000.0,
            "available_balance": 800.0,
            "unrealized_pnl": 12.5,
            "positions": [{"symbol": "BTCUSDT", "quantity": 0.01, "mark_price": 50000}],
            "open_orders": [{"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "price": 49000, "quantity": 0.01, "status": "NEW"}],
        }
        session_key = (exchange_connections.session_id(request), "LIVE")
        with patch.dict(exchange_connections._SESSION_CACHE, {session_key: ("api-key-a", "secret-key-a")}, clear=True), \
             patch.dict(exchange_connections._SESSION_META, {session_key: self.live_metadata()}, clear=True):
            application.state.v25_execution["snapshot_session_id"] = exchange_connections.session_id(request)
            application.state.v25_execution["connected"] = True
            status = v25_execution.public_status(application, request)
        self.assertTrue(status["connected"])
        self.assertTrue(status["readiness"]["gates"][2]["passed"])
        self.assertEqual(status["account"]["wallet_balance"], 1000.0)
        self.assertEqual(status["account"]["available_balance"], 800.0)
        self.assertEqual(status["account"]["unrealized_pnl"], 12.5)
        self.assertEqual(status["account"]["positions"][0]["symbol"], "BTCUSDT")
        self.assertEqual(status["account"]["open_orders"][0]["status"], "NEW")
        self.assertNotIn("api_key", status)
        self.assertNotIn("secret_key", status)

    def test_status_disconnects_when_snapshot_belongs_to_another_session(self):
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=v25_execution.initial_state()))
        request = self.request("session-b", application, bind=True)
        application.state.v25_execution["connected"] = True
        application.state.v25_execution["snapshot"] = {
            "wallet_balance": 1000.0,
            "available_balance": 800.0,
            "positions": [],
            "open_orders": [],
        }
        application.state.v25_execution["snapshot_session_id"] = exchange_connections.session_id(self.request("session-a"))
        status = v25_execution.public_status(application, request)
        self.assertFalse(status["connected"])
        self.assertIsNone(status["account"]["wallet_balance"])

    def test_read_only_connect_uses_current_session_credentials_and_publishes_snapshot(self):
        state = v25_execution.initial_state()
        state["lock"] = asyncio.Lock()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, v22_commercial={"authenticated": True}))
        request = self.request("session-a", application, method="POST")
        snapshot = {
            "wallet_balance": 1000.0,
            "available_balance": 800.0,
            "positions": [],
            "open_orders": [],
        }
        client = SimpleNamespace(time_offset_ms=0)
        with patch.dict(exchange_connections._SESSION_CACHE, {(exchange_connections.session_id(request), "LIVE"): ("api-key-session-a", "secret-key-session-a")}, clear=True), \
             patch.dict(exchange_connections._SESSION_META, {(exchange_connections.session_id(request), "LIVE"): self.live_metadata()}, clear=True), \
             patch.object(v25_execution, "client_for", return_value=client) as client_for, \
             patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=snapshot)), \
             patch.object(v25_execution, "persist_state"):
            status = asyncio.run(v25_execution.v25_connect(request))
        client_for.assert_called_once_with(application, request)
        self.assertTrue(status["connected"])
        self.assertEqual(status["account"]["wallet_balance"], 1000.0)
        account_gate = next(gate for gate in status["readiness"]["gates"] if gate["key"] == "read_only")
        self.assertTrue(account_gate["passed"])

    def test_reconcile_preserves_authenticated_snapshot_binding(self):
        state = v25_execution.initial_state()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        request = self.request("session-a", application, bind=True)
        snapshot = {
            "wallet_balance": 1000.0,
            "available_balance": 800.0,
            "positions": [],
            "open_orders": [],
        }
        state["snapshot"] = snapshot
        state["snapshot_session_id"] = exchange_connections.session_id(request)
        state["connected"] = True
        client = SimpleNamespace(time_offset_ms=0)
        with patch.object(v25_execution, "client_for", return_value=client), \
             patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value={**snapshot, "unrealized_pnl": 1.0})), \
             patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
             patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application))
        self.assertEqual(state["snapshot_session_id"], exchange_connections.session_id(request))
        status = v25_execution.public_status(application, request)
        self.assertTrue(status["connected"])

    def test_read_only_connect_rejects_other_session_snapshot(self):
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=v25_execution.initial_state()))
        state = application.state.v25_execution
        request = self.request("session-b", application, bind=True)
        state["connected"] = True
        state["snapshot"] = {
            "wallet_balance": 1000.0,
            "available_balance": 800.0,
            "positions": [],
            "open_orders": [],
        }
        state["snapshot_session_id"] = exchange_connections.session_id(self.request("session-a"))
        self.assertFalse(v25_execution.public_status(application, request)["connected"])
        with self.assertRaises(v25_execution.HTTPException) as denied:
            v25_execution.public_status(application, self.request("session-a", application))
        self.assertEqual(denied.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()