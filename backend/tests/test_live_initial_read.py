"""Initial account-vault reads stay offline and never submit orders."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app import account_settings, exchange_connections as vault, v25_execution as live


def test_missing_first_account_row_is_retried_before_locking(monkeypatch):
    credentials = ("offline-api-key-long", "offline-secret-long")
    row = {
        "user_id": "owner", "mode": "LIVE", "encrypted_payload": b"offline",
        "fingerprint": vault.key_fingerprint(credentials[0]), "active": True,
        "account_summary": {"api_permissions": {"withdrawal_check": "VERIFIED", "enableWithdrawals": False}},
    }
    pool = SimpleNamespace(fetch=AsyncMock(return_value=[]), fetchrow=AsyncMock(side_effect=[None, row]))
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)),
        state=SimpleNamespace(member={"id": "owner", "role": "OWNER"}),
        headers={"x-protrebot-session": "offline"},
    )
    monkeypatch.setattr(vault, "_SESSION_CACHE", {})
    monkeypatch.setattr(vault, "_SESSION_META", {})
    monkeypatch.setattr(vault, "ensure_schema", AsyncMock())
    monkeypatch.setattr(vault, "decrypt_credentials", Mock(return_value=credentials))
    monkeypatch.setattr(vault, "_bind_live_credential", Mock())
    lock = Mock()
    monkeypatch.setattr(vault, "_lock_runtime", lock)
    asyncio.run(vault.ensure_session_cache(request))
    assert vault.session_credentials(request, "LIVE") == credentials
    assert pool.fetchrow.await_count == 2
    lock.assert_not_called()


@pytest.mark.parametrize("mfa_enabled", [True, False])
def test_status_reads_balance_when_vault_ready_without_enabling_entries(monkeypatch, mfa_enabled):
    state = live.initial_state()
    state["lock"] = asyncio.Lock()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(v25_execution=state)),
        state=SimpleNamespace(member={"id": "owner", "role": "OWNER", "auth_version": 1}),
        headers={"x-protrebot-session": "offline"}, method="GET",
    )
    monkeypatch.setattr(vault, "ensure_session_cache", AsyncMock())
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=mfa_enabled))
    monkeypatch.setattr(live, "live_credentials_status", Mock(return_value=("offline-key-long", "offline-secret-long", "fingerprint")))
    snapshot = {"wallet_balance": 100, "available_balance": 100, "positions": [], "open_orders": []}
    client = SimpleNamespace(time_offset_ms=0)
    monkeypatch.setattr(live, "client_for", Mock(return_value=client))
    read = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(live, "account_snapshot", read)
    monkeypatch.setattr(live, "persist_state", Mock())
    monkeypatch.setattr(live, "public_status", Mock(side_effect=lambda *_: {"account": state["snapshot"]}))
    if not mfa_enabled:
        with pytest.raises(HTTPException) as error:
            asyncio.run(live.v25_status(request))
        assert error.value.status_code == 403
        read.assert_not_awaited()
        return
    result = asyncio.run(live.v25_status(request))
    assert result["account"]["wallet_balance"] == 100
    read.assert_awaited_once()
    assert state["live_session_authorization"]["user_id"] == "owner"
    assert state["armed_until"] == 0
    assert state["auto"]["enabled"] is False
    assert state["real_trading_locked"] is True


@pytest.mark.parametrize("active", [False, True])
def test_missing_or_inactive_vault_still_fails_closed(monkeypatch, active):
    row = {"user_id": "owner", "active": False} if not active else None
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value=row))
    result = asyncio.run(vault._read_live_vault_record(pool, "owner", retry_missing=True))
    assert result == row
    assert pool.fetchrow.await_count == (1 if row else 2)


def test_background_without_monitoring_grant_waits_without_vault_retry(monkeypatch):
    state = live.initial_state()
    application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
    authority = AsyncMock()
    monkeypatch.setattr(live, "background_authorization_active", authority)
    resolver = AsyncMock()
    monkeypatch.setattr(live, "session_credentials_for_identity", resolver)
    assert asyncio.run(live.auto_session_credentials(application, state)) == ("", "")
    assert state["credential_resolution_reason"] == "monitoring_authorization_pending"
    assert state["credential_resolution_retryable"] is False
    authority.assert_not_awaited()
    resolver.assert_not_awaited()


def test_background_account_read_recovers_missing_row_and_warms_owner_cache(monkeypatch):
    credentials = ("offline-api-key-long", "offline-secret-long")
    fingerprint = vault.key_fingerprint(credentials[0])
    row = {
        "user_id": "owner", "mode": "LIVE", "encrypted_payload": b"offline",
        "fingerprint": fingerprint, "active": True,
        "account_summary": {"api_permissions": {"withdrawal_check": "VERIFIED", "enableWithdrawals": False}},
    }
    pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=[None, row]))
    application = SimpleNamespace(state=SimpleNamespace(db_pool=pool))
    monkeypatch.setattr(vault, "_SESSION_CACHE", {})
    monkeypatch.setattr(vault, "_SESSION_META", {})
    monkeypatch.setattr(vault, "authoritative_live_owner_active", AsyncMock(return_value=True))
    monkeypatch.setattr(vault, "ensure_exchange_vault", AsyncMock(return_value=True))
    monkeypatch.setattr(vault, "ensure_schema", AsyncMock())
    monkeypatch.setattr(vault, "decrypt_credentials", Mock(return_value=credentials))
    result = asyncio.run(vault.session_credentials_for_identity(application, "old-session", "owner", "LIVE", fingerprint))
    assert result == credentials
    assert pool.fetchrow.await_count == 2
    assert vault._SESSION_CACHE[("owner:owner", "LIVE")] == credentials
    assert all("protrebot_exchange_account_vault" in call.args[0] for call in pool.fetchrow.await_args_list)
