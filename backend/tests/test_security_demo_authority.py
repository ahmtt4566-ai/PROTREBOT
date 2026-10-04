import unittest
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import binance_demo, v21_demo, v22_commercial
from app.binance_demo import BinanceDemoClient, BinanceDemoError, bind_demo_private_authority


class DemoAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def client(self, state, version=1, expiry=None):
        application = SimpleNamespace(state=SimpleNamespace(db_pool=None))
        client = BinanceDemoClient(AsyncMock(), "synthetic-demo-key-not-live", "synthetic-demo-secret-not-live")
        client.sync_clock = AsyncMock()
        client._request = AsyncMock(return_value={"ok": True})
        return bind_demo_private_authority(client, application, state, "synthetic-user", version, time.time() + 3600 if expiry is None else expiry), application

    async def test_revoked_shared_version_blocks_retained_client_before_any_provider_call(self):
        state = {"armed_until": 9999999999}
        client, application = self.client(state)
        with patch.object(v22_commercial, "validate_authoritative_session", AsyncMock(side_effect=HTTPException(401, "Revoked"))) as authority:
            with self.assertRaises(BinanceDemoError) as caught:
                await client.signed("GET", "/fapi/v3/account")
            self.assertEqual(caught.exception.http_status, 401)
            authority.assert_awaited_once_with(application, "synthetic-user", 1)
        client.sync_clock.assert_not_awaited()
        client._request.assert_not_awaited()
        self.assertIsNone(state["armed_until"])

    async def test_background_client_never_adopts_mutated_current_version(self):
        state = {"_auth_version": 1, "armed_until": 9999999999}
        client, application = self.client(state, state["_auth_version"])
        state["_auth_version"] = 2
        with patch.object(v22_commercial, "validate_authoritative_session", AsyncMock()) as authority:
            await client.signed("GET", "/fapi/v3/account")
        self.assertEqual(authority.await_count, 2)
        authority.assert_awaited_with(application, "synthetic-user", 1)

    async def test_revocation_during_clock_sync_blocks_the_private_request(self):
        state = {"armed_until": 9999999999}
        client, _ = self.client(state)
        with patch.object(v22_commercial, "validate_authoritative_session", AsyncMock(side_effect=[{}, HTTPException(401, "Revoked")])):
            with self.assertRaises(BinanceDemoError):
                await client.signed("GET", "/fapi/v3/account")
        client.sync_clock.assert_awaited_once()
        client._request.assert_not_awaited()
        self.assertIsNone(state["armed_until"])

    async def test_missing_and_corrupt_captured_versions_fail_closed(self):
        for version in (None, 0, -1, True, "1"):
            state = {"armed_until": 9999999999}
            client, _ = self.client(state, version)
            with self.assertRaises(BinanceDemoError) as caught:
                await client.signed("GET", "/fapi/v3/account")
            self.assertEqual(caught.exception.http_status, 412)
            client.sync_clock.assert_not_awaited()
            client._request.assert_not_awaited()
            self.assertIsNone(state["armed_until"])

    async def test_storage_outage_does_not_use_worker_credentials(self):
        state = {"armed_until": 9999999999}
        client, _ = self.client(state)
        with patch.object(v22_commercial, "validate_authoritative_session", AsyncMock(side_effect=HTTPException(503, "Unavailable"))):
            with self.assertRaises(BinanceDemoError) as caught:
                await client.signed("GET", "/fapi/v3/account")
            self.assertEqual(caught.exception.http_status, 503)
        client._request.assert_not_awaited()

    async def test_unattributed_production_background_credentials_are_not_loaded(self):
        application = SimpleNamespace(state=SimpleNamespace())
        with patch.dict("os.environ", {"PROTREBOT_DURABLE_AUTH_REQUIRED": "true"}), patch.object(binance_demo, "load_demo_credentials") as credentials:
            with self.assertRaises(BinanceDemoError) as caught:
                binance_demo.client_for_state(application, {})
            self.assertEqual(caught.exception.http_status, 412)
            credentials.assert_not_called()

    async def test_expired_or_corrupt_expiry_blocks_every_private_call(self):
        for expiry in (0, time.time() - 1, True, "9999999999", float("inf"), float("nan")):
            state = {"armed_until": 9999999999}
            client, _ = self.client(state, expiry=expiry)
            with patch.object(v22_commercial, "validate_authoritative_session", AsyncMock()) as authority:
                with self.assertRaises(BinanceDemoError) as caught:
                    await client.signed("GET", "/fapi/v3/account")
            self.assertEqual(caught.exception.http_status, 401)
            authority.assert_not_awaited()
            client._request.assert_not_awaited()
            self.assertIsNone(state["armed_until"])

    async def test_expiry_during_clock_sync_cannot_be_extended_by_mutating_state(self):
        state = {"armed_until": 9999999999, "_auth_expires_at": 101}
        client, _ = self.client(state, expiry=state["_auth_expires_at"])
        state["_auth_expires_at"] = 9999999999
        with patch.object(binance_demo.time, "time", side_effect=[100, 102]), patch.object(
            v22_commercial, "validate_authoritative_session", AsyncMock()
        ):
            with self.assertRaises(BinanceDemoError):
                await client.signed("GET", "/fapi/v3/account")
        client.sync_clock.assert_awaited_once()
        client._request.assert_not_awaited()

    async def test_status_lookup_cannot_renew_background_version_or_expiry(self):
        state = {"_auth_version": 1, "_auth_expires_at": 100, "_session_id": "synthetic-session"}
        application = SimpleNamespace(state=SimpleNamespace(_binance_demo_user_state={"synthetic-user": state}))
        request = SimpleNamespace(app=application, state=SimpleNamespace(member={"id": "synthetic-user", "auth_version": 2}), headers={})
        with patch("app.exchange_connections.session_id", return_value="synthetic-session"):
            current = binance_demo.state_for(request)
        self.assertIs(current, state)
        self.assertEqual(current["_auth_version"], 1)
        self.assertEqual(current["_auth_expires_at"], 100)

    async def test_restored_v21_state_never_restores_private_authority(self):
        current = v21_demo._state_from_payload({"_auth_version": 1, "_auth_expires_at": 9999999999}, "synthetic-user")
        self.assertFalse(current["auto"]["enabled"])
        self.assertNotIn("_auth_version", current)
        self.assertNotIn("_auth_expires_at", current)

    async def test_explicit_grant_captures_verified_expiry_and_rejects_missing_proof(self):
        request = SimpleNamespace(state=SimpleNamespace(member={"id": "synthetic-user", "auth_version": 2}))
        state = {}
        expiry = time.time() + 3600
        with patch("app.v25_execution.authenticated_live_expiry", return_value=expiry):
            binance_demo.bind_demo_grant(request, state)
        self.assertEqual(state["_auth_version"], 2)
        self.assertEqual(state["_auth_expires_at"], expiry)
        with patch("app.v25_execution.authenticated_live_expiry", return_value=None):
            with self.assertRaises(HTTPException) as caught:
                binance_demo.bind_demo_grant(request, state)
        self.assertEqual(caught.exception.status_code, 401)
        self.assertIsNone(state["armed_until"])
