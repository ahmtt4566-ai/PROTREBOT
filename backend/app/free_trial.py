"""One payment-free trial per verified account; no Stripe subscription writes."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta

from asyncpg import PostgresError
from fastapi import APIRouter, HTTPException, Request

from . import binance_manual_payments as payments

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v22/free-trial", tags=["Payment-free trial"])
LIVE_MESSAGE = "LIVE işlem MASTER MODE ile açılır"


def enabled() -> bool:
    value = os.getenv("FREE_TRIAL_ENABLED", "false").strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise HTTPException(503, "Deneme ayarı doğrulanamadı.")
    return value in {"true", "1"}


def account_hash(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


async def current(conn, user_id: str):
    row = await conn.fetchrow(
        """SELECT user_id,started_at,ends_at,revoked_at,clock_timestamp() AS now
           FROM member_free_trials WHERE user_hash=$1""", account_hash(user_id),
    )
    if row is None:
        return None
    if row["user_id"] != user_id or row["revoked_at"] is not None:
        return None
    start, end, now = row["started_at"], row["ends_at"], row["now"]
    if not all(isinstance(value, datetime) and value.tzinfo is not None for value in (start, end, now)) \
            or end - start != timedelta(days=7) or start > now:
        logger.warning("Free trial record failed validation")
        raise HTTPException(503, "Deneme dönemi doğrulanamadı.")
    return {**dict(row), "active": start <= now < end}


def snapshot(original, trial):
    if trial is None:
        return original
    return {
        **original, "mode": "FREE_TRIAL", "status": "TRIALING" if trial["active"] else "EXPIRED", "plan": "TRIAL",
        "master_trade_access": trial["active"], "trial_start": trial["started_at"].isoformat(),
        "trial_end": trial["ends_at"].isoformat(), "current_period_start": trial["started_at"].isoformat(),
        "current_period_end": trial["ends_at"].isoformat(), "next_payment_amount": None,
        "features": ["AI Analist · mevcut kredi sınırı", "Demo işlem", "Master Trade analiz ve grafikler"],
        "entitlements": {"canAccessMasterTrade": True}, "cancel_at_period_end": False,
    }


async def start(conn, user):
    async with conn.transaction():
        await payments.lock_user(conn, user["id"])
        canonical = await conn.fetchrow(
            "SELECT auth_version,security FROM commercial_auth_users WHERE user_id=$1 FOR SHARE", user["id"],
        )
        security = json.loads(canonical["security"]) if canonical and isinstance(canonical["security"], str) else canonical["security"] if canonical else {}
        if canonical is None or canonical["auth_version"] != user["auth_version"]:
            raise HTTPException(401, "Oturum değişti; yeniden giriş yapın.")
        if security.get("active") is not True or security.get("email_verified") is not True:
            raise HTTPException(403, "Deneme için e-posta adresinizi doğrulayın.")
        if security.get("role") != "CUSTOMER":
            raise HTTPException(403, "Deneme yalnız müşteri hesapları içindir.")
        existing = await current(conn, user["id"])
        if existing is not None:
            if existing["active"]:
                return existing
            raise HTTPException(409, "Bu hesabın deneme hakkı kullanıldı.")
        old_trial = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM subscriptions WHERE user_id=$1 AND plan='TRIAL')", user["id"],
        )
        if old_trial:
            raise HTTPException(409, "Bu hesabın deneme hakkı kullanıldı.")
        await payments.ensure_no_paid_stripe(conn, user["id"])
        if await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM binance_manual_periods p
               JOIN binance_manual_payment_requests r ON r.id=p.payment_request_id
               WHERE p.user_id=$1 AND r.user_id=$1 AND r.status='approved' AND r.erased_at IS NULL
                 AND p.revoked_at IS NULL AND p.period_end>clock_timestamp())""", user["id"],
        ):
            raise HTTPException(409, "MASTER MODE döneminiz zaten mevcut.")
        await conn.execute(
            """INSERT INTO member_free_trials(user_hash,user_id) VALUES ($1,$2)
               ON CONFLICT (user_hash) DO NOTHING""", account_hash(user["id"]), user["id"],
        )
        trial = await current(conn, user["id"])
        if trial is None:
            raise HTTPException(409, "Bu hesabın deneme hakkı kullanıldı.")
        return trial


@router.get("")
async def status(request: Request):
    from .v22_commercial import authenticated_user_async
    user = await authenticated_user_async(request)
    if not enabled():
        return {"enabled": False}
    try:
        async with payments_connection(request) as conn:
            trial = await current(conn, user["id"])
            used = bool(trial) or await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM subscriptions WHERE user_id=$1 AND plan='TRIAL')", user["id"],
            )
            return {"enabled": True, "used": used, "active": bool(trial and trial["active"]),
                    "started_at": trial["started_at"] if trial else None, "ends_at": trial["ends_at"] if trial else None}
    except (PostgresError, OSError, RuntimeError) as exc:
        logger.warning("Free trial status failed type=%s", type(exc).__name__)
        raise HTTPException(503, "Deneme bilgileri yüklenemedi.") from None


def payments_connection(request):
    from .binance_manual_routes import connection
    return connection(request)


@router.post("/start")
async def activate(request: Request):
    from .v22_commercial import authenticated_user_async
    user = await authenticated_user_async(request)
    if not enabled():
        raise HTTPException(503, "Ücretsiz deneme yakında açılacak.")
    try:
        async with payments_connection(request) as conn:
            trial = await start(conn, user)
        return {"enabled": True, "used": True, "active": trial["active"],
                "started_at": trial["started_at"], "ends_at": trial["ends_at"]}
    except (PostgresError, OSError, RuntimeError) as exc:
        logger.warning("Free trial start failed type=%s", type(exc).__name__)
        raise HTTPException(503, "Deneme başlatılamadı; erişim açılmadı.") from None


async def require_background_entry(application, user_id: str, *, live: bool):
    if not enabled():
        return
    pool = getattr(application.state, "db_pool", None)
    if pool is None or not user_id:
        raise HTTPException(503, "İşlem üyeliği doğrulanamadı.")
    from .member_entitlements import access, target_subscription
    try:
        async with pool.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT security->>'role' AS role FROM commercial_auth_users WHERE user_id=$1", user_id,
            )
            if user is None:
                raise HTTPException(403, "İşlem üyeliği bulunamadı.")
            if user["role"] == "OWNER":
                return
            subscription = await target_subscription(conn, application.state.v22_commercial["state"], user_id)
            rights = access(subscription, {"role": user["role"]})
            if not rights["canExecuteLive" if live else "canExecuteMasterTrade"]:
                raise HTTPException(403, LIVE_MESSAGE if live else "Deneme sona erdi. MASTER MODE'a geçin.")
    except (PostgresError, OSError, RuntimeError) as exc:
        logger.warning("Background entry entitlement failed type=%s", type(exc).__name__)
        raise HTTPException(503, "İşlem üyeliği doğrulanamadı.") from None
