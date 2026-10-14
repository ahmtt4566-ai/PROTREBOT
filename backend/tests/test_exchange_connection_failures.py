"""Credential verification failures must remain explicit and read-only."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

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
    assert stage in public_error.detail
    assert "HTTP 400" in public_error.detail
    assert str(code) in public_error.detail
    assert path in caplog.text and str(code) in caplog.text
    for private in (key, secret, "private-signature"):
        assert private not in public_error.detail + caplog.text
    assert http.get.await_count == 1
