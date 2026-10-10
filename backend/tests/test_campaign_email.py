import json
from unittest.mock import patch

import httpx
import pytest

from app import campaign_email, email_service
from app.campaign_content import unsubscribe_token
from test_email_service import mocked_https, resend_configuration


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    resend_configuration(monkeypatch)
    monkeypatch.setenv("APP_BASE_URL", "https://kaistrade.com")
    monkeypatch.setenv("CAMPAIGN_EMAIL_ENABLED", "true")


def send(test=False):
    campaign_email.send_campaign(to_email="member@example.test", campaign={"subject": "Bilgilendirme", "body": "Yeni özellik.\n\nhttps://kaistrade.com/settings"},
        token=unsubscribe_token(b"offline-secret", "member", 1700000000), dedupe_key="campaign:one:member", test=test)


def test_only_fake_transport_used_text_matches_html_headers_and_stable_key():
    requests = []
    with mocked_https(lambda req: requests.append(req) or httpx.Response(200, json={"id": "accepted"})):
        send(test=True); send(test=True)
    body = json.loads(requests[0].content)
    assert body["subject"] == "[TEST] Bilgilendirme"
    assert "yatırım tavsiyesi değildir" in body["text"]
    assert body["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert body["headers"]["List-Unsubscribe"].startswith("<https://kaistrade.com/announcements/unsubscribe?token=")
    assert "member@example.test" not in body["text"] + body["html"]
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]


@pytest.mark.parametrize("status,code", [(422, "permanent_rejection"), (400, "permanent_rejection"), (429, "provider_rejected"), (503, "provider_rejected")])
def test_short_provider_error_codes_and_no_response_text(status, code):
    with mocked_https(lambda _: httpx.Response(status, json={"message": "private secret address"})):
        with pytest.raises(email_service.EmailDeliveryError) as error:
            send()
    assert error.value.code == code and "private" not in str(error.value)


def test_disabled_provider_never_contacts_resend(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_EMAIL_ENABLED", "false")
    with patch.object(campaign_email.httpx, "Client") as client:
        with pytest.raises(email_service.EmailDeliveryError) as error:
            send()
    assert error.value.code == "provider_unavailable"
    client.assert_not_called()


@pytest.mark.parametrize("missing_key", [True, False])
def test_missing_key_or_invalid_origin_never_contacts_provider(monkeypatch, missing_key):
    if missing_key:
        monkeypatch.delenv("RESEND_API_KEY")
    else:
        monkeypatch.setenv("APP_BASE_URL", "not-an-origin")
    with patch.object(campaign_email.httpx, "Client") as client:
        with pytest.raises(email_service.EmailDeliveryError) as error:
            send()
    assert error.value.code == "provider_unavailable"
    client.assert_not_called()
