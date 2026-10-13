"""V28 in-application Binance connection vault.

Secrets enter the owner-only API over HTTPS, are tested against the selected
Binance USD-M Futures host, and are persisted only as Fernet ciphertext in
PostgreSQL.  Public responses contain metadata and account summaries, never
API or secret key material.  Activating a connection does not arm an order
channel; V25's independent consent, evidence, policy and time-limited locks
remain authoritative for real orders.

LIVE keys require a signed Spot SAPI withdrawal-restrictions check on save
and activation. Legacy LIVE rows without that proof cannot supply active
credentials until reactivated. Futures Testnet has no withdrawal capability;
its permission status is explicitly not applicable, not reported as verified.
Background LIVE cache reads require the captured session auth_version to match
the shared active-user security row; missing or unavailable proof never falls
back to worker-local user state. Grants also require the original signed USER
expiry and fail closed while commercial security metadata synchronization fails.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr, model_validator

from .binance_rate_limit import (
    BINANCE_GLOBAL_BAN,
    BINANCE_RATE_LIMITER,
    RATE_LIMIT_MAX_WAIT_SECONDS,
    account_egress_route,
    retry_after_seconds,
)

VERSION = "28.0.0"
Mode = Literal["TESTNET", "LIVE"]
HOSTS: dict[str, str] = {
    "TESTNET": "https://demo-fapi.binance.com",
    "LIVE": "https://fapi.binance.com",
}
SAVE_CONFIRMATIONS = {
    "TESTNET": "TESTNET KASAYA KAYDET",
    "LIVE": "CANLI KASAYA KAYDET",
}
ACTIVATE_CONFIRMATIONS = {
    "TESTNET": "TESTNET BAĞLANTIYI AÇ",
    "LIVE": "CANLI SALT OKUNUR BAĞLANTIYI AÇ",
}
DELETE_CONFIRMATION = "ANAHTARI KALICI SİL"

router = APIRouter(prefix="/api/exchange-connections", tags=["V28 Exchange Connections"])
logger = logging.getLogger(__name__)

# One Render web process is used by the supplied deployment.  Every process
# reloads the encrypted rows at startup; plaintext never enters app.state or a
# response/log payload.
_CACHE: dict[str, tuple[str, str]] = {}
_META: dict[str, dict[str, Any]] = {}
_SESSION_CACHE: dict[tuple[str, str], tuple[str, str]] = {}
_SESSION_META: dict[tuple[str, str], dict[str, Any]] = {}
SERVER_TIME_CACHE_TTL_SECONDS = 10.0
BINANCE_VERIFICATION_REQUEST_TIMEOUT_SECONDS = max(
    1.0,
    min(float(os.getenv("BINANCE_VERIFICATION_REQUEST_TIMEOUT_SECONDS", "15")), 60.0),
)
_SERVER_TIME_CACHE: dict[str, tuple[float, int]] = {}
_SERVER_TIME_REJECTION_CACHE: dict[str, tuple[float, str, int]] = {}
_SERVER_TIME_LOCKS: dict[str, asyncio.Lock] = {}


class VaultError(RuntimeError):
    pass


class BinanceServerTimeRejected(VaultError):
    def __init__(self, message: str, *, retry_after: int, upstream_status: int = 418, exchange_code: int | str | None = None, used_weight_1m: str | None = None):
        super().__init__(message)
        self.retry_after = retry_after
        self.upstream_status = upstream_status
        self.exchange_code = exchange_code
        self.used_weight_1m = used_weight_1m


def _active_global_ban_error() -> BinanceServerTimeRejected | None:
    status = BINANCE_GLOBAL_BAN.public_status()
    if not status["active"]:
        return None
    return BinanceServerTimeRejected(
        str(status["message"]),
        retry_after=int(status["remaining_seconds"]),
        upstream_status=int(status["status_code"] or 429),
        exchange_code=status["exchange_code"],
        used_weight_1m=str(status["used_weight_1m"]) if status["used_weight_1m"] is not None else None,
    )


class SaveCredentialsRequest(BaseModel):
    mode: Mode
    api_key: SecretStr
    secret_key: SecretStr
    confirmation: str = Field(min_length=1, max_length=80)
    totp_code: str | None = Field(default=None, min_length=6, max_length=32, repr=False)


class TestCredentialsRequest(BaseModel):
    mode: Mode
    api_key: SecretStr | None = None
    secret_key: SecretStr | None = None

    @model_validator(mode="after")
    def keys_are_a_pair(self):
        if (self.api_key is None) != (self.secret_key is None):
            raise ValueError("API Key ve Secret Key birlikte girilmelidir.")
        return self


class ConnectionActionRequest(BaseModel):
    mode: Mode
    confirmation: str = Field(min_length=1, max_length=100)
    totp_code: str | None = Field(default=None, min_length=6, max_length=32, repr=False)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_mode(mode: str) -> str:
    value = str(mode or "").strip().upper()
    if value not in HOSTS:
        raise VaultError("Bilinmeyen borsa bağlantı modu.")
    return value


def key_fingerprint(api_key: str) -> str:
    """Return a non-reversible identifier; never reveal key suffixes."""
    if len(api_key.strip()) < 10:
        return ""
    digest = hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest().upper()
    return f"SHA256:{digest[:12]}"


def masked_api_key(api_key: str) -> str:
    value = api_key.strip()
    if len(value) < 10:
        return ""
    return f"{value[:4]}****{value[-4:]}"


def normalize_fingerprint(value: str) -> str:
    return str(value or "").strip().upper().removeprefix("SHA256:")


def account_identity(account: Any, mode: str = "LIVE", fingerprint: str = "") -> str:
    """Return a stable, non-secret identity from a verified exchange account."""
    payload = account if isinstance(account, dict) else {}
    for key in ("uid", "account_id", "accountId", "accountAlias"):
        value = str(payload.get(key) or "").strip()
        if value:
            return f"BINANCE:{normalize_mode(mode)}:{value[:160]}"
    normalized = normalize_fingerprint(fingerprint)
    return f"BINANCE:{normalize_mode(mode)}:FINGERPRINT:{normalized[:64]}" if normalized else ""


def _master_secret() -> str:
    # A dedicated key is optional.  The existing owner access token is already
    # required in the hosted build and lets the user configure the vault fully
    # inside the program without returning to Render.
    return (
        os.getenv("PROTREBOT_VAULT_MASTER_KEY", "").strip()
        or os.getenv("PROTREBOT_WEB_ACCESS_TOKEN", "").strip()
    )


def vault_cipher(master_secret: str | None = None) -> Fernet:
    secret = (master_secret if master_secret is not None else _master_secret()).strip()
    if len(secret) < 24:
        raise VaultError("Sunucu yönetici erişim kodu en az 24 karakter olmalıdır; şifreli kasa hazır değil.")
    digest = hashlib.sha256(b"ProTreBot:ExchangeVault:V28\0" + secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_credentials(api_key: str, secret_key: str, *, mode: str, master_secret: str | None = None) -> bytes:
    payload = {
        "scope": f"BINANCE_USDM_FUTURES_{normalize_mode(mode)}",
        "api_key": api_key.strip(),
        "secret_key": secret_key.strip(),
        "saved_at": now_iso(),
    }
    return vault_cipher(master_secret).encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def decrypt_credentials(ciphertext: bytes, *, mode: str, master_secret: str | None = None) -> tuple[str, str]:
    try:
        payload = json.loads(vault_cipher(master_secret).decrypt(bytes(ciphertext)).decode("utf-8"))
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
        raise VaultError("Şifreli anahtar kaydı açılamadı; kasa anahtarı değişmiş olabilir.") from exc
    expected_scope = f"BINANCE_USDM_FUTURES_{normalize_mode(mode)}"
    if not isinstance(payload, dict) or payload.get("scope") != expected_scope:
        raise VaultError("Şifreli anahtar kaydının kapsamı geçersiz.")
    api_key = str(payload.get("api_key") or "").strip()
    secret_key = str(payload.get("secret_key") or "").strip()
    validate_key_pair(api_key, secret_key)
    return api_key, secret_key


def validate_key_pair(api_key: str, secret_key: str) -> None:
    if len(api_key.strip()) < 10 or len(secret_key.strip()) < 10:
        raise VaultError("API Key veya Secret Key eksik ya da çok kısa.")
    if any(char.isspace() for char in api_key.strip()) or any(char.isspace() for char in secret_key.strip()):
        raise VaultError("API anahtarlarında boşluk veya satır sonu bulunamaz.")


def vault_managed(mode: str) -> bool:
    return bool(_META.get(normalize_mode(mode), {}).get("configured"))


def cached_credentials(mode: str, *, active_only: bool = True) -> tuple[str, str]:
    normalized = normalize_mode(mode)
    meta = _META.get(normalized, {})
    if active_only and (not bool(meta.get("active")) or not withdrawal_permissions_safe(normalized, meta.get("account"))):
        return "", ""
    return _CACHE.get(normalized, ("", ""))


def credential_source(mode: str) -> str:
    normalized = normalize_mode(mode)
    if not vault_managed(normalized):
        return "YAPILANDIRILMADI"
    return "UYGULAMA_KASASI" if _META.get(normalized, {}).get("active") else "UYGULAMA_KASASI_KAPALI"


def clear_vault_cache() -> None:
    _CACHE.clear()
    _META.clear()


async def ensure_schema(pool: Any) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS protrebot_exchange_vault (
          mode TEXT PRIMARY KEY CHECK (mode IN ('TESTNET', 'LIVE')),
          encrypted_payload BYTEA NOT NULL,
          fingerprint TEXT NOT NULL,
          active BOOLEAN NOT NULL DEFAULT FALSE,
          last_test_ok BOOLEAN NOT NULL DEFAULT FALSE,
          last_test_at TIMESTAMPTZ,
          last_error TEXT,
          account_summary JSONB,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS protrebot_exchange_session_vault (
          session_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          mode TEXT NOT NULL CHECK (mode IN ('TESTNET', 'LIVE')),
          encrypted_payload BYTEA NOT NULL,
          fingerprint TEXT NOT NULL,
          active BOOLEAN NOT NULL DEFAULT FALSE,
          last_test_ok BOOLEAN NOT NULL DEFAULT FALSE,
          last_test_at TIMESTAMPTZ,
          last_error TEXT,
          account_summary JSONB,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY (session_id, mode)
        );
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS protrebot_exchange_account_vault (
          user_id TEXT NOT NULL,
          mode TEXT NOT NULL CHECK (mode = 'LIVE'),
          encrypted_payload BYTEA NOT NULL,
          fingerprint TEXT NOT NULL,
          active BOOLEAN NOT NULL DEFAULT FALSE,
          last_test_ok BOOLEAN NOT NULL DEFAULT FALSE,
          last_test_at TIMESTAMPTZ,
          last_error TEXT,
          account_summary JSONB,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY (user_id, mode)
        )
        """
    )
    await pool.execute(
        """
        INSERT INTO protrebot_exchange_account_vault
          (user_id, mode, encrypted_payload, fingerprint, active, last_test_ok, last_test_at,
           last_error, account_summary, created_at, updated_at)
        SELECT DISTINCT ON (user_id)
          user_id, 'LIVE', encrypted_payload, fingerprint, active, last_test_ok, last_test_at,
          last_error, account_summary, created_at, updated_at
        FROM protrebot_exchange_session_vault
        WHERE mode = 'LIVE'
        ORDER BY user_id, updated_at DESC, created_at DESC
        ON CONFLICT (user_id, mode) DO NOTHING
        """
    )
    await pool.execute(
        """
        DELETE FROM protrebot_exchange_session_vault
        WHERE mode = 'LIVE' AND user_id IN (
          SELECT user_id FROM protrebot_exchange_account_vault WHERE mode = 'LIVE'
        )
        """
    )


def session_id(request: Request) -> str:
    headers = getattr(request, "headers", {})
    session_header = str(headers.get("x-protrebot-session") or "").strip()
    authorization = str(headers.get("authorization") or "").strip()
    identity = session_header or authorization
    credential = identity[7:].strip() if identity.lower().startswith("bearer ") else identity
    if not session_header and authorization.casefold() == "bearer":
        identity = ""
    if not identity or credential.casefold().startswith("cookie-session:"):
        from .v22_commercial import request_session_token

        # A public browser marker identifies no session; bind to the signed cookie.
        identity = request_session_token(request)
        if identity.casefold().startswith("cookie-session:"):
            return ""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest() if identity else ""


def _vault_scope(request: Request, mode: str) -> tuple[str, str]:
    normalized = normalize_mode(mode)
    if normalized == "LIVE":
        user = getattr(request.state, "member", None)
        user_id = str((user or {}).get("id") or "")
        if not user_id and bool(getattr(request.state, "web_owner_authenticated", False)):
            user_id = "WEB_OWNER"
        if not user_id:
            raise HTTPException(401, "Oturum gerekli")
        return f"owner:{user_id}", user_id
    sid = session_id(request)
    if not sid:
        raise HTTPException(401, "Oturum gerekli")
    return sid, sid


def _require_live_owner(user: dict[str, Any]) -> None:
    if user.get("role") != "OWNER":
        raise HTTPException(403, "LIVE bağlantısını yalnız OWNER yönetebilir.")


async def _require_live_mfa(request: Request, user: dict[str, Any], code: str | None) -> None:
    _require_live_owner(user)
    from . import account_settings

    if not await account_settings.enabled(request, user):
        raise HTTPException(403, "LIVE yönetimi için önce 2FA'yı etkinleştirin.")
    if not code:
        raise HTTPException(401, "LIVE yönetimi için güncel 2FA kodu gerekli.")
    await account_settings.require_totp(request, user, code, required=True)


def _require_member(request: Request, *, owner: bool = False) -> dict[str, Any]:
    user = getattr(request.state, "member", None)
    if not user and bool(getattr(request.state, "web_owner_authenticated", False)):
        user = {"id": "WEB_OWNER", "role": "OWNER"}
    if not user:
        raise HTTPException(401, "Oturum gerekli")
    if not session_id(request):
        raise HTTPException(401, "Oturum gerekli")
    if owner and user.get("role") != "OWNER":
        raise HTTPException(403, "Yönetici yetkisi gerekli")
    return user


def session_credentials(request: Request, mode: str, *, active_only: bool = True) -> tuple[str, str]:
    if normalize_mode(mode) == "TESTNET" and not session_id(request):
        return "", ""
    scope, _ = _vault_scope(request, mode)
    key = (scope, normalize_mode(mode))
    meta = _SESSION_META.get(key, {})
    if active_only and (not meta.get("active") or not withdrawal_permissions_safe(key[1], meta.get("account"))):
        return "", ""
    return _SESSION_CACHE.get(key, ("", ""))


def session_credentials_for_request(request: Request, mode: str, *, active_only: bool = True) -> tuple[str, str]:
    return session_credentials(request, mode, active_only=active_only)


def session_account_identity(request: Request, mode: str, fingerprint: str = "") -> str:
    scope, _ = _vault_scope(request, mode)
    meta = _SESSION_META.get((scope, normalize_mode(mode)), {})
    return account_identity(meta.get("account"), mode, fingerprint or str(meta.get("fingerprint") or ""))


async def authoritative_user_version_active(application: Any, user_id: str, auth_version: int | None) -> bool:
    """Background tasks cannot use middleware's worker-local USER proof."""
    pool = getattr(application.state, "db_pool", None)
    if not user_id or type(auth_version) is not int or auth_version < 1 or pool is None:
        return False
    try:
        from .v22_commercial import authoritative_user_version_active as validate_background_user

        return await validate_background_user(application, user_id, auth_version)
    except Exception:
        logger.warning("Background LIVE shared session authority unavailable")
        return False


async def authoritative_live_owner_active(application: Any, user_id: str) -> bool:
    """Validate the account behind an already MFA-authorized server-side LIVE grant."""
    pool = getattr(application.state, "db_pool", None)
    if not user_id or pool is None:
        return False
    try:
        row = await pool.fetchrow(
            "SELECT security FROM commercial_auth_users WHERE user_id = $1",
            user_id,
        )
        if row is None:
            return False
        security = row["security"]
        security = json.loads(security) if isinstance(security, str) else security
        return isinstance(security, dict) and security.get("active") is True and security.get("role") == "OWNER"
    except Exception:
        logger.warning("Background LIVE account authority unavailable")
        return False


async def session_credentials_for_identity(
    application: Any,
    session_value: str,
    user_id: str,
    mode: str,
    fingerprint: str,
    *,
    force_refresh: bool = False,
    auth_version: int | None = None,
    auth_expires_at_epoch: float | None = None,
) -> tuple[str, str]:
    """Resolve an active encrypted account-scoped LIVE vault row."""
    normalized = normalize_mode(mode)
    if not user_id or not fingerprint:
        return "", ""
    if normalized == "LIVE" and not await authoritative_live_owner_active(application, user_id):
        clear_session_vault_for_user_cache(application, user_id)
        return "", ""
    scope = f"owner:{user_id}" if normalized == "LIVE" else session_value
    if not scope:
        return "", ""
    key = (scope, normalized)
    meta = _SESSION_META.get(key, {})
    cached = _SESSION_CACHE.get(key, ("", ""))
    if not force_refresh and (
        meta.get("active")
        and withdrawal_permissions_safe(normalized, meta.get("account"))
        and str(meta.get("user_id") or "") == user_id
        and normalize_fingerprint(key_fingerprint(cached[0])) == normalize_fingerprint(fingerprint)
        and cached[1]
    ):
        return cached

    pool = getattr(application.state, "db_pool", None)
    if pool is None or not await ensure_exchange_vault(application):
        logger.warning("LIVE credential resolution failed: pool_unavailable")
        return "", ""
    await ensure_schema(pool)
    if normalized == "LIVE":
        row = await pool.fetchrow(
            """
            SELECT 'owner:' || user_id AS session_id, user_id, mode, encrypted_payload,
                   fingerprint, active, account_summary, last_test_ok, last_test_at,
                   last_error, updated_at
            FROM protrebot_exchange_account_vault
            WHERE user_id = $1 AND mode = 'LIVE' AND active = TRUE
            """,
            user_id,
        )
    else:
        row = await pool.fetchrow(
            """
            SELECT session_id, user_id, mode, encrypted_payload, fingerprint, active, account_summary
            FROM protrebot_exchange_session_vault
            WHERE session_id = $1 AND user_id = $2 AND mode = $3 AND active = TRUE
            """,
            session_value, user_id, normalized,
        )
    if not row and normalized != "LIVE":
        rows = await pool.fetch(
            """
            SELECT session_id, user_id, mode, encrypted_payload, fingerprint, active, account_summary
            FROM protrebot_exchange_session_vault
            WHERE user_id = $1 AND mode = $2 AND active = TRUE AND fingerprint = $3
            ORDER BY updated_at DESC
            LIMIT 2
            """,
            user_id, normalized, f"SHA256:{normalize_fingerprint(fingerprint)}",
        )
        if len(rows) == 1:
            row = rows[0]
    if not row or normalize_fingerprint(str(row.get("fingerprint") or "")) != normalize_fingerprint(fingerprint):
        logger.warning(
            "LIVE credential resolution failed: %s expected=%s stored=%s",
            "identity_miss" if not row else "fingerprint_mismatch",
            fingerprint,
            str(row.get("fingerprint") or "") if row else "",
        )
        return "", ""
    if not withdrawal_permissions_safe(normalized, _row_meta(row).get("account")):
        logger.warning("LIVE credential resolution failed: withdrawal_permissions_unverified")
        return "", ""
    try:
        credentials = decrypt_credentials(bytes(row["encrypted_payload"]), mode=normalized)
    except (KeyError, TypeError, ValueError, VaultError):
        logger.warning("LIVE credential resolution failed: decrypt_failed")
        return "", ""
    if key_fingerprint(credentials[0]) != fingerprint:
        return "", ""
    return credentials


def _session_connection(mode: str, key: tuple[str, str]) -> dict[str, Any]:
    meta = _SESSION_META.get(key, {})
    api_key = _SESSION_CACHE.get(key, ("", ""))[0]
    return {
        "mode": mode,
        "label": "Binance Futures Demo" if mode == "TESTNET" else "Binance USD-M Futures Gerçek",
        "host": HOSTS[mode],
        "configured": bool(meta.get("configured")),
        "active": bool(meta.get("active")),
        "fingerprint": meta.get("fingerprint"),
        "api_key_masked": masked_api_key(api_key),
        "last_test_ok": bool(meta.get("last_test_ok")),
        "last_test_at": meta.get("last_test_at"),
        "last_error": meta.get("last_error"),
        "account": meta.get("account"),
        "storage": ("HESAP_KASASI" if mode == "LIVE" else "OTURUM_KASASI") if meta.get("configured") else "YAPILANDIRILMADI",
        "secrets_returned": False,
    }


def session_public_status(application: Any, request: Request) -> dict[str, Any]:
    state = application.state.exchange_vault
    return {
        "version": VERSION,
        "vault": {"ready": bool(state.get("ready")), "storage": state.get("storage"), "reason": state.get("reason"), "loaded_at": state.get("loaded_at")},
        "connections": {
            mode: _session_connection(mode, (_vault_scope(request, mode)[0], mode))
            for mode in HOSTS
        },
        "binance_ban": BINANCE_GLOBAL_BAN.public_status(),
        "safety": {"https_required": True, "secrets_returned_to_browser": False, "connection_test_creates_orders": False, "activation_arms_orders": False, "live_orders_require_v25_gates": True, "withdrawals_supported": False},
    }


async def ensure_session_cache(request: Request) -> None:
    pool = getattr(request.app.state, "db_pool", None)
    sid = session_id(request)
    user = getattr(request.state, "member", None)
    if pool is None or not sid or not user:
        return
    await ensure_schema(pool)
    rows = await pool.fetch(
        "SELECT session_id, user_id, mode, encrypted_payload, fingerprint, active, last_test_ok, last_test_at, last_error, account_summary, updated_at FROM protrebot_exchange_session_vault WHERE session_id = $1 AND user_id = $2",
        sid, user["id"],
    )
    for row in rows:
        mode = normalize_mode(str(row["mode"]))
        if mode == "LIVE":
            continue
        try:
            _SESSION_CACHE[(sid, mode)] = decrypt_credentials(bytes(row["encrypted_payload"]), mode=mode)
            _SESSION_META[(sid, mode)] = _row_meta(row)
        except VaultError as exc:
            _SESSION_META[(sid, mode)] = {**_row_meta(row), "active": False, "last_test_ok": False, "last_error": str(exc)}
    live_row = await pool.fetchrow(
        """
        SELECT 'owner:' || user_id AS session_id, user_id, mode, encrypted_payload,
               fingerprint, active, last_test_ok, last_test_at, last_error,
               account_summary, updated_at
        FROM protrebot_exchange_account_vault
        WHERE user_id = $1 AND mode = 'LIVE'
        """,
        user["id"],
    )
    if live_row:
        key = (f"owner:{user['id']}", "LIVE")
        try:
            _SESSION_CACHE[key] = decrypt_credentials(bytes(live_row["encrypted_payload"]), mode="LIVE")
            _SESSION_META[key] = _row_meta(live_row)
        except VaultError as exc:
            _SESSION_META[key] = {**_row_meta(live_row), "active": False, "last_test_ok": False, "last_error": str(exc)}


def _row_meta(row: Any) -> dict[str, Any]:
    summary = row.get("account_summary") if hasattr(row, "get") else None
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except json.JSONDecodeError:
            summary = None
    tested_at = row.get("last_test_at") if hasattr(row, "get") else None
    updated_at = row.get("updated_at") if hasattr(row, "get") else None
    permission_safe = withdrawal_permissions_safe(str(row.get("mode") or "LIVE"), summary)
    return {
        "user_id": str(row.get("user_id") or "").strip(),
        "configured": True,
        "active": bool(row.get("active")) and permission_safe,
        "fingerprint": str(row.get("fingerprint") or ""),
        "last_test_ok": bool(row.get("last_test_ok")),
        "last_test_at": tested_at.isoformat() if hasattr(tested_at, "isoformat") else tested_at,
        "last_error": (str(row.get("last_error") or "")[:240] or None) if permission_safe else "Para çekme izni doğrulanmadı; bağlantıyı yeniden aktifleştirin.",
        "account": summary if isinstance(summary, dict) else None,
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
    }


async def ensure_exchange_vault(application: Any, *, force: bool = False) -> bool:
    state = getattr(application.state, "exchange_vault", None)
    if state is None:
        state = {
            "ready": False,
            "storage": "POSTGRESQL + FERNET",
            "reason": "Kasa başlatılıyor.",
            "pool_id": None,
            "loaded_at": None,
        }
        application.state.exchange_vault = state
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        clear_vault_cache()
        state.update({"ready": False, "reason": "PostgreSQL bağlantısı bekleniyor.", "pool_id": None})
        return False
    if state.get("ready") and state.get("pool_id") == id(pool) and not force:
        return True
    try:
        vault_cipher()
        await ensure_schema(pool)
        rows = await pool.fetch(
            "SELECT mode, encrypted_payload, fingerprint, active, last_test_ok, last_test_at, last_error, account_summary, updated_at FROM protrebot_exchange_vault"
        )
        clear_vault_cache()
        for row in rows:
            mode = normalize_mode(str(row["mode"]))
            try:
                _CACHE[mode] = decrypt_credentials(bytes(row["encrypted_payload"]), mode=mode)
                _META[mode] = _row_meta(row)
            except VaultError as exc:
                _META[mode] = {**_row_meta(row), "active": False, "last_test_ok": False, "last_error": str(exc)}
        state.update({"ready": True, "reason": None, "pool_id": id(pool), "loaded_at": now_iso()})
        return True
    except Exception as exc:
        clear_vault_cache()
        state.update({"ready": False, "reason": str(exc)[:240], "pool_id": id(pool)})
        return False


async def init_exchange_connections(application: Any) -> None:
    application.state.exchange_vault = {
        "ready": False,
        "storage": "POSTGRESQL + FERNET",
        "reason": "Kasa başlatılıyor.",
        "pool_id": None,
        "loaded_at": None,
    }
    await ensure_exchange_vault(application, force=True)


def _signed_query(secret_key: str, params: dict[str, Any]) -> str:
    query = urlencode(params)
    signature = hmac.new(secret_key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}"


def _safe_exchange_message(payload: Any, api_key: str) -> str:
    code = payload.get("code") if isinstance(payload, dict) else None
    message = exchange_rejection_message(code)
    if code == -2015 and isinstance(payload.get("msg"), str):
        match = re.search(r"\brequest ip:\s*([0-9a-fA-F:.]+)", payload["msg"], re.IGNORECASE)
        if match:
            try:
                address = ipaddress.ip_address(match.group(1).rstrip("."))
            except ValueError:
                address = None
            if address is not None and address.is_global:
                message += f" IP kısıtlaması varsa Binance'in bildirdiği şu çıkış IP'sini ekleyin: {address}."
    return message


def exchange_rejection_message(code: Any) -> str:
    messages = {
        -2014: "Binance API anahtarı geçersiz; API Key ve Secret Key bilgilerini kontrol edin.",
        -2015: "Binance API anahtarı, IP izni veya Futures yetkisi doğrulanamadı. Anahtar ve izinleri kontrol edin.",
        -1022: "Binance imza doğrulaması başarısız; API Key ve Secret Key eşleşmesini kontrol edin.",
        -1021: "Binance saat doğrulaması başarısız; sunucu saati eşitlendikten sonra tekrar deneyin.",
        -2019: "Binance kullanılabilir bakiyesi veya teminatı yetersiz; emir gönderilmedi.",
        -2022: "Binance reduce-only emrini reddetti; pozisyon miktarını ve açık emirleri kontrol edin.",
        -1111: "Binance miktar veya fiyat hassasiyetini reddetti; borsa adım kuralları kontrol edilmeli.",
        -1013: "Binance minimum miktar, tutar veya fiyat kuralı karşılanmadı.",
        -2011: "Binance emir kimliğini doğrulayamadı; emir ve pozisyon durumu uzlaştırılmalı.",
        -2013: "Binance emir kaydını bulamadı; emir ve pozisyon durumu uzlaştırılmalı.",
    }
    return messages.get(code if isinstance(code, int) else None,
                        "Binance isteği reddetti; bağlantı, yetki ve borsa kurallarını kontrol edin.")


async def _signed_get(http: httpx.AsyncClient, host: str, path: str, api_key: str, secret_key: str, time_offset_ms: int) -> Any:
    active_ban = _active_global_ban_error()
    if active_ban is not None:
        raise active_ban
    try:
        async def request() -> httpx.Response:
            async with BINANCE_RATE_LIMITER.slot(
                host,
                source="connection_signed",
                egress_route=account_egress_route(),
            ) as rate_limit:
                timestamp = int(time.time() * 1000) + time_offset_ms
                query = _signed_query(secret_key, {"timestamp": timestamp, "recvWindow": 5000})
                response = await http.get(f"{host}{path}?{query}", headers={"X-MBX-APIKEY": api_key})
                rate_limit.observe(response)
                return response

        response = await asyncio.wait_for(request(), timeout=BINANCE_VERIFICATION_REQUEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise VaultError("Binance bağlantı isteği zaman aşımına uğradı.") from exc
    except httpx.ProxyError as exc:
        logger.warning("Binance signed verification proxy failed: path=%s error_type=%s", path, type(exc).__name__)
        raise VaultError("Binance doğrulaması için sabit IP proxy bağlantısı kurulamadı. Sunucunun proxy erişimini ve ayarını kontrol edin.") from exc
    except httpx.TimeoutException as exc:
        raise VaultError("Binance bağlantı isteği zaman aşımına uğradı.") from exc
    except httpx.RequestError as exc:
        raise VaultError("Binance sunucusuna ulaşılamadı.") from exc
    if response.status_code >= 400:
        if response.status_code in {418, 429}:
            raise _server_time_error(response)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        code = payload.get("code") if isinstance(payload, dict) else None
        if not isinstance(code, int):
            if response.status_code == 451:
                raise VaultError("Binance sunucunun bulunduğu bölgeden erişime izin vermiyor (HTTP 451).")
            if response.status_code >= 500:
                raise VaultError(f"Binance doğrulama servisi geçici sunucu hatası döndürdü (HTTP {response.status_code}); tekrar deneyin.")
            raise VaultError(f"Binance doğrulama erişimini reddetti (HTTP {response.status_code}); key, IP izinleri ve Futures yetkisini kontrol edin.")
        raise VaultError(_safe_exchange_message(payload, api_key))
    try:
        return response.json()
    except ValueError as exc:
        raise VaultError("Binance beklenmeyen bir yanıt döndürdü.") from exc


def _server_time_error(response: httpx.Response) -> VaultError:
    if response.status_code in {418, 429}:
        active_ban = _active_global_ban_error()
        if active_ban is not None:
            return active_ban
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None
        exchange_code = payload.get("code") if isinstance(payload, dict) else None
        retry_after = retry_after_seconds(response)
        retry_seconds = retry_after or int(SERVER_TIME_CACHE_TTL_SECONDS)
        retry_message = f"Please wait {retry_seconds} seconds before retrying." if retry_after else "Please wait briefly before retrying."
        return BinanceServerTimeRejected(
            f"Binance server-time temporarily rejected the request (HTTP {response.status_code}). "
            + retry_message,
            retry_after=retry_seconds,
            upstream_status=response.status_code,
            exchange_code=exchange_code,
            used_weight_1m=response.headers.get("X-MBX-USED-WEIGHT-1M"),
        )
    return VaultError(f"Binance saat servisi HTTP {response.status_code} döndürdü.")


async def _server_time_offset(
    http: httpx.AsyncClient,
    mode: str,
    *,
    egress_route: str | None = None,
) -> int:
    normalized = normalize_mode(mode)
    active_ban = _active_global_ban_error()
    if active_ban is not None:
        raise active_ban
    now = time.monotonic()
    cached = _SERVER_TIME_CACHE.get(normalized)
    if cached and cached[0] > now:
        return cached[1]
    rejected = _SERVER_TIME_REJECTION_CACHE.get(normalized)
    if rejected and rejected[0] > now:
        raise BinanceServerTimeRejected(rejected[1], retry_after=rejected[2])

    lock = _SERVER_TIME_LOCKS.setdefault(normalized, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _SERVER_TIME_CACHE.get(normalized)
        if cached and cached[0] > now:
            return cached[1]
        rejected = _SERVER_TIME_REJECTION_CACHE.get(normalized)
        if rejected and rejected[0] > now:
            raise BinanceServerTimeRejected(rejected[1], retry_after=rejected[2])
        host = HOSTS[normalized]
        try:
            async def request() -> tuple[httpx.Response, int, int]:
                async with BINANCE_RATE_LIMITER.slot(
                    host,
                    source="connection_server_time",
                    egress_route=egress_route or account_egress_route(),
                ) as rate_limit:
                    before = int(time.time() * 1000)
                    response = await http.get(f"{host}/fapi/v1/time")
                    after = int(time.time() * 1000)
                    rate_limit.observe(response)
                    return response, before, after

            response, before, after = await asyncio.wait_for(request(), timeout=BINANCE_VERIFICATION_REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
        except asyncio.TimeoutError as exc:
            raise VaultError("Binance saat servisine ulaşırken zaman aşımı oluştu.") from exc
        except httpx.TimeoutException as exc:
            raise VaultError("Binance saat servisine ulaşırken zaman aşımı oluştu.") from exc
        except httpx.ProxyError as exc:
            logger.warning("Binance server-time proxy failed: mode=%s error_type=%s", normalized, type(exc).__name__)
            raise VaultError("Binance saat servisine proxy bağlantısı kurulamadı. Sunucunun sabit IP proxy ayarını kontrol edin.") from exc
        except httpx.ConnectError as exc:
            raise VaultError("Binance saat servisine bağlantı veya DNS kurulamadı.") from exc
        except httpx.NetworkError as exc:
            raise VaultError("Binance saat servisine ağ bağlantısı kurulamadı.") from exc
        except httpx.HTTPStatusError as exc:
            error = _server_time_error(exc.response)
            if isinstance(error, BinanceServerTimeRejected):
                logger.warning(
                    "Binance server-time rejected request: mode=%s status=%s code=%s retry_after_seconds=%s used_weight_1m=%s",
                    normalized,
                    error.upstream_status,
                    error.exchange_code,
                    error.retry_after,
                    error.used_weight_1m,
                )
                _SERVER_TIME_REJECTION_CACHE[normalized] = (
                    time.monotonic() + error.retry_after,
                    str(error),
                    error.retry_after,
                )
            raise error from exc
        except httpx.HTTPError as exc:
            logger.warning("Binance server-time transport failed: mode=%s error_type=%s", normalized, type(exc).__name__)
            raise VaultError("Binance saat servisi HTTP isteği başarısız oldu.") from exc

        try:
            time_payload = response.json()
        except ValueError as exc:
            raise VaultError("Binance saat servisi geçerli JSON döndürmedi.") from exc
        if not isinstance(time_payload, dict) or "serverTime" not in time_payload:
            raise VaultError("Binance saat servisi yanıtında serverTime alanı bulunamadı.")
        try:
            server_time = int(time_payload["serverTime"])
        except (TypeError, ValueError) as exc:
            raise VaultError("Binance saat servisi serverTime alanını geçerli bir sayıya dönüştüremedi.") from exc
        offset = server_time - ((before + after) // 2)
        _SERVER_TIME_CACHE[normalized] = (time.monotonic() + SERVER_TIME_CACHE_TTL_SECONDS, offset)
        return offset


def withdrawal_permissions_safe(mode: str, account: Any) -> bool:
    if normalize_mode(mode) == "TESTNET":
        return True
    permissions = account.get("api_permissions") if isinstance(account, dict) else None
    return bool(
        isinstance(permissions, dict)
        and permissions.get("withdrawal_check") == "VERIFIED"
        and permissions.get("enableWithdrawals") is False
    )


async def test_binance_credentials(http: httpx.AsyncClient, mode: str, api_key: str, secret_key: str, *, public_http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    normalized = normalize_mode(mode)
    validate_key_pair(api_key, secret_key)
    host = HOSTS[normalized]
    started = time.monotonic()
    logger.info("LIVE_CONNECTION_TEST TEST_START mode=%s", normalized)
    logger.info("LIVE_CONNECTION_TEST CREDENTIALS_RESOLVED mode=%s elapsed_ms=%d", normalized, round((time.monotonic() - started) * 1000))
    logger.info("LIVE_CONNECTION_TEST SERVER_TIME_START mode=%s elapsed_ms=%d", normalized, round((time.monotonic() - started) * 1000))
    offset = await _server_time_offset(
        public_http if public_http is not None else http,
        normalized,
        egress_route="render_direct" if public_http is not None else account_egress_route(),
    )
    logger.info("LIVE_CONNECTION_TEST SERVER_TIME_DONE mode=%s elapsed_ms=%d", normalized, round((time.monotonic() - started) * 1000))
    if normalized == "LIVE":
        # API restrictions are a signed Spot SAPI endpoint, not a Futures endpoint.
        restrictions = await _signed_get(
            http, "https://api.binance.com", "/sapi/v1/account/apiRestrictions",
            api_key, secret_key, offset,
        )
        if not isinstance(restrictions, dict) or type(restrictions.get("enableWithdrawals")) is not bool:
            raise VaultError("Binance para çekme izni doğrulanamadı; canlı anahtar reddedildi.")
        if restrictions["enableWithdrawals"]:
            raise VaultError("Para çekme izni açık Binance anahtarı kabul edilmez.")
        if restrictions.get("enableFutures") is False:
            raise VaultError("Binance Futures yetkisi kapalı; API anahtarında Futures işlem iznini açın.")
        api_permissions = {"withdrawal_check": "VERIFIED", "enableWithdrawals": False}
    else:
        # Futures Testnet has no withdrawals and does not implement the SAPI check.
        api_permissions = {"withdrawal_check": "NOT_APPLICABLE_TESTNET", "enableWithdrawals": None}
    logger.info("LIVE_CONNECTION_TEST ACCOUNT_VERIFY_START mode=%s elapsed_ms=%d", normalized, round((time.monotonic() - started) * 1000))
    account = await _signed_get(http, host, "/fapi/v3/account", api_key, secret_key, offset)
    position_mode = await _signed_get(http, host, "/fapi/v1/positionSide/dual", api_key, secret_key, offset)
    logger.info("LIVE_CONNECTION_TEST ACCOUNT_VERIFY_DONE mode=%s elapsed_ms=%d", normalized, round((time.monotonic() - started) * 1000))
    if not isinstance(account, dict):
        raise VaultError("Binance hesap yanıtı geçersiz.")
    if normalized == "LIVE" and account.get("canTrade") is False:
        raise VaultError("Binance hesap veya API anahtarı işlem yetkisine sahip değil; Futures izinlerini kontrol edin.")
    positions = account.get("positions") if isinstance(account.get("positions"), list) else []
    active_positions = sum(
        1 for row in positions
        if isinstance(row, dict) and abs(float(row.get("positionAmt") or 0)) > 0
    )
    result = {
        "mode": normalized,
        "host": host,
        "account_identity": str(account.get("uid") or account.get("accountId") or account.get("accountAlias") or "")[:160],
        "wallet_balance": float(account.get("totalWalletBalance") or 0),
        "available_balance": float(account.get("availableBalance") or 0),
        "unrealized_pnl": float(account.get("totalUnrealizedProfit") or 0),
        "active_positions": active_positions,
        "hedge_mode": bool(position_mode.get("dualSidePosition")) if isinstance(position_mode, dict) else None,
        "clock_offset_ms": offset,
        "tested_at": now_iso(),
        "orders_created": False,
        "api_permissions": api_permissions,
    }
    logger.info("LIVE_CONNECTION_TEST TEST_RESPONSE mode=%s elapsed_ms=%d", normalized, round((time.monotonic() - started) * 1000))
    return result


async def _verify_request_credentials(request: Request, mode: str, api_key: str, secret_key: str) -> dict[str, Any]:
    from .main import ensure_market_data_http_client
    public_http = await ensure_market_data_http_client(request.app)
    return await test_binance_credentials(request.app.state.http, mode, api_key, secret_key, public_http=public_http)


def _exchange_test_http_exception(exc: VaultError) -> HTTPException:
    if isinstance(exc, BinanceServerTimeRejected):
        wait = exc.retry_after
        return HTTPException(
            429,
            {
                "detail": f"Binance bağlantısı geçici olarak hız sınırına ulaştı. Lütfen {wait} saniye bekleyip tekrar deneyin.",
                "code": "BINANCE_RATE_LIMITED",
                "upstream_status": exc.upstream_status,
                "exchange_code": exc.exchange_code,
                "retry_after_seconds": wait,
                "used_weight_1m": exc.used_weight_1m,
            },
            headers={"Retry-After": str(wait)},
        )
    return HTTPException(502, str(exc))


def _rate_limit_public_message(exc: BinanceServerTimeRejected) -> str:
    return f"Binance bağlantısı geçici olarak hız sınırına ulaştı. Lütfen {exc.retry_after} saniye bekleyip tekrar deneyin."


def _public_connection(mode: str) -> dict[str, Any]:
    meta = _META.get(mode, {})
    return {
        "mode": mode,
        "label": "Binance Futures Demo" if mode == "TESTNET" else "Binance USD-M Futures Gerçek",
        "host": HOSTS[mode],
        "configured": bool(meta.get("configured")),
        "active": bool(meta.get("active")),
        "fingerprint": meta.get("fingerprint"),
        "last_test_ok": bool(meta.get("last_test_ok")),
        "last_test_at": meta.get("last_test_at"),
        "last_error": meta.get("last_error"),
        "account": meta.get("account"),
        "storage": credential_source(mode) if meta.get("configured") else "YAPILANDIRILMADI",
        "secrets_returned": False,
    }


def public_status(application: Any) -> dict[str, Any]:
    state = application.state.exchange_vault
    return {
        "version": VERSION,
        "vault": {
            "ready": bool(state.get("ready")),
            "storage": state.get("storage"),
            "reason": state.get("reason"),
            "loaded_at": state.get("loaded_at"),
        },
        "connections": {mode: _public_connection(mode) for mode in HOSTS},
        "binance_ban": BINANCE_GLOBAL_BAN.public_status(),
        "safety": {
            "https_required": True,
            "secrets_returned_to_browser": False,
            "connection_test_creates_orders": False,
            "activation_arms_orders": False,
            "live_orders_require_v25_gates": True,
            "withdrawals_supported": False,
        },
    }


def _lock_live_controller(state: dict[str, Any]) -> None:
    from . import original_v2_live
    if original_v2_live.running(state):
        original_v2_live.pause(state, "LIVE API anahtarı değiştirildi veya bağlantı kapatıldı")
    state["armed_until"] = 0.0
    state["connected"] = False
    state["auto"]["enabled"] = False
    state["auto"]["session_until"] = 0.0
    state["real_trading_locked"] = True
    state["live_auto_trade"] = False
    state["auto_authorization"] = {"session_id": "", "user_id": "", "fingerprint": "", "expires_at_epoch": 0.0}
    state["web_consent"] = {"accepted_at": None, "expires_at_epoch": 0.0, "key_fingerprint": None}


def clear_session_vault_for_user_cache(
    application: Any, user_id: str, session_ids: Iterable[str] = (),
) -> None:
    """Offline account-erasure hook; session_ids are already-hashed vault IDs."""
    user_id = str(user_id or "")
    if not user_id:
        return
    known_sessions = {str(value) for value in session_ids if value}
    for key in set(_SESSION_CACHE) | set(_SESSION_META):
        owner = str(_SESSION_META.get(key, {}).get("user_id") or "")
        if owner == user_id or (key[0] in known_sessions and not owner):
            _SESSION_CACHE.pop(key, None)
            _SESSION_META.pop(key, None)
    state = getattr(application.state, "v25_execution", None)
    if not isinstance(state, dict):
        return
    authorization = state.get("live_session_authorization") or {}
    owner = str(authorization.get("user_id") or "")
    if owner == user_id or (
        not owner and any(
            str((state.get(key) or {}).get("user_id") or "") == user_id
            for key in ("auto_authorization", "web_consent")
        )
    ):
        _lock_live_controller(state)


def _lock_runtime(application: Any, mode: str, request: Request) -> None:
    if mode == "TESTNET" and hasattr(application.state, "binance_demo"):
        state = application.state.binance_demo
        state["armed_until"] = 0
        state["connected"] = False
    if mode == "LIVE" and hasattr(application.state, "v25_execution"):
        state = application.state.v25_execution
        from .v25_execution import current_user_id

        authorization = state.get("live_session_authorization") or {}
        if authorization.get("user_id") != current_user_id(request):
            return
        _lock_live_controller(state)


async def _require_ready(application: Any) -> Any:
    if not await ensure_exchange_vault(application):
        reason = application.state.exchange_vault.get("reason") or "Şifreli kasa hazır değil."
        raise HTTPException(503, reason)
    return application.state.db_pool


def _require_owner_member(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "member", None)
    if not user:
        raise HTTPException(401, "Oturum gerekli")
    if user.get("role") != "OWNER":
        raise HTTPException(403, "Yönetici yetkisi gerekli")
    return user


@router.get("/status")
async def exchange_connection_status(request: Request) -> dict[str, Any]:
    _require_member(request)
    await ensure_exchange_vault(request.app)
    await ensure_session_cache(request)
    return session_public_status(request.app, request)


@router.get("/time-status")
async def exchange_server_time_status(request: Request, mode: Mode = "TESTNET") -> dict[str, Any]:
    _require_owner_member(request)
    from .main import ensure_market_data_http_client
    try:
        offset = await _server_time_offset(await ensure_market_data_http_client(request.app), mode)
    except VaultError as exc:
        error = _exchange_test_http_exception(exc)
        detail = error.detail if isinstance(error.detail, dict) else {"detail": error.detail}
        error.detail = {
            **detail,
            "mode": mode,
            "error_type": type(exc.__cause__).__name__ if exc.__cause__ is not None else type(exc).__name__,
            "proxy_configured": bool(os.getenv("QUOTAGUARD_URL", "").strip()),
        }
        raise error from exc
    return {
        "ok": True,
        "mode": mode,
        "offset_ms": offset,
        "proxy_configured": bool(os.getenv("QUOTAGUARD_URL", "").strip()),
    }


@router.post("/test")
async def exchange_connection_test(request: Request, body: TestCredentialsRequest) -> dict[str, Any]:
    _require_member(request)
    await _require_ready(request.app)
    await ensure_session_cache(request)
    mode = normalize_mode(body.mode)
    if body.api_key is not None and body.secret_key is not None:
        api_key = body.api_key.get_secret_value().strip()
        secret_key = body.secret_key.get_secret_value().strip()
    else:
        api_key, secret_key = session_credentials(request, mode, active_only=False)
    if not api_key or not secret_key:
        raise HTTPException(412, "Önce API Key ve Secret Key girin veya kasaya kaydedin.")
    try:
        account = await _verify_request_credentials(request, mode, api_key, secret_key)
    except VaultError as exc:
        raise _exchange_test_http_exception(exc) from exc
    return {
        "ok": True,
        "message": "Bağlantı ve imza doğrulandı; hiçbir emir oluşturulmadı.",
        "fingerprint": key_fingerprint(api_key),
        "account": account,
    }


@router.post("/save")
async def exchange_connection_save(request: Request, body: SaveCredentialsRequest) -> dict[str, Any]:
    user = _require_member(request)
    pool = await _require_ready(request.app)
    await ensure_schema(pool)
    mode = normalize_mode(body.mode)
    if mode == "LIVE":
        _require_live_owner(user)
    if body.confirmation.strip().upper() != SAVE_CONFIRMATIONS[mode]:
        raise HTTPException(422, "Güvenli kaydetme onayı eksik.")
    api_key = body.api_key.get_secret_value().strip()
    secret_key = body.secret_key.get_secret_value().strip()
    try:
        validate_key_pair(api_key, secret_key)
        account = await _verify_request_credentials(request, mode, api_key, secret_key)
        encrypted = encrypt_credentials(api_key, secret_key, mode=mode)
    except VaultError as exc:
        if isinstance(exc, BinanceServerTimeRejected):
            raise _exchange_test_http_exception(exc) from exc
        raise HTTPException(422 if "kısa" in str(exc) or "boşluk" in str(exc) else 502, str(exc)) from exc
    fingerprint = key_fingerprint(api_key)
    scope, owner_id = _vault_scope(request, mode)
    if mode == "LIVE":
        await pool.execute(
            """
            INSERT INTO protrebot_exchange_account_vault
              (user_id, mode, encrypted_payload, fingerprint, active, last_test_ok,
               last_test_at, last_error, account_summary, updated_at)
            VALUES ($1, 'LIVE', $2, $3, TRUE, TRUE, NOW(), NULL, $4::jsonb, NOW())
            ON CONFLICT (user_id, mode) DO UPDATE SET
              encrypted_payload = EXCLUDED.encrypted_payload,
              fingerprint = EXCLUDED.fingerprint,
              active = TRUE,
              last_test_ok = TRUE,
              last_test_at = NOW(),
              last_error = NULL,
              account_summary = EXCLUDED.account_summary,
              updated_at = NOW()
            """,
            owner_id, encrypted, fingerprint, json.dumps(account, ensure_ascii=False),
        )
    else:
        await pool.execute(
            """
            INSERT INTO protrebot_exchange_session_vault
              (session_id, user_id, mode, encrypted_payload, fingerprint, active, last_test_ok, last_test_at, last_error, account_summary, updated_at)
            VALUES ($1, $2, $3, $4, $5, TRUE, TRUE, NOW(), NULL, $6::jsonb, NOW())
            ON CONFLICT (session_id, mode) DO UPDATE SET
              encrypted_payload = EXCLUDED.encrypted_payload,
              fingerprint = EXCLUDED.fingerprint,
              active = TRUE,
              last_test_ok = TRUE,
              last_test_at = NOW(),
              last_error = NULL,
              account_summary = EXCLUDED.account_summary,
              updated_at = NOW()
            """,
            scope, user["id"], mode, encrypted, fingerprint, json.dumps(account, ensure_ascii=False),
        )
    _SESSION_CACHE[(scope, mode)] = (api_key, secret_key)
    _SESSION_META[(scope, mode)] = {
        "user_id": str(user["id"]),
        "configured": True,
        "active": True,
        "fingerprint": fingerprint,
        "last_test_ok": True,
        "last_test_at": account["tested_at"],
        "last_error": None,
        "account": account,
        "updated_at": now_iso(),
    }
    _lock_runtime(request.app, mode, request)
    return {**session_public_status(request.app, request), "message": "Canlı anahtar OWNER hesabının şifreli kasasına kaydedildi." if mode == "LIVE" else "Anahtarlar bu oturum için güvenli kasaya kaydedildi ve otomasyon için hazır."}


@router.post("/activate")
async def exchange_connection_activate(request: Request, body: ConnectionActionRequest) -> dict[str, Any]:
    user = _require_member(request)
    pool = await _require_ready(request.app)
    await ensure_session_cache(request)
    mode = normalize_mode(body.mode)
    if mode == "LIVE":
        _require_live_owner(user)
    if body.confirmation.strip().upper() != ACTIVATE_CONFIRMATIONS[mode]:
        raise HTTPException(422, "Bağlantı aktifleştirme onayı eksik.")
    api_key, secret_key = session_credentials(request, mode, active_only=False)
    if not api_key or not secret_key:
        raise HTTPException(412, "Bu kanal için kasada kayıtlı anahtar yok.")
    scope, owner_id = _vault_scope(request, mode)
    if mode == "LIVE":
        from .v25_execution import guard_execution_ownership
        guard_execution_ownership(request)
    try:
        account = await _verify_request_credentials(request, mode, api_key, secret_key)
    except VaultError as exc:
        table = "protrebot_exchange_account_vault" if mode == "LIVE" else "protrebot_exchange_session_vault"
        key_column = "user_id" if mode == "LIVE" else "session_id"
        key_value = owner_id if mode == "LIVE" else scope
        await pool.execute(
            f"UPDATE {table} SET active = FALSE, last_test_ok = FALSE, last_test_at = NOW(), last_error = $3, updated_at = NOW() WHERE {key_column} = $1 AND mode = $2",
            key_value, mode,
            _rate_limit_public_message(exc) if isinstance(exc, BinanceServerTimeRejected) else str(exc)[:240],
        )
        public_error = _rate_limit_public_message(exc) if isinstance(exc, BinanceServerTimeRejected) else str(exc)[:240]
        _SESSION_META[(scope, mode)].update({"active": False, "last_test_ok": False, "last_test_at": now_iso(), "last_error": public_error})
        _lock_runtime(request.app, mode, request)
        raise _exchange_test_http_exception(exc) from exc
    table = "protrebot_exchange_account_vault" if mode == "LIVE" else "protrebot_exchange_session_vault"
    key_column = "user_id" if mode == "LIVE" else "session_id"
    key_value = owner_id if mode == "LIVE" else scope
    await pool.execute(
        f"UPDATE {table} SET active = TRUE, last_test_ok = TRUE, last_test_at = NOW(), last_error = NULL, account_summary = $3::jsonb, updated_at = NOW() WHERE {key_column} = $1 AND mode = $2",
        key_value, mode, json.dumps(account, ensure_ascii=False),
    )
    _SESSION_META[(scope, mode)].update({"active": True, "last_test_ok": True, "last_test_at": account["tested_at"], "last_error": None, "account": account, "updated_at": now_iso()})
    if mode == "LIVE":
        from .v25_execution import connect_read_only_for_request

        return {
            **await connect_read_only_for_request(request.app, request, actor=str(user["id"])),
            "message": "Bağlantı aktifleştirildi ve canlı hesap salt-okunur snapshot'ı doğrulandı. Bu işlem emir kilidini açmadı.",
        }
    return {**session_public_status(request.app, request), "message": "Bağlantı aktifleştirildi. Bu işlem emir kilidini açmadı."}


@router.post("/deactivate")
async def exchange_connection_deactivate(request: Request, body: ConnectionActionRequest) -> dict[str, Any]:
    user = _require_member(request)
    pool = await _require_ready(request.app)
    await ensure_session_cache(request)
    mode = normalize_mode(body.mode)
    if mode == "LIVE":
        await _require_live_mfa(request, user, body.totp_code)
    if body.confirmation.strip().upper() != "BAĞLANTIYI KAPAT":
        raise HTTPException(422, "Bağlantıyı kapatma onayı eksik.")
    scope, owner_id = _vault_scope(request, mode)
    table = "protrebot_exchange_account_vault" if mode == "LIVE" else "protrebot_exchange_session_vault"
    key_column = "user_id" if mode == "LIVE" else "session_id"
    key_value = owner_id if mode == "LIVE" else scope
    await pool.execute(
        f"UPDATE {table} SET active = FALSE, updated_at = NOW() WHERE {key_column} = $1 AND mode = $2",
        key_value, mode,
    )
    if (scope, mode) in _SESSION_META:
        _SESSION_META[(scope, mode)]["active"] = False
        _SESSION_META[(scope, mode)]["updated_at"] = now_iso()
    _lock_runtime(request.app, mode, request)
    return {**session_public_status(request.app, request), "message": "Bağlantı kapatıldı; yeni emir yetkileri sıfırlandı."}


@router.delete("/credentials")
async def exchange_connection_delete(request: Request, body: ConnectionActionRequest) -> dict[str, Any]:
    user = _require_member(request)
    pool = await _require_ready(request.app)
    await ensure_session_cache(request)
    mode = normalize_mode(body.mode)
    if mode == "LIVE":
        await _require_live_mfa(request, user, body.totp_code)
    if body.confirmation.strip().upper() != DELETE_CONFIRMATION:
        raise HTTPException(422, f"Silmek için {DELETE_CONFIRMATION} onayı gerekir.")
    scope, owner_id = _vault_scope(request, mode)
    if _SESSION_META.get((scope, mode), {}).get("active"):
        raise HTTPException(409, "Önce bağlantıyı devre dışı bırakın; sonra anahtarı silebilirsiniz.")
    api_key, secret_key = session_credentials(request, mode, active_only=False)
    if api_key and secret_key:
        try:
            account = await _verify_request_credentials(request, mode, api_key, secret_key)
            if int(account.get("active_positions") or 0) > 0:
                raise HTTPException(409, "Açık pozisyon varken anahtar silinemez. Önce Binance hesabındaki pozisyonları güvenle kapatın.")
        except HTTPException:
            raise
        except VaultError:
            # Invalid/expired credentials must remain removable after the
            # connection has been deactivated.
            pass
    if mode == "LIVE":
        await pool.execute(
            "DELETE FROM protrebot_exchange_account_vault WHERE user_id = $1 AND mode = 'LIVE'",
            owner_id,
        )
    else:
        await pool.execute("DELETE FROM protrebot_exchange_session_vault WHERE session_id = $1 AND mode = $2", scope, mode)
    _SESSION_CACHE.pop((scope, mode), None)
    _SESSION_META.pop((scope, mode), None)
    _lock_runtime(request.app, mode, request)
    message = "Şifreli LIVE anahtar OWNER hesabından silindi." if mode == "LIVE" else "Şifreli anahtar kaydı bu oturum için silindi."
    return {**session_public_status(request.app, request), "message": message}


@router.delete("/session")
async def exchange_connection_clear_session(request: Request) -> dict[str, Any]:
    _require_member(request)
    pool = await _require_ready(request.app)
    sid = session_id(request)
    await pool.execute("DELETE FROM protrebot_exchange_session_vault WHERE session_id = $1 AND mode = 'TESTNET'", sid)
    _SESSION_CACHE.pop((sid, "TESTNET"), None)
    _SESSION_META.pop((sid, "TESTNET"), None)
    _lock_runtime(request.app, "TESTNET", request)
    return {"ok": True, "message": "Oturum borsa bağlantıları temizlendi."}


async def clear_session_vault_for_request(request: Request) -> None:
    pool = getattr(request.app.state, "db_pool", None)
    sid = session_id(request)
    if pool is not None and sid:
        await pool.execute(
            "DELETE FROM protrebot_exchange_session_vault WHERE session_id = $1 AND mode = 'TESTNET'",
            sid,
        )
    _SESSION_CACHE.pop((sid, "TESTNET"), None)
    _SESSION_META.pop((sid, "TESTNET"), None)
    _lock_runtime(request.app, "TESTNET", request)
