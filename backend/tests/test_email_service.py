"""Offline provider contract tests: no real mail or network."""
import asyncio
import json
import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import email_service as mail


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    for name in ("EMAIL_PROVIDER", "RESEND_API_KEY", "EMAIL_FROM", "EMAIL_REPLY_TO",
                 "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN",
                 "GMAIL_FROM_EMAIL", "GMAIL_FROM_NAME"):
        monkeypatch.delenv(name, raising=False)


def resend_configuration(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "offline-provider-fixture")
    monkeypatch.setenv("EMAIL_FROM", "Old Brand <no-reply@kaistrade.com>")


def send():
    mail.send_auth_email(
        to_email="sample@icloud.com", display_name="Ada",
        subject=mail.VERIFY_SUBJECT, title="Hesabını doğrula",
        action_url="https://kaistrade.com/verify-email?token=offline-token",
        action_label="E-posta adresimi doğrula",
    )


def mocked_https(handler):
    client = httpx.Client
    transport = httpx.MockTransport(handler)
    return patch.object(mail.httpx, "Client", side_effect=lambda **kwargs: client(transport=transport, **kwargs))


def test_resend_turkish_template_sender_and_plain_text(monkeypatch):
    resend_configuration(monkeypatch)
    monkeypatch.setenv("EMAIL_REPLY_TO", "support@kaistrade.com")
    requests = []

    def accept(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "offline-message"})

    with mocked_https(accept), patch.object(mail, "build") as gmail:
        send()
    gmail.assert_not_called()
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST" and str(request.url) == "https://api.resend.com/emails"
    assert request.headers["User-Agent"].startswith("KaisTrade")
    payload = json.loads(request.content)
    assert payload["subject"] == "KaisTrade hesabını doğrula"
    assert payload["from"] == "KaisTrade <no-reply@kaistrade.com>"
    assert payload["reply_to"] == "support@kaistrade.com"
    assert payload["to"] == ["sample@icloud.com"]
    for body in (payload["html"], payload["text"]):
        assert "https://kaistrade.com/verify-email?token=offline-token" in body
        assert "Bu işlemi sen yapmadıysan bu maili yok sayabilirsin" in body
        assert "ProTreBot" not in body and "PROTREBOT" not in body
    assert 'lang="tr"' in payload["html"]
    assert "E-posta adresimi doğrula" in payload["html"]


def test_absent_provider_preserves_gmail_even_with_resend_configuration(monkeypatch):
    for name in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
        monkeypatch.setenv(name, "offline-google-fixture")
    monkeypatch.setenv("RESEND_API_KEY", "offline-unused-fixture")
    monkeypatch.setenv("EMAIL_FROM", "no-reply@kaistrade.com")
    gmail = MagicMock()
    gmail.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "offline-gmail"}
    with patch.object(mail, "build", return_value=gmail) as build, patch.object(mail.httpx, "Client") as https:
        send()
    assert mail.provider() == "smtp"
    build.assert_called_once()
    https.assert_not_called()


@pytest.mark.parametrize("missing", ["RESEND_API_KEY", "EMAIL_FROM"])
def test_missing_resend_configuration_never_falls_back(monkeypatch, missing):
    resend_configuration(monkeypatch)
    monkeypatch.delenv(missing)
    with patch.object(mail, "build") as gmail, patch.object(mail.httpx, "Client") as https:
        with pytest.raises(mail.EmailDeliveryError, match=missing):
            send()
    gmail.assert_not_called()
    https.assert_not_called()


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_provider_errors_are_explicit_and_logs_redacted(monkeypatch, caplog, status):
    resend_configuration(monkeypatch)
    caplog.set_level(logging.WARNING, logger=mail.__name__)
    with mocked_https(lambda request: httpx.Response(status, json={"message": "private-provider-body"})):
        with pytest.raises(mail.EmailDeliveryError) as error:
            send()
    assert error.value.code == str(status)
    assert "s***@icloud.com" in caplog.text
    assert str(status) in caplog.text
    for private in ("sample@icloud.com", "offline-provider-fixture", "offline-token", "private-provider-body"):
        assert private not in caplog.text and private not in str(error.value)


@pytest.mark.parametrize("body", [b"not-json", b"{}", b'{"id":""}', b'{"id":123}', b"null"])
def test_success_without_valid_delivery_id_is_failure(monkeypatch, body):
    resend_configuration(monkeypatch)
    with mocked_https(lambda request: httpx.Response(200, content=body)):
        with pytest.raises(mail.EmailDeliveryError) as error:
            send()
    assert error.value.code == "invalid_response"


def test_timeout_does_not_retry_or_leak_transport_details(monkeypatch, caplog):
    resend_configuration(monkeypatch)
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("private transport details", request=request)

    with mocked_https(timeout):
        with pytest.raises(mail.EmailDeliveryError) as error:
            send()
    assert len(calls) == 1 and error.value.code == "timeout"
    assert "private transport details" not in caplog.text + str(error.value)


@pytest.mark.parametrize("name,value", [
    ("EMAIL_FROM", "not-an-address"), ("EMAIL_FROM", "a@kaistrade.com\r\nBcc: b@example.test"),
    ("EMAIL_REPLY_TO", "invalid"), ("EMAIL_PROVIDER", "unsupported"),
])
def test_invalid_configuration_is_explicit_without_network(monkeypatch, name, value):
    resend_configuration(monkeypatch)
    monkeypatch.setenv(name, value)
    with patch.object(mail.httpx, "Client") as https, patch.object(mail, "build") as gmail:
        assert not mail.configured()
        with pytest.raises(mail.EmailDeliveryError):
            send()
    https.assert_not_called()
    gmail.assert_not_called()


def test_resend_public_route_and_cors_headers_preserve_other_guards():
    from app import main

    assert "/api/v22/auth/resend-verification" in main.MEMBER_PUBLIC_PATHS
    assert "/api/v22/account/overview" not in main.MEMBER_PUBLIC_PATHS
    cors = next(item for item in main.app.user_middleware if item.cls.__name__ == "CORSMiddleware")
    assert set(cors.kwargs["expose_headers"]) == {"Retry-After", "X-Email-Delivery-Error"}


def test_resend_health_checks_configuration_without_delivery(monkeypatch):
    from app import main

    resend_configuration(monkeypatch)
    with patch.object(mail.httpx, "Client") as https, patch.object(mail, "build") as gmail:
        configured = asyncio.run(main.health_check_gmail(main.app))
        monkeypatch.delenv("RESEND_API_KEY")
        missing = asyncio.run(main.health_check_gmail(main.app))
    assert configured["status"] == "CONFIGURED"
    assert missing["status"] == "NOT CONFIGURED"
    https.assert_not_called()
    gmail.assert_not_called()
