"""Announcement transport only; authentication mail templates are not involved."""
import hashlib
import os
from email.utils import parseaddr
from html import escape

import httpx

from . import email_service as mail
from .campaign_content import email_text, validate_text


def send_campaign(*, to_email: str, campaign: dict, token: str, dedupe_key: str, test=False):
    if os.getenv("CAMPAIGN_EMAIL_ENABLED", "false").lower() not in ("true", "1", "yes"):
        raise mail.EmailDeliveryError("Announcement delivery disabled", provider="resend", code="provider_unavailable")
    try:
        selected = mail.validate_configuration()
        origin = mail.validate_app_base_url()
    except mail.EmailDeliveryError:
        raise mail.EmailDeliveryError("Provider unavailable", provider="resend", code="provider_unavailable") from None
    if selected != "resend" or origin != mail.CANONICAL_EMAIL_ORIGIN:
        raise mail.EmailDeliveryError("Canonical Resend configuration required", provider="resend", code="provider_unavailable")
    validate_text(campaign["subject"], campaign["body"])
    text = email_text(campaign, token)
    link = mail.CANONICAL_EMAIL_ORIGIN + "/announcements/unsubscribe?token=" + token
    body = {"from": f"KaisTrade <{parseaddr(os.environ['EMAIL_FROM'])[1]}>", "to": [to_email],
            "subject": ("[TEST] " if test else "") + campaign["subject"], "text": text,
            "html": '<!doctype html><html lang="tr"><body><div style="white-space:pre-wrap;font-family:Arial,sans-serif">' +
                    escape(text).replace(escape(link), '<a href="' + escape(link, quote=True) + '">' + escape(link) + "</a>") + "</div></body></html>",
            "headers": {"List-Unsubscribe": "<" + link + ">", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}}
    reply = os.getenv("EMAIL_REPLY_TO", "").strip()
    if reply:
        body["reply_to"] = reply
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            response = client.post("https://api.resend.com/emails", json=body, headers={
                "Authorization": "Bearer " + os.environ["RESEND_API_KEY"].strip(),
                "User-Agent": "KaisTrade-Announcements/1.0",
                "Idempotency-Key": "campaign-" + hashlib.sha256(dedupe_key.encode()).hexdigest(),
            })
        if response.status_code in (400, 422):
            raise mail.EmailDeliveryError("Permanent rejection", provider="resend", code="permanent_rejection")
        if not 200 <= response.status_code < 300:
            raise mail.EmailDeliveryError("Provider rejected announcement", provider="resend", code="provider_rejected")
        value = response.json()
        if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"].strip():
            raise ValueError("Invalid provider response")
    except httpx.TimeoutException:
        raise mail.EmailDeliveryError("Provider timeout", provider="resend", code="provider_timeout") from None
    except (httpx.HTTPError, OSError):
        raise mail.EmailDeliveryError("Provider failure", provider="resend", code="delivery_error") from None
    except ValueError:
        raise mail.EmailDeliveryError("Invalid provider response", provider="resend", code="invalid_response") from None
