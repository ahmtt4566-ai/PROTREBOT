"""Production mail URL policy, using fake transports only."""
import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest
from app import email_service as mail
from app import v22_commercial as auth
from fastapi import HTTPException

PRODUCTION_SIGNALS = (
    "PROTREBOT_ENVIRONMENT", "ENVIRONMENT", "APP_ENV", "NODE_ENV", "VERCEL_ENV",
    "RAILWAY_ENVIRONMENT_NAME", "RENDER", "VERCEL", "DYNO",
)
INVALID_BASES = (
    None, "", " ", "http://kaistrade.com", "https://frontend-nu-two-18.vercel.app",
    "https://other.example", "https://www.kaistrade.com", "https://kaistrade.com:443",
    "https://kaistrade.com:8000", "https://kaistrade.com/", "https://kaistrade.com/path",
    "https://kaistrade.com?next=x", "https://kaistrade.com#fragment",
    "https://kaistrade.com@other.example", "https://user:private-password@kaistrade.com",
)
MAIL_PATHS = ("/verify-email?token=private-token", "/reset-password?token=private-token",
              "/profile?email_token=private-token", "/login")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in (*PRODUCTION_SIGNALS, "APP_BASE_URL", "PROTREBOT_EXPOSE_DEV_TOKENS",
                 "EMAIL_PROVIDER", "RESEND_API_KEY", "EMAIL_FROM", "EMAIL_REPLY_TO",
                 "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def configure(monkeypatch, provider="resend"):
    monkeypatch.setenv("EMAIL_PROVIDER", provider)
    if provider == "resend":
        monkeypatch.setenv("RESEND_API_KEY", "private-provider-fixture")
        monkeypatch.setenv("EMAIL_FROM", "noreply@kaistrade.com")
    else:
        for name in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
            monkeypatch.setenv(name, "private-provider-fixture")


def send(path="/verify-email?token=private-token", *, origin="https://kaistrade.com",
         verification_v2=False):
    mail.send_auth_email(
        to_email="private-recipient@example.test", display_name="Ada",
        subject="KaisTrade", title="Hesap işlemi",
        action_url=origin + path, action_label="Devam et",
        information_only=path == "/login", verification_v2=verification_v2,
    )


def test_missing_production_base_never_constructs_transport(monkeypatch):
    monkeypatch.setenv("PROTREBOT_ENVIRONMENT", "production")
    configure(monkeypatch)
    with (
        patch.object(mail.httpx, "Client", side_effect=AssertionError("Transport must not run")) as https,
        patch.object(mail, "build", side_effect=AssertionError("Transport must not run")) as gmail,
        pytest.raises(mail.EmailDeliveryError, match="APP_BASE_URL"),
    ):
        send()
    https.assert_not_called()
    gmail.assert_not_called()


@pytest.mark.parametrize("base", INVALID_BASES)
@pytest.mark.parametrize("provider", ["resend", "smtp"])
def test_invalid_production_base_blocks_both_providers(monkeypatch, caplog, base, provider):
    monkeypatch.setenv("PROTREBOT_ENVIRONMENT", "production")
    if base is not None:
        monkeypatch.setenv("APP_BASE_URL", base)
    configure(monkeypatch, provider)
    caplog.set_level(logging.ERROR, logger=mail.__name__)
    with patch.object(mail.httpx, "Client") as https, patch.object(mail, "build") as gmail:
        assert not mail.configured()
        assert "APP_BASE_URL" in mail.unavailable_message()
        with pytest.raises(mail.EmailDeliveryError, match="APP_BASE_URL"):
            send()
    https.assert_not_called()
    gmail.assert_not_called()
    assert "APP_BASE_URL" in caplog.text
    for secret in ("private-password", "private-provider-fixture", "private-token",
                   "private-recipient@example.test"):
        assert secret not in caplog.text


@pytest.mark.parametrize("base", INVALID_BASES)
def test_startup_rejects_invalid_base_before_http_or_storage(monkeypatch, base):
    from app import main
    monkeypatch.setenv("PROTREBOT_ENVIRONMENT", "production")
    if base is not None:
        monkeypatch.setenv("APP_BASE_URL", base)
    async def start():
        async with main.lifespan(main.app):
            pytest.fail("Invalid production configuration started")
    with (
        patch.object(main, "build_http_client") as http,
        patch.object(main, "init_v22_commercial") as storage,
        pytest.raises(mail.EmailDeliveryError, match="APP_BASE_URL"),
    ):
        asyncio.run(start())
    http.assert_not_called()
    storage.assert_not_called()


@pytest.mark.parametrize("key,value", [
    ("PROTREBOT_ENVIRONMENT", "production"), ("ENVIRONMENT", "prod"),
    ("APP_ENV", "production"), ("NODE_ENV", "production"), ("VERCEL_ENV", "production"),
    ("RAILWAY_ENVIRONMENT_NAME", "production"), ("RENDER", "true"),
    ("VERCEL", "1"), ("DYNO", "web.1"),
])
def test_production_or_hosted_signal_cannot_be_overridden_by_local_mode(monkeypatch, key, value):
    monkeypatch.setenv("PROTREBOT_ENVIRONMENT", "development")
    monkeypatch.setenv(key, value)
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:5173")
    with pytest.raises(HTTPException) as error:
        auth.app_base_url()
    assert error.value.status_code == 503
    assert "APP_BASE_URL" in error.value.detail


@pytest.mark.parametrize("path", MAIL_PATHS)
@pytest.mark.parametrize("provider", ["resend", "smtp"])
@pytest.mark.parametrize("origin", [
    "http://kaistrade.com", "https://kaistrade.com:443", "https://kaistrade.com.other.example",
    "https://kaistrade.com@other.example", "https://frontend-nu-two-18.vercel.app",
    "https://user:private-password@kaistrade.com", "https://kais\ntrade.com",
])
def test_all_mail_types_reject_wrong_action_origin(monkeypatch, path, origin, provider):
    monkeypatch.setenv("PROTREBOT_ENVIRONMENT", "production")
    monkeypatch.setenv("APP_BASE_URL", "https://kaistrade.com")
    configure(monkeypatch, provider)
    with (
        patch.object(mail.httpx, "Client") as https,
        patch.object(mail, "build") as gmail,
        pytest.raises(mail.EmailDeliveryError, match="kaistrade.com"),
    ):
        send(path, origin=origin)
    https.assert_not_called()
    gmail.assert_not_called()


@pytest.mark.parametrize("provider", ["resend", "smtp"])
@pytest.mark.parametrize("path,v2", [*((path, False) for path in MAIL_PATHS), (MAIL_PATHS[0], True)])
def test_canonical_production_urls_deliver_all_mail_types(monkeypatch, provider, path, v2):
    monkeypatch.setenv("PROTREBOT_ENVIRONMENT", "production")
    monkeypatch.setenv("APP_BASE_URL", "https://kaistrade.com")
    configure(monkeypatch, provider)
    gmail = MagicMock()
    gmail.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "fake-gmail"}
    with patch.object(mail.httpx, "Client") as https, patch.object(mail, "build", return_value=gmail) as build:
        https.return_value.__enter__.return_value.post.return_value.status_code = 200
        https.return_value.__enter__.return_value.post.return_value.json.return_value = {"id": "fake-resend"}
        send(path, verification_v2=v2)
    assert auth.app_base_url() == "https://kaistrade.com"
    if provider == "resend":
        https.return_value.__enter__.return_value.post.assert_called_once()
        build.assert_not_called()
    else:
        gmail.users.return_value.messages.return_value.send.return_value.execute.assert_called_once()
        https.assert_not_called()


@pytest.mark.parametrize("mode", ["development", "test", ""])
@pytest.mark.parametrize("base", [None, "http://localhost:5173", "http://127.0.0.1:5173",
                                "http://localhost:5173/"])
def test_local_development_urls_and_default_still_work(monkeypatch, mode, base):
    monkeypatch.setenv("PROTREBOT_ENVIRONMENT", mode)
    if base is not None:
        monkeypatch.setenv("APP_BASE_URL", base)
    configure(monkeypatch)
    expected = (base or "http://localhost:5173").rstrip("/")
    assert auth.app_base_url() == expected
    with patch.object(mail.httpx, "Client") as https:
        https.return_value.__enter__.return_value.post.return_value.status_code = 200
        https.return_value.__enter__.return_value.post.return_value.json.return_value = {"id": "fake-local"}
        send(origin=expected)
    https.return_value.__enter__.return_value.post.assert_called_once()


def test_runtime_environment_change_is_rechecked_for_each_send(monkeypatch):
    monkeypatch.setenv("PROTREBOT_ENVIRONMENT", "production")
    monkeypatch.setenv("APP_BASE_URL", "https://kaistrade.com")
    configure(monkeypatch)
    with patch.object(mail.httpx, "Client") as https:
        https.return_value.__enter__.return_value.post.return_value.status_code = 200
        https.return_value.__enter__.return_value.post.return_value.json.return_value = {"id": "fake-first"}
        send()
        https.reset_mock()
        monkeypatch.setenv("APP_BASE_URL", "https://frontend-nu-two-18.vercel.app")
        with pytest.raises(mail.EmailDeliveryError, match="APP_BASE_URL"):
            send()
        https.assert_not_called()


@pytest.mark.parametrize("mode,base", [
    ("production", "https://kaistrade.com"), ("development", "http://localhost:5173"),
    ("test", None), ("", None),
])
def test_valid_startup_configuration_reaches_http_initialization(monkeypatch, mode, base):
    from app import main
    monkeypatch.setenv("PROTREBOT_ENVIRONMENT", mode)
    if base is not None:
        monkeypatch.setenv("APP_BASE_URL", base)
    async def start():
        async with main.lifespan(main.app):
            pytest.fail("Fake HTTP boundary must stop initialization")
    with (
        patch.object(main, "build_http_client", side_effect=RuntimeError("offline HTTP boundary")) as http,
        pytest.raises(RuntimeError, match="offline HTTP boundary"),
    ):
        asyncio.run(start())
    http.assert_called_once()
