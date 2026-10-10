"""Optional email verification using the existing serialized account store."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from typing import NoReturn

from fastapi import HTTPException

from . import account_store as store
from .browser_security import REGISTRATION_PENDING_COOKIE
from .web_security import env_flag

TTL = 1800
PREFIX = "ev2_"
COOKIE_PATH = "/api"


def enabled() -> bool:
    return env_flag("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED", default=False)


def require_enabled() -> None:
    if not enabled():
        raise HTTPException(404, "E-posta doğrulama ekranı etkin değil.")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def iso(stamp: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if stamp is None else stamp, timezone.utc).isoformat()


def reject(code: str) -> NoReturn:
    from . import v22_commercial as auth
    messages = {
        "expired": "Bağlantının süresi doldu",
        "used": "Bu bağlantı zaten kullanılmış",
        "invalid": "Doğrulama bağlantısı geçersiz",
        "pending": "Bekleyen kayıt oturumu geçersiz veya süresi dolmuş. Giriş yapabilirsin.",
    }
    auth.logger.warning("Email verification rejected: code=%s", code)
    raise HTTPException(401 if code == "pending" else 400,
                        {"code": code, "message": messages[code]})


async def load_user(request, user_id: str, doc: dict | None = None) -> dict | None:
    from . import v22_commercial as auth
    rt = auth.runtime(request)
    if rt.get("auth_storage_sync_failed"):
        raise HTTPException(503, "Kimlik doğrulama deposu kullanılamıyor")
    user = next((item for item in rt["state"]["users"] if item["id"] == user_id), None)
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        db = store.active_connection(request, user_id) or pool
        row = await db.fetchrow(
            "SELECT auth_version, security FROM commercial_auth_users WHERE user_id = $1", user_id)
        if row is None:
            return None
        security = row["security"]
        security = json.loads(security) if isinstance(security, str) else security
        if user is None:
            user = {"id": user_id}
            rt["state"]["users"].append(user)
        user.update(security)
        user["auth_version"] = int(row["auth_version"])
    else:
        if auth.DURABLE_AUTH_REQUIRED or rt.get("authoritative_auth_enabled"):
            raise HTTPException(503, "Kalıcı kimlik doğrulama deposu hazır değil")
        if doc is None:
            doc = await store.read(request, user_id)
        overlay = (doc or {}).get("auth_overlay")
        if overlay:
            if user is None:
                user = {"id": user_id}
                rt["state"]["users"].append(user)
            user.update(copy.deepcopy(overlay))
    if user is not None:
        rt.setdefault("auth_baseline", {})[user_id] = {
            "auth_version": int(user.get("auth_version", 1)), **auth.auth_security(user),
        }
    return user


def pending_token(user: dict, secret: bytes) -> str:
    stamp = int(time.time())
    payload = {"sub": user["id"], "email": user["email"], "kind": "EMAIL_PENDING",
               "role": "CUSTOMER", "iat": stamp, "exp": stamp + TTL,
               "jti": secrets.token_urlsafe(32)}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(
        hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()).decode().rstrip("=")
    return encoded + "." + signature


def set_pending_cookie(response, token: str) -> None:
    if response is None:
        raise HTTPException(503, "Güvenli kayıt çerezi yanıtı gerekli")
    response.set_cookie(REGISTRATION_PENDING_COOKIE, token, max_age=TTL, path=COOKIE_PATH,
                        secure=True, httponly=True, samesite="lax")


def clear_pending_cookie(response) -> None:
    response.delete_cookie(REGISTRATION_PENDING_COOKIE, path=COOKIE_PATH,
                           secure=True, httponly=True, samesite="lax")


def pending_proof(request) -> tuple[str, dict]:
    from . import v22_commercial as auth
    token = request.cookies.get(REGISTRATION_PENDING_COOKIE, "")
    try:
        proof = auth.verify_token(token, auth.runtime(request)["secret"], expected_kind="EMAIL_PENDING")
    except ValueError:
        reject("pending")
    return token, proof


def pending_valid(row: dict | None, proof: dict, user: dict | None) -> bool:
    return bool(row and not row.get("used_at") and row["expires"] > time.time()
                and row["email"] == proof["email"]
                and (user is None or user.get("email") == proof["email"]))


def retry_after(doc: dict) -> int:
    return max(0, int(doc.get("verification_last_sent", 0) + 60 - time.time() + 0.999))


def reserve_send(doc: dict) -> None:
    now = time.time()
    history = [stamp for stamp in doc.get("verification_sends", []) if stamp > now - 3600]
    wait = retry_after(doc)
    if len(history) >= 5:
        wait = max(wait, int(history[0] + 3600 - now + 0.999))
    if wait:
        raise HTTPException(429, "Çok fazla doğrulama gönderimi; daha sonra tekrar dene.",
                            headers={"Retry-After": str(wait)})
    doc["verification_sends"] = [*history, now]
    doc["verification_last_sent"] = now


def create_link(doc: dict, user: dict) -> str:
    stamp = time.time()
    rows = doc.setdefault("email_verification_tokens", {})
    for row in rows.values():
        if not row.get("used_at") and not row.get("revoked_at"):
            row["revoked_at"] = iso(stamp)
    doc["legacy_verification_revoked_at"] = stamp
    token = PREFIX + secrets.token_urlsafe(32)
    rows[digest(token)] = {"user_id": user["id"], "email": user["email"],
                          "version": int(user.get("auth_version", 1)),
                          "expires": stamp + TTL, "used_at": None, "revoked_at": None}
    return token


async def registration_link(request, user: dict) -> str:
    from . import v22_commercial as auth
    async with store.edit(request, user["id"]) as doc:
        if getattr(request.app.state, "db_pool", None) is None:
            doc["auth_overlay"] = {"auth_version": int(user.get("auth_version", 1)), **auth.auth_security(user)}
        reserve_send(doc)
        return create_link(doc, user)


async def complete_registration(request, response, user: dict) -> None:
    from . import v22_commercial as auth
    token = pending_token(user, auth.runtime(request)["secret"])
    proof = auth.verify_token(token, auth.runtime(request)["secret"], expected_kind="EMAIL_PENDING")
    async with store.edit(request, user["id"]) as doc:
        doc.setdefault("registration_pending", {})[digest(token)] = {
            "email": user["email"], "version": int(user.get("auth_version", 1)),
            "expires": proof["exp"], "used_at": None,
        }
        # Synthetic registration responses have the same cookie and cooldown.
        if not doc.get("verification_sends"):
            reserve_send(doc)
    set_pending_cookie(response, token)


async def session_user(request) -> dict:
    """Read an existing USER session only for verification, never private APIs."""
    from . import account_settings as account
    from . import v22_commercial as auth
    try:
        token = auth.bearer(request)
        proof = auth.verify_token(token, auth.runtime(request)["secret"], expected_kind="USER")
    except (ValueError, HTTPException):
        reject("pending")
    user = await load_user(request, proof["sub"])
    if not user or not user.get("active") or int(user.get("auth_version", 1)) != int(proof.get("ver", 1)):
        reject("pending")
    await account.track_session(request, token)
    return user


async def pending_status(request) -> dict:
    require_enabled()
    if not request.cookies.get(REGISTRATION_PENDING_COOKIE):
        user = await session_user(request)
        doc = await store.read(request, user["id"]) or {}
        return {"verified": bool(user.get("email_verified")), "email": user["email"],
                "can_exchange": False, "already_authenticated": bool(user.get("email_verified")),
                "retry_after": retry_after(doc), "can_change_email": False}
    token, proof = pending_proof(request)
    async with store.edit(request, proof["sub"]) as doc:
        user = await load_user(request, proof["sub"], doc)
        row = doc.get("registration_pending", {}).get(digest(token))
        if not pending_valid(row, proof, user):
            reject("pending")
        verified = bool(user and user.get("active") and user.get("email_verified"))
        can_exchange = bool(verified and not doc.get("two_factor_enabled")
                            and row["version"] == int(user.get("auth_version", 1)))
        return {"verified": verified, "email": proof["email"], "can_exchange": can_exchange,
                "already_authenticated": False, "retry_after": retry_after(doc),
                "can_change_email": not verified}


async def send_link(request, user_id: str, *, pending: tuple[str, dict] | None = None) -> dict:
    from . import v22_commercial as auth
    async with store.edit(request, user_id) as doc:
        user = await load_user(request, user_id, doc)
        if pending:
            token, proof = pending
            if not pending_valid(doc.get("registration_pending", {}).get(digest(token)), proof, user):
                reject("pending")
        reserve_send(doc)
        if user and user.get("active") and not user.get("email_verified"):
            token = create_link(doc, user)
            try:
                await asyncio.to_thread(
                    auth.send_auth_email, to_email=user["email"], display_name=user["display_name"],
                    subject=auth.VERIFY_SUBJECT, title="E-posta adresini onayla",
                    action_url=auth.app_base_url() + "/verify-email?token=" + token,
                    action_label="E-postamı doğrula", expiry="30 dakika", verification_v2=True)
            except auth.GMAIL_DELIVERY_ERRORS as exc:
                auth.log_gmail_failure(exc, request.app)
                auth.record_event(request, user, "auth.email_verification_failed", "verification_failed", "verification", 503)
                raise HTTPException(503, "Doğrulama maili gönderilemedi. Lütfen tekrar dene.",
                                    headers={"Retry-After": "60"}) from None
            auth.record_event(request, user, "auth.email_verification_sent", "verification_sent", "verification")
    return {"ok": True, "message": "Yeni bağlantı gönderildi — gelen kutunu kontrol et", "retry_after": 60}


async def pending_resend(request, link_token: str | None = None) -> dict:
    require_enabled()
    if request.cookies.get(REGISTRATION_PENDING_COOKIE):
        pending = pending_proof(request)
        return await send_link(request, pending[1]["sub"], pending=pending)
    if link_token:
        from . import v22_commercial as auth
        if link_token.startswith(PREFIX):
            user_id = await find_link_user(request, digest(link_token))
        else:
            try:
                user_id = auth.verify_token(link_token, auth.runtime(request)["secret"],
                                            expected_kind="EMAIL_VERIFY", now=0)["sub"]
            except ValueError:
                user_id = None
        if user_id:
            return await send_link(request, user_id)
        return {"ok": True, "message": "Yeni bağlantı gönderildi — gelen kutunu kontrol et", "retry_after": 60}
    user = await session_user(request)
    return await send_link(request, user["id"])


async def change_pending_email(request, response, email: str, password: str) -> dict:
    require_enabled()
    from . import account_settings as account
    from . import v22_commercial as auth
    from .commercial_core import verify_password
    from .google_oauth import registration_guard
    token, proof = pending_proof(request)
    await auth.enforce_auth_limit(request, "account", proof["sub"])
    async with registration_guard(request, email), store.edit(request, proof["sub"]) as doc:
        user = await load_user(request, proof["sub"], doc)
        row = doc.get("registration_pending", {}).get(digest(token))
        if (not pending_valid(row, proof, user) or not user or user.get("role") not in {"CUSTOMER", "MODERATOR"} or not user.get("active")
                or user.get("email_verified") or doc.get("two_factor_enabled")
                or row["version"] != int(user.get("auth_version", 1))):
            raise HTTPException(401, "Parola doğrulanamadı veya kayıt oturumunun süresi doldu.")
        if not user.get("password") or not verify_password(password, user["password"]):
            raise HTTPException(401, "Parola doğrulanamadı veya kayıt oturumunun süresi doldu.")
        if email == user["email"]:
            raise HTTPException(422, "Farklı bir e-posta adresi yaz.")
        await account.require_available_email(request, user["id"], email)
        reserve_send(doc)
        await auth.update_auth_security(request, user, {"email": email, "email_verified": False, "email_verified_at": None})
        for pending in doc.get("registration_pending", {}).values():
            pending["used_at"] = iso()
        link = create_link(doc, user)
        await complete_registration(request, response, user)
        try:
            await asyncio.to_thread(
                auth.send_auth_email, to_email=email, display_name=user["display_name"],
                subject=auth.VERIFY_SUBJECT, title="E-posta adresini onayla",
                action_url=auth.app_base_url() + "/verify-email?token=" + link,
                action_label="E-postamı doğrula", expiry="30 dakika", verification_v2=True)
        except auth.GMAIL_DELIVERY_ERRORS as exc:
            auth.log_gmail_failure(exc, request.app)
            auth.record_event(request, user, "auth.email_verification_failed", "verification_failed", "verification", 503)
            raise HTTPException(503, "Adres değiştirilemedi; e-posta gönderilemedi. Lütfen tekrar dene.",
                                headers={"Retry-After": "60"}) from None
        auth.record_event(request, user, "auth.email_verification_sent", "verification_sent", "verification")
    return {"ok": True, "email": email, "retry_after": 60}


async def find_link_user(request, token_hash: str) -> str | None:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        row = await pool.fetchrow(
            "SELECT user_id FROM commercial_account_settings WHERE payload->'email_verification_tokens' ? $1",
            token_hash)
        return row["user_id"] if row else None
    from . import v22_commercial as auth
    if auth.DURABLE_AUTH_REQUIRED or auth.runtime(request).get("authoritative_auth_enabled"):
        raise HTTPException(503, "Kalıcı doğrulama deposu hazır değil")
    db = store.connection(request)
    try:
        row = db.execute(
            "SELECT user_id FROM commercial_account_settings WHERE json_extract(payload, ?) IS NOT NULL",
            ('$.email_verification_tokens."' + token_hash + '"',)).fetchone()
        return row[0] if row else None
    finally:
        db.close()


async def verify_link(request, token: str) -> dict:
    from . import v22_commercial as auth
    legacy = not token.startswith(PREFIX)
    payload = None
    if legacy:
        try:
            payload = auth.verify_token(token, auth.runtime(request)["secret"], expected_kind="EMAIL_VERIFY")
        except ValueError:
            try:
                auth.verify_token(token, auth.runtime(request)["secret"], expected_kind="EMAIL_VERIFY", now=0)
            except ValueError:
                reject("invalid")
            reject("expired")
        user_id = payload["sub"]
    else:
        user_id = await find_link_user(request, digest(token))
        if not user_id:
            reject("invalid")
    user = await load_user(request, user_id)
    if not user:
        reject("invalid")
    legacy_used = {row["jti"]: row.get("used", False)
                   for row in auth.runtime(request)["state"].get("auth_tokens", [])
                   if row.get("user_id") == user_id}
    try:
        async with store.edit(request, user_id) as doc:
            user = await load_user(request, user_id, doc)
            if not user or not user.get("active"):
                reject("invalid")
            already_verified = bool(user.get("email_verified"))
            if payload is not None:
                if payload["iat"] <= doc.get("legacy_verification_revoked_at", 0):
                    reject("invalid")
                await store.consume_action_token(request, token, user, "EMAIL_VERIFY")
                doc.setdefault("legacy_verification_used_at", {})[digest(token)] = iso()
            else:
                row = doc.get("email_verification_tokens", {}).get(digest(token))
                if not row or row.get("revoked_at"):
                    reject("invalid")
                if row.get("used_at"):
                    reject("used")
                if row["expires"] <= time.time():
                    reject("expired")
                if row["email"] != user["email"] or row["version"] != int(user.get("auth_version", 1)):
                    reject("invalid")
                row["used_at"] = iso()
            await auth.update_auth_security(
                request, user, {"email_verified": True, "email_verified_at": user.get("email_verified_at") or iso()})
    except BaseException:
        auth.record_event(request, user, "auth.email_verification_failed", "verification_failed", "verification", 400)
        if legacy:
            for row in auth.runtime(request)["state"].get("auth_tokens", []):
                if row.get("user_id") == user_id and row.get("jti") in legacy_used:
                    row["used"] = legacy_used[row["jti"]]
        raise
    auth.record_event(request, user, "auth.email_verified", "email_verified", "verification")
    return {"ok": True, "verified": True, "email": user["email"], "already_verified": already_verified}


async def exchange_pending(request, response) -> dict:
    require_enabled()
    from . import account_settings as account
    from . import v22_commercial as auth
    from .server_cookie import SESSION_COOKIE_NAME, request_session_token
    token, proof = pending_proof(request)
    user = await load_user(request, proof["sub"])
    async with store.edit(request, proof["sub"]) as doc:
        user = await load_user(request, proof["sub"], doc)
        row = doc.get("registration_pending", {}).get(digest(token))
        if not pending_valid(row, proof, user) or not user or not user.get("active"):
            reject("pending")
        if not user.get("email_verified"):
            raise HTTPException(403, "E-posta doğrulaması gerekli")
        row["used_at"] = iso()
        if doc.get("two_factor_enabled") or row["version"] != int(user.get("auth_version", 1)):
            result = {"requires_login": True, "email": user["email"]}
        else:
            existing_tokens = {request_session_token(request), request.cookies.get(SESSION_COOKIE_NAME, "")}
            for existing in existing_tokens:
                if not existing:
                    continue
                try:
                    current = auth.verify_token(existing, auth.runtime(request)["secret"], expected_kind="USER")
                except ValueError:
                    current = None
                if current and current["sub"] != user["id"]:
                    raise HTTPException(409, "Başka bir hesabın oturumu açık. Önce mevcut oturumdan çıkış yap.")
            session = auth.issue_token(user["id"], user["role"], auth.runtime(request)["secret"],
                                       token_version=int(user.get("auth_version", 1)))
            await account.login_record(request, user, session)
            marker = auth.browser_session_response_token(
                session, user, request, response, browser_session=True, remember=False)
            result = {"requires_login": False, "token": marker, "user": auth.public_user(user)}
    clear_pending_cookie(response)
    return result


async def rollback_registration(request, user_id: str) -> None:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        async with pool.acquire() as db, db.transaction():
            await db.execute("DELETE FROM commercial_account_settings WHERE user_id = $1", user_id)
            await db.execute("DELETE FROM commercial_auth_users WHERE user_id = $1", user_id)
    else:
        db = store.connection(request)
        try:
            db.execute("DELETE FROM commercial_account_settings WHERE user_id = ?", (user_id,))
        finally:
            db.close()


def migrate_local_accounts(request) -> int:
    """Explicit SQLite migration; never called by signup or application startup."""
    from . import v22_commercial as auth
    db = store.connection(request)
    changed = []
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS commercial_feature_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        if db.execute("SELECT 1 FROM commercial_feature_migrations WHERE name=?", ("email-verification-v2",)).fetchone():
            db.rollback()
            return 0
        stamp = iso()
        for user in auth.runtime(request)["state"]["users"]:
            row = db.execute("SELECT payload FROM commercial_account_settings WHERE user_id=?", (user["id"],)).fetchone()
            doc = json.loads(row[0]) if row else store.defaults()
            security = doc.get("auth_overlay") or {"auth_version": int(user.get("auth_version", 1)), **auth.auth_security(user)}
            if not security.get("email") or security.get("closed_at"):
                continue
            security.update(email_verified=True, email_verified_at=security.get("email_verified_at") or stamp)
            doc["auth_overlay"] = security
            db.execute("INSERT OR REPLACE INTO commercial_account_settings VALUES (?,?)", (user["id"], json.dumps(doc)))
            changed.append((user, security))
        db.execute("INSERT INTO commercial_feature_migrations VALUES (?,?)", ("email-verification-v2", stamp))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    for user, security in changed:
        user.update(security)
    return len(changed)
