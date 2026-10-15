"""Credential verification failures must remain explicit and read-only."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import exchange_connections as connections
from test_exchange_connections_server_time import FakeBinanceHttp


def test_signed_proxy_failure_has_explicit_safe_reason(caplog):
    key, secret = "offline-private-key", "offline-private-secret"
    http = SimpleNamespace(get=AsyncMock(side_effect=httpx.ProxyError(f"{key} {secret} proxy rejected")))
    with caplog.at_level(logging.WARNING), pytest.raises(connections.VaultError) as error:
        asyncio.run(connections._signed_get(http, "https://api.binance.com",
                                           "/sapi/v1/account/apiRestrictions", key, secret, 1))
    assert "proxy" in str(error.value).lower()
    assert key not in str(error.value) + caplog.text and secret not in str(error.value) + caplog.text


def test_ambiguous_binance_rejection_can_show_only_confirmed_public_request_ip():
    message = connections._safe_exchange_message({
        "code": -2015, "msg": "Invalid API-key, IP, or permissions. request ip: 8.8.8.8 offline-private-secret",
    }, "offline-private-key")
    assert "8.8.8.8" in message and "IP" in message
    assert "offline-private-secret" not in message
    assert "veya" in message  # -2015 cannot prove whether key, IP, or permissions caused rejection.


@pytest.mark.parametrize("ip", ["bad-ip", "127.0.0.1", "10.0.0.1"])
def test_unknown_or_private_ip_is_not_presented_as_a_binance_allowlist_address(ip):
    payload = {"code": -2015, "msg": f"request ip: {ip} offline-private-secret"}
    assert connections._safe_exchange_message(payload, "offline-private-key") == connections.exchange_rejection_message(-2015)


def test_futures_disabled_is_rejected_without_account_or_order_requests():
    http = FakeBinanceHttp()
    get = http.get
    async def disabled(url, headers=None):
        if httpx.URL(url).path == "/sapi/v1/account/apiRestrictions":
            http.paths.append(httpx.URL(url).path)
            return httpx.Response(200, json={"enableWithdrawals": False, "enableFutures": False},
                                  request=httpx.Request("GET", url))
        return await get(url, headers=headers)
    http.get = disabled
    connections._SERVER_TIME_CACHE.clear()
    connections._SERVER_TIME_LOCKS.clear()
    connections._SERVER_TIME_REJECTION_CACHE.clear()
    with pytest.raises(connections.VaultError, match="Futures yetkisi kapalı"):
        asyncio.run(connections.test_binance_credentials(http, "LIVE", "offline-key", "offline-secret"))
    assert http.paths == ["/fapi/v1/time", "/sapi/v1/account/apiRestrictions"]


@pytest.mark.parametrize("status,reason", [(403, "IP izinleri"), (451, "bölgeden"), (500, "sunucu hatası")])
def test_non_json_verification_rejection_has_safe_http_reason(status, reason):
    http = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(
        status, text="offline-private-key offline-private-secret", request=httpx.Request("GET", "https://offline.invalid"),
    )))
    with pytest.raises(connections.VaultError) as error:
        asyncio.run(connections._signed_get(http, "https://api.binance.com",
                                           "/sapi/v1/account/apiRestrictions", "offline-private-key", "offline-private-secret", 1))
    assert reason in str(error.value) and str(status) in str(error.value)
    assert "offline-private-key" not in str(error.value) and "offline-private-secret" not in str(error.value)


@pytest.mark.parametrize("path,stage", [
    ("/sapi/v1/account/apiRestrictions", "API izinleri"),
    ("/fapi/v3/account", "Futures hesap"),
    ("/fapi/v1/positionSide/dual", "pozisyon modu"),
])
@pytest.mark.parametrize("code", [-2015, -2008, -3000])
def test_signed_rejection_preserves_safe_stage_status_and_code(path, stage, code, caplog):
    key, secret = "offline-private-key", "offline-private-secret"
    host = "https://api.binance.com" if path.startswith("/sapi/") else "https://fapi.binance.com"
    http = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(
        400, json={"code": code, "msg": f"{key} {secret} signature=private-signature"},
        request=httpx.Request("GET", f"{host}{path}"),
    )))
    with caplog.at_level(logging.WARNING), pytest.raises(connections.VaultError) as error:
        asyncio.run(connections._signed_get(http, host, path, key, secret, 1))
    public_error = connections._exchange_test_http_exception(error.value)
    assert public_error.status_code == 502
    assert isinstance(public_error.detail, dict)
    message = public_error.detail["message"]
    assert stage in message
    assert "HTTP 400" in message
    assert str(code) in message
    assert public_error.detail["endpoint"] == path
    assert public_error.detail["credential_fingerprint"] == connections.key_fingerprint(key)
    assert public_error.detail["exchange_code"] == code
    assert path in caplog.text and str(code) in caplog.text
    for private in (key, secret, "private-signature"):
        assert private not in str(public_error.detail) + caplog.text
    assert http.get.await_count == 1


def test_invalid_api_key_id_is_not_misreported_as_disabled_futures_permissions():
    message = connections.exchange_rejection_message(-2008)
    assert "anahtar kimliğini geçersiz" in message
    assert "Futures izni kapalı" not in message
    assert "veya" not in message


def test_error_code_in_success_http_response_is_still_rejected():
    http = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(
        200, json={"code": -2008, "msg": "offline-private-key signature=offline-secret"},
        request=httpx.Request("GET", "https://api.binance.com/sapi/v1/account/apiRestrictions"),
    )))
    with pytest.raises(connections.BinanceVerificationRejected) as error:
        asyncio.run(connections._signed_get(
            http, "https://api.binance.com", "/sapi/v1/account/apiRestrictions",
            "offline-private-key", "offline-private-secret", 0,
        ))
    assert error.value.exchange_code == -2008
    assert error.value.stage == "api_permissions"
    assert "offline-private-key" not in str(error.value)


@pytest.mark.parametrize("path,body", [
    ("/save", {"mode": "LIVE", "api_key": "offline-private-key", "secret_key": "offline-private-secret", "totp_code": "654321"}),
    ("/save", {"mode": "LIVE", "api_key": "offline-private-key", "secret_key": "offline-private-secret", "confirmation": "CANLI KASAYA KAYDET", "totp_code": "54321"}),
    ("/activate", {"mode": "LIVE", "confirmation": "CANLI SALT OKUNUR BAĞLANTIYI AÇ", "expected_fingerprint": "offline-private-key"}),
])
def test_invalid_connection_request_never_echoes_credentials_or_mfa(path, body, caplog):
    application = FastAPI()
    application.include_router(connections.router)
    with caplog.at_level(logging.WARNING), TestClient(application) as client:
        response = client.post("/api/exchange-connections" + path, json=body)
    assert response.status_code == 422
    for private in ("offline-private-key", "offline-private-secret", "654321", "54321"):
        assert private not in response.text + caplog.text
    assert all("input" not in error for error in response.json()["detail"])
