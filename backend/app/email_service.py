"""Transactional email transport; never log bodies, action links or credentials."""
from __future__ import annotations

import base64
import logging
import os
import re
from email.message import EmailMessage
from email.utils import parseaddr
from html import escape
from urllib.parse import urlsplit

import httpx
from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .web_security import production_or_hosted_environment

logger = logging.getLogger(__name__)
CANONICAL_EMAIL_ORIGIN = "https://kaistrade.com"
VERIFY_SUBJECT = "KaisTrade hesabını doğrula"
RESET_SUBJECT = "KaisTrade parolanı yenile"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_TOKEN_URI = "https://oauth2.googleapis.com/token"


class EmailDeliveryError(RuntimeError):
    def __init__(self, message: str, *, provider: str, code: str = "configuration"):
        super().__init__(message)
        self.provider = provider
        self.code = code


def validate_app_base_url() -> str:
    if production_or_hosted_environment():
        value = os.getenv("APP_BASE_URL", "").strip()
        if value != CANONICAL_EMAIL_ORIGIN:
            logger.error("Production APP_BASE_URL must be https://kaistrade.com; mail disabled")
            raise EmailDeliveryError(
                "Production ortamında APP_BASE_URL yalnız https://kaistrade.com olmalıdır.",
                provider="configuration", code="app_base_url",
            )
        return value
    value = os.getenv("APP_BASE_URL", "http://localhost:5173").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.query and not parsed.fragment
    except ValueError:
        valid = False
    if not valid:
        logger.error("APP_BASE_URL must be a safe absolute URL; mail disabled")
        raise EmailDeliveryError(
            "APP_BASE_URL güvenli bir mutlak URL olarak yapılandırılmalı",
            provider="configuration", code="app_base_url",
        )
    return value


def validate_action_url(action_url: str) -> None:
    if not production_or_hosted_environment():
        return
    try:
        parsed = urlsplit(action_url)
        valid = (
            parsed.scheme == "https" and parsed.netloc == "kaistrade.com"
            and (action_url == CANONICAL_EMAIL_ORIGIN or action_url.startswith(CANONICAL_EMAIL_ORIGIN + "/"))
            and not any(ord(character) <= 32 for character in action_url)
        )
    except ValueError:
        valid = False
    if not valid:
        logger.error("Production mail action URL must use https://kaistrade.com; mail disabled")
        raise EmailDeliveryError(
            "Production ortamında e-posta bağlantıları yalnız https://kaistrade.com adresini kullanmalıdır.",
            provider="configuration", code="action_url",
        )


def provider() -> str:
    value = os.getenv("EMAIL_PROVIDER", "").strip().lower() or "smtp"
    if value not in {"smtp", "resend"}:
        raise EmailDeliveryError("EMAIL_PROVIDER yalnızca resend veya smtp olabilir.", provider="invalid")
    return value


def validate_configuration() -> str:
    validate_app_base_url()
    selected = provider()
    required = ("RESEND_API_KEY", "EMAIL_FROM") if selected == "resend" else (
        "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN",
    )
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        label = "Resend" if selected == "resend" else "Gmail API"
        raise EmailDeliveryError(f"{label} yapılandırması eksik: {', '.join(missing)}", provider=selected)
    for name in ("EMAIL_FROM", "EMAIL_REPLY_TO"):
        if (selected == "resend" or name == "EMAIL_REPLY_TO") and any(
            character in os.getenv(name, "") for character in ("\r", "\n")
        ):
            raise EmailDeliveryError(f"{name} geçersiz.", provider=selected)
        if (selected == "resend" or name == "EMAIL_REPLY_TO") and os.getenv(name, "").strip():
            address = parseaddr(os.environ[name])[1]
            if not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", address):
                raise EmailDeliveryError(f"{name} geçersiz.", provider=selected)
    return selected


def configured() -> bool:
    try:
        validate_configuration()
    except EmailDeliveryError:
        return False
    return True


def unavailable_message() -> str:
    try:
        validate_configuration()
    except EmailDeliveryError as exc:
        return str(exc)
    return "E-posta servisi kullanılamıyor."


def masked_recipient(address: str) -> str:
    local, separator, domain = address.rpartition("@")
    if not separator or not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
        return "***"
    first = local[:1] if re.fullmatch(r"[A-Za-z0-9]", local[:1]) else "*"
    return f"{first}***@{domain.lower()}"


def failure_details(exc: BaseException) -> dict[str, str]:
    status = getattr(getattr(exc, "resp", None), "status", None) or getattr(exc, "status_code", None)
    code = exc.code if isinstance(exc, EmailDeliveryError) else str(status) if isinstance(status, int) else "none"
    reason = "authentication" if code in {"401", "403"} else "timeout" if code == "timeout" else "email_api"
    return {"type": "EmailDeliveryError", "code": code, "reason": reason,
            "message": "E-posta sağlayıcısı gönderimi tamamlayamadı."}


def auth_email_html(title: str, display_name: str, action_url: str, action_label: str, expiry: str, *, information_only: bool = False) -> str:
    title, display_name, action_url, action_label, expiry = (
        escape(str(value), quote=True) for value in (title, display_name, action_url, action_label, expiry)
    )
    instruction = ("Giriş sayfasını kullanabilirsin. Parolanı unuttuysan aynı sayfadan sıfırlayabilirsin."
                   if information_only else "KaisTrade hesabındaki işlemi tamamlamak için aşağıdaki düğmeyi kullan.")
    expiry_notice = "" if information_only else f'<p style="font-size:13px">Güvenlik bağlantısı veya kodu {expiry} içinde geçerliliğini yitirir.</p>'
    return f"""<!doctype html><html lang="tr"><body style="margin:0;background:#f4f6f8;font-family:Arial,sans-serif;color:#18242d">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;background:#fff;border-radius:12px"><tr><td style="padding:32px">
<p style="font-weight:700;color:#2457c5">KaisTrade</p>
<h1 style="font-size:24px">{title}</h1>
<p>Merhaba {display_name or 'KaisTrade kullanıcısı'},</p>
<p>{instruction}</p>
<p style="margin:28px 0"><a href="{action_url}" style="display:inline-block;padding:14px 24px;background:#2457c5;color:#fff;border-radius:8px;text-decoration:none;font-weight:700">{action_label}</a></p>
<p style="font-size:13px">Düğme açılmıyorsa bu bağlantıyı tarayıcına kopyala:</p>
<p style="font-size:13px;overflow-wrap:anywhere"><a href="{action_url}">{action_url}</a></p>
{expiry_notice}
<p style="font-size:13px;color:#566570">Bu işlemi sen yapmadıysan bu maili yok sayabilirsin.</p>
<p style="font-size:13px;color:#566570">KaisTrade parolanı veya borsa API anahtarını e-posta ile istemez.</p>
</td></tr></table></td></tr></table></body></html>"""


def verification_email(action_url: str) -> tuple[str, str]:
    link = escape(action_url, quote=True)
    text = (
        "KaisTrade\n\nE-posta adresini onayla\n\n"
        "KaisTrade hesabını doğrulamak için aşağıdaki bağlantıyı aç. "
        "Bağlantı tek kullanımlıktır ve 30 dakika geçerlidir.\n\n"
        f"{action_url}\n\n"
        "KaisTrade hesabı sen açmadıysan bu e-postayı görmezden gelebilirsin. "
        "Şifreni veya API anahtarlarını asla e-postayla istemeyiz."
    )
    html = f"""<!doctype html><html lang="tr"><body style="margin:0;background:#0B0E0C;color:#E6ECE8;font-family:Arial,sans-serif">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#0B0E0C"><tr><td align="center" style="padding:36px 16px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:480px;background:#121714;border:1px solid #1F2823;border-radius:18px"><tr><td style="padding:32px">
<p style="margin:0 0 28px;font-size:22px;font-weight:700;color:#F2F6F3">Kais<span style="color:#5FCB8E">Trade</span></p>
<p style="font-size:32px;color:#5FCB8E" aria-hidden="true">&#9993;</p>
<h1 style="font-size:25px;color:#F2F6F3">E-posta adresini onayla</h1>
<p style="font-size:15px;line-height:1.7">KaisTrade hesabını doğrulamak için aşağıdaki butona dokun. Bağlantı tek kullanımlıktır ve 30 dakika geçerlidir.</p>
<table role="presentation" cellspacing="0" cellpadding="0" style="margin:28px 0"><tr><td bgcolor="#5FCB8E" style="border-radius:14px"><a href="{link}" style="display:inline-block;padding:16px 24px;color:#06120C;font-size:15px;font-weight:700;text-decoration:none">E-postamı doğrula</a></td></tr></table>
<p style="font-size:13px;color:#A3B0A9;line-height:1.6">Buton çalışmıyor mu? Bu bağlantıyı tarayıcına yapıştır:</p>
<p style="font-size:13px;overflow-wrap:anywhere;word-break:break-all"><a href="{link}" style="color:#7FD8A6">{link}</a></p>
<p style="margin-top:28px;font-size:12px;line-height:1.7;color:#8E9C95">KaisTrade hesabı sen açmadıysan bu e-postayı görmezden gelebilirsin. Şifreni veya API anahtarlarını asla e-postayla istemeyiz.</p>
</td></tr></table></td></tr></table></body></html>"""
    return text, html


def send_auth_email(*, to_email: str, display_name: str, subject: str, title: str,
                    action_url: str, action_label: str, expiry: str = "24 saat", information_only: bool = False,
                    verification_v2: bool = False, security_notice: str | None = None) -> None:
    selected = provider()
    try:
        validate_configuration()
        validate_action_url(action_url)
        expiry_notice = "" if information_only else f"Bağlantı veya kod {expiry} içinde geçerliliğini yitirir.\n"
        text = (f"KaisTrade\n\n{title}\n\nMerhaba {display_name},\n"
                f"KaisTrade hesabındaki işlemi tamamla: {action_label}\n\n{action_url}\n\n"
                f"{expiry_notice}"
                "Bu işlemi sen yapmadıysan bu maili yok sayabilirsin.\n"
                "KaisTrade parolanı veya borsa API anahtarını e-posta ile istemez.")
        html = auth_email_html(title, display_name, action_url, action_label, expiry, information_only=information_only)
        if verification_v2:
            text, html = verification_email(action_url)
        if security_notice is not None:
            text = f"KaisTrade\n\n{title}\n\n{security_notice}\n\nBu işlemi sen yapmadıysan hesabını hemen kontrol et:\n{action_url}\n\nParolanı veya API anahtarlarını e-postayla istemeyiz."
            html = f"""<!doctype html><html lang="tr"><body style="margin:0;background:#080e0c;color:#e7ecf3;font-family:Arial,sans-serif">
<div style="max-width:480px;margin:32px auto;padding:28px;border:1px solid #ffffff26;border-radius:16px">
<p style="color:#37c98a;font-weight:bold">KaisTrade</p><h1 style="font-size:24px">{escape(title)}</h1>
<p>{escape(security_notice)}</p><p>Bu işlemi sen yapmadıysan hesabını hemen kontrol et.</p>
<p><a style="color:#37c98a" href="{escape(action_url, quote=True)}">Hesabımı kontrol et</a></p>
<p>Parolanı veya API anahtarlarını e-postayla istemeyiz.</p></div></body></html>"""
        reply_to = os.getenv("EMAIL_REPLY_TO", "").strip()
        if selected == "resend":
            payload = {"from": f"KaisTrade <{parseaddr(os.environ['EMAIL_FROM'])[1]}>", "to": [to_email],
                       "subject": subject, "html": html, "text": text}
            if reply_to:
                payload["reply_to"] = reply_to
            with httpx.Client(timeout=10.0, follow_redirects=False) as client:
                response = client.post("https://api.resend.com/emails", json=payload, headers={
                    "Authorization": "Bearer " + os.environ["RESEND_API_KEY"].strip(),
                    "User-Agent": "KaisTrade-Transactional-Mail/1.0",
                })
            if not 200 <= response.status_code < 300:
                raise EmailDeliveryError("Resend gönderimi kabul etmedi.", provider=selected, code=str(response.status_code))
            try:
                result = response.json()
            except ValueError:
                raise EmailDeliveryError("Resend geçerli bir gönderim yanıtı vermedi.", provider=selected, code="invalid_response") from None
            if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"].strip():
                raise EmailDeliveryError("Resend gönderim kimliği vermedi.", provider=selected, code="invalid_response")
        else:
            credentials = Credentials(
                token=None, refresh_token=os.environ["GMAIL_REFRESH_TOKEN"].strip(),
                token_uri=GMAIL_TOKEN_URI, client_id=os.environ["GMAIL_CLIENT_ID"].strip(),
                client_secret=os.environ["GMAIL_CLIENT_SECRET"].strip(), scopes=[GMAIL_SEND_SCOPE],
            )
            message = EmailMessage()
            message["Subject"] = subject
            message["From"] = f"KaisTrade <{os.getenv('GMAIL_FROM_EMAIL', 'privacykais@gmail.com').strip()}>"
            message["To"] = to_email
            if reply_to:
                message["Reply-To"] = reply_to
            message.set_content(text)
            message.add_alternative(html, subtype="html")
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
            result = build("gmail", "v1", credentials=credentials, cache_discovery=False).users().messages().send(
                userId="me", body={"raw": raw},
            ).execute()
            if not isinstance(result, dict) or not result.get("id"):
                raise EmailDeliveryError("Gmail gönderim kimliği vermedi.", provider=selected, code="invalid_response")
    except EmailDeliveryError as exc:
        logger.warning("Email delivery failed provider=%s code=%s recipient=%s", selected, exc.code, masked_recipient(to_email))
        raise
    except (httpx.HTTPError, HttpError, GoogleAuthError, OSError, RuntimeError, ValueError) as exc:
        details = failure_details(exc)
        code = "timeout" if isinstance(exc, (httpx.TimeoutException, TimeoutError)) else details["code"]
        logger.warning("Email delivery failed provider=%s code=%s recipient=%s", selected, code, masked_recipient(to_email))
        raise EmailDeliveryError("E-posta gönderilemedi; lütfen tekrar dene.", provider=selected, code=code) from None
