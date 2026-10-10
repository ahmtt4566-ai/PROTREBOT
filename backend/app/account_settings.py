"""Profile/settings APIs. Preferences never mutate execution policy or exchange orders."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import account_store as store
from . import v22_commercial as auth
from .commercial_core import hash_password, issue_token, normalize_email, verify_password, verify_token
from .subscription_core import entitlement_snapshot
from .auth_failures import MFA_ACCOUNT, decode_state, enforce_failure_wait, new_state, record_failure
from .password_policy import validate_new_password

router = APIRouter(prefix="/api/v22", tags=["Account settings"])
DEFAULT_PREFERENCES = {"trading_mode": "MANUAL", "timeframe": "15m", "exchange": "BINANCE",
                       "risk_per_trade": 1.0, "symbols": []}
auth.AUTH_LIMITS.update({"account": (12, 300), "account-mail": (3, 300), "mfa": (8, 300)})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Proof(StrictModel):
    current_password: str | None = Field(default=None, max_length=256)
    totp_code: str | None = Field(default=None, max_length=80)
    challenge_id: str | None = Field(default=None, max_length=100)
    email_code: str | None = Field(default=None, max_length=20)


class Profile(StrictModel):
    display_name: str = Field(min_length=2, max_length=80)

    @field_validator("display_name")
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if len(value) < 2 or any(ord(character) < 32 for character in value):
            raise ValueError("Geçerli görünen ad gerekli")
        return value


class Preferences(StrictModel):
    trading_mode: Literal["MANUAL", "AUTO"] | None = None
    timeframe: Literal["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "1w"] | None = None
    exchange: Literal["BINANCE"] | None = None
    risk_per_trade: float | None = Field(default=None, ge=0.1, le=1, allow_inf_nan=False, strict=True)
    symbols: list[str] | None = Field(default=None, max_length=100)

    @field_validator("symbols")
    @classmethod
    def valid_symbols(cls, value):
        if value is None:
            return value
        import re
        if any(not re.fullmatch(r"[A-Z0-9]{1,30}USDT", symbol) for symbol in value):
            raise ValueError("Geçersiz sembol")
        return list(dict.fromkeys(value))


class EmailChange(Proof):
    new_email: str = Field(min_length=5, max_length=180)

    @field_validator("new_email")
    @classmethod
    def valid_email(cls, value):
        import re
        value = normalize_email(value)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Geçerli e-posta gerekli")
        return value


class PasswordChange(Proof):
    new_password: str = Field(min_length=10, max_length=256)
    confirm_password: str = Field(min_length=10, max_length=256)


class Code(StrictModel):
    code: str = Field(min_length=6, max_length=80)


class LoginCode(Code):
    challenge_id: str = Field(min_length=20, max_length=100)


class Token(StrictModel):
    token: str = Field(min_length=20, max_length=100)


class SessionRevoke(StrictModel):
    session_id: str = Field(min_length=1, max_length=100)


class Close(Proof):
    confirmation: Literal["HESABI KAPAT"]


def iso(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def cipher(request):
    key = hashlib.sha256(b"account-totp-v1:" + auth.runtime(request)["secret"]).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def activity(doc, kind, message):
    doc.setdefault("activity", []).insert(0, {"id": uuid.uuid4().hex, "kind": kind,
                                            "message": message, "created_at": iso()})
    del doc["activity"][100:]


async def member(request, *, owner=False):
    return await auth.authenticated_user_async(request, owner=owner)


def token_payload(request):
    try:
        return verify_token(auth.bearer(request), auth.runtime(request)["secret"], expected_kind="USER")
    except ValueError as exc:
        raise HTTPException(401, "Oturum geçersiz veya süresi dolmuş") from exc


def session_row(request, payload):
    agent = request.headers.get("user-agent", "").lower()
    browser = next((name for text, name in (("edg/", "Edge"), ("firefox", "Firefox"),
                    ("chrome", "Chrome"), ("safari", "Safari")) if text in agent), "Unknown")
    device = "Mobile" if any(text in agent for text in ("mobile", "android", "iphone")) else "Desktop" if agent else "Unknown"
    return {"id": payload["jti"], "device": device, "browser": browser,
            "created_at": iso(payload["iat"]), "last_seen_at": iso(), "expires_at": iso(payload["exp"]),
            "exp": payload["exp"], "auth_version": payload.get("ver", 1), "revoked": False}


async def track_session(request, token, *, force=False, issued=False):
    payload = verify_token(token, auth.runtime(request)["secret"], expected_kind="USER")
    if not payload.get("jti"):
        raise HTTPException(401, "Oturum kimliği gerekli")
    doc = await store.read(request, payload["sub"])
    # Legacy sessions are registered when a user's settings are first accessed;
    # no inferred devices or unobserved session counts are fabricated.
    if doc is None and not force:
        return
    async with store.edit(request, payload["sub"]) as doc:
        row = doc.setdefault("sessions", {}).get(payload["jti"])
        if not issued and (row or {}).get("issued_at", payload.get("iat", 0)) <= doc.get("sessions_valid_after", 0) and payload["jti"] != doc.get("preserved_session"):
            raise HTTPException(401, "Oturum iptal edildi")
        if row and row.get("revoked"):
            raise HTTPException(401, "Oturum iptal edildi")
        if row is None:
            row = session_row(request, payload)
            if issued:
                row["issued_at"] = time.time()
            doc["sessions"][payload["jti"]] = row
        row["last_seen_at"] = iso()


async def local_security(request, user):
    if getattr(request.app.state, "db_pool", None) is None:
        doc = await store.read(request, user["id"])
        if doc and doc.get("auth_overlay"):
            user.update(copy.deepcopy(doc["auth_overlay"]))
        if doc and doc.get("display_name"):
            user["display_name"] = doc["display_name"]


async def enabled(request, user):
    doc = await store.read(request, user["id"])
    return bool(doc and doc.get("two_factor_enabled"))


async def require_totp(request, user, code):
    if not await enabled(request, user):
        return
    await auth.enforce_auth_limit(request, "mfa", user["id"])
    valid = False
    async with store.edit(request, user["id"]) as doc:
        valid = check_limited_totp(request, doc, code)
    if not valid:
        auth.record_event(request, user, "auth.mfa_failed", "invalid_mfa", "security", 401)
        raise HTTPException(401, "İki aşamalı doğrulama kodu geçersiz")


def check_totp(request, doc, code, *, pending=False):
    if not isinstance(code, str):
        return False
    if not pending:
        hashed = digest(code.upper().replace(" ", ""))
        hashes = doc.get("recovery_hashes", [])
        found = next((value for value in hashes if hmac.compare_digest(value, hashed)), None)
        if found:
            hashes.remove(found)
            return True
    encrypted = doc.get("pending_totp" if pending else "totp_seed")
    if not encrypted or (pending and doc.get("pending_totp_expires", 0) <= time.time()):
        return False
    try:
        totp = pyotp.TOTP(cipher(request).decrypt(encrypted.encode()).decode())
    except (InvalidToken, ValueError):
        raise HTTPException(503, "İki aşamalı doğrulama deposu kullanılamıyor") from None
    if len(code) != 6 or not code.isdigit():
        return False
    step = int(time.time() // 30)
    for offset in (-1, 0, 1):
        candidate = step + offset
        if candidate > doc.get("last_totp_step", -1) and totp.verify(code, for_time=candidate * 30):
            doc["last_totp_step"] = candidate
            return True
    return False


def mfa_failure_state(doc):
    try:
        return decode_state(doc.get("mfa_failures", new_state()))
    except (ValueError, TypeError) as exc:
        auth.logger.error("Account MFA failure ledger is invalid")
        raise HTTPException(503, "İki aşamalı doğrulama sınırı deposu kullanılamıyor") from exc


def check_limited_totp(request, doc, code, *, pending=False):
    state = mfa_failure_state(doc)
    enforce_failure_wait(state, time.time())
    valid = check_totp(request, doc, code, pending=pending)
    if not valid:
        record_failure(state, MFA_ACCOUNT, time.time())
        doc["mfa_failures"] = state
    return valid


async def proof(request, user, payload: Proof):
    await auth.enforce_auth_limit(request, "account", user["id"])
    if user.get("password"):
        if not payload.current_password or not verify_password(payload.current_password, user["password"]):
            raise HTTPException(401, "Mevcut parola hatalı")
    else:
        if not user.get("email_verified"):
            raise HTTPException(403, "Doğrulanmış e-posta gerekli")
        valid = False
        async with store.edit(request, user["id"]) as doc:
            row = doc.get("challenges", {}).get(digest(payload.challenge_id or ""))
            if challenge_valid(row, user, "reauth") and row.get("attempts", 0) < 5:
                row["attempts"] = row.get("attempts", 0) + 1
                valid = hmac.compare_digest(row["code_hash"], digest((payload.challenge_id or "") + ":" + (payload.email_code or "")))
                if valid:
                    row["used"] = True
        if not valid:
            raise HTTPException(401, "E-posta doğrulama kodu geçersiz")
    await require_totp(request, user, payload.totp_code)


def challenge_valid(row, user, kind):
    return bool(row and row.get("kind") == kind and not row.get("used") and
                row.get("expires", 0) > time.time() and row.get("user_id") == user["id"] and
                row.get("version") == int(user.get("auth_version", 1)) and row.get("email") == user.get("email"))


def new_challenge(doc, user, kind, *, seconds=600, **extra):
    token = secrets.token_urlsafe(32)
    row = {"kind": kind, "user_id": user["id"], "version": int(user.get("auth_version", 1)),
           "email": user.get("email"), "expires": time.time() + seconds, "used": False, "attempts": 0, **extra}
    challenges = doc.setdefault("challenges", {})
    for key in list(challenges):
        if challenges[key].get("expires", 0) <= time.time():
            del challenges[key]
    challenges[digest(token)] = row
    return token, row


async def delivery(request, user, *, email=None, title, action_url, label, expiry="24 saat"):
    if not auth.gmail_configured():
        auth.record_event(request, user, "auth.email_verification_failed", "verification_failed", "verification", 503) if title == auth.VERIFY_SUBJECT else None
        raise HTTPException(503, "E-posta servisi kullanılamıyor")
    try:
        await asyncio.to_thread(auth.send_auth_email, to_email=email or user["email"],
                                display_name=user.get("display_name", ""), subject=title, title=title,
                                action_url=action_url, action_label=label, expiry=expiry)
    except auth.GMAIL_DELIVERY_ERRORS as exc:
        auth.log_gmail_failure(exc, request.app)
        auth.record_event(request, user, "auth.email_verification_failed", "verification_failed", "verification", 503) if title == auth.VERIFY_SUBJECT else None
        # Provider exception messages may contain recipients, OTPs or URLs.
        raise HTTPException(503, "E-posta gönderilemedi") from None
    auth.record_event(request, user, "auth.email_verification_sent", "verification_sent", "verification") if title == auth.VERIFY_SUBJECT else None


async def notify_two_factor(request, user, *, active):
    title = "İki aşamalı doğrulama etkinleştirildi" if active else "İki aşamalı doğrulama kapatıldı"
    try:
        await asyncio.wait_for(asyncio.to_thread(
            auth.send_auth_email, to_email=user["email"], display_name=user.get("display_name", ""),
            subject="KaisTrade · " + title, title=title,
            action_url=auth.app_base_url() + "/settings", action_label="Hesabımı kontrol et",
            information_only=True, security_notice=title + "."), timeout=5.0)
    except TimeoutError:
        auth.logger.warning("Two-factor security notification delivery was not confirmed before its deadline")
        return False
    except auth.GMAIL_DELIVERY_ERRORS as exc:
        auth.log_gmail_failure(exc, request.app)
        # The security change is committed; report delivery separately, never undo it.
        return False
    return True


async def require_available_email(request, user_id, email):
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        db = store.active_connection(request, user_id) or pool
        duplicate = await db.fetchrow(
            "SELECT user_id FROM commercial_auth_users WHERE lower(security->>'email') = $1 AND user_id <> $2",
            email, user_id)
        if duplicate:
            raise HTTPException(409, "E-posta kullanılamıyor")
    else:
        db = store.active_local_connection(request, user_id)
        for other_id, body in db.execute("SELECT user_id,payload FROM commercial_account_settings WHERE user_id <> ?", (user_id,)):
            if json.loads(body).get("auth_overlay", {}).get("email") == email:
                raise HTTPException(409, "E-posta kullanılamıyor")
    if any(other["id"] != user_id and other.get("email") == email for other in auth.runtime(request)["state"]["users"]):
        raise HTTPException(409, "E-posta kullanılamıyor")


async def rotate(request, user, doc, updates=None, *, preserve_current=False):
    payload = token_payload(request) if preserve_current else None
    if payload and payload["sub"] != user["id"]:
        raise HTTPException(401, "Oturum sahibi eşleşmiyor")
    expected = int(user.get("auth_version", 1))
    if payload and int(payload.get("ver", 1)) != expected:
        raise HTTPException(401, "Oturum iptal edildi")
    await auth.invalidate_user_sessions(request, user, security_updates=updates or {}, expected_version=expected)
    if getattr(request.app.state, "db_pool", None) is None:
        doc["auth_overlay"] = {"auth_version": user["auth_version"], **auth.auth_security(user)}
    for row in doc.get("sessions", {}).values():
        row["revoked"] = True
    if payload:
        token = issue_token(
            user["id"], user["role"], auth.runtime(request)["secret"],
            token_version=int(user["auth_version"]), session_id=payload["jti"],
            now=int(payload["iat"]), ttl_seconds=int(payload["exp"]) - int(payload["iat"]),
        )
        row = doc.setdefault("sessions", {}).setdefault(payload["jti"], session_row(request, payload))
        row.update(revoked=False, auth_version=user["auth_version"], last_seen_at=iso())
        doc.pop("sessions_valid_after", None)
        doc.pop("preserved_session", None)
        return token
    return None


def refreshed_session_response(request, response, user, token):
    if not token:
        raise HTTPException(503, "Mevcut oturum yenilenemedi")
    try:
        payload = verify_token(token, auth.runtime(request)["secret"], expected_kind="USER")
    except ValueError as exc:
        raise HTTPException(401, "Oturum süresi doldu; yeniden giriş yapın") from exc
    cookie = request.cookies.get(auth.SESSION_COOKIE_NAME)
    if cookie and cookie != auth.bearer(request):
        return token
    return auth.browser_session_response_token(
        token, user, request, response,
        browser_session=bool(request.cookies.get(auth.SESSION_COOKIE_NAME)),
        remember=int(payload["exp"]) - int(payload["iat"]) > auth.STANDARD_SESSION_SECONDS,
    )


async def persist_projection(request):
    rt = auth.runtime(request)
    try:
        auth.save_state(rt["state"])
    except Exception:
        raise HTTPException(503, "Hesap yansıması kaydedilemedi") from None
    if getattr(request.app.state, "db_pool", None) is not None:
        if not await auth.persist_v22_commercial(request.app):
            raise HTTPException(503, "Hesap yansıması kalıcı depoya yazılamadı")


def preferences(request, user, doc):
    profile = next((row for row in auth.runtime(request)["state"].get("profiles", []) if row.get("user_id") == user["id"]), {})
    source = {**profile.get("preferences", {}), **doc.get("preferences", {})}
    result = copy.deepcopy(DEFAULT_PREFERENCES)
    for key in DEFAULT_PREFERENCES:
        if key in source and source[key] is not None:
            try:
                result[key] = Preferences.model_validate({key: source[key]}).model_dump(exclude_unset=True)[key]
            except ValidationError:
                pass
    return result


async def close_blocker(request, user):
    if user.get("role") == "OWNER":
        return "Ana yönetici hesabı kapatılamaz"
    for name in ("_binance_demo_user_state", "_v21_demo_user_state"):
        state = getattr(request.app.state, name, {}).get(user["id"], {})
        snapshot = state.get("snapshot") or {}
        if any(snapshot.get(key) for key in ("positions", "open_orders", "open_algo_orders")) or state.get("plans") or state.get("paper_positions") or state.get("auto", {}).get("enabled"):
            return "Açık pozisyon, emir veya otomasyon mevcut"
        if state.get("connected") or state.get("reconciliation_required"):
            return "Borsa bağlantısını kapatın ve recovery durumunu doğrulayın"
    live = getattr(request.app.state, "v25_execution", {})
    if live.get("_user_id") == user["id"]:
        snapshot = live.get("snapshot") or {}
        if live.get("connected") or live.get("plans") or any(snapshot.get(key) for key in ("positions", "open_orders", "open_algo_orders")):
            return "Canlı borsa bağlantısı veya açık işlem mevcut"
        if live.get("reconciliation_required") or live.get("execution_state") == "UNKNOWN":
            return "Borsa recovery tamamlanmalı"
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            rows = await pool.fetch("SELECT active, account_summary FROM protrebot_exchange_session_vault WHERE user_id = $1", user["id"])
        except Exception:
            raise HTTPException(503, "Borsa bağlantı durumu güvenli şekilde doğrulanamadı") from None
        for row in rows:
            summary = row["account_summary"] or {}
            if isinstance(summary, str):
                try:
                    summary = json.loads(summary)
                except ValueError:
                    raise HTTPException(503, "Borsa hesap durumu doğrulanamadı") from None
            if not isinstance(summary, dict):
                raise HTTPException(503, "Borsa hesap durumu doğrulanamadı")
            if row["active"] or any(summary.get(key) for key in ("positions", "open_orders", "open_algo_orders", "position_count", "open_order_count")):
                return "Başka bir oturumda borsa bağlantısı veya açık işlem mevcut"
    return None


async def overview(request, user, *, current=True):
    doc = await store.read(request, user["id"]) or store.defaults()
    sub = entitlement_snapshot(auth.runtime(request)["state"], user["id"])
    sessions = [row for row in doc.get("sessions", {}).values() if not row.get("revoked") and
                row.get("exp", 0) > time.time() and row.get("auth_version") == int(user.get("auth_version", 1))]
    current_id = token_payload(request).get("jti") if current else None
    blocker = await close_blocker(request, user)
    pending = doc.get("pending_email")
    if pending and pending["expires"] <= time.time():
        pending = None
    user_dto = {key: user.get(key) for key in ("id", "display_name", "email", "role", "created_at")}
    google_linked = bool(user.get("google_sub") or user.get("google_id") or user.get("auth_provider") == "google" or doc.get("google_linked"))
    if not google_linked and getattr(request.app.state, "google_oauth_schema_ready", False):
        try:
            pool = request.app.state.db_pool
            google_linked = bool(await pool.fetchrow("SELECT user_id FROM commercial_google_identities WHERE user_id = $1", user["id"]))
        except Exception:
            raise HTTPException(503, "Hesap giriş yöntemleri doğrulanamadı") from None
    user_dto.update(active=bool(user.get("active")), email_verified=bool(user.get("email_verified")),
                    last_login=doc.get("last_login"), password_changed_at=user.get("password_changed_at"),
                    auth_methods=(["password"] if user.get("password") else []) +
                    (["google"] if google_linked else []))
    return {"user": user_dto, "subscription": {"plan": sub.get("plan") or "FREE", "status": sub["status"],
                "expires_at": sub.get("current_period_end") or sub.get("trial_end"),
                "is_premium": bool(sub.get("master_trade_access")),
                "features": [{"key": key, "label": {"canAccessMasterTrade": "Master Trade"}.get(key, key), "included": bool(value)}
                             for key, value in sub.get("entitlements", {}).items()]},
            "preferences": preferences(request, user, doc),
            "security": {"two_factor_enabled": bool(doc.get("two_factor_enabled")), "active_sessions": len(sessions),
                         "email_delivery_available": auth.gmail_configured(), "can_close_account": blocker is None and bool(user.get("active")),
                         "close_blocker": blocker},
            "pending_email": {"email": pending["email"], "expires_at": iso(pending["expires"])} if pending else None,
            "activity": [{key: row.get(key) for key in ("id", "kind", "message", "created_at")} for row in doc.get("activity", [])],
            "sessions": [{**{key: row.get(key) for key in ("id", "device", "browser", "created_at", "last_seen_at", "expires_at")},
                          "current": row["id"] == current_id} for row in sessions]}


@router.get("/account/overview")
async def account_overview(request: Request):
    user = await member(request)
    await track_session(request, auth.bearer(request), force=True)
    return await overview(request, user)


@router.patch("/account/profile")
async def update_profile(payload: Profile, request: Request):
    user = await member(request)
    async with store.edit(request, user["id"]) as doc:
        doc["display_name"] = payload.display_name
        await auth.update_auth_security(request, user, {"display_name": payload.display_name})
        activity(doc, "PROFILE_CHANGED", "Görünen ad güncellendi")
    profile = next((row for row in auth.runtime(request)["state"].get("profiles", []) if row.get("user_id") == user["id"]), None)
    if profile is not None:
        profile.update(full_name=payload.display_name, updated_at=iso())
    await persist_projection(request)
    return {"ok": True}


@router.patch("/account/preferences")
async def update_preferences(payload: Preferences, request: Request):
    user = await member(request)
    fields = payload.model_dump(exclude_unset=True)
    if any(value is None for value in fields.values()):
        raise HTTPException(422, "Tercih alanları boş olamaz")
    async with store.edit(request, user["id"]) as doc:
        doc.setdefault("preferences", {}).update(fields)
        activity(doc, "PREFERENCES_CHANGED", "Form varsayılanları güncellendi")
        result = preferences(request, user, doc)
    profiles = auth.runtime(request)["state"].setdefault("profiles", [])
    profile = next((row for row in profiles if row.get("user_id") == user["id"]), None)
    if profile is None:
        profile = {"id": uuid.uuid4().hex, "user_id": user["id"], "created_at": iso()}
        profiles.append(profile)
    profile.setdefault("preferences", {}).update(fields)
    profile["updated_at"] = iso()
    await persist_projection(request)
    return result


@router.post("/account/reauth/email")
async def reauth_email(request: Request):
    user = await member(request)
    if user.get("password") or not user.get("email_verified"):
        raise HTTPException(403, "E-posta yeniden doğrulaması bu hesap için kullanılamaz")
    await auth.enforce_auth_limit(request, "account-mail", user["id"])
    code = f"{secrets.randbelow(1000000):06d}"
    async with store.edit(request, user["id"]) as doc:
        challenge, row = new_challenge(doc, user, "reauth")
        row["code_hash"] = digest(challenge + ":" + code)
        await delivery(request, user, title="KaisTrade hesap doğrulaması", action_url=auth.app_base_url() + "/profile",
                       label="Doğrulama kodu: " + code, expiry="10 dakika")
    return {"challenge_id": challenge, "expires_at": iso(row["expires"])}


def pending_response(pending):
    return {"ok": True, "pending_email": {"email": pending["email"], "expires_at": iso(pending["expires"])}}


async def request_new_email(request, user, email):
    if email == user["email"]:
        raise HTTPException(422, "Yeni e-posta mevcut adresle aynı")
    async with store.edit(request, user["id"]) as doc:
        token, row = new_challenge(doc, user, "email-change", seconds=1800, new_email=email)
        doc["pending_email"] = {"email": email, "expires": row["expires"], "token_hash": digest(token)}
        await delivery(request, user, email=email, title="KaisTrade yeni e-posta doğrulaması",
                       action_url=auth.app_base_url() + "/profile?email_token=" + quote(token), label="E-posta adresimi onayla", expiry="30 dakika")
        activity(doc, "EMAIL_CHANGE_REQUESTED", "E-posta değişikliği istendi")
        pending = copy.deepcopy(doc["pending_email"])
    return pending_response(pending)


@router.post("/account/email/request")
async def email_request(payload: EmailChange, request: Request):
    user = await member(request)
    await proof(request, user, payload)
    await auth.enforce_auth_limit(request, "account-mail", user["id"])
    return await request_new_email(request, user, payload.new_email)


@router.post("/account/email/cancel")
async def email_cancel(request: Request):
    user = await member(request)
    async with store.edit(request, user["id"]) as doc:
        pending = doc.pop("pending_email", None)
        if pending:
            doc["challenges"].pop(pending["token_hash"], None)
    return {"ok": True}


@router.post("/account/email/resend")
async def email_resend(request: Request):
    user = await member(request)
    await auth.enforce_auth_limit(request, "account-mail", user["id"])
    doc = await store.read(request, user["id"]) or {}
    pending = doc.get("pending_email")
    if not pending or pending["expires"] <= time.time():
        raise HTTPException(409, "Bekleyen e-posta değişikliği yok")
    return await request_new_email(request, user, pending["email"])


@router.post("/account/email/confirm")
async def email_confirm(payload: Token, request: Request, response: Response):
    user = await member(request)
    await auth.enforce_auth_limit(request, "verify", user["id"])
    old_email = user["email"]
    # Reuse the same shared email registration lock as password/Google signup.
    from .google_oauth import registration_guard
    doc = await store.read(request, user["id"]) or {}
    pending = doc.get("pending_email")
    if not pending:
        raise HTTPException(400, "E-posta doğrulama isteği geçersiz")
    async with registration_guard(request, pending["email"]):
        async with store.edit(request, user["id"]) as doc:
            row = doc.get("challenges", {}).get(digest(payload.token))
            pending = doc.get("pending_email")
            if not challenge_valid(row, user, "email-change") or not pending or pending["token_hash"] != digest(payload.token):
                raise HTTPException(400, "E-posta doğrulama isteği geçersiz")
            email = row["new_email"]
            await require_available_email(request, user["id"], email)
            row["used"] = True
            doc.pop("pending_email", None)
            await rotate(request, user, doc, {"email": email, "email_verified": True})
            activity(doc, "EMAIL_CHANGED", "E-posta adresi değiştirildi; oturumlar kapatıldı")
    await persist_projection(request)
    auth.clear_rotated_session_cookie(request, response, user["id"])
    try:
        await delivery(request, user, email=old_email, title="KaisTrade e-posta adresin değiştirildi",
                       action_url=auth.app_base_url() + "/login", label="HESABINIZI KONTROL EDİN")
    except HTTPException:
        # Confirmation is committed. Never undo it because notification failed.
        pass
    return {"ok": True, "reauthenticate": True}


@router.post("/account/verification/resend")
async def verification_resend(request: Request):
    user = await member(request)
    if auth.email_verification.enabled():
        try:
            return await auth.email_verification.send_link(request, user["id"])
        except auth.email_service.EmailDeliveryError as exc:
            auth.log_gmail_failure(exc, request.app)
            raise HTTPException(503, "Doğrulama maili gönderilemedi. Lütfen tekrar dene.",
                                headers={"Retry-After": "60"}) from None
    if user.get("email_verified"):
        return {"ok": True}
    await auth.enforce_auth_limit(request, "account-mail", user["id"])
    token = auth.issue_one_time_token(auth.runtime(request)["state"], user, auth.runtime(request)["secret"], kind="EMAIL_VERIFY")
    await store.save_action_token(request, token, user, "EMAIL_VERIFY")
    await persist_projection(request)
    await delivery(request, user, title=auth.VERIFY_SUBJECT,
                   action_url=auth.app_base_url() + "/verify-email?token=" + quote(token), label="E-posta adresimi doğrula")
    return {"ok": True}


@router.post("/account/password")
async def password_change(payload: PasswordChange, request: Request, response: Response):
    user = await member(request)
    if payload.new_password != payload.confirm_password:
        raise HTTPException(422, "Parolalar eşleşmiyor")
    validate_new_password(payload.new_password)
    await proof(request, user, payload)
    async with store.edit(request, user["id"]) as doc:
        token = await rotate(request, user, doc, {"password": hash_password(payload.new_password), "password_changed_at": iso()}, preserve_current=True)
        activity(doc, "PASSWORD_CHANGED", "Parola güncellendi; diğer oturumlar kapatıldı")
    await persist_projection(request)
    marker = refreshed_session_response(request, response, user, token)
    return {"ok": True, "reauthenticate": False, "token": marker}


@router.post("/account/2fa/setup")
async def setup_totp(payload: Proof, request: Request):
    user = await member(request)
    await proof(request, user, payload)
    async with store.edit(request, user["id"]) as doc:
        if doc.get("two_factor_enabled"):
            raise HTTPException(409, "İki aşamalı doğrulama zaten etkin")
        secret = pyotp.random_base32()
        doc["pending_totp"] = cipher(request).encrypt(secret.encode()).decode()
        doc["pending_totp_expires"] = time.time() + 600
        doc["pending_totp_version"] = int(user.get("auth_version", 1))
        doc["last_totp_step"] = -1
    return {"secret": secret, "otpauth_uri": pyotp.TOTP(secret).provisioning_uri(name=user["email"], issuer_name="ProTreBot")}


@router.post("/account/2fa/enable")
async def enable_totp(payload: Code, request: Request, response: Response):
    user = await member(request)
    await auth.enforce_auth_limit(request, "mfa", user["id"])
    valid = False
    codes = []
    async with store.edit(request, user["id"]) as doc:
        if doc.get("two_factor_enabled"):
            raise HTTPException(409, "İki aşamalı doğrulama zaten etkin")
        valid = doc.get("pending_totp_version") == int(user.get("auth_version", 1)) and check_limited_totp(request, doc, payload.code, pending=True)
        if valid:
            doc["totp_seed"] = doc.pop("pending_totp")
            doc.pop("pending_totp_expires", None)
            doc["two_factor_enabled"] = True
            codes = [secrets.token_hex(8).upper() for _ in range(10)]
            doc["recovery_hashes"] = [digest(code) for code in codes]
            token = await rotate(request, user, doc, preserve_current=True)
            activity(doc, "TWO_FACTOR_ENABLED", "İki aşamalı doğrulama etkinleştirildi")
    if not valid:
        auth.record_event(request, user, "auth.mfa_failed", "invalid_mfa", "security", 401)
        raise HTTPException(401, "İki aşamalı doğrulama kodu geçersiz veya süresi dolmuş")
    await persist_projection(request)
    marker = refreshed_session_response(request, response, user, token)
    notification_sent = await notify_two_factor(request, user, active=True)
    return {"recovery_codes": codes, "reauthenticate": False, "token": marker, "notification_sent": notification_sent}


@router.post("/account/2fa/disable")
async def disable_totp(payload: Proof, request: Request, response: Response):
    user = await member(request)
    await proof(request, user, payload)
    async with store.edit(request, user["id"]) as doc:
        doc["two_factor_enabled"] = False
        doc["recovery_hashes"] = []
        for key in ("totp_seed", "pending_totp", "pending_totp_expires"):
            doc.pop(key, None)
        token = await rotate(request, user, doc, preserve_current=True)
        activity(doc, "TWO_FACTOR_DISABLED", "İki aşamalı doğrulama kapatıldı")
    await persist_projection(request)
    marker = refreshed_session_response(request, response, user, token)
    notification_sent = await notify_two_factor(request, user, active=False)
    return {"ok": True, "reauthenticate": False, "token": marker, "notification_sent": notification_sent}


async def login_challenge(request, user, *, remember=False, browser_session=False, google=False):
    doc = await store.read(request, user["id"])
    enforce_failure_wait(mfa_failure_state(doc or {}), time.time())
    await auth.enforce_auth_limit(request, "mfa", user["id"])
    async with store.edit(request, user["id"]) as doc:
        enforce_failure_wait(mfa_failure_state(doc), time.time())
        challenge, _ = new_challenge(doc, user, "login", seconds=300, remember=remember,
                                    browser_session=browser_session, google=google)
    return {"mfa_required": True, "challenge_id": challenge}


async def login_record(request, user, token, *, google=False):
    await track_session(request, token, issued=True, force=True)
    async with store.edit(request, user["id"]) as doc:
        doc["last_login"] = iso()
        if google:
            doc["google_linked"] = True
        activity(doc, "LOGIN", "Yeni oturum açıldı")
    auth.record_event(request, user, "auth.login_succeeded", "login_ok", "auth")


@router.post("/auth/2fa/login")
async def mfa_login(payload: LoginCode, request: Request, response: Response):
    await auth.enforce_auth_limit(request, "mfa", payload.challenge_id)
    uid = None
    # Challenge ID embeds no identity. Locate only its hash, not a raw token.
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            row = await pool.fetchrow("SELECT user_id FROM commercial_account_settings WHERE payload->'challenges' ? $1", digest(payload.challenge_id))
            uid = row["user_id"] if row else None
        except Exception:
            raise HTTPException(503, "Doğrulama deposu kullanılamıyor") from None
    else:
        db = store.connection(request)
        try:
            for row in db.execute("SELECT user_id,payload FROM commercial_account_settings"):
                if digest(payload.challenge_id) in json.loads(row[1]).get("challenges", {}):
                    uid = row[0]
                    break
        finally:
            db.close()
    user = next((row for row in auth.runtime(request)["state"]["users"] if row["id"] == uid), None)
    if user is None and uid:
        from .google_oauth import hydrate_user
        user = await hydrate_user(request, user_id=uid)
    if not user:
        raise HTTPException(401, "Doğrulama isteği geçersiz")
    await auth.refresh_auth_security(request, user)
    if not user.get("active"):
        raise HTTPException(401, "Doğrulama isteği geçersiz")
    valid = False
    async with store.edit(request, user["id"]) as doc:
        row = doc.get("challenges", {}).get(digest(payload.challenge_id))
        if challenge_valid(row, user, "login") and doc.get("two_factor_enabled") and row.get("attempts", 0) < 5:
            row["attempts"] = row.get("attempts", 0) + 1
            valid = check_limited_totp(request, doc, payload.code)
            if valid:
                row["used"] = True
                remember, browser, google = row["remember"], row["browser_session"], row["google"]
    if not valid:
        auth.record_event(request, user, "auth.mfa_failed", "invalid_mfa", "security", 401)
        raise HTTPException(401, "Doğrulama kodu veya isteği geçersiz")
    await auth.validate_authoritative_session(request.app, user["id"], int(user.get("auth_version", 1)))
    await auth.restore_demo_state_for_user(request.app, user["id"])
    await auth.restore_v21_state_for_user(request.app, user["id"])
    token = issue_token(user["id"], user["role"], auth.runtime(request)["secret"],
                        token_version=user.get("auth_version", 1),
                        ttl_seconds=auth.REMEMBER_SESSION_SECONDS if remember else auth.STANDARD_SESSION_SECONDS)
    await login_record(request, user, token, google=google)
    marker = auth.browser_session_response_token(token, user, request, response, browser_session=browser, remember=remember)
    return {"token": marker, "user": auth.public_user(user), "license": auth.active_license(auth.runtime(request)["state"], user["id"]), "demo_only": True, "remember": remember}


@router.post("/account/sessions/revoke")
async def revoke_session(payload: SessionRevoke, request: Request, response: Response):
    user = await member(request)
    async with store.edit(request, user["id"]) as doc:
        row = doc.get("sessions", {}).get(payload.session_id)
        if not row:
            raise HTTPException(404, "Oturum bulunamadı")
        row["revoked"] = True
        activity(doc, "SESSION_REVOKED", "Oturum kapatıldı")
    current = payload.session_id == token_payload(request).get("jti")
    if current:
        auth.clear_rotated_session_cookie(request, response, user["id"])
    auth.record_event(request, user, "account.session_revoked", "session_revoked", "account")
    return {"ok": True, **({"reauthenticate": True} if current else {})}


@router.post("/account/sessions/revoke-others")
async def revoke_others(request: Request):
    user = await member(request)
    current = token_payload(request).get("jti")
    async with store.edit(request, user["id"]) as doc:
        for row in doc.get("sessions", {}).values():
            if row["id"] != current:
                row["revoked"] = True
        # Legacy, previously unseen tokens cannot return after revoke-others.
        doc["sessions_valid_after"] = time.time()
        doc["preserved_session"] = current
        activity(doc, "OTHER_SESSIONS_REVOKED", "Diğer oturumlar kapatıldı")
    auth.record_event(request, user, "account.session_revoked", "session_revoked", "account")
    return {"ok": True}


@router.post("/account/close")
async def close_account(payload: Close, request: Request, response: Response):
    user = await member(request)
    blocker = await close_blocker(request, user)
    if blocker:
        raise HTTPException(409, blocker)
    await proof(request, user, payload)
    async with store.edit(request, user["id"]) as doc:
        await rotate(request, user, doc, {"active": False, "closed_at": iso()})
        activity(doc, "ACCOUNT_CLOSED", "Hesap kapatıldı; veriler saklanıyor")
    await persist_projection(request)
    auth.clear_rotated_session_cookie(request, response, user["id"])
    return {"ok": True, "reauthenticate": True}


async def admin_target(request, user_id):
    await member(request, owner=True)
    user = next((row for row in auth.runtime(request)["state"]["users"] if row["id"] == user_id), None)
    if user is None:
        from .google_oauth import hydrate_user
        user = await hydrate_user(request, user_id=user_id)
    if user is None:
        pool = getattr(request.app.state, "db_pool", None)
        if pool is not None:
            try:
                row = await pool.fetchrow("SELECT auth_version, security FROM commercial_auth_users WHERE user_id = $1", user_id)
            except Exception:
                raise HTTPException(503, "Hesap deposu kullanılamıyor") from None
            if row:
                security = row["security"]
                security = json.loads(security) if isinstance(security, str) else security
                user = {"id": user_id, **security, "auth_version": int(row["auth_version"])}
                auth.runtime(request)["state"]["users"].append(user)
    if user is None:
        raise HTTPException(404, "Hesap bulunamadı")
    await auth.refresh_auth_security(request, user)
    return user


@router.get("/admin/accounts")
async def admin_accounts(request: Request, search: str = Query(default="", max_length=180),
                         premium: Literal["all", "premium", "free"] = "all",
                         status: Literal["all", "active", "inactive"] = "all",
                         verified: Literal["all", "verified", "unverified"] = "all",
                         page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)):
    await member(request, owner=True)
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            rows = await pool.fetch("SELECT user_id, auth_version, security FROM commercial_auth_users")
        except Exception:
            raise HTTPException(503, "Hesap listesi kullanılamıyor") from None
        state = auth.runtime(request)["state"]
        for row in rows:
            if not any(user["id"] == row["user_id"] for user in state["users"]):
                security = row["security"]
                security = json.loads(security) if isinstance(security, str) else security
                state["users"].append({"id": row["user_id"], **security, "auth_version": int(row["auth_version"])})
    users = []
    for user in list(auth.runtime(request)["state"]["users"]):
        await auth.refresh_auth_security(request, user)
        if search.casefold() not in (str(user.get("email", "")) + " " + str(user.get("display_name", ""))).casefold():
            continue
        if status != "all" and bool(user.get("active")) != (status == "active"):
            continue
        if verified != "all" and bool(user.get("email_verified")) != (verified == "verified"):
            continue
        result = await overview(request, user, current=False)
        if premium != "all" and result["subscription"]["is_premium"] != (premium == "premium"):
            continue
        users.append({**result["user"], "subscription": result["subscription"],
                      "two_factor_enabled": result["security"]["two_factor_enabled"],
                      "active_sessions": result["security"]["active_sessions"], "closed_at": user.get("closed_at")})
    offset = (page - 1) * page_size
    return {"users": users[offset:offset + page_size], "total": len(users), "page": page, "page_size": page_size}


@router.get("/admin/accounts/{user_id}")
async def admin_account(user_id: str, request: Request):
    user = await admin_target(request, user_id)
    return await overview(request, user, current=token_payload(request)["sub"] == user_id)


@router.post("/admin/accounts/{user_id}/password-reset")
async def admin_password_reset(user_id: str, request: Request):
    user = await admin_target(request, user_id)
    auth.record_event(request, user, "auth.password_reset_requested", "reset_requested", "security")
    await auth.enforce_auth_limit(request, "account-mail", user["id"])
    if not auth.gmail_configured():
        raise HTTPException(503, "E-posta servisi kullanılamıyor")
    token = auth.issue_one_time_token(auth.runtime(request)["state"], user, auth.runtime(request)["secret"], kind="PASSWORD_RESET")
    await store.save_action_token(request, token, user, "PASSWORD_RESET")
    await persist_projection(request)
    await delivery(request, user, title=auth.RESET_SUBJECT,
                   action_url=auth.app_base_url() + "/reset-password?token=" + quote(token), label="Parolamı yenile")
    return {"ok": True}
