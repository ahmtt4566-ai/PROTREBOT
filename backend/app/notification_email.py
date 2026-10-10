"""Approval-only Resend adapter; shared mail validation is left unchanged."""
import hashlib
import os
from email.utils import parseaddr
from html import escape

import httpx

from . import email_service as mail

SUBJECT = "KaisTrade · Onay bildirimi"
DECISION_TEXT = {
    1: "Talebiniz onaylandı.",
    2: "Talebiniz reddedildi.",
    3: "Talebiniz için yeniden inceleme gerekiyor.",
    4: "Talebinizin uygulanması tamamlanamadı; yeniden inceleme gerekiyor.",
}


def notification_content(kind: str, payload: dict) -> tuple[str, str, str]:
    base = mail.validate_app_base_url()
    if base != mail.CANONICAL_EMAIL_ORIGIN:
        raise mail.EmailDeliveryError("Canonical notification origin required", provider="resend", code="provider_unavailable")
    if kind == "approval.pending_digest":
        count = payload["count"]
        if type(count) is not int or count < 1:
            raise ValueError("Invalid digest count")
        message = f"Bekleyen onay talebiniz var ({count})."
        path = "/admin"
    elif kind == "approval.decision":
        message = DECISION_TEXT[payload["result"]]
        path = "/moderator"
    else:
        raise ValueError("Unknown notification kind")
    link = base + path
    mail.validate_action_url(link)
    text = f"KaisTrade\n\n{message}\n\nPanele git:\n{link}"
    html = f"""<!doctype html><html lang="tr"><body style="margin:0;background:#f4f6f8;color:#18242d;font-family:Arial,sans-serif">
<div style="max-width:520px;margin:32px auto;padding:28px;background:white;border-radius:16px">
<p style="font-weight:700">KaisTrade</p><p>{escape(message)}</p>
<p><a href="{escape(link, quote=True)}">Panele git</a></p></div></body></html>"""
    return SUBJECT, text, html


def send_notification(*, to_email: str, kind: str, payload: dict, dedupe_key: str) -> None:
    if os.getenv("APPROVAL_EMAIL_ENABLED", "true").strip().lower() not in ("true", "1", "yes"):
        raise mail.EmailDeliveryError("Notification sending disabled", provider="resend", code="provider_unavailable")
    try:
        selected = mail.validate_configuration()
    except mail.EmailDeliveryError:
        raise mail.EmailDeliveryError("Notification provider unavailable", provider="resend", code="provider_unavailable") from None
    if selected != "resend":
        raise mail.EmailDeliveryError("Resend required", provider="resend", code="provider_unavailable")
    subject, text, html = notification_content(kind, payload)
    body = {"from": f"KaisTrade <{parseaddr(os.environ['EMAIL_FROM'])[1]}>", "to": [to_email],
            "subject": subject, "text": text, "html": html}
    reply_to = os.getenv("EMAIL_REPLY_TO", "").strip()
    if reply_to:
        body["reply_to"] = reply_to
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            response = client.post("https://api.resend.com/emails", json=body, headers={
                "Authorization": "Bearer " + os.environ["RESEND_API_KEY"].strip(),
                "User-Agent": "KaisTrade-Transactional-Mail/1.0",
                "Idempotency-Key": "approval-" + hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest(),
            })
        if not 200 <= response.status_code < 300:
            raise mail.EmailDeliveryError("Provider rejected notification", provider="resend", code="provider_rejected")
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"].strip():
            raise mail.EmailDeliveryError("Invalid provider response", provider="resend", code="invalid_response")
    except httpx.TimeoutException:
        raise mail.EmailDeliveryError("Provider timeout", provider="resend", code="provider_timeout") from None
    except (httpx.HTTPError, OSError):
        raise mail.EmailDeliveryError("Provider delivery failed", provider="resend", code="delivery_error") from None
    except ValueError:
        raise mail.EmailDeliveryError("Invalid provider response", provider="resend", code="invalid_response") from None
