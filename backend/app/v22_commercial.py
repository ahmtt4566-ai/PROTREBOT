"""V24 Commercial Complete control plane (keeps the /api/v22 compatibility path).

The endpoints here are a production-shaped local lab: membership, licensing,
device pairing, audit and fee-aware planning.  Billing and exchange execution
remain disabled so this package cannot move real money or place real orders.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request, Response
from google.auth.exceptions import GoogleAuthError, RefreshError
from googleapiclient.errors import HttpError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .error_monitoring import build_error_event, schedule_log_event
from . import email_service, email_verification
from .auth_failures import login_failure_limits
from .password_policy import MAX_LENGTH as PASSWORD_MAX_LENGTH, validate_new_password
from .email_service import auth_email_html, send_auth_email, VERIFY_SUBJECT, RESET_SUBJECT
from .browser_security import (
    COOKIE_SESSION_PREFIX as BROWSER_SESSION_MARKER_PREFIX,
    browser_request,
    clear_browser_cookie,
    set_browser_cookie,
)
from .server_cookie import SESSION_COOKIE_NAME, request_session_token
from .account_erasure import apply_erasure_tombstones, ensure_erasure_schema, erase_user_account

from .binance_demo import credentials_configured, public_status as demo_public_status, restore_demo_state_for_user
from .v21_demo import restore_v21_state_for_user
from .maintenance import get_maintenance_mode
from .commercial_core import (
    FeeGuardInput,
    PASSWORD_ALGORITHM,
    LEGACY_PASSWORD_ALGORITHM,
    V22_VERSION,
    calculate_fee_guard,
    calculate_grid_guard,
    default_commercial_state,
    device_fingerprint_hash,
    hash_password,
    issue_token,
    normalize_email,
    pairing_code_hash,
    verify_password,
    verify_token,
)
from .commerce_core import sanitize_business_settings
from .local_storage import DATA_DIR, migrate_legacy_files
from .web_security import MIN_ACCESS_TOKEN_LENGTH, bootstrap_access_allowed, env_flag, validate_auth_security_configuration
from .subscription_core import (
    ACCESS_STATUSES,
    MASTER_MODE_PRICE,
    PAST_DUE_GRACE_SECONDS,
    PLAN_CATALOG as SUBSCRIPTION_PLAN_CATALOG,
    TRIAL_DAYS,
    active_subscription,
    canonical_status,
    entitlement_snapshot,
)

try:
    import stripe
except ImportError:  # pragma: no cover - dependency is installed in deployed environments
    stripe = None


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v22", tags=["V24 Commercial Complete"])
migrate_legacy_files((
    "v22_commercial_state.json",
    "v22_commercial_state.backup.json",
    "v22_server_secret.dat",
))
STATE_PATH = DATA_DIR / "v22_commercial_state.json"
BACKUP_PATH = DATA_DIR / "v22_commercial_state.backup.json"
SECRET_PATH = DATA_DIR / "v22_server_secret.dat"
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
AUTH_LIMITS = {
    "login": (8, 300), "register": (5, 900), "forgot": (5, 900),
    "reset": (8, 900), "verify": (12, 300), "verify-status": (120, 60),
    "verification-resend": (1, 60), "verification-resend-hour": (5, 3600),
}
AUTH_SECURITY_FIELDS = ("password", "active", "role", "email_verified", "email", "display_name", "password_changed_at", "closed_at", "email_verified_at")
STANDARD_SESSION_SECONDS = 8 * 60 * 60
REMEMBER_SESSION_SECONDS = 30 * 24 * 60 * 60
DUMMY_PASSWORD_RECORD = hash_password(uuid.uuid4().hex)
AUTH_FAILURE_RESPONSE_SECONDS = 0.5
REGISTRATION_RESPONSE_SECONDS = 2.0
COMMERCIAL_STATE_KEY = "v22-commercial"
DURABLE_AUTH_REQUIRED = str(os.getenv("PROTREBOT_DURABLE_AUTH_REQUIRED", "")).strip().lower() in {"1", "true", "yes", "on"}
BOOTSTRAP_OWNER_EMAIL = normalize_email(os.getenv("PROTREBOT_BOOTSTRAP_OWNER_EMAIL", "ahmtt4565@gmail.com"))
GMAIL_DELIVERY_ERRORS = (HttpError, GoogleAuthError, RefreshError, OSError, RuntimeError, ValueError)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dev_tokens_exposed() -> bool:
    exposed = env_flag("PROTREBOT_EXPOSE_DEV_TOKENS", default=False)
    try:
        validate_auth_security_configuration(expose_dev_tokens=exposed)
    except RuntimeError as exc:
        logger.error("%s", exc)
        raise HTTPException(503, "Güvensiz geliştirme kodu yapılandırması") from None
    return exposed


def gmail_failure_log(exc: BaseException) -> dict[str, str]:
    return email_service.failure_details(exc)


def log_gmail_failure(exc: BaseException, application: Any | None = None) -> None:
    details = gmail_failure_log(exc)
    logger.warning("Email delivery failed: reason=%s type=%s code=%s", details["reason"], details["type"], details["code"])
    if application is not None:
        schedule_log_event(application, build_error_event(source="backend", service="email", kind=details["type"], code="EMAIL_DELIVERY_FAILED", severity="ERROR", message=details["message"], details={"reason": details["reason"], "provider_code": details["code"]}))


def gmail_configured() -> bool:
    return email_service.configured()


def app_base_url() -> str:
    try:
        return email_service.validate_app_base_url()
    except email_service.EmailDeliveryError as exc:
        raise HTTPException(503, str(exc)) from None


def issue_one_time_token(state: dict[str, Any], user: dict[str, Any], secret: bytes, *, kind: str) -> str:
    token = issue_token(user["id"], user["role"], secret, kind=kind, ttl_seconds=24 * 60 * 60)
    payload = verify_token(token, secret, expected_kind=kind)
    state.setdefault("auth_tokens", []).append({"jti": payload["jti"], "kind": kind, "user_id": user["id"], "expires_at": datetime.fromtimestamp(payload["exp"], timezone.utc).isoformat(), "used": False})
    return token


def consume_one_time_token(state: dict[str, Any], token: str, secret: bytes, *, kind: str) -> dict[str, Any]:
    try:
        payload = verify_token(token, secret, expected_kind=kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    row = next((item for item in state.get("auth_tokens", []) if item.get("jti") == payload.get("jti") and item.get("kind") == kind and item.get("user_id") == payload.get("sub")), None)
    if not row or row.get("used"):
        raise HTTPException(400, "Bu güvenlik bağlantısı geçersiz veya daha önce kullanılmış")
    row["used"] = True
    return payload


def parse_date(value: str | None) -> datetime:
    try:
        return datetime.fromisoformat(value or "").astimezone(timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)


def configured_session_secret() -> str:
    configured = str(os.environ.get("SESSION_SECRET") or "").strip()
    legacy = str(os.environ.get("PROTREBOT_SESSION_SECRET") or "").strip()
    if configured and legacy and configured != legacy:
        raise RuntimeError("SESSION_SECRET ve PROTREBOT_SESSION_SECRET farklı; yalnızca birini yapılandırın")
    value = configured or legacy
    if value and len(value) < 32:
        raise RuntimeError("SESSION_SECRET / PROTREBOT_SESSION_SECRET en az 32 karakter olmalıdır")
    return value


def load_secret() -> bytes:
    configured = configured_session_secret()
    if configured:
        return hashlib.sha256(configured.encode("utf-8")).digest()
    web_owner_token = str(os.getenv("PROTREBOT_WEB_ACCESS_TOKEN") or "").strip()
    if len(web_owner_token) >= MIN_ACCESS_TOKEN_LENGTH:
        return hashlib.sha256(f"protrebot-v22-session-v1:{web_owner_token}".encode("utf-8")).digest()
    if DURABLE_AUTH_REQUIRED:
        raise RuntimeError("Kalıcı oturum anahtarı eksik: SESSION_SECRET veya PROTREBOT_SESSION_SECRET yapılandırın")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_PATH.exists():
        raw = SECRET_PATH.read_bytes()
        if len(raw) >= 32:
            return raw
    raw = secrets.token_bytes(48)
    temp = SECRET_PATH.with_suffix(".tmp")
    temp.write_bytes(raw)
    os.chmod(temp, 0o600)
    temp.replace(SECRET_PATH)
    return raw


def has_stable_session_secret() -> bool:
    configured = configured_session_secret()
    web_owner_token = str(os.getenv("PROTREBOT_WEB_ACCESS_TOKEN") or "").strip()
    return len(configured) >= 32 or len(web_owner_token) >= MIN_ACCESS_TOKEN_LENGTH


def sanitize_state(payload: Any) -> dict[str, Any]:
    base = default_commercial_state()
    if not isinstance(payload, dict):
        return base
    for key in (
        "users", "profiles", "subscriptions", "auth_tokens", "stripe_event_ids", "stripe_checkout_sessions", "licenses", "pairing_codes", "agents", "audit", "plans",
        "release_evidence", "leads", "demo_invoices", "support_tickets", "acceptances",
    ):
        if key in payload and isinstance(payload[key], type(base[key])):
            base[key] = payload[key]
    if isinstance(payload.get("owner_user_id"), str) or payload.get("owner_user_id") is None:
        base["owner_user_id"] = payload.get("owner_user_id")
    base["business"] = sanitize_business_settings(payload.get("business"))
    # These safety switches are never restored from disk as enabled values.
    base["security"] = default_commercial_state()["security"]
    base["billing"] = {"provider": "MANUAL_DEMO", "live": False, "currency": "USD"}
    base["version"] = V22_VERSION
    return base


def load_state() -> dict[str, Any]:
    for path in (STATE_PATH, BACKUP_PATH):
        try:
            return sanitize_state(json.loads(path.read_text(encoding="utf-8")))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return default_commercial_state()


def save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    serializable = sanitize_state(state)
    body = json.dumps(serializable, ensure_ascii=False, indent=2)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(body, encoding="utf-8")
    if STATE_PATH.exists():
        try:
            BACKUP_PATH.write_text(STATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    temp.replace(STATE_PATH)
    state["_database_revision"] = int(state.get("_database_revision", 0)) + 1
    state["_database_dirty"] = True


async def ensure_commercial_schema(application: Any) -> None:
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        return
    if hasattr(application.state, "v22_commercial"):
        application.state.v22_commercial["authoritative_auth_enabled"] = True
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS application_state_snapshots (
          state_key TEXT PRIMARY KEY,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          payload JSONB NOT NULL
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS commercial_auth_users (
          user_id TEXT PRIMARY KEY,
          auth_version BIGINT NOT NULL,
          security JSONB NOT NULL
        )
        """
    )
    await ensure_erasure_schema(pool)
    from .account_store import SCHEMA as account_schema
    await pool.execute(account_schema)
    await pool.execute(
        """
        INSERT INTO commercial_auth_users (user_id, auth_version, security)
        SELECT u->>'id', COALESCE((u->>'auth_version')::bigint, 1),
               jsonb_build_object('password', u->'password', 'active', u->'active',
                                  'role', u->'role', 'email_verified', u->'email_verified',
                                  'email', u->'email', 'display_name', u->'display_name')
        FROM application_state_snapshots,
             jsonb_array_elements(payload->'users') AS u
        WHERE state_key = 'v22-commercial'
        ON CONFLICT (user_id) DO NOTHING
        """
    )
    await pool.execute("""
        UPDATE commercial_auth_users AS a SET security = jsonb_build_object(
          'email', u->'email', 'display_name', u->'display_name') || a.security
        FROM application_state_snapshots AS s, jsonb_array_elements(s.payload->'users') AS u
        WHERE s.state_key = 'v22-commercial' AND a.user_id = u->>'id'
          AND NOT (a.security ? 'email')
    """)
    await pool.execute("""CREATE UNIQUE INDEX IF NOT EXISTS commercial_auth_email_unique
        ON commercial_auth_users (lower(security->>'email')) WHERE security->>'email' IS NOT NULL""")
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS commercial_auth_limits (
          bucket TEXT PRIMARY KEY,
          window_start TIMESTAMPTZ NOT NULL,
          attempts INTEGER NOT NULL
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS trading_accounts (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          provider TEXT NOT NULL,
          environment TEXT NOT NULL CHECK (environment IN ('DEMO', 'TESTNET', 'PAPER', 'LIVE')),
          account_reference TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'UNASSIGNED',
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE (user_id, provider, environment, account_reference)
        )
        """
    )
    await pool.execute(
        """
        CREATE INDEX IF NOT EXISTS trading_accounts_user_id_idx
        ON trading_accounts (user_id)
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          stripe_customer_id TEXT,
          stripe_subscription_id TEXT UNIQUE,
          stripe_price_id TEXT,
          plan TEXT NOT NULL CHECK (plan IN ('TRIAL', 'MASTER_MODE')),
          status TEXT NOT NULL CHECK (status IN ('TRIALING', 'ACTIVE', 'PAST_DUE', 'UNPAID', 'CANCELLED', 'EXPIRED')),
          trial_start TIMESTAMPTZ,
          trial_end TIMESTAMPTZ,
          current_period_start TIMESTAMPTZ,
          current_period_end TIMESTAMPTZ,
          cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
          canceled_at TIMESTAMPTZ,
          last_payment_status TEXT,
          last_payment_at TIMESTAMPTZ,
          failed_payment_attempts INTEGER NOT NULL DEFAULT 0,
          grace_until TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS subscriptions_user_id_idx ON subscriptions (user_id)")
    await pool.execute("CREATE INDEX IF NOT EXISTS subscriptions_stripe_customer_id_idx ON subscriptions (stripe_customer_id)")
    await pool.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_one_recoverable_per_user_idx
        ON subscriptions (user_id)
        WHERE status IN ('TRIALING', 'ACTIVE', 'PAST_DUE')
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS stripe_webhook_events (
          id TEXT PRIMARY KEY,
          stripe_event_id TEXT NOT NULL UNIQUE,
          event_type TEXT NOT NULL,
          processed BOOLEAN NOT NULL DEFAULT FALSE,
          processed_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    from .google_oauth import SCHEMA as google_schema, configured as google_configured
    if google_configured():
        await pool.execute(google_schema)
        application.state.google_oauth_schema_ready = True


_DB_CREDENTIAL_RE = re.compile(r"(?i)(\w+://)[^\s@/]+@")


def _sanitize_db_error(exc: BaseException) -> str:
    """Strips DSN credentials from a database exception message before logging."""
    return _DB_CREDENTIAL_RE.sub(r"\1[redacted]@", str(exc))


async def persist_v22_commercial(application: Any) -> bool:
    pool = getattr(application.state, "db_pool", None)
    if pool is None or not hasattr(application.state, "v22_commercial"):
        return False
    rt = application.state.v22_commercial
    state = rt["state"]
    revision = int(state.get("_database_revision", 0))
    try:
        async with rt["storage_lock"]:
            await apply_erasure_tombstones(application)
            await persist_auth_security(application)
            await refresh_state_auth_security(application)
            query = """
                INSERT INTO application_state_snapshots (state_key, updated_at, payload)
                VALUES ($1, NOW(), $2::jsonb)
                ON CONFLICT (state_key) DO UPDATE
                SET updated_at = NOW(), payload = EXCLUDED.payload
                """
            from .google_oauth import configured, merge_registered_users
            if configured():
                async with pool.acquire() as connection, connection.transaction():
                    await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", COMMERCIAL_STATE_KEY)
                    await merge_registered_users(application, connection)
                    await connection.execute(query, COMMERCIAL_STATE_KEY, json.dumps(sanitize_state(state), ensure_ascii=False))
            else:
                await pool.execute(query, COMMERCIAL_STATE_KEY, json.dumps(sanitize_state(state), ensure_ascii=False))
        if int(state.get("_database_revision", 0)) == revision:
            state["_database_dirty"] = False
        rt["storage_status"] = "POSTGRESQL_KALICI"
        return True
    except Exception as exc:
        logger.exception("persist_v22_commercial database write failed: %s", _sanitize_db_error(exc))
        rt["storage_status"] = "YEREL_YEDEK"
        return False


async def persist_subscription_record(application: Any, row: dict[str, Any]) -> None:
    pool = getattr(application.state, "db_pool", None)
    subscription_id = str(row.get("stripe_subscription_id") or row.get("stripeSubscriptionId") or "").strip()
    if pool is None or not subscription_id:
                return
    await pool.execute(
                """
                INSERT INTO subscriptions (
                    id, user_id, stripe_customer_id, stripe_subscription_id, stripe_price_id,
                    plan, status, trial_start, trial_end, current_period_start, current_period_end,
                    cancel_at_period_end, canceled_at, last_payment_status, last_payment_at,
                    failed_payment_attempts, grace_until, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::timestamptz, $9::timestamptz,
                                    $10::timestamptz, $11::timestamptz, $12, $13::timestamptz, $14,
                                    $15::timestamptz, $16, $17::timestamptz, NOW())
                ON CONFLICT (stripe_subscription_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    stripe_customer_id = EXCLUDED.stripe_customer_id,
                    stripe_price_id = EXCLUDED.stripe_price_id,
                    plan = EXCLUDED.plan,
                    status = EXCLUDED.status,
                    trial_start = EXCLUDED.trial_start,
                    trial_end = EXCLUDED.trial_end,
                    current_period_start = EXCLUDED.current_period_start,
                    current_period_end = EXCLUDED.current_period_end,
                    cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                    canceled_at = EXCLUDED.canceled_at,
                    last_payment_status = EXCLUDED.last_payment_status,
                    last_payment_at = EXCLUDED.last_payment_at,
                    failed_payment_attempts = EXCLUDED.failed_payment_attempts,
                    grace_until = EXCLUDED.grace_until,
                    updated_at = NOW()
                """,
                str(row.get("id") or uuid.uuid4().hex),
                str(row.get("user_id") or ""),
                row.get("stripe_customer_id") or row.get("stripeCustomerId"),
                subscription_id,
                row.get("stripe_price_id"),
                str(row.get("plan") or "MASTER_MODE"),
                canonical_status(row.get("status")),
                row.get("trial_start"),
                row.get("trial_end"),
                row.get("current_period_start"),
                row.get("current_period_end"),
                bool(row.get("cancel_at_period_end", False)),
                row.get("canceled_at"),
                row.get("last_payment_status"),
                row.get("last_payment_at"),
                int(row.get("failed_payment_attempts", 0)),
                row.get("grace_until"),
        )


async def persist_subscription_record_safe(application: Any, row: dict[str, Any]) -> None:
    try:
        await persist_subscription_record(application, row)
    except Exception as exc:
        schedule_log_event(application, build_error_event(source="backend", service="subscriptions", kind=type(exc).__name__, code="SUBSCRIPTION_PERSIST_FAILED", severity="ERROR", message=str(exc), details={"subscription_id": row.get("stripe_subscription_id") or row.get("stripeSubscriptionId")}))
        raise


async def restore_v22_commercial(application: Any) -> bool:
    pool = getattr(application.state, "db_pool", None)
    if pool is None or not hasattr(application.state, "v22_commercial"):
        return False
    rt = application.state.v22_commercial
    try:
        row = await pool.fetchrow(
            "SELECT payload FROM application_state_snapshots WHERE state_key = $1",
            COMMERCIAL_STATE_KEY,
        )
    except Exception:
        rt["storage_status"] = "YEREL_YEDEK"
        return False
    if row is None:
        return False
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return False
    if not isinstance(payload, dict):
        return False
    restored = sanitize_state(payload)
    restored["_database_revision"] = 0
    restored["_database_dirty"] = False
    rt["state"] = restored
    await apply_erasure_tombstones(application)
    await refresh_state_auth_security(application)
    rt["auth_baseline"] = {
        user["id"]: {"auth_version": int(user.get("auth_version", 1)), **auth_security(user)}
        for user in restored["users"]
    }
    rt["storage_status"] = "POSTGRESQL_KALICI"
    return True


async def sync_v22_storage(application: Any) -> None:
    if not hasattr(application.state, "v22_commercial"):
        return
    rt = application.state.v22_commercial
    if getattr(application.state, "db_pool", None) is None:
        rt["storage_status"] = "YEREL_YEDEK"
        return
    try:
        if not rt.get("storage_ready"):
            await ensure_commercial_schema(application)
            rt["storage_ready"] = True
        await apply_erasure_tombstones(application)
        if not rt.get("restore_attempted"):
            restored = await restore_v22_commercial(application)
            rt["restore_attempted"] = True
            if not restored:
                await persist_v22_commercial(application)
        elif rt["state"].get("_database_dirty"):
            await persist_v22_commercial(application)
        rt["auth_storage_sync_failed"] = False
    except Exception:
        rt["storage_status"] = "YEREL_YEDEK"
        rt["auth_storage_sync_failed"] = True


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {key: user.get(key) for key in ("id", "email", "display_name", "role", "active", "created_at", "last_activity", "email_verified")}


def issue_email_token(user: dict[str, Any], secret: bytes, *, kind: str) -> str:
    return issue_token(user["id"], user["role"], secret, kind=kind, ttl_seconds=24 * 60 * 60)


def add_audit(state: dict[str, Any], kind: str, message: str, *, actor: str = "SYSTEM", subject: str | None = None) -> None:
    state["audit"].insert(0, {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "message": message,
        "actor": actor,
        "subject": subject,
        "created_at": now_iso(),
        "demo_only": True,
    })
    del state["audit"][250:]


def runtime(request: Request) -> dict[str, Any]:
    return request.app.state.v22_commercial


def auth_security(user: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(user.get(key)) for key in AUTH_SECURITY_FIELDS
            if key != "email_verified_at" or key in user}


async def persist_auth_security(application: Any) -> None:
    """Keep security state independent of replaceable, worker-local snapshots."""
    rt = application.state.v22_commercial
    pool = application.state.db_pool
    baseline = rt.setdefault("auth_baseline", {})
    for user in rt["state"]["users"]:
        uid = user["id"]
        current = {"auth_version": int(user.get("auth_version", 1)), **auth_security(user)}
        previous = baseline.get(uid)
        if previous is not None and current["auth_version"] < previous["auth_version"]:
            user.update(copy.deepcopy(previous))
            current = copy.deepcopy(previous)
        await pool.execute(
            """INSERT INTO commercial_auth_users (user_id, auth_version, security)
               VALUES ($1, $2, $3::jsonb) ON CONFLICT (user_id) DO NOTHING""",
            uid, current["auth_version"], json.dumps(auth_security(user)),
        )
        if previous is not None and previous != current:
            changes = {key: current.get(key) for key in AUTH_SECURITY_FIELDS if current.get(key) != previous.get(key)}
            delta = max(0, current["auth_version"] - previous["auth_version"])
            row = await pool.fetchrow(
                """UPDATE commercial_auth_users
                   SET auth_version = auth_version + $2, security = security || $3::jsonb
                   WHERE user_id = $1 AND auth_version = $4 AND security = $5::jsonb
                   RETURNING auth_version, security""",
                uid, delta, json.dumps(changes), previous["auth_version"],
                json.dumps({key: previous[key] for key in AUTH_SECURITY_FIELDS if key in previous}),
            )
            if row is None:
                raise RuntimeError("Authentication state changed concurrently; retry required")
            user["auth_version"] = int(row["auth_version"])
            security = row["security"]
            user.update(json.loads(security) if isinstance(security, str) else security)
            current = {"auth_version": user["auth_version"], **auth_security(user)}
        baseline[uid] = copy.deepcopy(current)


async def refresh_auth_security(request: Request, user: dict[str, Any]) -> None:
    pool = getattr(request.app.state, "db_pool", None)
    rt = runtime(request)
    if pool is None:
        if DURABLE_AUTH_REQUIRED or rt.get("authoritative_auth_enabled"):
            raise HTTPException(503, "Kalıcı kimlik doğrulama veritabanı hazır değil")
        from .account_settings import local_security
        await local_security(request, user)
        return
    rt["authoritative_auth_enabled"] = True
    try:
        row = await pool.fetchrow(
            "SELECT auth_version, security FROM commercial_auth_users WHERE user_id = $1", user["id"],
        )
    except Exception as exc:
        raise HTTPException(503, "Kimlik doğrulama deposu kullanılamıyor") from exc
    if row is None:
        raise HTTPException(401, "Kullanıcı etkin değil")
    security = row["security"]
    user.update(json.loads(security) if isinstance(security, str) else security)
    user["auth_version"] = int(row["auth_version"])
    runtime(request).setdefault("auth_baseline", {})[user["id"]] = {
        "auth_version": user["auth_version"], **auth_security(user),
    }


async def refresh_state_auth_security(application: Any) -> None:
    """Snapshots are projections of shared auth, never its authority."""
    pool = getattr(application.state, "db_pool", None)
    rt = application.state.v22_commercial
    if pool is None:
        if DURABLE_AUTH_REQUIRED or rt.get("authoritative_auth_enabled"):
            raise HTTPException(503, "Kalıcı kimlik doğrulama veritabanı hazır değil")
        return
    rt["authoritative_auth_enabled"] = True
    for user in rt["state"].get("users", []):
        row = await pool.fetchrow(
            "SELECT auth_version, security FROM commercial_auth_users WHERE user_id = $1", user["id"],
        )
        if row is None:
            user.update(active=False, password={})
        else:
            security = row["security"]
            user.update(json.loads(security) if isinstance(security, str) else security)
            user["auth_version"] = int(row["auth_version"])
        rt.setdefault("auth_baseline", {})[user["id"]] = {
            "auth_version": int(user.get("auth_version", 1)), **auth_security(user),
        }


async def validate_authoritative_session(
    application: Any, user_id: str, expected_version: int | None,
) -> dict[str, Any]:
    """Validate every background cycle against the original session version.

    Capture expected_version from the authenticated USER request when granting
    controller/vault access. Never replace it with a fresh database version:
    doing so would resurrect an authorization after logout or erasure.
    """
    try:
        version = int(expected_version)
    except (ValueError, TypeError, OverflowError) as exc:
        raise HTTPException(401, "Oturum sürümü gerekli") from exc
    if not user_id or version < 1:
        raise HTTPException(401, "Oturum gerekli")
    rt = application.state.v22_commercial
    if rt.get("auth_storage_sync_failed"):
        raise HTTPException(503, "Kimlik doğrulama depolama eşitlemesi başarısız")
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        if DURABLE_AUTH_REQUIRED or rt.get("authoritative_auth_enabled"):
            raise HTTPException(503, "Kalıcı kimlik doğrulama veritabanı hazır değil")
        user = next((item for item in rt["state"].get("users", []) if item.get("id") == user_id), None)
        if user:
            from types import SimpleNamespace
            from .account_settings import local_security
            await local_security(SimpleNamespace(app=application), user)
        security = auth_security(user) if user else {}
        actual_version = int(user.get("auth_version", 1)) if user else 0
    else:
        rt["authoritative_auth_enabled"] = True
        try:
            row = await pool.fetchrow(
                "SELECT auth_version, security FROM commercial_auth_users WHERE user_id = $1", user_id,
            )
        except Exception as exc:
            raise HTTPException(503, "Kimlik doğrulama deposu kullanılamıyor") from exc
        if row is None:
            raise HTTPException(401, "Kullanıcı etkin değil")
        security = row["security"]
        security = json.loads(security) if isinstance(security, str) else security
        actual_version = int(row["auth_version"])
    if security.get("active") is not True or actual_version != version:
        raise HTTPException(401, "Oturum yenilenmeli")
    if security.get("role") != "OWNER" and security.get("email_verified") is not True:
        raise HTTPException(403, "E-posta doğrulaması gerekli")
    return {
        "id": user_id, "auth_version": actual_version, "active": True,
        "role": security.get("role"), "email_verified": security.get("email_verified"),
    }


async def authoritative_user_version_active(
    application: Any, user_id: str, expected_auth_version: int | None,
) -> bool:
    """Fail-closed shared-store predicate for cached LIVE/background grants."""
    try:
        if not isinstance(expected_auth_version, int) or isinstance(expected_auth_version, bool) or expected_auth_version < 1:
            return False
        if getattr(application.state, "db_pool", None) is None:
            return False
        await validate_authoritative_session(application, user_id, expected_auth_version)
    except Exception:
        return False
    return True


async def invalidate_user_sessions(
    request: Request, user: dict[str, Any], *, security_updates: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> None:
    """Atomically revoke every session; also suitable for account deactivation."""
    updates = security_updates or {}
    if any(key not in AUTH_SECURITY_FIELDS for key in updates):
        raise ValueError("Unsupported security field")
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        if DURABLE_AUTH_REQUIRED or runtime(request).get("authoritative_auth_enabled"):
            raise HTTPException(503, "Kalıcı kimlik doğrulama veritabanı hazır değil")
        from . import account_store
        async with account_store.edit(request, user["id"]) as doc:
            if doc.get("auth_overlay"):
                user.update(copy.deepcopy(doc["auth_overlay"]))
            if expected_version is not None and int(user.get("auth_version", 1)) != expected_version:
                raise HTTPException(401, "Oturum yenilenmeli")
            user.update(updates)
            user["auth_version"] = int(user.get("auth_version", 1)) + 1
            doc["auth_overlay"] = {"auth_version": user["auth_version"], **auth_security(user)}
        return
    from .account_store import active_connection
    connection = active_connection(request, user["id"]) or pool
    try:
        row = await connection.fetchrow(
            """UPDATE commercial_auth_users
               SET auth_version = auth_version + 1, security = security || $2::jsonb
               WHERE user_id = $1 AND ($3::bigint IS NULL OR auth_version = $3)
               RETURNING auth_version, security""",
            user["id"], json.dumps(updates), expected_version,
        )
    except Exception as exc:
        if "email" in updates and type(exc).__name__ == "UniqueViolationError":
            raise HTTPException(409, "E-posta kullanılamıyor") from None
        raise HTTPException(503, "Oturum iptali kalıcı depoya yazılamadı") from exc
    if row is None:
        raise HTTPException(401, "Kullanıcı etkin değil")
    security = row["security"]
    user.update(json.loads(security) if isinstance(security, str) else security)
    user["auth_version"] = int(row["auth_version"])
    runtime(request).setdefault("auth_baseline", {})[user["id"]] = {
        "auth_version": user["auth_version"], **auth_security(user),
    }


async def authenticated_user_async(request: Request, *, owner: bool = False) -> dict[str, Any]:
    """Await in USER-token middleware before routes using the sync guard.

    Initialize tables with sync_v22_storage first. Database failures raise 503;
    revoked tokens raise 401. The request-scoped proof never skips the sync
    guard's signature, active-account, verification, version or role checks.
    """
    rt = runtime(request)
    if rt.get("auth_storage_sync_failed"):
        raise HTTPException(503, "Kimlik doğrulama depolama eşitlemesi başarısız")
    token = bearer(request)
    try:
        payload = verify_token(token, rt["secret"], expected_kind="USER")
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc
    user = next((item for item in rt["state"]["users"] if item.get("id") == payload["sub"]), None)
    if user is None:
        from .google_oauth import hydrate_user
        user = await hydrate_user(request, user_id=payload["sub"])
        if user is None and getattr(request.app.state, "db_pool", None) is None:
            from .account_store import read
            doc = await read(request, payload["sub"])
            security = (doc or {}).get("auth_overlay")
            if security:
                user = {"id": payload["sub"], **copy.deepcopy(security)}
                rt["state"]["users"].append(user)
        if user is None and getattr(request.app.state, "db_pool", None) is not None:
            try:
                row = await request.app.state.db_pool.fetchrow(
                    "SELECT auth_version, security FROM commercial_auth_users WHERE user_id = $1", payload["sub"])
            except Exception:
                raise HTTPException(503, "Kimlik doğrulama deposu kullanılamıyor") from None
            if row:
                security = row["security"]
                security = json.loads(security) if isinstance(security, str) else security
                user = {"id": payload["sub"], **security, "auth_version": int(row["auth_version"])}
                rt["state"]["users"].append(user)
        if user is None:
            raise HTTPException(401, "Kullanıcı etkin değil")
    await refresh_auth_security(request, user)
    from .account_settings import track_session
    await track_session(request, token)
    request.state.v22_authoritative_token = token
    return authenticated_user(request, owner=owner)


async def update_auth_security(request: Request, user: dict[str, Any], updates: dict[str, Any]) -> None:
    """CAS non-revoking metadata/verification updates against canonical authority."""
    if any(key not in AUTH_SECURITY_FIELDS for key in updates):
        raise ValueError("Unsupported security field")
    from . import account_store
    pool = getattr(request.app.state, "db_pool", None)
    async with account_store.edit(request, user["id"]) as doc:
        if pool is not None:
            connection = account_store.active_connection(request, user["id"]) or pool
            try:
                row = await connection.fetchrow("""UPDATE commercial_auth_users
                    SET security = security || $2::jsonb WHERE user_id = $1 AND auth_version = $3
                    RETURNING auth_version, security""",
                    user["id"], json.dumps(updates), int(user.get("auth_version", 1)))
            except Exception:
                raise HTTPException(503, "Hesap güvenliği kaydedilemedi") from None
            if row is None:
                raise HTTPException(409, "Hesap eşzamanlı olarak değişti")
            security = row["security"]
            user.update(json.loads(security) if isinstance(security, str) else security)
            user["auth_version"] = int(row["auth_version"])
        elif doc.get("auth_overlay"):
            if int(doc["auth_overlay"].get("auth_version", 1)) != int(user.get("auth_version", 1)):
                raise HTTPException(409, "Hesap eşzamanlı olarak değişti")
            user.update(copy.deepcopy(doc["auth_overlay"]))
        user.update(updates)
        if pool is None:
            doc["auth_overlay"] = {"auth_version": int(user.get("auth_version", 1)), **auth_security(user)}
        runtime(request).setdefault("auth_baseline", {})[user["id"]] = {
            "auth_version": int(user.get("auth_version", 1)), **auth_security(user)}


async def enforce_auth_limit(request: Request, action: str, account: str | None) -> None:
    """Fixed-window shared counters, keyed by IP and normalized account hashes."""
    if runtime(request).get("auth_storage_sync_failed"):
        raise HTTPException(503, "Kimlik doğrulama depolama eşitlemesi başarısız")
    limit, window = AUTH_LIMITS[action]
    host = request.client.host if request.client else "unknown"
    identities = [("ip", host)]
    if account is not None:
        identities.append(("account", normalize_email(account)))
    buckets = [
        f"{action}:{kind}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
        for kind, value in identities
    ]
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        if DURABLE_AUTH_REQUIRED or runtime(request).get("authoritative_auth_enabled"):
            raise HTTPException(503, "Kalıcı güvenlik sınırı deposu hazır değil")
        now = time.monotonic()
        counts = []
        for bucket in buckets:
            attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(bucket, []) if now - stamp < window]
            attempts.append(now)
            LOGIN_ATTEMPTS[bucket] = attempts
            counts.append(len(attempts))
            if counts[-1] > limit:
                break
    else:
        try:
            counts = []
            for bucket in buckets:
                row = await pool.fetchrow(
                    """INSERT INTO commercial_auth_limits (bucket, window_start, attempts)
                       VALUES ($1, NOW(), 1)
                       ON CONFLICT (bucket) DO UPDATE SET
                         attempts = CASE WHEN commercial_auth_limits.window_start <= NOW() - $2 * INTERVAL '1 second'
                                         THEN 1 ELSE commercial_auth_limits.attempts + 1 END,
                         window_start = CASE WHEN commercial_auth_limits.window_start <= NOW() - $2 * INTERVAL '1 second'
                                             THEN NOW() ELSE commercial_auth_limits.window_start END
                       RETURNING attempts""",
                    bucket, window,
                )
                counts.append(int(row["attempts"]))
                if counts[-1] > limit:
                    raise HTTPException(429, "Çok fazla deneme; daha sonra tekrar deneyin", headers={"Retry-After": str(window)})
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "Güvenlik sınırı deposu kullanılamıyor") from exc
    if max(counts) > limit:
        raise HTTPException(429, "Çok fazla deneme; daha sonra tekrar deneyin", headers={"Retry-After": str(window)})


def token_limit_account(token: str, secret: bytes, kind: str) -> str:
    try:
        return str(verify_token(token, secret, expected_kind=kind)["sub"])
    except ValueError:
        return "invalid-token"


def bearer(request: Request) -> str:
    token = request_session_token(request)
    if token:
        return token
    raise HTTPException(401, "Oturum gerekli")


def browser_session_response_token(
    token: str, user: dict[str, Any], request: Request, response: Response | None,
    *, browser_session: bool, remember: bool,
) -> str:
    headers = getattr(request, "headers", {})
    browser_session = (
        browser_session or browser_request(request) or bool(headers.get("origin"))
        or headers.get("x-protrebot-browser-session") == "1"
    )
    if not browser_session:
        return token
    if response is None:
        raise HTTPException(503, "Tarayıcı oturumu için güvenli çerez yanıtı gerekli")
    set_browser_cookie(
        response, request, SESSION_COOKIE_NAME, token,
        max_age=REMEMBER_SESSION_SECONDS if remember else None,
    )
    return f"{BROWSER_SESSION_MARKER_PREFIX}{user['id']}"


def clear_rotated_session_cookie(request: Request, response: Response | None, user_id: str) -> None:
    if response is None:
        return
    cookie = str(getattr(request, "cookies", {}).get(SESSION_COOKIE_NAME) or "")
    if not cookie:
        return
    try:
        payload = verify_token(cookie, runtime(request)["secret"], expected_kind="USER")
    except ValueError:
        clear_browser_cookie(response, request, SESSION_COOKIE_NAME)
        return
    if payload["sub"] == user_id:
        clear_browser_cookie(response, request, SESSION_COOKIE_NAME)


def authenticated_user(request: Request, *, owner: bool = False) -> dict[str, Any]:
    rt = runtime(request)
    if rt.get("auth_storage_sync_failed"):
        raise HTTPException(503, "Kimlik doğrulama depolama eşitlemesi başarısız")
    if (DURABLE_AUTH_REQUIRED or rt.get("authoritative_auth_enabled") or getattr(request.app.state, "db_pool", None) is not None) and (
        getattr(request.state, "v22_authoritative_token", None) != bearer(request)
    ):
        raise HTTPException(503, "Ortak oturum doğrulaması gerekli")
    try:
        payload = verify_token(bearer(request), rt["secret"], expected_kind="USER")
    except ValueError as exc:
        schedule_log_event(request.app, build_error_event(source="backend", service="auth", kind="AuthenticationError", code="TOKEN_INVALID", severity="WARNING", message=str(exc), route=request.url.path, method=request.method, request_id=getattr(request.state, "request_id", None)))
        raise HTTPException(401, str(exc)) from exc
    user = next((item for item in rt["state"]["users"] if item.get("id") == payload["sub"] and item.get("active")), None)
    if not user:
        schedule_log_event(request.app, build_error_event(source="backend", service="auth", kind="AuthenticationError", code="USER_INACTIVE", severity="WARNING", message="Authenticated user is inactive.", route=request.url.path, method=request.method, request_id=getattr(request.state, "request_id", None)))
        raise HTTPException(401, "Kullanıcı etkin değil")
    if user.get("role") != "OWNER" and user.get("email_verified") is False and request.url.path != "/api/v22/account/verification/resend":
        raise HTTPException(403, "E-posta doğrulaması gerekli")
    if int(payload.get("ver", 1)) != int(user.get("auth_version", 1)):
        schedule_log_event(request.app, build_error_event(source="backend", service="auth", kind="AuthenticationError", code="SESSION_STALE", severity="WARNING", message="Session version is stale.", route=request.url.path, method=request.method, request_id=getattr(request.state, "request_id", None), user_id=user.get("id")))
        raise HTTPException(401, "Oturum yenilenmeli")
    if owner and user.get("role") != "OWNER":
        raise HTTPException(403, "Yönetici yetkisi gerekli")
    return user


def active_license(state: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    candidates = [item for item in state["licenses"] if item.get("user_id") == user_id and item.get("status") == "ACTIVE" and parse_date(item.get("expires_at")) > now]
    return max(candidates, key=lambda item: item.get("expires_at", ""), default=None)


def admin_overview(state: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    active_licenses = [item for item in state["licenses"] if item.get("status") == "ACTIVE" and parse_date(item.get("expires_at")) > now]
    active_subscriptions = [item for item in state.get("subscriptions", []) if item.get("status") in {"active", "ACTIVE", "TRIAL"} and (not item.get("expires_at") or parse_date(item.get("expires_at")) > now)]
    pro_users = {item.get("user_id") for item in active_subscriptions if str(item.get("plan", "")).upper() == "PRO"} | {item.get("user_id") for item in active_licenses if str(item.get("plan", "")).upper() == "PRO"}
    new_cutoff = now - timedelta(days=30)
    online_cutoff = now - timedelta(minutes=3)
    online_agents = [item for item in state["agents"] if parse_date(item.get("last_seen_at")) >= online_cutoff and item.get("status") == "ACTIVE"]
    return {
        "users": len(state["users"]), "total_users": len(state["users"]),
        "active_users": sum(1 for item in state["users"] if item.get("active")),
        "verified_users": sum(1 for item in state["users"] if item.get("email_verified") is True),
        "admins": sum(1 for item in state["users"] if item.get("role") == "OWNER"),
        "new_users": sum(1 for item in state["users"] if parse_date(item.get("created_at")) >= new_cutoff),
        "pro_users": len(pro_users), "free_users": max(0, len(state["users"]) - len(pro_users)),
        "active_subscriptions": len(active_subscriptions), "expired_subscriptions": sum(1 for item in state.get("subscriptions", []) if item.get("expires_at") and parse_date(item.get("expires_at")) <= now),
        "licenses": len(state["licenses"]),
        "active_licenses": len(active_licenses),
        "agents": len(state["agents"]),
        "online_agents": len(online_agents),
        "monthly_demo_revenue_usd": round(sum(float(state["plans"].get(item.get("plan"), {}).get("monthly_usd", 0)) for item in active_licenses), 2),
        "customers": [{**public_user(user), "license": active_license(state, user["id"]), "subscription": subscription_for_user(state, user["id"])} for user in state["users"]],
        "agents_list": state["agents"][-40:],
        "audit": state["audit"][:50],
        "billing_live": False,
        "demo_only": True,
    }


def operations_overview(application: Any) -> dict[str, Any]:
    """Return a secret-free summary of the local professional stack."""
    demo_state = getattr(application.state, "binance_demo", {})
    v21 = getattr(application.state, "v21_demo", {})
    paper = getattr(application.state, "paper", {})
    paper_bot = getattr(application.state, "paper_bot", {})
    infrastructure = getattr(application.state, "infrastructure", {})
    demo = demo_public_status(demo_state) if demo_state else {
        "configured": credentials_configured(), "connected": False, "armed": False,
        "events": [], "real_trading_locked": True,
    }
    snapshot = v21.get("snapshot") or {}
    return {
        "version": V22_VERSION,
        "demo_connector": {
            "configured": bool(demo.get("configured")),
            "connected": bool(demo.get("connected")),
            "armed": bool(demo.get("armed")),
            "armed_until": demo.get("armed_until"),
            "last_error": demo.get("last_error"),
        },
        "demo_account": {
            "positions": len(snapshot.get("positions", [])),
            "open_orders": len(snapshot.get("open_orders", [])),
            "open_algo_orders": len(snapshot.get("open_algo_orders", [])),
            "available_balance": snapshot.get("available_balance"),
            "wallet_balance": snapshot.get("wallet_balance"),
            "one_way": not bool(snapshot.get("hedge_mode", False)),
        },
        "automation": {
            "demo_enabled": bool(v21.get("auto", {}).get("enabled")),
            "demo_cycles": int(v21.get("auto", {}).get("cycles", 0)),
            "demo_last_decision": v21.get("auto", {}).get("last_decision"),
            "paper_enabled": bool(paper_bot.get("enabled")),
            "paper_cycles": int(paper_bot.get("cycles", 0)),
        },
        "paper": {
            "balance": paper.get("balance", 0),
            "positions": len(paper.get("positions", [])),
            "pending_orders": len(paper.get("limit_orders", [])),
            "closed_trades": len(paper.get("trades", [])),
            "emergency_brake": bool(paper.get("emergency_brake", {}).get("active")),
        },
        "services": {
            "api": infrastructure.get("api", "BAĞLI"),
            "database": infrastructure.get("database", "BEKLENİYOR"),
            "redis": infrastructure.get("redis", "BEKLENİYOR"),
            "paper_storage": infrastructure.get("paper_storage", "BEKLENİYOR"),
        },
        "recent_demo_events": demo.get("events", [])[:8],
        "real_orders_enabled": False,
        "testnet_orders_enabled": False,
        "withdrawals_supported": False,
        "demo_only": True,
    }


class BootstrapRequest(BaseModel):
    display_name: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=5, max_length=180)
    password: str = Field(min_length=10, max_length=256)
    remember: bool = True
    browser_session: bool = False


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=180)
    password: str = Field(min_length=1, max_length=256)
    remember: bool = False
    browser_session: bool = False


class RegisterRequest(BaseModel):
    display_name: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=5, max_length=180)
    password: str = Field(min_length=10, max_length=256)
    confirm_password: str = Field(min_length=10, max_length=256)
    terms_accepted: bool = False


class EmailTokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=600)


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=5, max_length=180)


class PasswordResetConfirmRequest(BaseModel):
    token: str = Field(min_length=20, max_length=600)
    password: str = Field(min_length=10, max_length=256)
    confirm_password: str = Field(min_length=10, max_length=256)
    totp_code: str | None = Field(default=None, max_length=80)


class ProfileUpdateRequest(BaseModel):
    display_name: str = Field(min_length=2, max_length=80)
    preferences: dict[str, Any] = Field(default_factory=dict)


class CustomerRequest(BootstrapRequest):
    plan: Literal["TRIAL", "STARTER", "PRO", "ELITE"] = "TRIAL"
    days: int = Field(default=7, ge=1, le=730)


class SubscriptionRequest(BaseModel):
    user_id: str = Field(min_length=8, max_length=80)
    plan: Literal["TRIAL", "STARTER", "PRO", "ELITE"]
    days: int = Field(default=30, ge=1, le=730)


class CheckoutRequest(BaseModel):
    plan: Literal["TRIAL", "MASTER_MODE"]
    billing_interval: Literal["monthly"] = "monthly"


class CancellationRequest(BaseModel):
    immediate: bool = False


class TradingAccountLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["BINANCE"]
    environment: Literal["DEMO", "TESTNET", "PAPER", "LIVE"]
    account_reference: str = Field(min_length=1, max_length=160)


class PlanUpdateRequest(BaseModel):
    monthly_usd: float = Field(ge=0, le=100_000)
    agents: int = Field(ge=1, le=1_000)
    bots: int = Field(ge=1, le=10_000)


class PairAgentRequest(BaseModel):
    code: str = Field(min_length=6, max_length=24)
    device_name: str = Field(min_length=2, max_length=100)
    fingerprint: str = Field(min_length=12, max_length=500)


class HeartbeatRequest(BaseModel):
    app_version: str = Field(default=V22_VERSION, max_length=40)
    status: str = Field(default="READY", max_length=40)


class FeeGuardRequest(BaseModel):
    entry: float = Field(gt=0)
    target: float = Field(gt=0)
    notional_usdt: float = Field(gt=0, le=1_000_000)
    direction: Literal["LONG", "SHORT"] = "LONG"
    fee_bps_per_side: float = Field(default=4.0, ge=0, le=500)
    slippage_bps_per_side: float = Field(default=2.0, ge=0, le=500)
    funding_bps: float = Field(default=0.0, ge=-500, le=500)
    minimum_net_usdt: float = Field(default=0.25, ge=0, le=100_000)
    minimum_net_pct: float = Field(default=0.05, ge=0, le=100)


class GridGuardRequest(BaseModel):
    lower: float = Field(gt=0)
    upper: float = Field(gt=0)
    grid_count: int = Field(default=20, ge=3, le=200)
    capital_usdt: float = Field(default=1_000, gt=0, le=1_000_000)
    maker_share_pct: float = Field(default=80, ge=0, le=100)
    maker_fee_bps: float = Field(default=2.0, ge=0, le=500)
    taker_fee_bps: float = Field(default=5.0, ge=0, le=500)
    slippage_bps_per_side: float = Field(default=1.0, ge=0, le=500)
    funding_bps: float = Field(default=0.0, ge=-500, le=500)
    minimum_cycle_net_usdt: float = Field(default=0.05, ge=0, le=100_000)


class CustomerStatusRequest(BaseModel):
    active: bool
    reason: str = Field(default="Yönetici işlemi", max_length=180)


class RoleUpdateRequest(BaseModel):
    role: Literal["OWNER", "CUSTOMER"]


class UserDeleteRequest(BaseModel):
    email: str = Field(min_length=5, max_length=180)
    confirmation: Literal["DELETE USER"]


class RevokeRequest(BaseModel):
    confirmation: str = Field(min_length=3, max_length=40)
    reason: str = Field(default="Yönetici işlemi", max_length=180)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)
    totp_code: str | None = Field(default=None, max_length=80)


class ReleaseEvidenceRequest(BaseModel):
    status: Literal["PENDING", "RECORDED"]
    note: str = Field(min_length=3, max_length=500)


@router.get("/public")
async def v22_public(request: Request):
    rt = runtime(request)
    state = rt["state"]
    storage_ready = rt.get("storage_status") == "POSTGRESQL_KALICI" or not DURABLE_AUTH_REQUIRED
    auth_ready = storage_ready and (not DURABLE_AUTH_REQUIRED or has_stable_session_secret())
    return {
        "version": V22_VERSION,
        "edition": "COMMERCIAL COMPLETE · LAUNCH LAB",
        "setup_required": auth_ready and not bool(state.get("owner_user_id")),
        "auth_available": auth_ready,
        "plans": state["plans"],
        "billing": state["billing"],
        "security": state["security"],
        "account_storage": "WINDOWS_LOCAL_APP_DATA" if os.name == "nt" else rt.get("storage_status", "YEREL_YEDEK"),
        "message": "Üyelik, lisans, yerel ajan, satış ve müşteri kurulum altyapısı tek Demo/Paper paketinde; gerçek para ve gerçek emir yok.",
        **({"email_verification_v2_enabled": True} if email_verification.enabled() else {}),
    }


@router.post("/bootstrap")
async def v22_bootstrap(payload: BootstrapRequest, request: Request, response: Response = None):
    rt = runtime(request)
    if DURABLE_AUTH_REQUIRED and rt.get("storage_status") != "POSTGRESQL_KALICI":
        raise HTTPException(503, "Kalıcı hesap veritabanı hazır değil; kayıt güvenli şekilde başlatılamıyor")
    host = request.client.host if request.client else ""
    if not bootstrap_access_allowed(
        host,
        web_owner_authenticated=bool(getattr(request.state, "web_owner_authenticated", False)),
    ):
        raise HTTPException(403, "İlk yönetici yalnızca yerel uygulamadan oluşturulabilir")
    async with rt["lock"]:
        state = rt["state"]
        previous_state = copy.deepcopy(state)
        email = normalize_email(payload.email)
        if "@" not in email:
            raise HTTPException(422, "Geçerli bir e-posta yazın")
        if email != BOOTSTRAP_OWNER_EMAIL:
            raise HTTPException(403, "Bu hesap için yönetici kurulumu yapılamaz")
        existing_user = next((item for item in state["users"] if item.get("email") == email), None)
        owner_exists = bool(state.get("owner_user_id"))
        if owner_exists:
            raise HTTPException(409, "İlk yönetici daha önce oluşturuldu")
        if existing_user:
            await refresh_auth_security(request, existing_user)
        if existing_user and not verify_password(payload.password, existing_user.get("password", {})):
            raise HTTPException(401, "E-posta veya parola hatalı")
        if existing_user is None:
            user_id = uuid.uuid4().hex
            user = {
                "id": user_id,
                "email": email,
                "display_name": payload.display_name.strip(),
                "role": "OWNER",
                "active": True,
                "auth_version": 1,
                "password": hash_password(payload.password),
                "created_at": now_iso(),
            }
            state["users"].append(user)
        else:
            user = existing_user
            user_id = user["id"]
            user["role"] = "OWNER"
            user["active"] = True
            user["auth_version"] = int(user.get("auth_version", 1)) + 1
        state["owner_user_id"] = user_id
        if not any(license_item.get("user_id") == user_id for license_item in state["licenses"]):
            expires_at = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()
            state["licenses"].append({"id": uuid.uuid4().hex, "user_id": user_id, "plan": "ELITE", "status": "ACTIVE", "starts_at": now_iso(), "expires_at": expires_at, "source": "OWNER_BOOTSTRAP", "demo_only": True})
        add_audit(state, "OWNER_BOOTSTRAP", "Bootstrap sahibi OWNER olarak hazırlandı.", actor=user_id, subject=user_id)
        granted_version = int(user["auth_version"])
        save_state(state)
    persisted = await persist_v22_commercial(request.app)
    if DURABLE_AUTH_REQUIRED and not persisted:
        async with rt["lock"]:
            rt["state"] = previous_state
            save_state(previous_state)
        raise HTTPException(503, "Hesap PostgreSQL'e yazılamadı; kayıt tamamlanmadı")
    if not user.get("active") or user.get("role") != "OWNER" or int(user["auth_version"]) != granted_version:
        raise HTTPException(409, "Hesap güvenliği değişti; yönetici kurulumunu yeniden deneyin")
    from .account_settings import enabled, login_challenge, login_record
    if await enabled(request, user):
        return await login_challenge(request, user, remember=payload.remember, browser_session=payload.browser_session)
    token = issue_token(
        user_id,
        "OWNER",
        rt["secret"],
        token_version=user["auth_version"],
        ttl_seconds=REMEMBER_SESSION_SECONDS if payload.remember else STANDARD_SESSION_SECONDS,
    )
    await login_record(request, user, token)
    token = browser_session_response_token(
        token, user, request, response, browser_session=payload.browser_session, remember=payload.remember,
    )
    return {"token": token, "user": public_user(user), "license": active_license(state, user_id), "demo_only": True}


def _ensure_bootstrap_owner_privileges(state: dict[str, Any], user: dict[str, Any]) -> None:
    """Self-heal the designated bootstrap owner's role and license on every successful login.

    Guards against the account silently losing OWNER/ELITE status after a state
    restore from non-durable storage, without ever bypassing the password check.
    """
    if user.get("email") != BOOTSTRAP_OWNER_EMAIL or not state.get("owner_user_id") or state.get("owner_user_id") != user.get("id"):
        return
    user["role"] = "OWNER"
    user["active"] = True
    state["owner_user_id"] = user["id"]
    expires_at = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()
    license_item = next((item for item in state["licenses"] if item.get("user_id") == user["id"]), None)
    if license_item is None:
        state["licenses"].append({"id": uuid.uuid4().hex, "user_id": user["id"], "plan": "ELITE", "status": "ACTIVE", "starts_at": now_iso(), "expires_at": expires_at, "source": "OWNER_BOOTSTRAP", "demo_only": True})
    else:
        license_item.update({"plan": "ELITE", "status": "ACTIVE", "expires_at": expires_at})


@router.post("/auth/login")
async def v22_login(payload: LoginRequest, request: Request, response: Response = None):
    started = time.monotonic()
    rt = runtime(request)
    await login_failure_limits(request, payload.email)
    await enforce_auth_limit(request, "login", payload.email)
    user = next((item for item in rt["state"]["users"] if item.get("email") == normalize_email(payload.email)), None)
    pool = getattr(request.app.state, "db_pool", None)
    if user is None and pool is None:
        from .account_store import local_user_by_email
        canonical = await local_user_by_email(request, normalize_email(payload.email))
        if canonical:
            user = next((item for item in rt["state"]["users"] if item["id"] == canonical["id"]), None)
            if user is None:
                user = canonical
                rt["state"]["users"].append(user)
    if user is None and pool is not None:
        try:
            row = await pool.fetchrow("SELECT user_id, auth_version, security FROM commercial_auth_users WHERE lower(security->>'email') = $1", normalize_email(payload.email))
        except Exception:
            raise HTTPException(503, "Kimlik doğrulama deposu kullanılamıyor") from None
        if row:
            security = row["security"]
            security = json.loads(security) if isinstance(security, str) else security
            user = next((item for item in rt["state"]["users"] if item["id"] == row["user_id"]), None)
            if user is None:
                user = {"id": row["user_id"], **security, "auth_version": int(row["auth_version"])}
                rt["state"]["users"].append(user)
    if user:
        await refresh_auth_security(request, user)
    record = user.get("password") if user else None
    usable_record = isinstance(record, dict) and record.get("algorithm") in {PASSWORD_ALGORITHM, LEGACY_PASSWORD_ALGORITHM}
    password_valid = verify_password(payload.password, record if usable_record else DUMMY_PASSWORD_RECORD)
    if usable_record and record.get("algorithm") != PASSWORD_ALGORITHM:
        verify_password(payload.password, DUMMY_PASSWORD_RECORD)
    if not user or user.get("email") != normalize_email(payload.email) or not user.get("active") or not password_valid:
        await login_failure_limits(request, payload.email, failed=True)
        await asyncio.sleep(max(0.0, AUTH_FAILURE_RESPONSE_SECONDS - (time.monotonic() - started)))
        raise HTTPException(401, "E-posta veya parola hatalı")
    from .account_settings import enabled, login_challenge, login_record
    if await enabled(request, user):
        return await login_challenge(request, user, remember=payload.remember, browser_session=payload.browser_session)
    verified_version = int(user.get("auth_version", 1))
    if user["password"].get("algorithm") != PASSWORD_ALGORITHM:
        user["password"] = hash_password(payload.password)
    user["last_activity"] = now_iso()
    _ensure_bootstrap_owner_privileges(rt["state"], user)
    save_state(rt["state"])
    persisted = await persist_v22_commercial(request.app)
    if DURABLE_AUTH_REQUIRED and not persisted:
        raise HTTPException(503, "Kalıcı hesap veritabanı hazır değil")
    if not user.get("active") or int(user.get("auth_version", 1)) != verified_version:
        raise HTTPException(401, "Oturum değişti; yeniden giriş yapın")
    await restore_demo_state_for_user(request.app, user["id"])
    await restore_v21_state_for_user(request.app, user["id"])
    token = issue_token(
        user["id"],
        user["role"],
        rt["secret"],
        token_version=int(user.get("auth_version", 1)),
        ttl_seconds=REMEMBER_SESSION_SECONDS if payload.remember else STANDARD_SESSION_SECONDS,
    )
    await login_record(request, user, token)
    token = browser_session_response_token(
        token, user, request, response, browser_session=payload.browser_session, remember=payload.remember,
    )
    return {"token": token, "user": public_user(user), "license": active_license(rt["state"], user["id"]), "demo_only": True,
            **({"email_verification_v2_enabled": True} if email_verification.enabled() else {})}


@router.post("/auth/register")
async def v22_register(payload: RegisterRequest, request: Request, response: Response = None):
    from .google_oauth import registration_guard
    started = time.monotonic()
    try:
        async with registration_guard(request, payload.email):
            return await register_password_user(payload, request, response)
    finally:
        await asyncio.sleep(max(0.0, REGISTRATION_RESPONSE_SECONDS - (time.monotonic() - started)))


async def register_password_user(payload: RegisterRequest, request: Request, browser_response: Response = None):
    await enforce_auth_limit(request, "register", payload.email)
    if not payload.terms_accepted:
        raise HTTPException(422, "Kullanım koşullarını kabul etmelisiniz")
    if payload.password != payload.confirm_password:
        raise HTTPException(422, "Parolalar eşleşmiyor")
    validate_new_password(payload.password)
    rt = runtime(request)
    if DURABLE_AUTH_REQUIRED and not has_stable_session_secret():
        raise HTTPException(503, "Kalıcı oturum anahtarı yapılandırılmamış; kayıt güvenli şekilde başlatılamıyor")
    email = normalize_email(payload.email)
    if "@" not in email:
        raise HTTPException(422, "Geçerli bir e-posta yazın")
    expose_dev_token = dev_tokens_exposed()
    if not gmail_configured() and not expose_dev_token:
        raise HTTPException(503, email_service.unavailable_message(), headers={"X-Email-Delivery-Error": "1", "Retry-After": "60"})
    created_user = None
    async with rt["lock"]:
        await apply_erasure_tombstones(request.app)
        state = rt["state"]
        if any(item.get("email") == email for item in state["users"]):
            hash_password(payload.password)
            # Return the same shape with unlinked identifiers, never account data.
            synthetic = {
                "id": uuid.uuid4().hex, "email": email, "display_name": payload.display_name.strip(),
                "role": "CUSTOMER", "active": True, "created_at": now_iso(), "email_verified": False,
            }
            response = {
                "user": public_user(synthetic), "message": "E-posta kayıt için uygunsa doğrulama bağlantısı gönderildi.",
                "email_verification_required": True,
                "verification_status_token": issue_token(synthetic["id"], "CUSTOMER", rt["secret"], kind="EMAIL_STATUS", ttl_seconds=24 * 60 * 60),
            }
            if expose_dev_token:
                response["development_verification_token"] = issue_email_token(synthetic, rt["secret"], kind="EMAIL_VERIFY")
            mail = {
                "to_email": email, "display_name": synthetic["display_name"],
                "subject": "KaisTrade hesap bilgilendirmesi",
                "title": "Bu e-postayla zaten bir hesabın var. Giriş yap veya şifreni sıfırla.",
                "action_url": f"{app_base_url()}/login", "action_label": "Hesabına giriş yap",
                "information_only": True,
            }
        else:
            user_id = uuid.uuid4().hex
            user = {
                "id": user_id, "email": email, "display_name": payload.display_name.strip(),
                "role": "CUSTOMER", "active": True, "auth_version": 1,
                "email_verified": False, "password": hash_password(payload.password), "created_at": now_iso(),
            }
            created_user = user
            state["users"].append(user)
            state["profiles"].append({"id": uuid.uuid4().hex, "user_id": user_id, "full_name": user["display_name"], "avatar_url": None, "role": "user", "preferences": {}, "created_at": user["created_at"], "updated_at": user["created_at"]})
            state["subscriptions"].append({"id": uuid.uuid4().hex, "user_id": user_id, "plan": "FREE", "status": "inactive", "started_at": user["created_at"], "expires_at": None, "created_at": user["created_at"], "updated_at": user["created_at"]})
            if email_verification.enabled():
                user["email_verified_at"] = None
                verification_token = await email_verification.registration_link(request, user)
            else:
                verification_token = issue_one_time_token(state, user, rt["secret"], kind="EMAIL_VERIFY")
                from .account_store import save_action_token
                await save_action_token(request, verification_token, user, "EMAIL_VERIFY")
            verification_status_token = issue_token(user_id, user["role"], rt["secret"], kind="EMAIL_STATUS", ttl_seconds=24 * 60 * 60)
            add_audit(state, "USER_REGISTERED", "Yeni kullanıcı hesabı oluşturuldu.", actor=user_id, subject=user_id)
            save_state(state)
            response = {"user": public_user(user), "message": "E-posta kayıt için uygunsa doğrulama bağlantısı gönderildi.", "email_verification_required": True, "verification_status_token": verification_status_token}
            if expose_dev_token:
                response["development_verification_token"] = verification_token
            mail = {"to_email": email, "display_name": user["display_name"], "subject": VERIFY_SUBJECT,
                    "title": "Hesabını doğrula", "action_url": f"{app_base_url()}/verify-email?token={verification_token}",
                    "action_label": "E-posta adresimi doğrula"}
            if email_verification.enabled():
                mail.update(title="E-posta adresini onayla", action_label="E-postamı doğrula",
                            expiry="30 dakika", verification_v2=True)
    persisted = await persist_v22_commercial(request.app)
    if DURABLE_AUTH_REQUIRED and not persisted:
        if created_user:
            await rollback_password_registration(request, created_user["id"])
        raise HTTPException(503, "Kalıcı hesap veritabanı hazır değil; kayıt güvenli şekilde tamamlanamadı")
    if gmail_configured():
        try:
            await asyncio.to_thread(send_auth_email, **mail)
        except GMAIL_DELIVERY_ERRORS as exc:
            log_gmail_failure(exc, request.app)
            if created_user:
                await rollback_password_registration(request, created_user["id"])
            raise HTTPException(503, "Doğrulama maili gönderilemedi. Lütfen tekrar dene veya Google ile giriş yap.", headers={"X-Email-Delivery-Error": "1", "Retry-After": "60"}) from None
    if email_verification.enabled():
        await email_verification.complete_registration(request, browser_response, created_user or synthetic)
        response["email_verification_v2_enabled"] = True
    return response


async def rollback_password_registration(request: Request, user_id: str) -> None:
    rt = runtime(request)
    async with rt["lock"]:
        for key, field in (("users", "id"), ("profiles", "user_id"), ("subscriptions", "user_id"), ("auth_tokens", "user_id")):
            rt["state"][key] = [item for item in rt["state"].get(key, []) if item.get(field) != user_id]
        save_state(rt["state"])
    await persist_v22_commercial(request.app)
    if email_verification.enabled():
        await email_verification.rollback_registration(request, user_id)


@router.post("/auth/verify-email")
async def v22_verify_email(payload: EmailTokenRequest, request: Request):
    rt = runtime(request)
    account = (await email_verification.find_link_user(request, email_verification.digest(payload.token))
               if payload.token.startswith(email_verification.PREFIX)
               else token_limit_account(payload.token, rt["secret"], "EMAIL_VERIFY"))
    await enforce_auth_limit(request, "verify", account or "invalid-token")
    if email_verification.enabled() or payload.token.startswith(email_verification.PREFIX):
        return await email_verification.verify_link(request, payload.token)
    try:
        token = verify_token(payload.token, rt["secret"], expected_kind="EMAIL_VERIFY")
    except ValueError:
        raise HTTPException(400, "Doğrulama bağlantısı geçersiz") from None
    user = next((item for item in rt["state"]["users"] if item.get("id") == token["sub"]), None)
    if not user:
        raise HTTPException(404, "Kullanıcı bulunamadı")
    await refresh_auth_security(request, user)
    if not user.get("active"):
        raise HTTPException(400, "Doğrulama bağlantısı geçersiz")
    from .account_store import consume_action_token
    await consume_action_token(request, payload.token, user, "EMAIL_VERIFY")
    await update_auth_security(request, user, {"email_verified": True})
    save_state(rt["state"])
    persisted = await persist_v22_commercial(request.app)
    if DURABLE_AUTH_REQUIRED and not persisted:
        raise HTTPException(503, "E-posta doğrulaması kalıcı depoya yazılamadı")
    return {"ok": True, "message": "E-posta doğrulandı. Artık giriş yapabilirsiniz."}


@router.post("/auth/resend-verification")
async def v22_resend_verification(payload: EmailTokenRequest, request: Request):
    rt = runtime(request)
    account = token_limit_account(payload.token, rt["secret"], "EMAIL_STATUS")
    await enforce_auth_limit(request, "verification-resend", account)
    await enforce_auth_limit(request, "verification-resend-hour", account)
    try:
        proof = verify_token(payload.token, rt["secret"], expected_kind="EMAIL_STATUS")
    except ValueError:
        raise HTTPException(400, "Doğrulama isteği geçersiz veya süresi dolmuş.") from None
    if not gmail_configured():
        raise HTTPException(503, email_service.unavailable_message(), headers={"X-Email-Delivery-Error": "1", "Retry-After": "60"})
    if email_verification.enabled():
        try:
            return await email_verification.send_link(request, proof["sub"])
        except email_service.EmailDeliveryError as exc:
            log_gmail_failure(exc, request.app)
            raise HTTPException(503, "Doğrulama maili gönderilemedi. Lütfen tekrar dene.",
                                headers={"Retry-After": "60"}) from None
    user = next((row for row in rt["state"]["users"] if row.get("id") == proof["sub"]), None)
    if user is None and getattr(request.app.state, "db_pool", None) is not None:
        from .google_oauth import hydrate_user
        user = await hydrate_user(request, user_id=proof["sub"])
    result = {"ok": True, "message": "E-posta kayıt için uygunsa doğrulama bağlantısı gönderildi.", "retry_after": 60}
    if user is None:
        return result
    try:
        await refresh_auth_security(request, user)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        return result
    if not user.get("active") or user.get("email_verified"):
        return result
    async with rt["lock"]:
        token = issue_one_time_token(rt["state"], user, rt["secret"], kind="EMAIL_VERIFY")
        from .account_store import save_action_token
        await save_action_token(request, token, user, "EMAIL_VERIFY")
        save_state(rt["state"])
    persisted = await persist_v22_commercial(request.app)
    if DURABLE_AUTH_REQUIRED and not persisted:
        raise HTTPException(503, "Kalıcı hesap veritabanı hazır değil.")
    try:
        await asyncio.to_thread(send_auth_email, to_email=user["email"], display_name=user["display_name"],
                                subject=VERIFY_SUBJECT, title="Hesabını doğrula",
                                action_url=f"{app_base_url()}/verify-email?token={token}", action_label="E-posta adresimi doğrula")
    except GMAIL_DELIVERY_ERRORS as exc:
        log_gmail_failure(exc, request.app)
        raise HTTPException(503, "Doğrulama maili gönderilemedi. Lütfen tekrar dene veya Google ile giriş yap.",
                            headers={"X-Email-Delivery-Error": "1", "Retry-After": "60"}) from None
    return result


class RegistrationResendRequest(BaseModel):
    token: str | None = Field(default=None, min_length=20, max_length=600)


class RegistrationEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_email: str = Field(min_length=5, max_length=180)
    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("new_email")
    @classmethod
    def valid_email(cls, value):
        from . import account_settings
        return account_settings.EmailChange.valid_email(value)


@router.get("/auth/registration/status")
async def registration_status(request: Request):
    return await email_verification.pending_status(request)


@router.post("/auth/registration/resend")
async def registration_resend(request: Request, payload: RegistrationResendRequest | None = None):
    email_verification.require_enabled()
    await enforce_auth_limit(request, "verification-resend", None)
    try:
        return await email_verification.pending_resend(request, payload.token if payload else None)
    except email_service.EmailDeliveryError as exc:
        log_gmail_failure(exc, request.app)
        raise HTTPException(503, "Doğrulama maili gönderilemedi. Lütfen tekrar dene.",
                            headers={"Retry-After": "60"}) from None


@router.post("/auth/registration/exchange")
async def registration_exchange(request: Request, response: Response):
    return await email_verification.exchange_pending(request, response)


@router.post("/auth/registration/email")
async def registration_email(payload: RegistrationEmailRequest, request: Request, response: Response):
    return await email_verification.change_pending_email(request, response, payload.new_email, payload.current_password)


@router.get("/auth/verification-status")
async def v22_verification_status(request: Request, token: str = Query(..., min_length=20, max_length=600)):
    rt = runtime(request)
    account = token_limit_account(token, rt["secret"], "EMAIL_STATUS")
    if account == "invalid-token":
        account = token_limit_account(token, rt["secret"], "EMAIL_VERIFY")
    await enforce_auth_limit(request, "verify-status", account)
    try:
        try:
            payload = verify_token(token, rt["secret"], expected_kind="EMAIL_STATUS")
        except ValueError:
            payload = verify_token(token, rt["secret"], expected_kind="EMAIL_VERIFY")
    except ValueError as exc:
        raise HTTPException(400, "Geçersiz veya süresi dolmuş doğrulama bağlantısı") from exc
    user = next((item for item in rt["state"]["users"] if item.get("id") == payload["sub"]), None)
    if not user:
        from .google_oauth import hydrate_user
        user = await hydrate_user(request, user_id=payload["sub"])
    if user:
        try:
            await refresh_auth_security(request, user)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            user = None
    if not user:
        if payload.get("kind") == "EMAIL_STATUS":
            return {"verified": False}
        raise HTTPException(400, "Geçersiz veya süresi dolmuş doğrulama bağlantısı")
    return {"verified": bool(user.get("email_verified", False))}


@router.post("/auth/forgot-password")
async def v22_forgot_password(payload: PasswordResetRequest, request: Request):
    rt = runtime(request)
    await enforce_auth_limit(request, "forgot", payload.email)
    expose_dev_token = dev_tokens_exposed()
    if not gmail_configured() and not expose_dev_token:
        raise HTTPException(503, "E-posta servisi yapılandırılmamış")
    user = next((item for item in rt["state"]["users"] if item.get("email") == normalize_email(payload.email)), None)
    if user:
        try:
            await refresh_auth_security(request, user)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            user = None
        if user and not user.get("active"):
            user = None
    response: dict[str, Any] = {"ok": True, "message": "E-posta kayıtlıysa parola yenileme bağlantısı gönderildi."}
    if user:
        async with rt["lock"]:
            reset_token = issue_one_time_token(rt["state"], user, rt["secret"], kind="PASSWORD_RESET")
            from .account_store import save_action_token
            await save_action_token(request, reset_token, user, "PASSWORD_RESET")
            save_state(rt["state"])
        persisted = await persist_v22_commercial(request.app)
        if DURABLE_AUTH_REQUIRED and not persisted:
            raise HTTPException(503, "Kalıcı hesap veritabanı hazır değil")
        if gmail_configured():
            try:
                await asyncio.to_thread(send_auth_email, to_email=user["email"], display_name=user["display_name"], subject=RESET_SUBJECT, title="Parolanı yenile", action_url=f"{app_base_url()}/reset-password?token={reset_token}", action_label="Parolamı yenile")
            except GMAIL_DELIVERY_ERRORS as exc:
                log_gmail_failure(exc, request.app)
                raise HTTPException(503, "Parola yenileme e-postası gönderilemedi") from exc
        if expose_dev_token:
            response["development_reset_token"] = reset_token
    return response


@router.post("/auth/reset-password")
async def v22_reset_password(payload: PasswordResetConfirmRequest, request: Request, response: Response = None):
    rt = runtime(request)
    await enforce_auth_limit(request, "reset", token_limit_account(payload.token, rt["secret"], "PASSWORD_RESET"))
    if payload.password != payload.confirm_password:
        raise HTTPException(422, "Parolalar eşleşmiyor")
    validate_new_password(payload.password)
    try:
        token = verify_token(payload.token, rt["secret"], expected_kind="PASSWORD_RESET")
    except ValueError:
        raise HTTPException(400, "Parola yenileme bağlantısı geçersiz") from None
    user = next((item for item in rt["state"]["users"] if item.get("id") == token["sub"]), None)
    if not user:
        raise HTTPException(404, "Kullanıcı bulunamadı")
    await refresh_auth_security(request, user)
    if not user.get("active"):
        raise HTTPException(400, "Parola yenileme bağlantısı geçersiz")
    from .account_settings import require_totp
    await require_totp(request, user, payload.totp_code)
    from .account_store import consume_action_token
    token = await consume_action_token(request, payload.token, user, "PASSWORD_RESET")
    try:
        await invalidate_user_sessions(request, user, security_updates={"password": hash_password(payload.password), "password_changed_at": now_iso()})
    except HTTPException:
        row = next((item for item in rt["state"]["auth_tokens"] if item.get("jti") == token["jti"]), None)
        if row:
            row["used"] = False
        raise
    save_state(rt["state"])
    persisted = await persist_v22_commercial(request.app)
    if DURABLE_AUTH_REQUIRED and not persisted:
        raise HTTPException(503, "Parola yenileme durumu kalıcı depoya yazılamadı")
    clear_rotated_session_cookie(request, response, user["id"])
    return {"ok": True, "message": "Parolanız güncellendi. Yeni parolanızla giriş yapabilirsiniz."}


@router.get("/session")
async def v22_session(request: Request):
    user = authenticated_user(request)
    state = runtime(request)["state"]
    mode = get_maintenance_mode(getattr(request.app.state, "maintenance", None))
    return {"user": public_user(user), "license": active_license(state, user["id"]), "access": access_snapshot(state, user), "demo_only": True, "maintenance": {"mode": mode}}


@router.get("/profile")
async def v22_profile(request: Request):
    user = authenticated_user(request)
    state = runtime(request)["state"]
    profile = next((item for item in state.get("profiles", []) if item.get("user_id") == user["id"]), None)
    from .account_store import read as read_account_settings
    account = await read_account_settings(request, user["id"])
    if account and account.get("preferences"):
        if profile is None:
            profile = {"user_id": user["id"], "preferences": {}}
        profile.setdefault("preferences", {}).update(copy.deepcopy(account["preferences"]))
    return {"user": public_user(user), "profile": profile, "subscription": subscription_for_user(state, user["id"]), "access": access_snapshot(state, user), "demo_only": True}


@router.patch("/profile")
async def v22_update_profile(payload: ProfileUpdateRequest, request: Request):
    user = authenticated_user(request)
    rt = runtime(request)
    async with rt["lock"]:
        await refresh_auth_security(request, user)
        user = authenticated_user(request)
        user["display_name"] = payload.display_name.strip()
        profile = next((item for item in rt["state"].setdefault("profiles", []) if item.get("user_id") == user["id"]), None)
        if profile is None:
            profile = {"id": uuid.uuid4().hex, "user_id": user["id"], "created_at": now_iso()}
            rt["state"]["profiles"].append(profile)
        profile.update({"full_name": user["display_name"], "preferences": payload.preferences, "updated_at": now_iso()})
        save_state(rt["state"])
    await persist_v22_commercial(request.app)
    return {"user": public_user(user), "profile": profile}


@router.delete("/profile")
async def v22_delete_profile(request: Request, response: Response = None):
    user = authenticated_user(request)
    if user.get("role") == "OWNER":
        raise HTTPException(422, "Ana yönetici hesabı bu ekrandan silinemez")
    result = await erase_user_account(request, user)
    if response is not None:
        clear_browser_cookie(response, request, SESSION_COOKIE_NAME)
    return result


def subscription_for_user(state: dict[str, Any], user_id: str) -> dict[str, Any]:
    # Legacy licenses remain available to admin/reporting surfaces, but never
    # authorize the Stripe-backed Master Trade entitlement.
    return entitlement_snapshot(state, user_id)


def access_snapshot(state: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    subscription = subscription_for_user(state, user["id"])
    is_admin = user.get("role") == "OWNER"
    premium = is_admin or bool(subscription.get("master_trade_access"))
    return {
        "isAdmin": is_admin,
        "canAccessMasterTrade": True,
        "isPremium": premium,
        "canExecuteMasterTrade": premium,
        "entitlements": {**subscription.get("entitlements", {}), "canExecuteMasterTrade": premium, "unlimitedAnalyst": premium},
    }


def stripe_configured() -> bool:
    required = ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "APP_BASE_URL", "STRIPE_PRICE_MASTER_MODE_MONTHLY")
    return bool(stripe and all(os.getenv(key, "").strip() for key in required))


def stripe_base_url() -> str:
    value = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise HTTPException(503, "APP_BASE_URL is not configured as a safe absolute URL")
    return value


def stripe_price_id(plan: str, interval: str) -> str:
    if plan not in {"TRIAL", "MASTER_MODE"} or interval != "monthly":
        raise HTTPException(422, "Invalid subscription product")
    key = "STRIPE_PRICE_MASTER_MODE_MONTHLY"
    price_id = os.getenv(key, "").strip()
    if not price_id:
        raise HTTPException(503, "Stripe price mapping is not configured")
    return price_id


def stripe_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def validate_master_mode_price(price: Any) -> None:
    """Fail closed unless the configured Stripe Price is the approved product."""
    if bool(stripe_value(price, "active", True)) is False:
        raise HTTPException(503, "Stripe Master Mode price is inactive")
    if int(stripe_value(price, "unit_amount", 0) or 0) != 11_990:
        raise HTTPException(503, "Stripe Master Mode price must be 119.90 USD")
    if str(stripe_value(price, "currency", "")).lower() != "usd":
        raise HTTPException(503, "Stripe Master Mode price must use USD")
    recurring = stripe_value(price, "recurring", {}) or {}
    if str(stripe_value(recurring, "interval", "")).lower() != "month" or int(stripe_value(recurring, "interval_count", 1) or 0) != 1:
        raise HTTPException(503, "Stripe Master Mode price must recur monthly")


def stripe_customer_for_user(state: dict[str, Any], user_id: str) -> str | None:
    rows = [row for row in state.get("subscriptions", []) if row.get("user_id") == user_id]
    return next((str(row.get("stripeCustomerId") or row.get("stripe_customer_id")) for row in reversed(rows) if row.get("stripeCustomerId") or row.get("stripe_customer_id")), None)


def subscription_user_for_customer(state: dict[str, Any], customer_id: str | None) -> str | None:
    if not customer_id:
        return None
    return next((str(row.get("user_id")) for row in state.get("subscriptions", []) if str(row.get("stripeCustomerId") or row.get("stripe_customer_id") or "") == str(customer_id)), None)


def normalize_stripe_status(value: Any) -> str:
    return {
        "active": "ACTIVE",
        "trialing": "TRIALING",
        "past_due": "PAST_DUE",
        "unpaid": "UNPAID",
        "canceled": "CANCELLED",
        "incomplete": "PAST_DUE",
        "incomplete_expired": "EXPIRED",
    }.get(str(value or "").lower(), "EXPIRED")


def upsert_stripe_subscription(state: dict[str, Any], payload: Any, *, fallback_user_id: str | None = None, fallback_plan: str | None = None, fallback_interval: str | None = None) -> dict[str, Any]:
    metadata = stripe_value(payload, "metadata", {}) or {}
    customer_id = str(stripe_value(payload, "customer") or stripe_value(payload, "customer_id") or "") or None
    user_id = str(metadata.get("user_id") or fallback_user_id or subscription_user_for_customer(state, customer_id) or "")
    if not user_id:
        raise HTTPException(422, "Stripe subscription is not linked to an application user")
    items = stripe_value(payload, "items", {}) or {}
    data = stripe_value(items, "data", []) or []
    price = stripe_value(data[0], "price", {}) if data else {}
    price_id = str(stripe_value(price, "id") or "")
    plan = str(fallback_plan or metadata.get("plan") or "MASTER_MODE").upper()
    if plan == "TRIAL":
        plan = "TRIAL"
    elif plan != "MASTER_MODE":
        plan = "MASTER_MODE"
    interval = "monthly"
    if price_id and price_id != os.getenv("STRIPE_PRICE_MASTER_MODE_MONTHLY", "").strip():
        raise HTTPException(422, "Stripe price is not an approved ProTreBot price")
    status = normalize_stripe_status(stripe_value(payload, "status"))
    now = now_iso()
    stripe_subscription_id = str(stripe_value(payload, "id") or "")
    row = next(
        (
            item for item in reversed(state.setdefault("subscriptions", []))
            if str(item.get("stripe_subscription_id") or item.get("stripeSubscriptionId") or "") == stripe_subscription_id
        ),
        None,
    )
    if row is None:
        row = {"id": uuid.uuid4().hex, "user_id": user_id}
        state["subscriptions"].append(row)
    current_start = datetime.fromtimestamp(int(stripe_value(payload, "current_period_start") or 0), timezone.utc).isoformat() if stripe_value(payload, "current_period_start") else row.get("current_period_start")
    current_end = datetime.fromtimestamp(int(stripe_value(payload, "current_period_end") or 0), timezone.utc).isoformat() if stripe_value(payload, "current_period_end") else row.get("current_period_end")
    trial_start = datetime.fromtimestamp(int(stripe_value(payload, "trial_start") or 0), timezone.utc).isoformat() if stripe_value(payload, "trial_start") else row.get("trial_start")
    trial_end = datetime.fromtimestamp(int(stripe_value(payload, "trial_end") or 0), timezone.utc).isoformat() if stripe_value(payload, "trial_end") else row.get("trial_end")
    row.update({
        "status": status,
        "plan": plan,
        "billing_interval": interval,
        "stripe_customer_id": customer_id,
        "stripe_subscription_id": str(stripe_value(payload, "id") or "") or None,
        "stripe_price_id": price_id or os.getenv("STRIPE_PRICE_MASTER_MODE_MONTHLY", "").strip() or None,
        "current_period_start": current_start,
        "current_period_end": current_end,
        "trial_start": trial_start,
        "trial_end": trial_end,
        "cancel_at_period_end": bool(stripe_value(payload, "cancel_at_period_end", False)),
        "current_price": MASTER_MODE_PRICE,
        "last_payment_status": row.get("last_payment_status"),
        "provider": "STRIPE",
        "updated_at": now,
    })
    return row


def apply_stripe_event(state: dict[str, Any], event: Any) -> bool:
    event_id = str(stripe_value(event, "id") or "")
    event_type = str(stripe_value(event, "type") or "")
    if not event_id:
        raise HTTPException(400, "Stripe event ID is required")
    processed = state.setdefault("stripe_event_ids", [])
    if event_id in processed:
        return False
    event_data = stripe_value(stripe_value(event, "data", {}), "object", {})
    if event_type == "checkout.session.completed":
        # Checkout completion only proves that Stripe created the session. The
        # subscription webhook is the first authoritative entitlement event.
        metadata = stripe_value(event_data, "metadata", {}) or {}
        state.setdefault("stripe_checkout_sessions", []).append({
            "session_id": str(stripe_value(event_data, "id") or ""),
            "subscription_id": str(stripe_value(event_data, "subscription") or ""),
            "customer_id": str(stripe_value(event_data, "customer") or ""),
            "user_id": metadata.get("user_id"),
            "created_at": now_iso(),
        })
    elif event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        row = upsert_stripe_subscription(state, event_data)
        if event_type == "customer.subscription.deleted":
            row["status"] = "CANCELLED"
            row["canceled_at"] = now_iso()
    elif event_type == "customer.subscription.trial_will_end":
        row = upsert_stripe_subscription(state, event_data)
        add_audit(state, "TRIAL_ENDING", "Free trial ends tomorrow; renewal is scheduled unless cancelled.", actor="SYSTEM", subject=row.get("user_id"))
    elif event_type in {"invoice.paid", "invoice.payment_failed"}:
        customer_id = str(stripe_value(event_data, "customer") or "")
        subscription_id = str(stripe_value(event_data, "subscription") or "")
        row = next((item for item in state.get("subscriptions", []) if str(item.get("stripe_subscription_id") or item.get("stripeSubscriptionId") or "") == subscription_id), None)
        row = row or next((item for item in state.get("subscriptions", []) if str(item.get("stripe_customer_id") or item.get("stripeCustomerId") or "") == customer_id), None)
        if row:
            now = now_iso()
            row["status"] = "ACTIVE" if event_type == "invoice.paid" else "PAST_DUE"
            row["last_payment_status"] = "PAID" if event_type == "invoice.paid" else "FAILED"
            row["last_payment_at"] = now
            row["failed_payment_attempts"] = 0 if event_type == "invoice.paid" else int(row.get("failed_payment_attempts", 0)) + 1
            row["grace_until"] = None if event_type == "invoice.paid" else (datetime.now(timezone.utc) + timedelta(seconds=PAST_DUE_GRACE_SECONDS)).isoformat()
            row["updated_at"] = now
    else:
        raise HTTPException(400, "Unsupported Stripe event")
    processed.append(event_id)
    del processed[:-1000]
    return True


@router.get("/subscription")
async def v22_subscription(request: Request):
    user = authenticated_user(request)
    return subscription_for_user(runtime(request)["state"], user["id"])


@router.post("/subscription/trial")
async def v22_start_trial(request: Request):
    return await create_subscription_checkout(request, "TRIAL")


def has_recoverable_or_active_subscription(state: dict[str, Any], user_id: str) -> bool:
    return any(
        row.get("user_id") == user_id and canonical_status(row.get("status")) in {"TRIALING", "ACTIVE", "PAST_DUE"}
        for row in state.get("subscriptions", [])
    )


async def create_subscription_checkout(request: Request, plan: Literal["TRIAL", "MASTER_MODE"]) -> dict[str, Any]:
    user = authenticated_user(request)
    if not stripe_configured():
        raise HTTPException(503, "Stripe billing is not configured; no subscription was activated")
    state = runtime(request)["state"]
    if has_recoverable_or_active_subscription(state, user["id"]):
        raise HTTPException(409, "An active or recoverable subscription already exists")
    price_id = stripe_price_id(plan, "monthly")
    base_url = stripe_base_url()
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    try:
        validate_master_mode_price(stripe.Price.retrieve(price_id))
        customer_id = stripe_customer_for_user(state, user["id"])
        if not customer_id:
            customer = stripe.Customer.create(email=user["email"], name=user.get("display_name"), metadata={"user_id": user["id"]})
            customer_id = str(stripe_value(customer, "id"))
        metadata = {"user_id": user["id"], "plan": plan, "billing_interval": "monthly"}
        subscription_data: dict[str, Any] = {"metadata": metadata}
        if plan == "TRIAL":
            subscription_data["trial_period_days"] = TRIAL_DAYS
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            payment_method_collection="always",
            success_url=f"{base_url}/billing?checkout=success",
            cancel_url=f"{base_url}/pricing?checkout=cancelled",
            metadata=metadata,
            subscription_data=subscription_data,
        )
    except Exception as exc:
        logger.warning("Stripe checkout creation failed: user_id=%s plan=%s error_type=%s", user["id"], plan, type(exc).__name__)
        raise HTTPException(502, "Checkout could not be created. Please try again.") from exc
    return {"mode": "STRIPE", "checkout_url": stripe_value(session, "url"), "session_id": stripe_value(session, "id"), "plan": plan, "billing_interval": "monthly", "trial_days": TRIAL_DAYS if plan == "TRIAL" else 0, "amount_today": 0 if plan == "TRIAL" else MASTER_MODE_PRICE, "recurring_amount": MASTER_MODE_PRICE, "currency": "USD"}


@router.post("/subscription/checkout")
async def v22_subscription_checkout(payload: CheckoutRequest, request: Request):
    return await create_subscription_checkout(request, payload.plan)


@router.post("/subscription/customer-portal")
async def v22_customer_portal(request: Request):
    user = authenticated_user(request)
    if not stripe_configured():
        raise HTTPException(503, "Stripe billing is not configured; customer portal is unavailable")
    customer_id = stripe_customer_for_user(runtime(request)["state"], user["id"])
    if not customer_id:
        raise HTTPException(409, "No Stripe customer is linked to this account")
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    portal = stripe.billing_portal.Session.create(customer=customer_id, return_url=f"{stripe_base_url()}/billing")
    return {"url": stripe_value(portal, "url"), "mode": "STRIPE"}


@router.post("/subscription/webhook")
async def v22_subscription_webhook(request: Request):
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    signature = request.headers.get("stripe-signature", "").strip()
    body = await request.body()
    if not stripe or not secret or not signature:
        raise HTTPException(503, "Stripe webhook verification is not configured")
    try:
        event = stripe.Webhook.construct_event(body, signature, secret)
    except Exception as exc:
        raise HTTPException(400, "Invalid Stripe webhook signature") from exc
    rt = runtime(request)
    pool = getattr(request.app.state, "db_pool", None)
    async with rt["lock"]:
        state = rt["state"]
        event_id = str(stripe_value(event, "id") or "")
        event_type = str(stripe_value(event, "type") or "")
        if pool is not None:
            existing_event = await pool.fetchrow("SELECT processed FROM stripe_webhook_events WHERE stripe_event_id = $1", event_id)
            if existing_event and existing_event["processed"]:
                return {"ok": True, "duplicate": True, "event_id": event_id}
        applied = apply_stripe_event(state, event)
        if not applied:
            return {"ok": True, "duplicate": True, "event_id": event_id}
        save_state(state)
    await persist_v22_commercial(request.app)
    event_object = stripe_value(stripe_value(event, "data", {}), "object", {})
    if event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted", "customer.subscription.trial_will_end"}:
        row = next((item for item in reversed(rt["state"].get("subscriptions", [])) if str(item.get("stripe_subscription_id") or item.get("stripeSubscriptionId") or "") == str(stripe_value(event_object, "id") or "")), None)
        if row:
            await persist_subscription_record_safe(request.app, row)
    elif event_type in {"invoice.paid", "invoice.payment_failed"}:
        subscription_id = str(stripe_value(event_object, "subscription") or "")
        customer_id = str(stripe_value(event_object, "customer") or "")
        row = next((item for item in reversed(rt["state"].get("subscriptions", [])) if str(item.get("stripe_subscription_id") or item.get("stripeSubscriptionId") or "") == subscription_id), None)
        row = row or next((item for item in reversed(rt["state"].get("subscriptions", [])) if str(item.get("stripe_customer_id") or item.get("stripeCustomerId") or "") == customer_id), None)
        if row:
            await persist_subscription_record_safe(request.app, row)
    if pool is not None:
        await pool.execute(
            """
            INSERT INTO stripe_webhook_events (id, stripe_event_id, event_type, processed, processed_at)
            VALUES ($1, $2, $3, TRUE, NOW())
            ON CONFLICT (stripe_event_id) DO UPDATE SET processed = TRUE, processed_at = NOW(), event_type = EXCLUDED.event_type
            """,
            uuid.uuid4().hex,
            event_id,
            event_type,
        )
    return {"ok": True, "event_id": event_id, "event_type": event_type}


@router.post("/subscription/cancel")
async def v22_subscription_cancel(payload: CancellationRequest, request: Request):
    user = authenticated_user(request)
    rt = runtime(request)
    async with rt["lock"]:
        row = active_subscription(rt["state"], user["id"])
        if not row:
            raise HTTPException(409, "No active subscription exists")
        subscription_id = str(row.get("stripe_subscription_id") or row.get("stripeSubscriptionId") or "")
        if not subscription_id or not stripe_configured():
            raise HTTPException(409, "A Stripe subscription is not linked to this account")
        stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
        try:
            if payload.immediate:
                stripe.Subscription.cancel(subscription_id)
                audit_kind = "SUBSCRIPTION_CANCEL_REQUESTED"
                audit_message = "Immediate subscription cancellation requested; Stripe webhook confirmation is pending."
            else:
                stripe.Subscription.modify(subscription_id, cancel_at_period_end=True)
                audit_kind = "SUBSCRIPTION_CANCEL_SCHEDULED"
                audit_message = "Subscription cancellation scheduled for the end of the current billing period."
        except Exception as exc:
            logger.warning("Stripe cancellation failed: user_id=%s subscription_id=%s error_type=%s", user["id"], subscription_id, type(exc).__name__)
            raise HTTPException(502, "The subscription could not be cancelled. Please try again.") from exc
        row["cancel_at_period_end"] = not payload.immediate
        row["updated_at"] = now_iso()
        add_audit(rt["state"], audit_kind, audit_message, actor=user["id"], subject=user["id"])
        save_state(rt["state"])
    await persist_v22_commercial(request.app)
    return subscription_for_user(rt["state"], user["id"])


@router.post("/auth/logout")
async def v22_logout(request: Request, response: Response = None):
    user = await authenticated_user_async(request)
    rt = runtime(request)
    async with rt["lock"]:
        await invalidate_user_sessions(request, user)
        save_state(rt["state"])
    try:
        from .exchange_connections import clear_session_vault_for_request

        await clear_session_vault_for_request(request)
    except (ImportError, RuntimeError, ValueError):
        pass
    await persist_v22_commercial(request.app)
    if response is not None:
        clear_browser_cookie(response, request, SESSION_COOKIE_NAME)
        from .google_oauth import BINDING_COOKIE, PENDING_COOKIE, clear_flow_cookie
        clear_flow_cookie(response, request, BINDING_COOKIE)
        clear_flow_cookie(response, request, PENDING_COOKIE)
    return {"ok": True}


@router.get("/admin/overview")
async def v22_admin_overview(request: Request):
    authenticated_user(request, owner=True)
    return admin_overview(runtime(request)["state"])


@router.get("/admin/users/{user_id}/trading-accounts")
async def v22_admin_trading_accounts(user_id: str, request: Request):
    authenticated_user(request, owner=True)
    rt = runtime(request)
    if not any(str(item.get("id")) == user_id for item in rt["state"]["users"]):
        raise HTTPException(404, "Kullanıcı bulunamadı")
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        return {"user_id": user_id, "accounts": []}
    await ensure_commercial_schema(request.app)
    rows = await pool.fetch(
        """
        SELECT id, provider, environment, account_reference, status, created_at, updated_at
        FROM trading_accounts
        WHERE user_id = $1
        ORDER BY created_at ASC, id ASC
        """,
        user_id,
    )
    return {
        "user_id": user_id,
        "accounts": [
            {
                "id": str(row["id"]),
                "provider": str(row["provider"]),
                "environment": str(row["environment"]),
                "account_reference": str(row["account_reference"]),
                "status": str(row["status"]),
                "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
                "updated_at": row["updated_at"].isoformat() if hasattr(row["updated_at"], "isoformat") else str(row["updated_at"]),
            }
            for row in rows
        ],
    }


@router.post("/admin/users/{user_id}/trading-accounts")
async def v22_admin_link_trading_account(user_id: str, payload: TradingAccountLinkRequest, request: Request):
    authenticated_user(request, owner=True)
    rt = runtime(request)
    if not any(str(item.get("id")) == user_id for item in rt["state"]["users"]):
        raise HTTPException(404, "Kullanıcı bulunamadı")
    account_reference = " ".join(payload.account_reference.split())
    if not account_reference:
        raise HTTPException(422, "Account reference boş olamaz")
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Trading account ownership storage unavailable")
    await ensure_commercial_schema(request.app)
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO trading_accounts (id, user_id, provider, environment, account_reference, status)
            VALUES ($1, $2, $3, $4, $5, 'UNASSIGNED')
            RETURNING id, provider, environment, account_reference, status, created_at, updated_at
            """,
            uuid.uuid4().hex,
            user_id,
            payload.provider,
            payload.environment,
            account_reference,
        )
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise HTTPException(409, "Bu trading account mapping zaten mevcut") from exc
        raise HTTPException(503, "Trading account ownership storage unavailable") from exc
    return {
        "user_id": user_id,
        "account": {
            "id": str(row["id"]),
            "provider": str(row["provider"]),
            "environment": str(row["environment"]),
            "account_reference": str(row["account_reference"]),
            "status": str(row["status"]),
            "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
            "updated_at": row["updated_at"].isoformat() if hasattr(row["updated_at"], "isoformat") else str(row["updated_at"]),
        },
    }


@router.delete("/admin/users/{user_id}/trading-accounts/{account_id}")
async def v22_admin_unlink_trading_account(user_id: str, account_id: str, request: Request):
    authenticated_user(request, owner=True)
    rt = runtime(request)
    if not any(str(item.get("id")) == user_id for item in rt["state"]["users"]):
        raise HTTPException(404, "Kullanıcı bulunamadı")
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Trading account ownership storage unavailable")
    await ensure_commercial_schema(request.app)
    deleted = await pool.fetchrow(
        "DELETE FROM trading_accounts WHERE id = $1 AND user_id = $2 RETURNING id",
        account_id,
        user_id,
    )
    if not deleted:
        raise HTTPException(404, "Trading account mapping bulunamadı")
    return {"ok": True, "user_id": user_id, "account_id": account_id}


@router.patch("/admin/users/{user_id}/role")
async def v22_admin_update_role(user_id: str, payload: RoleUpdateRequest, request: Request):
    owner = authenticated_user(request, owner=True)
    rt = runtime(request)
    async with rt["lock"]:
        user = next((item for item in rt["state"]["users"] if item.get("id") == user_id), None)
        if not user:
            raise HTTPException(404, "Kullanıcı bulunamadı")
        await refresh_auth_security(request, user)
        if user.get("role") == "OWNER" and payload.role != "OWNER":
            raise HTTPException(409, "OWNER hesabının rolü düşürülemez")
        await invalidate_user_sessions(request, user, security_updates={"role": payload.role})
        add_audit(rt["state"], "ROLE_CHANGED", f"Kullanıcı rolü {payload.role} olarak güncellendi.", actor=owner["id"], subject=user_id)
        save_state(rt["state"])
    await persist_v22_commercial(request.app)
    return {"user": public_user(user)}


@router.get("/operations")
async def v22_operations(request: Request):
    authenticated_user(request)
    return operations_overview(request.app)


@router.post("/admin/users/{user_id}/password-reset")
async def v22_admin_password_reset(user_id: str, request: Request):
    owner = authenticated_user(request, owner=True)
    rt = runtime(request)
    user = next((item for item in rt["state"]["users"] if item.get("id") == user_id), None)
    if not user:
        raise HTTPException(404, "Kullanıcı bulunamadı")
    if not gmail_configured():
        raise HTTPException(503, "E-posta servisi yapılandırılmamış")
    await refresh_auth_security(request, user)
    await enforce_auth_limit(request, "forgot", user["email"])
    async with rt["lock"]:
        reset_token = issue_one_time_token(rt["state"], user, rt["secret"], kind="PASSWORD_RESET")
        from .account_store import save_action_token
        await save_action_token(request, reset_token, user, "PASSWORD_RESET")
        add_audit(rt["state"], "PASSWORD_RESET_REQUESTED", "Yönetici parola yenileme bağlantısı istedi.", actor=owner["id"], subject=user_id)
        try:
            save_state(rt["state"])
        except Exception:
            raise HTTPException(503, "Parola yenileme isteği kaydedilemedi") from None
    persisted = await persist_v22_commercial(request.app)
    if (DURABLE_AUTH_REQUIRED or getattr(request.app.state, "db_pool", None) is not None) and not persisted:
        raise HTTPException(503, "Parola yenileme isteği kalıcı depoya yazılamadı")
    try:
        await asyncio.to_thread(send_auth_email, to_email=user["email"], display_name=user["display_name"], subject=RESET_SUBJECT, title="Parolanı yenile", action_url=f"{app_base_url()}/reset-password?token={reset_token}", action_label="Parolamı yenile")
    except GMAIL_DELIVERY_ERRORS:
        raise HTTPException(503, "Parola yenileme e-postası gönderilemedi") from None
    return {"ok": True, "message": "Parola yenileme bağlantısı gönderildi.", "demo_only": True}


@router.post("/admin/users/{user_id}/sessions/revoke")
async def v22_admin_revoke_sessions(user_id: str, request: Request, response: Response = None):
    owner = authenticated_user(request, owner=True)
    rt = runtime(request)
    async with rt["lock"]:
        user = next((item for item in rt["state"]["users"] if item.get("id") == user_id), None)
        if not user:
            raise HTTPException(404, "Kullanıcı bulunamadı")
        await invalidate_user_sessions(request, user)
        add_audit(rt["state"], "SESSIONS_REVOKED", "Kullanıcının tüm oturumları sonlandırıldı.", actor=owner["id"], subject=user_id)
        try:
            save_state(rt["state"])
        except Exception:
            raise HTTPException(503, "Oturum iptali kaydedilemedi") from None
    persisted = await persist_v22_commercial(request.app)
    if (DURABLE_AUTH_REQUIRED or getattr(request.app.state, "db_pool", None) is not None) and not persisted:
        raise HTTPException(503, "Oturum iptali kalıcı depoya yazılamadı")
    clear_rotated_session_cookie(request, response, user_id)
    return {"ok": True, "message": "Tüm kullanıcı oturumları sonlandırıldı.", "demo_only": True}


@router.delete("/admin/users/{user_id}")
async def v22_admin_delete_user(user_id: str, payload: UserDeleteRequest, request: Request):
    owner = authenticated_user(request, owner=True)
    rt = runtime(request)
    email = normalize_email(payload.email)
    async with rt["lock"]:
        state = rt["state"]
        user = next((item for item in state["users"] if item.get("id") == user_id), None)
        if not user:
            from .account_erasure import user_hash

            pool = getattr(request.app.state, "db_pool", None)
            erased = False
            if pool is not None:
                try:
                    erased = bool(await pool.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM commercial_erased_users WHERE user_hash = $1)", user_hash(user_id),
                    ))
                except Exception as exc:
                    raise HTTPException(503, "Account erasure storage is unavailable") from exc
            if not erased:
                raise HTTPException(404, "Kullanıcı bulunamadı")
            if any(
                item.get("id") != user_id and normalize_email(str(item.get("email") or "")) == email
                for item in state["users"]
            ):
                raise HTTPException(422, "Erasure retry email belongs to another account")
            user = {"id": user_id, "email": email, "role": "CUSTOMER", "active": False}
        if user_id == owner["id"]:
            raise HTTPException(409, "OWNER kendi hesabını silemez")
        if user.get("role") == "OWNER":
            raise HTTPException(409, "OWNER hesabı silinemez")
        if email != normalize_email(user.get("email", "")):
            raise HTTPException(422, "Silinecek hesabın e-posta adresi eşleşmiyor")
    return await erase_user_account(request, user)


@router.post("/auth/change-password")
async def v22_change_password(payload: PasswordChangeRequest, request: Request, response: Response = None):
    validate_new_password(payload.new_password)
    user = authenticated_user(request)
    rt = runtime(request)
    from .account_settings import Proof, proof, refreshed_session_response, rotate
    await proof(request, user, Proof(current_password=payload.current_password, totp_code=payload.totp_code))
    async with rt["lock"]:
        from . import account_store
        async with account_store.edit(request, user["id"]) as doc:
            token = await rotate(request, user, doc, {"password": hash_password(payload.new_password), "password_changed_at": now_iso()}, preserve_current=True)
        add_audit(rt["state"], "PASSWORD_CHANGED", "Hesap parolası değiştirildi; diğer oturumlar kapatıldı.", actor=user["id"], subject=user["id"])
        save_state(rt["state"])
    persisted = await persist_v22_commercial(request.app)
    if DURABLE_AUTH_REQUIRED and not persisted:
        raise HTTPException(503, "Parola değişikliği kalıcı depoya yazılamadı")
    marker = refreshed_session_response(request, response, user, token)
    return {"ok": True, "reauthenticate": False, "token": marker, "message": "Parola değişti; diğer cihazlardaki oturumlar kapatıldı."}


@router.post("/customers")
async def v22_create_customer(payload: CustomerRequest, request: Request):
    owner = authenticated_user(request, owner=True)
    rt = runtime(request)
    async with rt["lock"]:
        state = rt["state"]
        email = normalize_email(payload.email)
        if "@" not in email:
            raise HTTPException(422, "Geçerli bir e-posta yazın")
        if any(item.get("email") == email for item in state["users"]):
            raise HTTPException(409, "Bu e-posta zaten kayıtlı")
        user_id = uuid.uuid4().hex
        user = {"id": user_id, "email": email, "display_name": payload.display_name.strip(), "role": "CUSTOMER", "active": True, "auth_version": 1, "password": hash_password(payload.password), "created_at": now_iso()}
        state["users"].append(user)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=payload.days)).isoformat()
        license_row = {"id": uuid.uuid4().hex, "user_id": user_id, "plan": payload.plan, "status": "ACTIVE", "starts_at": now_iso(), "expires_at": expires_at, "source": "MANUAL_DEMO", "demo_only": True}
        state["licenses"].append(license_row)
        created_at = now_iso()
        state["subscriptions"].append({"id": uuid.uuid4().hex, "user_id": user_id, "plan": "STARTER" if payload.plan == "TRIAL" else payload.plan, "status": "TRIAL" if payload.plan == "TRIAL" else "ACTIVE", "billingInterval": "monthly", "trialStart": created_at if payload.plan == "TRIAL" else None, "trialEnd": expires_at if payload.plan == "TRIAL" else None, "currentPeriodStart": created_at, "currentPeriodEnd": expires_at, "currentPrice": 0 if payload.plan == "TRIAL" else state["plans"].get(payload.plan, {}).get("monthly_usd"), "stripeCustomerId": None, "stripeSubscriptionId": None, "cancelAtPeriodEnd": False, "provider": "DEVELOPMENT", "createdAt": created_at, "updatedAt": created_at})
        add_audit(state, "CUSTOMER_CREATED", f"{email} için {payload.plan} Demo lisansı oluşturuldu.", actor=owner["id"], subject=user_id)
        save_state(state)
    return {"user": public_user(user), "license": license_row, "demo_only": True}


@router.post("/customers/{user_id}/status")
async def v22_customer_status(user_id: str, payload: CustomerStatusRequest, request: Request):
    owner = authenticated_user(request, owner=True)
    rt = runtime(request)
    async with rt["lock"]:
        user = next((item for item in rt["state"]["users"] if item.get("id") == user_id), None)
        if not user:
            raise HTTPException(404, "Kullanıcı bulunamadı")
        await refresh_auth_security(request, user)
        if user.get("role") == "OWNER":
            raise HTTPException(409, "Sahip hesabı bu ekrandan askıya alınamaz")
        if not payload.active:
            from .account_settings import close_blocker
            blocker = await close_blocker(request, user)
            if blocker:
                raise HTTPException(409, blocker)
        await invalidate_user_sessions(request, user, security_updates={"active": payload.active})
        if not payload.active:
            for agent in rt["state"]["agents"]:
                if agent.get("user_id") == user_id and agent.get("status") == "ACTIVE":
                    agent["status"] = "REVOKED"
                    agent["revoked_at"] = now_iso()
                    agent["token_version"] = int(agent.get("token_version", 1)) + 1
        kind = "CUSTOMER_ACTIVATED" if payload.active else "CUSTOMER_SUSPENDED"
        message = f"{user['email']} {'etkinleştirildi' if payload.active else 'askıya alındı'}: {payload.reason}"
        add_audit(rt["state"], kind, message, actor=owner["id"], subject=user_id)
        try:
            save_state(rt["state"])
        except Exception:
            raise HTTPException(503, "Hesap durumu kaydedilemedi") from None
    persisted = await persist_v22_commercial(request.app)
    if (DURABLE_AUTH_REQUIRED or getattr(request.app.state, "db_pool", None) is not None) and not persisted:
        raise HTTPException(503, "Hesap durumu kalıcı depoya yazılamadı")
    return {"user": public_user(user), "agents_revoked": not payload.active, "demo_only": True}


@router.post("/subscriptions/activate-demo")
async def v22_activate_subscription(payload: SubscriptionRequest, request: Request):
    owner = authenticated_user(request, owner=True)
    rt = runtime(request)
    async with rt["lock"]:
        state = rt["state"]
        if not any(item.get("id") == payload.user_id for item in state["users"]):
            raise HTTPException(404, "Kullanıcı bulunamadı")
        expires_at = (datetime.now(timezone.utc) + timedelta(days=payload.days)).isoformat()
        row = {"id": uuid.uuid4().hex, "user_id": payload.user_id, "plan": payload.plan, "status": "ACTIVE", "starts_at": now_iso(), "expires_at": expires_at, "source": "MANUAL_DEMO", "demo_only": True}
        state["licenses"].append(row)
        state["subscriptions"].append({"id": uuid.uuid4().hex, "user_id": payload.user_id, "plan": payload.plan, "status": "TEST_ACTIVE", "period_end": expires_at, "provider": "MANUAL_DEMO", "created_at": now_iso()})
        add_audit(state, "LICENSE_ACTIVATED", f"{payload.plan} Demo lisansı {payload.days} gün etkinleştirildi.", actor=owner["id"], subject=payload.user_id)
        save_state(state)
    return {"license": row, "billing_live": False, "demo_only": True}


@router.post("/licenses/{license_id}/revoke")
async def v22_revoke_license(license_id: str, payload: RevokeRequest, request: Request):
    if payload.confirmation.strip().upper() != "LİSANS İPTAL":
        raise HTTPException(422, "İşlem için LİSANS İPTAL yazın")
    owner = authenticated_user(request, owner=True)
    rt = runtime(request)
    async with rt["lock"]:
        row = next((item for item in rt["state"]["licenses"] if item.get("id") == license_id), None)
        if not row:
            raise HTTPException(404, "Lisans bulunamadı")
        target = next((item for item in rt["state"]["users"] if item.get("id") == row.get("user_id")), None)
        if target and target.get("role") == "OWNER":
            raise HTTPException(409, "Sahip geliştirme lisansı iptal edilemez")
        row.update({"status": "REVOKED", "revoked_at": now_iso(), "revoked_reason": payload.reason})
        for subscription in rt["state"]["subscriptions"]:
            if subscription.get("user_id") == row.get("user_id") and subscription.get("status") in {"TEST_ACTIVE", "ACTIVE", "TRIAL"}:
                subscription["status"] = "CANCELED"
        for agent in rt["state"]["agents"]:
            if agent.get("user_id") == row.get("user_id") and agent.get("status") == "ACTIVE":
                agent["status"] = "REVOKED"
                agent["revoked_at"] = now_iso()
                agent["token_version"] = int(agent.get("token_version", 1)) + 1
        add_audit(rt["state"], "LICENSE_REVOKED", f"Demo lisansı iptal edildi: {payload.reason}", actor=owner["id"], subject=row.get("user_id"))
        save_state(rt["state"])
    return {"ok": True, "license_id": license_id, "agents_revoked": True, "demo_only": True}


@router.put("/plans/{plan_code}")
async def v22_update_plan(plan_code: str, payload: PlanUpdateRequest, request: Request):
    owner = authenticated_user(request, owner=True)
    rt = runtime(request)
    code = plan_code.upper()
    async with rt["lock"]:
        if code not in rt["state"]["plans"]:
            raise HTTPException(404, "Paket bulunamadı")
        rt["state"]["plans"][code].update({"monthly_usd": payload.monthly_usd, "agents": payload.agents, "bots": payload.bots})
        add_audit(rt["state"], "PLAN_UPDATED", f"{code} fiyatı ve sınırları güncellendi.", actor=owner["id"], subject=code)
        save_state(rt["state"])
    return {"code": code, **rt["state"]["plans"][code], "billing_live": False}


@router.post("/agent/pair-code")
async def v22_pair_code(request: Request):
    user = authenticated_user(request)
    rt = runtime(request)
    license_row = active_license(rt["state"], user["id"])
    if not license_row:
        raise HTTPException(403, "Etkin lisans gerekli")
    raw = f"{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
    async with rt["lock"]:
        rt["state"]["pairing_codes"] = [item for item in rt["state"]["pairing_codes"] if parse_date(item.get("expires_at")) > datetime.now(timezone.utc) and not item.get("used")]
        rt["state"]["pairing_codes"].append({"id": uuid.uuid4().hex, "user_id": user["id"], "code_hash": pairing_code_hash(raw, rt["secret"]), "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), "used": False, "created_at": now_iso()})
        add_audit(rt["state"], "PAIR_CODE_CREATED", "10 dakikalık yerel ajan eşleştirme kodu üretildi.", actor=user["id"], subject=user["id"])
        save_state(rt["state"])
    return {"code": raw, "expires_in_seconds": 600, "message": "Bu kod yalnızca yerel V24 ajanına yazılır; API anahtarı değildir.", "demo_only": True}


@router.post("/agent/pair")
async def v22_pair_agent(payload: PairAgentRequest, request: Request):
    rt = runtime(request)
    code_hash = pairing_code_hash(payload.code, rt["secret"])
    async with rt["lock"]:
        state = rt["state"]
        code = next((item for item in state["pairing_codes"] if item.get("code_hash") == code_hash and not item.get("used") and parse_date(item.get("expires_at")) > datetime.now(timezone.utc)), None)
        if not code:
            raise HTTPException(401, "Eşleştirme kodu geçersiz veya süresi doldu")
        license_row = active_license(state, code["user_id"])
        if not license_row:
            raise HTTPException(403, "Etkin lisans bulunamadı")
        plan = state["plans"].get(license_row["plan"], {})
        active_agents = [item for item in state["agents"] if item.get("user_id") == code["user_id"] and item.get("status") == "ACTIVE"]
        fingerprint_hash = device_fingerprint_hash(payload.fingerprint)
        existing = next((item for item in active_agents if item.get("fingerprint_hash") == fingerprint_hash), None)
        if not existing and len(active_agents) >= int(plan.get("agents", 1)):
            raise HTTPException(409, "Paketin cihaz sınırına ulaşıldı")
        agent = existing or {"id": uuid.uuid4().hex, "user_id": code["user_id"], "fingerprint_hash": fingerprint_hash, "created_at": now_iso(), "token_version": 1}
        if existing:
            agent["token_version"] = int(agent.get("token_version", 1)) + 1
        agent.update({"device_name": payload.device_name.strip(), "status": "ACTIVE", "last_seen_at": now_iso(), "app_version": V22_VERSION, "mode": "DEMO_ONLY"})
        if not existing:
            state["agents"].append(agent)
        code["used"] = True
        add_audit(state, "AGENT_PAIRED", f"{agent['device_name']} güvenli yerel ajan olarak eşleştirildi.", actor=agent["id"], subject=code["user_id"])
        save_state(state)
    token = issue_token(
        agent["id"], "AGENT", rt["secret"], kind="AGENT",
        ttl_seconds=30 * 24 * 60 * 60, token_version=int(agent.get("token_version", 1)),
    )
    return {"agent": agent, "agent_token": token, "commands": [], "mode": "DEMO_ONLY", "exchange_credentials_received": False}


@router.post("/agent/heartbeat")
async def v22_agent_heartbeat(payload: HeartbeatRequest, request: Request):
    rt = runtime(request)
    try:
        token = verify_token(bearer(request), rt["secret"], expected_kind="AGENT")
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc
    async with rt["lock"]:
        agent = next((item for item in rt["state"]["agents"] if item.get("id") == token["sub"] and item.get("status") == "ACTIVE"), None)
        if not agent:
            raise HTTPException(401, "Ajan etkin değil")
        if int(token.get("ver", 1)) != int(agent.get("token_version", 1)):
            raise HTTPException(401, "Ajan oturumu yenilenmeli")
        if not active_license(rt["state"], agent["user_id"]):
            raise HTTPException(403, "Lisans süresi doldu")
        agent.update({"last_seen_at": now_iso(), "app_version": payload.app_version, "runtime_status": payload.status, "mode": "DEMO_ONLY"})
        save_state(rt["state"])
    return {"accepted": True, "server_time": now_iso(), "commands": [], "mode": "DEMO_ONLY", "real_orders_enabled": False}


@router.post("/agents/{agent_id}/revoke")
async def v22_revoke_agent(agent_id: str, payload: RevokeRequest, request: Request):
    if payload.confirmation.strip().upper() != "AJAN İPTAL":
        raise HTTPException(422, "İşlem için AJAN İPTAL yazın")
    user = authenticated_user(request)
    rt = runtime(request)
    async with rt["lock"]:
        agent = next((item for item in rt["state"]["agents"] if item.get("id") == agent_id), None)
        if not agent:
            raise HTTPException(404, "Ajan bulunamadı")
        if user.get("role") != "OWNER" and agent.get("user_id") != user.get("id"):
            raise HTTPException(403, "Bu cihaz için yetkiniz yok")
        agent.update({
            "status": "REVOKED", "revoked_at": now_iso(), "revoked_reason": payload.reason,
            "token_version": int(agent.get("token_version", 1)) + 1,
        })
        add_audit(rt["state"], "AGENT_REVOKED", f"{agent.get('device_name', 'Cihaz')} erişimi kaldırıldı: {payload.reason}", actor=user["id"], subject=agent.get("user_id"))
        save_state(rt["state"])
    return {"ok": True, "agent_id": agent_id, "status": "REVOKED", "demo_only": True}


@router.post("/fee-guard")
async def v22_fee_guard(payload: FeeGuardRequest, request: Request):
    authenticated_user(request)
    try:
        return calculate_fee_guard(FeeGuardInput(**payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/grid-guard")
async def v22_grid_guard(payload: GridGuardRequest, request: Request):
    authenticated_user(request)
    try:
        return calculate_grid_guard(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/release-evidence/{evidence_key}")
async def v22_release_evidence(evidence_key: str, payload: ReleaseEvidenceRequest, request: Request):
    owner = authenticated_user(request, owner=True)
    rt = runtime(request)
    key = evidence_key.strip().lower()
    if key not in {"backup", "support", "legal", "security_review"}:
        raise HTTPException(404, "Yayın kanıtı bulunamadı")
    async with rt["lock"]:
        row = rt["state"]["release_evidence"].setdefault(key, {})
        row.update({"status": payload.status, "note": payload.note.strip(), "updated_at": now_iso(), "actor": owner["id"]})
        add_audit(rt["state"], "RELEASE_EVIDENCE", f"{key} kanıt kaydı {payload.status} olarak güncellendi.", actor=owner["id"], subject=key)
        save_state(rt["state"])
    return {"key": key, **row, "self_attested": True, "production_approval": False}


@router.get("/readiness")
async def v22_readiness(request: Request):
    user = authenticated_user(request)
    state = runtime(request)["state"]
    now = datetime.now(timezone.utc)
    online_cutoff = now - timedelta(minutes=3)
    evidence = state.get("release_evidence", {})
    operations = operations_overview(request.app)
    agent_online = any(
        item.get("user_id") == user["id"] and item.get("status") == "ACTIVE"
        and parse_date(item.get("last_seen_at")) >= online_cutoff
        for item in state["agents"]
    )
    gates = [
        {"key": "owner", "label": "Yönetici hesabı", "passed": bool(state.get("owner_user_id")), "detail": "Yerel sahip oluşturuldu."},
        {"key": "auth", "label": "Parola ve imzalı oturum", "passed": True, "detail": "Scrypt parola özeti ve HMAC süreli oturum kullanılıyor."},
        {"key": "license", "label": "Etkin lisans", "passed": bool(active_license(state, user["id"])), "detail": "Plan, bitiş tarihi ve cihaz sınırı doğrulanıyor."},
        {"key": "agent", "label": "Güvenli yerel ajan", "passed": agent_online, "detail": "API anahtarını merkeze göndermeyen, sürekli kalp atışlı cihaz modeli."},
        {"key": "demo_connector", "label": "Binance Futures Demo bağlantısı", "passed": bool(operations["demo_connector"]["configured"]), "detail": "Anahtar yalnızca yerel Windows DPAPI kasasında tutulur."},
        {"key": "fee_guard", "label": "Net kâr koruması", "passed": True, "detail": "Komisyon, kayma ve fonlama tahmini karar öncesi düşülüyor."},
        {"key": "backup", "label": "Yedekleme tatbikatı", "passed": evidence.get("backup", {}).get("status") == "RECORDED", "detail": evidence.get("backup", {}).get("note", "YEDEKLE.bat tatbikatı bekleniyor.")},
        {"key": "support", "label": "Müşteri destek süreci", "passed": evidence.get("support", {}).get("status") == "RECORDED", "detail": evidence.get("support", {}).get("note", "Destek akışı bekleniyor.")},
        {"key": "payment", "label": "Canlı ödeme sağlayıcısı", "passed": False, "detail": "Şimdilik MANUAL_DEMO; para tahsilatı kapalı."},
        {"key": "legal", "label": "Hukuk ve sözleşmeler", "passed": False, "detail": evidence.get("legal", {}).get("note", "Satış öncesi ülkeye özel hukuk incelemesi gerekli.")},
        {"key": "security_review", "label": "Bağımsız güvenlik testi", "passed": False, "detail": evidence.get("security_review", {}).get("note", "Genel kullanıma açılmadan pentest ve gizli anahtar yönetimi gerekli.")},
    ]
    passed = sum(1 for item in gates if item["passed"])
    return {
        "version": V22_VERSION,
        "stage": "COMMERCIAL COMPLETE · LAUNCH LAB · DEMO",
        "score": round(passed / len(gates) * 100),
        "passed": passed,
        "total": len(gates),
        "gates": gates,
        "production_ready": False,
        "closed_beta_candidate": all(item["passed"] for item in gates if item["key"] in {"owner", "auth", "license", "agent", "demo_connector", "fee_guard", "backup", "support"}),
        "demo_only": True,
        "release_evidence": evidence,
        "next_step": "Demo emir, otomasyon, yedek ve destek tatbikatlarını tamamla; ardından bağımsız hukuk ve güvenlik incelemesine geç.",
    }


def init_v22_commercial(application: Any) -> None:
    state = load_state()
    application.state.v22_commercial = {
        "state": state,
        "secret": load_secret(),
        "lock": asyncio.Lock(),
        "storage_lock": asyncio.Lock(),
        "storage_ready": False,
        "restore_attempted": False,
        "storage_status": "YEREL_YEDEK",
        "auth_baseline": {
            user["id"]: {"auth_version": int(user.get("auth_version", 1)), **auth_security(user)}
            for user in state["users"]
        },
    }
    add_audit(state, "V24_START", "V24 Commercial Complete başladı; ödeme ve gerçek emir kanalları kapalı.")
    save_state(state)


async def shutdown_v22_commercial(application: Any) -> None:
    if hasattr(application.state, "v22_commercial"):
        save_state(application.state.v22_commercial["state"])
        try:
            await asyncio.wait_for(persist_v22_commercial(application), timeout=3)
        except asyncio.TimeoutError:
            pass
