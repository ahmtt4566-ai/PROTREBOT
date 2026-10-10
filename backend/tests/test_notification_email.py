import json
from urllib.parse import urlsplit
from unittest.mock import patch

import httpx
import pytest

from app import email_service as mail
from app import notification_email as notices
from test_email_service import mocked_https, resend_configuration


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    resend_configuration(monkeypatch)
    monkeypatch.setenv("APP_BASE_URL", "https://kaistrade.com")
    monkeypatch.setenv("APPROVAL_EMAIL_ENABLED", "true")


def send(kind="approval.decision", payload=None):
    notices.send_notification(to_email="recipient@example.test", kind=kind,
                              payload=payload or {"approval_id": "private-target-id", "result": 1}, dedupe_key="stable-id")


@pytest.mark.parametrize("kind,payload", [
    ("approval.pending_digest", {"bucket": 100, "count": 5}),
    *[("approval.decision", {"approval_id": "private-target-id", "result": i}) for i in range(1, 5)],
])
def test_content_contains_only_general_turkish_notice_and_canonical_panel_url(kind, payload):
    payloads = []
    with mocked_https(lambda request: payloads.append(json.loads(request.content)) or httpx.Response(200, json={"id": "accepted"})):
        send(kind, payload)
    body = payloads[0]
    assert body["subject"] == notices.SUBJECT
    for content in (body["subject"], body["text"], body["html"]):
        for forbidden in ("recipient@example.test", "private-target-id", "reason", "secret", "token", "password", "Ada"):
            assert forbidden not in content
    _, text, html = notices.notification_content(kind, payload)
    link = "https://kaistrade.com" + ("/admin" if kind.endswith("digest") else "/moderator")
    assert link in text and f'href="{link}"' in html
    assert urlsplit(link).netloc == "kaistrade.com" and urlsplit(link).query == ""
    assert html.count("<a ") == 1 and ">Panele git</a>" in html
    assert "/approve" not in html and "/reject" not in html


def test_resend_transport_reuses_validation_sender_reply_to_and_stable_idempotency(monkeypatch):
    monkeypatch.setenv("EMAIL_REPLY_TO", "support@kaistrade.com")
    requests = []
    with patch.object(mail, "validate_configuration", wraps=mail.validate_configuration) as validate, \
            mocked_https(lambda request: requests.append(request) or httpx.Response(200, json={"id": "accepted"})):
        send(); send()
    assert validate.call_count == 2
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]
    assert str(requests[0].url) == "https://api.resend.com/emails"
    body = json.loads(requests[0].content)
    assert body["from"] == "KaisTrade <no-reply@kaistrade.com>" and body["reply_to"] == "support@kaistrade.com"


@pytest.mark.parametrize("setting,value", [
    ("RESEND_API_KEY", ""), ("APPROVAL_EMAIL_ENABLED", "false"),
    ("EMAIL_PROVIDER", "smtp"), ("APP_BASE_URL", "https://evil.test"),
    ("APP_BASE_URL", "https://kaistrade.com.evil.test"), ("APP_BASE_URL", "https://kaistrade.com/admin"),
])
def test_missing_disabled_or_unsafe_configuration_never_sends(monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with patch.object(notices.httpx, "Client") as client:
        with pytest.raises(mail.EmailDeliveryError):
            send()
    client.assert_not_called()


@pytest.mark.parametrize("response,code", [
    (httpx.Response(500, json={"message": "private provider text"}), "provider_rejected"),
    (httpx.Response(200, json={}), "invalid_response"),
    (httpx.Response(200, content="not json"), "invalid_response"),
])
def test_provider_rejection_or_bad_response_never_leaks_provider_text(response, code):
    with mocked_https(lambda _: response):
        with pytest.raises(mail.EmailDeliveryError) as exc:
            send()
    assert exc.value.code == code and "private" not in str(exc.value)
