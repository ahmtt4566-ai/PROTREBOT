"""Offline Master Trade safety regressions; no exchange transport is used."""
import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException

from app import account_settings, exchange_connections as connections
from app import live_notifications, original_v2_live, v22_commercial
from app import v25_execution as live
from test_exchange_connections_server_time import FakeBinanceHttp


@pytest.fixture
def start_context(monkeypatch):
    state = live.initial_state()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(v25_execution=state)))
    monkeypatch.setattr(live, "execution_owner", Mock(return_value={"id": "owner", "role": "OWNER"}))
    monkeypatch.setattr(live, "live_auto_start_gate", Mock(return_value=(True, "ready")))
    monkeypatch.setattr(live, "live_credentials_status", Mock(return_value=("offline-key", "offline-secret", "fingerprint")))
    monkeypatch.setattr(live, "session_id", Mock(return_value="offline-session"))
    monkeypatch.setattr(live, "authenticated_live_expiry", Mock(return_value=time.time() + 600))
    monkeypatch.setattr(live, "public_status", Mock(return_value={}))
    monkeypatch.setattr(live, "persist_state", Mock())
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(account_settings, "require_totp", AsyncMock())
    return request, state


def test_live_auto_missing_mfa_never_changes_authority(start_context):
    request, state = start_context
    before = copy.deepcopy(state)
    with pytest.raises(HTTPException) as error:
        asyncio.run(live.v25_auto_start(request, live.AutoStartConfirmation(confirmation="CANLI OTOMATİK")))
    assert error.value.status_code == 401
    assert error.value.detail["code"] == "LIVE_AUTO_MFA_REQUIRED"
    assert state == before
    account_settings.require_totp.assert_not_awaited()
    live.live_credentials_status.assert_not_called()
    live.persist_state.assert_not_called()


@pytest.mark.parametrize("reason", ["Geçersiz 2FA kodu.", "Bu 2FA kodu zaten kullanıldı."])
def test_invalid_or_replayed_mfa_never_starts(start_context, reason):
    request, state = start_context
    before = copy.deepcopy(state)
    account_settings.require_totp.side_effect = HTTPException(401, reason)
    with pytest.raises(HTTPException) as error:
        asyncio.run(live.v25_auto_start(request, live.AutoStartConfirmation(
            confirmation="CANLI OTOMATİK", totp_code="123456")))
    assert error.value.status_code == 401
    assert state == before
    live.live_credentials_status.assert_not_called()
    live.persist_state.assert_not_called()


def test_valid_mfa_rechecks_gate_before_start(start_context):
    request, state = start_context
    asyncio.run(live.v25_auto_start(request, live.AutoStartConfirmation(
        confirmation="CANLI OTOMATİK", totp_code="123456")))
    account_settings.require_totp.assert_awaited_once_with(
        request, {"id": "owner", "role": "OWNER"}, "123456", required=True)
    assert live.live_auto_start_gate.call_count == 2
    assert state["auto"]["enabled"] and state["live_auto_trade"]


def test_mfa_cannot_bypass_changed_arm_gate(start_context):
    request, state = start_context
    before = copy.deepcopy(state)
    live.live_auto_start_gate.side_effect = [(True, "ready"), (False, "ARM expired")]
    with pytest.raises(HTTPException) as error:
        asyncio.run(live.v25_auto_start(request, live.AutoStartConfirmation(
            confirmation="CANLI OTOMATİK", totp_code="123456")))
    assert error.value.status_code == 423
    assert state == before
    live.persist_state.assert_not_called()


def test_mfa_disabled_account_preserves_existing_start(start_context):
    request, state = start_context
    account_settings.enabled.return_value = False
    asyncio.run(live.v25_auto_start(request, live.AutoStartConfirmation(confirmation="CANLI OTOMATİK")))
    assert state["auto"]["enabled"]
    account_settings.require_totp.assert_not_awaited()

@pytest.mark.parametrize("change", ["other-start", "timeframe"])
def test_async_account_mfa_read_cannot_overwrite_new_start_or_policy(start_context, change):
    request, state = start_context
    async def enabled(*args):
        if change == "other-start":
            state["auto"].update(enabled=True, session_until=time.time() + 600)
        else:
            state["policy"]["interval"] = "1h"
        return False
    account_settings.enabled.side_effect = enabled
    with pytest.raises(HTTPException) as error:
        asyncio.run(live.v25_auto_start(request, live.AutoStartConfirmation(confirmation="CANLI OTOMATİK")))
    assert error.value.status_code == (409 if change == "other-start" else 423)
    live.live_credentials_status.assert_not_called()
    live.persist_state.assert_not_called()


@pytest.mark.parametrize("code", [{}, [], None, "unexpected"])
def test_malformed_exchange_code_uses_controlled_generic_error(code):
    assert connections.exchange_rejection_message(code) == connections.exchange_rejection_message(None)


@pytest.mark.parametrize("code", [-2014, -2015, -1022, -2019, -1013, -9999])
def test_upstream_errors_never_expose_either_credential(monkeypatch, code):
    key, secret = "private-key-123456789", "private-secret-123456789"
    http = SimpleNamespace(request=AsyncMock(return_value=httpx.Response(
        400, json={"code": code, "msg": f"{key} {secret}"}, request=httpx.Request("GET", "https://offline.invalid"))))
    monkeypatch.setattr(live, "log_event", AsyncMock())
    client = live.BinanceLiveClient(http, key, secret, diagnostic_state=live.initial_state())
    with pytest.raises(live.LiveExchangeError) as error:
        asyncio.run(client._request("GET", "/fapi/v3/account", {}, signed=True))
    assert key not in str(error.value) and secret not in str(error.value)
    assert connections._safe_exchange_message({"code": code, "msg": f"{key} {secret}"}, key) == str(error.value)
    assert key not in str(live.log_event.await_args) and secret not in str(live.log_event.await_args)


def test_network_error_diagnostics_hide_unlabelled_credentials(monkeypatch):
    key, secret = "private-key-123456789", "private-secret-123456789"
    state = live.initial_state()
    monkeypatch.setattr(live, "log_event", AsyncMock())
    client = live.BinanceLiveClient(SimpleNamespace(timeout=None), key, secret, diagnostic_state=state)
    asyncio.run(client._record_request_error("GET", "/fapi/v3/account", httpx.ConnectError(
        f"connection failed {key} {secret}"), params={}, elapsed_ms=1))
    diagnostic = json.dumps(state["events"]) + str(live.log_event.await_args)
    assert key not in diagnostic and secret not in diagnostic
    assert "[gizli]" in diagnostic


@pytest.mark.parametrize("path,field", [
    ("/sapi/v1/account/apiRestrictions", "enableFutures"),
    ("/fapi/v3/account", "canTrade"),
])
def test_missing_trading_permission_is_explicit_and_readonly(monkeypatch, path, field):
    connections._SERVER_TIME_CACHE.clear()
    connections._SERVER_TIME_REJECTION_CACHE.clear()
    connections.BINANCE_RATE_LIMITER.reset()
    http = FakeBinanceHttp()
    original_get = http.get
    async def get(url, headers=None):
        response = await original_get(url, headers=headers)
        if httpx.URL(url).path == path:
            response = httpx.Response(200, json={**response.json(), field: False},
                                      request=httpx.Request("GET", url))
        return response
    http.get = get
    with pytest.raises(connections.VaultError, match="Futures"):
        asyncio.run(connections.test_binance_credentials(http, "LIVE", "offline-key-123", "offline-secret-123"))
    assert not any("order" in path for path in http.paths)


@pytest.mark.parametrize("kind", [
    "PROTECTION_FAIL", "TP_MONITORING_PERSISTED", "LIVE_STREAM_RECONNECT", "LIVE_ORDER_BLOCKED",
])
def test_live_safety_bell_is_owner_scoped_and_read_state_survives_restart(monkeypatch, kind):
    state = live.initial_state()
    state["live_session_authorization"]["user_id"] = "owner"
    monkeypatch.setattr(live, "persist_state", Mock())
    user = Mock(return_value={"id": "owner", "role": "OWNER"})
    monkeypatch.setattr(v22_commercial, "authenticated_user", user)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(v25_execution=state)))
    event = live.add_event(state, kind, "Türkçe güvenlik uyarısı")
    unrelated = [{"id": "demo-1", "timestamp": "", "read": False}]
    alert_id = f"live:{event['id']}"
    result = original_v2_live.merge_bell(request, unrelated)
    assert result[0]["id"] == alert_id and result[0]["read"] is False
    assert result[1] == unrelated[0]
    assert original_v2_live.merge_bell(request, unrelated, read_id=alert_id)[0]["read"]
    restored = live.sanitized_state(state)
    assert alert_id in restored["live_notification_read_ids"]
    request.app.state.v25_execution = restored
    assert live_notifications.merge_bell(request, [])[0]["read"]
    user.return_value = {"id": "customer", "role": "CUSTOMER"}
    assert live_notifications.merge_bell(request, unrelated) == unrelated
    with pytest.raises(HTTPException) as error:
        live_notifications.merge_bell(request, [], read_id=alert_id)
    assert error.value.status_code == 404


@pytest.fixture
def vault_context(monkeypatch):
    pool = SimpleNamespace(execute=AsyncMock())
    state = live.initial_state()
    state["live_session_authorization"].update(user_id="owner", session_id="offline-session")
    state["auto"].update(enabled=True, session_until=time.time() + 600)
    state["original_v2_live"] = {"enabled": True, "owner_id": "owner"}
    request = SimpleNamespace(
        state=SimpleNamespace(member={"id":"owner","role":"OWNER"}),
        app=SimpleNamespace(state=SimpleNamespace(
            v25_execution=state, db_pool=pool, http=object(), exchange_vault={"ready":True})),
    )
    monkeypatch.setattr(connections, "_SESSION_CACHE", {})
    monkeypatch.setattr(connections, "_SESSION_META", {})
    monkeypatch.setattr(connections, "session_id", Mock(return_value="offline-session"))
    monkeypatch.setattr(live, "session_id", Mock(return_value="offline-session"))
    monkeypatch.setattr(connections, "_require_ready", AsyncMock(return_value=pool))
    monkeypatch.setattr(connections, "ensure_schema", AsyncMock())
    monkeypatch.setattr(connections, "ensure_session_cache", AsyncMock())
    monkeypatch.setattr(connections, "encrypt_credentials", Mock(return_value=b"encrypted-payload"))
    monkeypatch.setattr(live, "persist_state", Mock())
    monkeypatch.setattr(live, "connect_read_only_for_request", AsyncMock(return_value={"connected":True}))
    monkeypatch.setattr(connections, "test_binance_credentials", AsyncMock(return_value={
        "tested_at":"2026-10-11T00:00:00Z","active_positions":0,
        "api_permissions":{"withdrawal_check":"VERIFIED","enableWithdrawals":False},
    }))
    return request, state, pool


def test_live_key_save_change_activate_deactivate_delete_stays_locked(vault_context):
    request, state, pool = vault_context
    for key in ["offline-key-first", "offline-key-second"]:
        result = asyncio.run(connections.exchange_connection_save(request, connections.SaveCredentialsRequest(
            mode="LIVE", api_key=key, secret_key="offline-secret-safe", confirmation="CANLI KASAYA KAYDET")))
        assert result["connections"]["LIVE"]["configured"]
        assert key not in json.dumps(result) and "offline-secret-safe" not in json.dumps(result)
        connections.encrypt_credentials.assert_called_with(key, "offline-secret-safe", mode="LIVE")
        assert state["real_trading_locked"] and not state["auto"]["enabled"]
        assert state["original_v2_live"]["paused"]
    result = asyncio.run(connections.exchange_connection_activate(request, connections.ConnectionActionRequest(
        mode="LIVE", confirmation="CANLI SALT OKUNUR BAĞLANTIYI AÇ")))
    assert result["connected"]
    assert state["real_trading_locked"]
    with pytest.raises(HTTPException) as error:
        asyncio.run(connections.exchange_connection_delete(request, connections.ConnectionActionRequest(
            mode="LIVE", confirmation="ANAHTARI KALICI SİL")))
    assert error.value.status_code == 409
    asyncio.run(connections.exchange_connection_deactivate(request, connections.ConnectionActionRequest(
        mode="LIVE", confirmation="BAĞLANTIYI KAPAT")))
    result = asyncio.run(connections.exchange_connection_delete(request, connections.ConnectionActionRequest(
        mode="LIVE", confirmation="ANAHTARI KALICI SİL")))
    assert not result["connections"]["LIVE"]["configured"]
    assert not connections._SESSION_CACHE and not connections._SESSION_META
    assert state["real_trading_locked"] and not state["auto"]["enabled"]
    writes = str(pool.execute.await_args_list)
    assert "offline-secret-safe" not in writes
    assert "offline-key-first" not in writes and "offline-key-second" not in writes


def test_live_key_delete_cannot_bypass_verified_open_positions(vault_context):
    request, state, pool = vault_context
    connections._SESSION_CACHE[("offline-session","LIVE")] = ("offline-key-first","offline-secret-safe")
    connections._SESSION_META[("offline-session","LIVE")] = {"configured":True,"active":False}
    connections.test_binance_credentials.return_value = {"active_positions":1}
    with pytest.raises(HTTPException) as error:
        asyncio.run(connections.exchange_connection_delete(request, connections.ConnectionActionRequest(
            mode="LIVE", confirmation="ANAHTARI KALICI SİL")))
    assert error.value.status_code == 409
    pool.execute.assert_not_awaited()
    assert connections._SESSION_CACHE
