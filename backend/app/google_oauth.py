"""Google OIDC authentication feeding the existing commercial USER session."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from pydantic import BaseModel, ConfigDict

from . import v22_commercial as auth
from .browser_security import browser_request, secure_cookie, validate_browser_request
from .commercial_core import normalize_email, issue_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v22/auth/google", tags=["Google authentication"])
AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUER = "https://accounts.google.com"
CALLBACK_PATH = "/api/v22/auth/google/callback"
ORIGINS = frozenset({"https://kaistrade.com", "https://frontend-nu-two-18.vercel.app"})
BINDING_COOKIE = "protrebot_google_binding"
PENDING_COOKIE = "protrebot_google_pending"
COOKIE_PATH = "/api/v22/auth/google"
ATTEMPT_SECONDS = 300
SCHEMA = """
CREATE TABLE IF NOT EXISTS commercial_google_attempts (
    handle_hash TEXT PRIMARY KEY,
    binding_hash TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('authorization', 'consent')),
    payload TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    user_id TEXT
);
CREATE INDEX IF NOT EXISTS commercial_google_attempts_expiry
    ON commercial_google_attempts (expires_at);
CREATE TABLE IF NOT EXISTS commercial_google_identities (
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    user_id TEXT NOT NULL UNIQUE REFERENCES commercial_auth_users(user_id) ON DELETE CASCADE,
    user_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (issuer, subject)
);
"""


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    remember: bool = False


class CompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    terms_accepted: bool


class OAuthFailure(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def configured() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID", "").strip() and os.environ.get("GOOGLE_CLIENT_SECRET", "").strip())


def configuration() -> tuple[str, str]:
    if not configured():
        raise HTTPException(503, "Google girişi yapılandırılmamış: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET gerekli")
    return os.environ["GOOGLE_CLIENT_ID"].strip(), os.environ["GOOGLE_CLIENT_SECRET"].strip()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def cipher(request: Request) -> Fernet:
    secret = auth.runtime(request)["secret"]
    key = hashlib.sha256(b"KAISTRADE:GOOGLE-OAUTH:V1\0" + secret).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def trusted_origin(request: Request) -> str:
    origin = request.headers.get("origin", "").rstrip("/")
    if origin not in ORIGINS:
        raise HTTPException(403, "Google giriş origin'i izinli değil")
    validate_browser_request(request, ORIGINS)
    if not browser_request(request):
        raise HTTPException(403, "Browser request verification is required")
    return origin


def set_flow_cookie(response: Response, request: Request, name: str, value: str) -> None:
    response.set_cookie(name, value, max_age=ATTEMPT_SECONDS, path=COOKIE_PATH,
                        secure=secure_cookie(request), httponly=True, samesite="lax")


def clear_flow_cookie(response: Response, request: Request, name: str) -> None:
    response.delete_cookie(name, path=COOKIE_PATH, secure=secure_cookie(request),
                           httponly=True, samesite="lax")


def callback_query(request: Request) -> None:
    # Uvicorn reads this scope for access logs after the response begins.
    parameters = request.query_params
    request.state.google_callback = {
        key: parameters.get(key, "") if len(parameters.getlist(key)) == 1 else ""
        for key in ("code", "state", "error")
    }
    request.scope["query_string"] = b""
    request.__dict__.pop("_url", None)
    request.__dict__.pop("_query_params", None)


async def store(request: Request) -> Any:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None or auth.runtime(request).get("auth_storage_sync_failed"):
        raise HTTPException(503, "Google girişinin kalıcı güvenlik deposu hazır değil")
    if not getattr(request.app.state, "google_oauth_schema_ready", False):
        try:
            await auth.ensure_commercial_schema(request.app)
            if not getattr(request.app.state, "google_oauth_schema_ready", False):
                await pool.execute(SCHEMA)
        except Exception as exc:
            logger.error("Google OAuth storage initialization failed: %s", type(exc).__name__)
            raise HTTPException(503, "Google girişinin kalıcı güvenlik deposu hazır değil") from None
        request.app.state.google_oauth_schema_ready = True
    return pool


async def create_attempt(
    request: Request, payload: dict[str, Any], binding: str, purpose: str,
) -> str:
    pool = await store(request)
    handle = secrets.token_urlsafe(32)
    encrypted = cipher(request).encrypt(json.dumps(payload).encode()).decode()
    try:
        await pool.execute("DELETE FROM commercial_google_attempts WHERE expires_at < NOW()")
        await pool.execute(
            """INSERT INTO commercial_google_attempts
               (handle_hash, binding_hash, purpose, payload, expires_at)
               VALUES ($1, $2, $3, $4, $5)""",
            digest(handle), digest(binding), purpose, encrypted,
            datetime.now(timezone.utc) + timedelta(seconds=ATTEMPT_SECONDS),
        )
    except Exception as exc:
        logger.error("Google OAuth attempt write failed: %s", type(exc).__name__)
        raise HTTPException(503, "Google giriş isteği güvenli şekilde saklanamadı") from None
    return handle


async def read_attempt(request: Request, handle: str, binding: str, purpose: str, *, consume: bool) -> dict[str, Any]:
    if not handle or not binding or len(handle) > 128 or len(binding) > 128:
        raise OAuthFailure("invalid_attempt")
    pool = await store(request)
    query = (
        """UPDATE commercial_google_attempts SET consumed_at = NOW()
           WHERE handle_hash = $1 AND binding_hash = $2 AND purpose = $3
             AND consumed_at IS NULL AND expires_at > NOW() RETURNING payload"""
        if consume else
        """SELECT payload FROM commercial_google_attempts
           WHERE handle_hash = $1 AND binding_hash = $2 AND purpose = $3
             AND consumed_at IS NULL AND expires_at > NOW()"""
    )
    try:
        row = await pool.fetchrow(query, digest(handle), digest(binding), purpose)
    except Exception as exc:
        logger.error("Google OAuth attempt read failed: %s", type(exc).__name__)
        raise HTTPException(503, "Google giriş isteği doğrulanamadı") from None
    if row is None:
        raise OAuthFailure("invalid_attempt")
    try:
        payload = json.loads(cipher(request).decrypt(row["payload"].encode()).decode())
    except (InvalidToken, ValueError, TypeError, KeyError):
        raise OAuthFailure("invalid_attempt") from None
    if not isinstance(payload, dict) or payload.get("origin") not in ORIGINS or not isinstance(payload.get("remember"), bool):
        raise OAuthFailure("invalid_attempt")
    return payload


def validate_identity(token: str, client_id: str, nonce_hash: str) -> dict[str, str]:
    transport = GoogleRequest()
    def certificate_request(url: str, **kwargs):
        if url != "https://www.googleapis.com/oauth2/v1/certs":
            raise OAuthFailure("invalid_identity")
        return transport(url=url, timeout=10, **kwargs)
    try:
        claims = id_token.verify_oauth2_token(token, certificate_request, audience=client_id)
    except (GoogleAuthError, ValueError, TypeError):
        raise OAuthFailure("invalid_identity") from None
    if not isinstance(claims, dict) or (
        claims.get("iss") not in {ISSUER, "accounts.google.com"}
        or claims.get("aud") != client_id
        or claims.get("azp", client_id) != client_id
        or not isinstance(claims.get("exp"), int)
        or isinstance(claims.get("exp"), bool)
        or claims["exp"] <= datetime.now(timezone.utc).timestamp()
        or not isinstance(claims.get("nonce"), str)
        or not hmac.compare_digest(digest(claims["nonce"]), nonce_hash)
        or claims.get("email_verified") is not True
    ):
        raise OAuthFailure("invalid_identity")
    subject, email = claims.get("sub"), claims.get("email")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise OAuthFailure("invalid_identity")
    if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise OAuthFailure("invalid_identity")
    name = claims.get("name")
    return {"issuer": ISSUER, "subject": subject, "email": normalize_email(email),
            "display_name": (name.strip() if isinstance(name, str) else email.split("@")[0])[:100]}


async def exchange_identity(request: Request, code: str, attempt: dict[str, Any]) -> dict[str, str]:
    client_id, client_secret = configuration()
    if not code or len(code) > 4096:
        raise OAuthFailure("invalid_identity")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = await client.post(TOKEN_URL, data={
                "grant_type": "authorization_code", "code": code,
                "client_id": client_id, "client_secret": client_secret,
                "redirect_uri": attempt["origin"] + CALLBACK_PATH,
                "code_verifier": attempt["verifier"],
            })
        if response.status_code != 200:
            raise OAuthFailure("provider_error")
        payload = response.json()
        token = payload.get("id_token") if isinstance(payload, dict) else None
        if not isinstance(token, str):
            raise OAuthFailure("invalid_identity")
        return await asyncio.to_thread(validate_identity, token, client_id, attempt["nonce_hash"])
    except (httpx.HTTPError, ValueError, GoogleAuthError):
        raise OAuthFailure("provider_error") from None


def decode_payload(value: Any) -> dict[str, Any]:
    payload = json.loads(value) if isinstance(value, str) else value
    if not isinstance(payload, dict):
        raise HTTPException(503, "Google kullanıcı kaydı doğrulanamadı")
    return payload


async def hydrate_user(request: Request, *, user_id: str | None = None, email: str | None = None) -> dict[str, Any] | None:
    if not configured():
        return None
    pool = await store(request)
    condition = "identity.user_id = $1" if user_id is not None else "identity.user_payload->>'email' = $1"
    try:
        row = await pool.fetchrow(
            f"""SELECT identity.user_payload, security.auth_version, security.security
                FROM commercial_google_identities AS identity
                JOIN commercial_auth_users AS security ON security.user_id = identity.user_id
                WHERE {condition}""", user_id if user_id is not None else email,
        )
    except Exception as exc:
        logger.error("Google identity lookup failed: %s", type(exc).__name__)
        raise HTTPException(503, "Google kullanıcı kaydı okunamadı") from None
    if row is None:
        return None
    return merge_user(auth.runtime(request), row)


def merge_user(rt: dict[str, Any], row: Any) -> dict[str, Any]:
    user = decode_payload(row["user_payload"])
    registration = user.pop("_registration", {})
    user.update(decode_payload(row["security"]))
    user["auth_version"] = int(row["auth_version"])
    user["auth_provider"] = "google"
    current = next((item for item in rt["state"]["users"] if item["id"] == user["id"]), None)
    if current is None:
        rt["state"]["users"].append(user)
    else:
        current.update({key: user[key] for key in (*auth.AUTH_SECURITY_FIELDS, "auth_version")})
        user = current
    rt.setdefault("auth_baseline", {})[user["id"]] = {"auth_version": user["auth_version"], **auth.auth_security(user)}
    for collection in ("profiles", "subscriptions", "acceptances"):
        for item in registration.get(collection, []):
            if not any(existing.get("id") == item["id"] for existing in rt["state"][collection]):
                rt["state"][collection].append(copy.deepcopy(item))
    return user


async def merge_registered_users(application: Any, connection: Any) -> None:
    rows = await connection.fetch(
        """SELECT identity.user_payload, security.auth_version, security.security
           FROM commercial_google_identities AS identity
           JOIN commercial_auth_users AS security ON security.user_id = identity.user_id"""
    )
    for row in rows:
        merge_user(application.state.v22_commercial, row)


@asynccontextmanager
async def identity_transaction(request: Request):
    pool = await store(request)
    lock = getattr(request.app.state, "google_identity_write_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.google_identity_write_lock = lock
    # Password helpers need the pool's second connection; do not occupy it with a waiting identity writer.
    async with lock, pool.acquire() as connection, connection.transaction():
        yield connection


@asynccontextmanager
async def registration_guard(request: Request, email: str):
    if not configured():
        yield
        return
    try:
        async with identity_transaction(request) as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", "google-email:" + normalize_email(email))
            row = await connection.fetchrow(
                "SELECT payload FROM application_state_snapshots WHERE state_key = $1", auth.COMMERCIAL_STATE_KEY,
            )
            if row:
                users = decode_payload(row["payload"]).get("users", [])
                found = next((user for user in users if user.get("email") == normalize_email(email)), None)
                if found and not any(user.get("email") == normalize_email(email) for user in auth.runtime(request)["state"]["users"]):
                    auth.runtime(request)["state"]["users"].append(copy.deepcopy(found))
            await hydrate_user(request, email=normalize_email(email))
            yield
    except (HTTPException, asyncio.CancelledError):
        raise
    except Exception as exc:
        logger.error("Google registration guard failed: %s", type(exc).__name__)
        raise HTTPException(503, "Kalıcı kayıt güvenliği doğrulanamadı") from None


async def resolve_identity(request: Request, identity: dict[str, str], *, create: bool) -> dict[str, Any] | None:
    try:
        async with identity_transaction(request) as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", "google-email:" + identity["email"])
            row = await connection.fetchrow(
                """SELECT user_id FROM commercial_google_identities
                   WHERE issuer = $1 AND subject = $2""", identity["issuer"], identity["subject"],
            )
            if row:
                uid = row["user_id"]
            else:
                await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", auth.COMMERCIAL_STATE_KEY)
                snapshot = await connection.fetchrow(
                    "SELECT payload FROM application_state_snapshots WHERE state_key = $1 FOR UPDATE", auth.COMMERCIAL_STATE_KEY,
                )
                state = auth.sanitize_state(decode_payload(snapshot["payload"]) if snapshot else copy.deepcopy(auth.runtime(request)["state"]))
                if any(user.get("email") == identity["email"] for user in state["users"]):
                    raise OAuthFailure("account_link_required")
                if not create:
                    return None
                uid = uuid.uuid4().hex
                created = auth.now_iso()
                user = {"id": uid, "email": identity["email"], "display_name": identity["display_name"],
                        "role": "CUSTOMER", "active": True, "auth_version": 1, "email_verified": True,
                        "password": {}, "auth_provider": "google", "created_at": created}
                state["users"].append(user)
                state["profiles"].append({"id": uuid.uuid4().hex, "user_id": uid, "full_name": user["display_name"],
                                          "avatar_url": None, "role": "user", "preferences": {},
                                          "created_at": created, "updated_at": created})
                state["subscriptions"].append({"id": uuid.uuid4().hex, "user_id": uid, "plan": "FREE",
                                               "status": "inactive", "started_at": created, "expires_at": None,
                                               "created_at": created, "updated_at": created})
                state["acceptances"].append({"id": uuid.uuid4().hex, "user_id": uid, "terms_accepted": True,
                                             "source": "GOOGLE_REGISTRATION", "accepted_at": created})
                auth.add_audit(state, "USER_REGISTERED", "Google kimliğiyle CUSTOMER kaydı oluşturuldu.", actor=uid, subject=uid)
                await connection.execute(
                    """INSERT INTO commercial_auth_users (user_id, auth_version, security)
                       VALUES ($1, $2, $3::jsonb)""", uid, 1, json.dumps(auth.auth_security(user)),
                )
                await connection.execute(
                    """INSERT INTO commercial_google_identities (issuer, subject, user_id, user_payload)
                       VALUES ($1, $2, $3, $4::jsonb)""", identity["issuer"], identity["subject"], uid,
                    json.dumps({**user, "_registration": {
                        collection: [item for item in state[collection] if item.get("user_id") == uid]
                        for collection in ("profiles", "subscriptions", "acceptances")
                    }}),
                )
                await connection.execute(
                    """INSERT INTO application_state_snapshots (state_key, updated_at, payload)
                       VALUES ($1, NOW(), $2::jsonb) ON CONFLICT (state_key)
                       DO UPDATE SET updated_at = NOW(), payload = EXCLUDED.payload""",
                    auth.COMMERCIAL_STATE_KEY, json.dumps(state),
                )
        user = await hydrate_user(request, user_id=uid)
        if user is None:
            raise OAuthFailure("account_unavailable")
        await auth.refresh_auth_security(request, user)
        if not user.get("active") or user.get("email_verified") is not True:
            raise OAuthFailure("account_unavailable")
        return user
    except (OAuthFailure, HTTPException, asyncio.CancelledError):
        raise
    except Exception as exc:
        logger.error("Google identity transaction failed: %s", type(exc).__name__)
        raise HTTPException(503, "Google hesabı güvenli şekilde kaydedilemedi") from None


async def establish_session(request: Request, response: Response, user: dict[str, Any], remember: bool) -> str:
    from .account_settings import enabled, login_record
    if await enabled(request, user):
        raise HTTPException(403, "İki aşamalı doğrulama gerekli")
    version = int(user.get("auth_version", 1))
    await auth.validate_authoritative_session(request.app, user["id"], version)
    await auth.restore_demo_state_for_user(request.app, user["id"])
    await auth.restore_v21_state_for_user(request.app, user["id"])
    await auth.validate_authoritative_session(request.app, user["id"], version)
    token = issue_token(user["id"], user["role"], auth.runtime(request)["secret"], token_version=version,
                        ttl_seconds=auth.REMEMBER_SESSION_SECONDS if remember else auth.STANDARD_SESSION_SECONDS)
    await login_record(request, user, token, google=True)
    return auth.browser_session_response_token(token, user, request, response, browser_session=True, remember=remember)


def redirect(request: Request, origin: str, result: str, remember: bool | None = None) -> RedirectResponse:
    if origin not in ORIGINS:
        origin = "https://kaistrade.com"
    parameters = {"google_login": result}
    if remember is not None:
        parameters["google_remember"] = "1" if remember else "0"
    response = RedirectResponse(origin + "/?" + urlencode(parameters), status_code=303,
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    clear_flow_cookie(response, request, BINDING_COOKIE)
    return response


@router.post("/start")
async def start(payload: StartRequest, request: Request, response: Response):
    client_id, _ = configuration()
    origin = trusted_origin(request)
    await store(request)
    await auth.enforce_auth_limit(request, "login", None)
    binding, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(3))
    state = await create_attempt(request, {"origin": origin, "remember": payload.remember,
                                         "verifier": verifier, "nonce_hash": digest(nonce)}, binding, "authorization")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    set_flow_cookie(response, request, BINDING_COOKIE, binding)
    clear_flow_cookie(response, request, PENDING_COOKIE)
    response.headers["Cache-Control"] = "no-store"
    return {"authorization_url": AUTHORIZATION_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": origin + CALLBACK_PATH, "response_type": "code",
        "scope": "openid email profile", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account",
    })}


@router.get("/callback")
async def callback(request: Request):
    if not hasattr(request.state, "google_callback"):
        callback_query(request)
    query = request.state.google_callback
    origin = "https://kaistrade.com"
    try:
        attempt = await read_attempt(request, query["state"], request.cookies.get(BINDING_COOKIE, ""),
                                     "authorization", consume=True)
        origin = attempt["origin"]
        if query["error"]:
            raise OAuthFailure("cancelled")
        identity = await exchange_identity(request, query["code"], attempt)
        user = await resolve_identity(request, identity, create=False)
        if user is None:
            binding = secrets.token_urlsafe(32)
            pending = await create_attempt(request, {"origin": origin, "remember": attempt["remember"],
                                                     "identity": identity}, binding, "consent")
            response = redirect(request, origin, "consent")
            set_flow_cookie(response, request, BINDING_COOKIE, binding)
            set_flow_cookie(response, request, PENDING_COOKIE, pending)
            return response
        from .account_settings import enabled, login_challenge
        if await enabled(request, user):
            challenge = await login_challenge(request, user, remember=attempt["remember"], browser_session=True, google=True)
            response = RedirectResponse(origin + "/login?mfa_challenge=" + challenge["challenge_id"] +
                                        "&google_remember=" + ("1" if attempt["remember"] else "0"), status_code=303,
                                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            clear_flow_cookie(response, request, BINDING_COOKIE)
            clear_flow_cookie(response, request, PENDING_COOKIE)
            return response
        response = redirect(request, origin, "success", attempt["remember"])
        await establish_session(request, response, user, attempt["remember"])
        clear_flow_cookie(response, request, PENDING_COOKIE)
        return response
    except (OAuthFailure, HTTPException) as exc:
        reason = exc.reason if isinstance(exc, OAuthFailure) else "service_unavailable"
        logger.warning("Google OAuth callback rejected: %s", reason)
        response = redirect(request, origin, reason)
        clear_flow_cookie(response, request, PENDING_COOKIE)
        return response


@router.get("/pending")
async def pending(request: Request, response: Response):
    try:
        attempt = await read_attempt(request, request.cookies.get(PENDING_COOKIE, ""),
                                     request.cookies.get(BINDING_COOKIE, ""), "consent", consume=False)
    except OAuthFailure:
        raise HTTPException(400, "Google kayıt isteği geçersiz veya süresi dolmuş; yeniden giriş başlatın") from None
    response.headers["Cache-Control"] = "no-store"
    return {"email": attempt["identity"]["email"], "display_name": attempt["identity"]["display_name"]}


@router.post("/complete")
async def complete(payload: CompleteRequest, request: Request, response: Response):
    origin = trusted_origin(request)
    if not payload.terms_accepted:
        raise HTTPException(422, "Kullanım koşulları ve gizlilik politikası kabul edilmelidir")
    try:
        attempt = await read_attempt(request, request.cookies.get(PENDING_COOKIE, ""),
                                     request.cookies.get(BINDING_COOKIE, ""), "consent", consume=True)
        if origin != attempt["origin"]:
            raise OAuthFailure("invalid_attempt")
        await auth.enforce_auth_limit(request, "register", attempt["identity"]["email"])
        user = await resolve_identity(request, attempt["identity"], create=True)
        if user is None:
            raise OAuthFailure("account_unavailable")
        from .account_settings import enabled, login_challenge
        if await enabled(request, user):
            result = await login_challenge(request, user, remember=attempt["remember"], browser_session=True, google=True)
            clear_flow_cookie(response, request, BINDING_COOKIE)
            clear_flow_cookie(response, request, PENDING_COOKIE)
            return result
        marker = await establish_session(request, response, user, attempt["remember"])
    except OAuthFailure as exc:
        logger.warning("Google registration rejected: %s", exc.reason)
        raise HTTPException(409 if exc.reason == "account_link_required" else 400,
                            "Google kaydı tamamlanamadı; mevcut hesabınız varsa e-posta/parola ile giriş yapın") from None
    clear_flow_cookie(response, request, BINDING_COOKIE)
    clear_flow_cookie(response, request, PENDING_COOKIE)
    response.headers["Cache-Control"] = "no-store"
    return {"token": marker, "user": auth.public_user(user), "remember": attempt["remember"]}
