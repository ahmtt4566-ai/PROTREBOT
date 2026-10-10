"""Announcement-only validation and domain-separated unsubscribe signatures."""
import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .email_service import CANONICAL_EMAIL_ORIGIN

AUDIENCES = ("all_users", "premium_users", "team_only")
WARNING = "Bu ifadeler yatırım vaadi gibi görünebilir, gönderimden önce gözden geçirin"
INVESTMENT_WORDS = re.compile(r"garanti|kazanç|kâr|getiri|\d+(?:[.,]\d+)?\s*%|%\s*\d+", re.IGNORECASE)
TOKEN_SECONDS = 180 * 24 * 60 * 60
ID = r"^[A-Za-z0-9_-]{1,160}$"


def validate_text(subject: str, body: str) -> None:
    if "\r" in subject or "\n" in subject or not 3 <= len(subject.strip()) <= 120:
        raise ValueError("Konu 3–120 karakter olmalı ve satır sonu içermemeli.")
    if not body.strip() or len(body) > 5000 or re.search(r"<[^>]*>|!\[", body):
        raise ValueError("Yalnız düz metin kullanın; HTML, görsel ve ek desteklenmiyor.")
    if re.search(r"(?im)^\s*(?:from|to|cc|bcc|subject|reply-to|content-type|list-unsubscribe)\s*:", body):
        raise ValueError("E-posta başlıkları elle yazılamaz.")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in subject + body):
        raise ValueError("Kontrol karakterleri kullanılamaz.")
    for match in re.finditer(r"(?:[a-z][a-z0-9+.-]*://|www\.|mailto:|(?<!:)//)\S+", body, re.IGNORECASE):
        link = match.group().rstrip(".,;!?)")
        parsed = urlsplit(link)
        if parsed.scheme != "https" or parsed.netloc != "kaistrade.com" or parsed.username or parsed.password:
            raise ValueError("Bağlantılar yalnız https://kaistrade.com adresine olabilir.")


class CampaignDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=3, max_length=120)
    body: str = Field(min_length=1, max_length=5000)
    audience: str
    scheduled_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def title_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("İç ad zorunlu.")
        return value.strip()

    @field_validator("audience")
    @classmethod
    def audience_valid(cls, value: str) -> str:
        if value not in AUDIENCES:
            raise ValueError("Geçersiz alıcı grubu.")
        return value


def validated(draft: CampaignDraft) -> dict:
    validate_text(draft.subject, draft.body)
    now = datetime.now(timezone.utc)
    if draft.scheduled_at is not None:
        if draft.scheduled_at.utcoffset() is None or not now + timedelta(minutes=10) <= draft.scheduled_at <= now + timedelta(days=30):
            raise HTTPException(422, "Tarih en az 10 dakika, en çok 30 gün sonrası olmalı.")
    result = draft.model_dump()
    result["content_hash"] = content_hash(result)
    return result


def content_hash(row: dict) -> str:
    schedule = row.get("scheduled_at")
    value = {key: row[key] for key in ("subject", "body", "audience")}
    value["scheduled_at"] = schedule.astimezone(timezone.utc).isoformat() if schedule else None
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def warnings(row: dict) -> list[str]:
    return [WARNING] if INVESTMENT_WORDS.search(row["subject"] + "\n" + row["body"]) else []


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def unsubscribe_token(secret: bytes, uid: str, stamp: int | None = None) -> str:
    if not re.fullmatch(ID, uid):
        raise ValueError("Invalid recipient ID")
    payload = encode(json.dumps([uid, int(time.time()) if stamp is None else stamp], separators=(",", ":")).encode())
    key = hmac.new(secret, b"kaistrade:announcements:unsubscribe:v1", hashlib.sha256).digest()
    return payload + "." + encode(hmac.new(key, payload.encode(), hashlib.sha256).digest())


def token_user(secret: bytes, token: str) -> str | None:
    try:
        if len(token) > 512:
            return None
        payload, signature = token.split(".")
        key = hmac.new(secret, b"kaistrade:announcements:unsubscribe:v1", hashlib.sha256).digest()
        expected = encode(hmac.new(key, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, signature):
            return None
        uid, stamp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if not isinstance(uid, str) or not re.fullmatch(ID, uid) or type(stamp) is not int or not 0 <= time.time() - stamp <= TOKEN_SECONDS:
            return None
        return uid
    except (ValueError, TypeError, UnicodeError):
        return None


def email_text(row: dict, token: str) -> str:
    return f"KaisTrade\n\n{row['body']}\n\nBu e-posta yalnız bilgilendirme amaçlıdır; yatırım tavsiyesi değildir.\n\nBu tür duyuruları almak istemiyorum:\n{CANONICAL_EMAIL_ORIGIN}/announcements/unsubscribe?token={token}"
