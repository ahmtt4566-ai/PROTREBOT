"""Payment-aware request proof; the existing Stripe calculator stays unchanged."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from asyncpg import PostgresError
from fastapi import HTTPException, Request

from .subscription_core import entitlement_snapshot, parse_datetime

logger = logging.getLogger(__name__)


def enabled() -> bool:
    value = os.getenv("BINANCE_MANUAL_ACCESS_ENABLED", "false").strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise HTTPException(503, "Binance üyelik ayarı doğrulanamadı.")
    return value in {"true", "1"}


@dataclass(frozen=True)
class EntitlementProof:
    user_id: str
    auth_version: int
    subscription: dict[str, Any]
    expires_at: datetime | None = None
    error: bool = False


def stripe_subscription(state: dict[str, Any], user_id: str) -> dict[str, Any]:
    return entitlement_snapshot(state, user_id)


def access(subscription: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    owner = user.get("role") == "OWNER"
    premium = owner or bool(subscription.get("master_trade_access"))
    return {
        "isAdmin": owner, "canAccessMasterTrade": True, "isPremium": premium,
        "canExecuteMasterTrade": premium,
        "entitlements": {**subscription.get("entitlements", {}), "canExecuteMasterTrade": premium,
                         "unlimitedAnalyst": premium},
    }


async def resolve(conn: Any, state: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Valid Stripe rights never depend on availability of the Binance tables."""
    original = stripe_subscription(state, user_id)
    if original["master_trade_access"] or not enabled():
        return original
    if conn is None:
        raise HTTPException(503, "Binance üyelik kaydı doğrulanamadı.")
    try:
        row = await conn.fetchrow(
            """SELECT p.user_id,p.period_start,p.period_end,p.payment_request_id,
                 r.user_id AS payment_user_id,r.status,r.amount,r.currency,r.method,r.decided_by,r.decided_at
               FROM binance_manual_periods p
               JOIN binance_manual_payment_requests r ON r.id=p.payment_request_id
               JOIN commercial_auth_users u ON u.user_id=p.user_id
               WHERE p.user_id=$1 AND r.user_id=$1 AND r.status='approved' AND r.erased_at IS NULL
                 AND p.revoked_at IS NULL AND p.period_start <= clock_timestamp()
                 AND p.period_end > clock_timestamp() AND u.security->'active'='true'::jsonb
                 AND u.security->'email_verified'='true'::jsonb
                 AND NOT EXISTS (SELECT 1 FROM commercial_erased_users e WHERE e.user_hash =
                   encode(sha256(convert_to(p.user_id,'UTF8')),'hex'))
               ORDER BY p.period_end DESC,p.payment_request_id LIMIT 1""", user_id,
        )
    except (PostgresError, OSError, RuntimeError) as exc:
        logger.warning("Binance entitlement lookup failed type=%s", type(exc).__name__)
        raise HTTPException(503, "Binance üyelik kaydı doğrulanamadı.") from None
    if row is None:
        return original
    start, end = parse_datetime(row.get("period_start")), parse_datetime(row.get("period_end"))
    decided = parse_datetime(row.get("decided_at"))
    now = datetime.now(timezone.utc)
    if (row.get("user_id") != user_id or row.get("payment_user_id") != user_id or row.get("status") != "approved"
            or str(row.get("amount")) != "119.90" or row.get("currency") != "USDT" or row.get("method") != "binance_manual"
            or not row.get("decided_by") or decided is None or decided > now or start is None or end is None
            or end - start != timedelta(days=30) or not start <= now < end):
        logger.warning("Binance entitlement record failed validation")
        raise HTTPException(503, "Binance üyelik kaydı doğrulanamadı.")
    return {
        **original, "status": "ACTIVE", "plan": "MASTER_MODE", "master_trade_access": True,
        "trial_start": None, "trial_end": None, "current_period_start": start.isoformat(),
        "current_period_end": end.isoformat(), "next_payment_amount": None, "currency": "USDT",
        "features": ["Master Trade access", "30 günlük erişim", "Elle yenileme"],
        "entitlements": {"canAccessMasterTrade": True}, "mode": "BINANCE_MANUAL",
        "cancel_at_period_end": True, "cancelAtPeriodEnd": True, "grace_until": None,
    }


async def prepare(request: Request, user: dict[str, Any]) -> EntitlementProof:
    state = request.app.state.v22_commercial["state"]
    original = stripe_subscription(state, user["id"])
    snapshot = original
    failed = False
    try:
        if not original["master_trade_access"] and user.get("role") != "OWNER" and enabled():
            pool = getattr(request.app.state, "db_pool", None)
            if pool is None:
                raise HTTPException(503, "Binance üyelik kaydı doğrulanamadı.")
            async with pool.acquire() as conn:
                snapshot = await resolve(conn, state, user["id"])
    except (HTTPException, PostgresError, OSError, RuntimeError) as exc:
        logger.warning("Member entitlement unavailable type=%s; Binance access denied", type(exc).__name__)
        failed = True
    proof = EntitlementProof(user["id"], int(user.get("auth_version", 1)), snapshot,
                             parse_datetime(snapshot.get("current_period_end")) if snapshot.get("mode") == "BINANCE_MANUAL" else None,
                             failed)
    request.state.member_entitlement_proof = proof
    return proof


def subscription_for_request(request: Request, user: dict[str, Any]) -> dict[str, Any]:
    proof = getattr(request.state, "member_entitlement_proof", None)
    if isinstance(proof, EntitlementProof):
        if proof.user_id != user["id"] or proof.auth_version != int(user.get("auth_version", 1)):
            raise HTTPException(503, "Üyelik sahipliği doğrulanamadı.")
        if proof.error:
            raise HTTPException(503, "Üyelik yetkisi doğrulanamadı.")
        if proof.expires_at is not None and proof.expires_at <= datetime.now(timezone.utc):
            raise HTTPException(403, "Binance üyelik süresi doldu.")
        return dict(proof.subscription)
    original = stripe_subscription(request.app.state.v22_commercial["state"], user["id"])
    if original["master_trade_access"] or user.get("role") == "OWNER" or not enabled():
        return original
    raise HTTPException(503, "Üyelik yetkisi doğrulanamadı.")


def access_for_request(request: Request, user: dict[str, Any]) -> dict[str, Any]:
    return access(subscription_for_request(request, user), user)


async def target_subscription(conn: Any, state: dict[str, Any], user_id: str) -> dict[str, Any]:
    if not enabled():
        return stripe_subscription(state, user_id)
    try:
        rows = await conn.fetch(
            """SELECT user_id,plan,status,current_period_start,current_period_end,trial_start,trial_end,
                 grace_until,updated_at,cancel_at_period_end FROM subscriptions WHERE user_id=$1""", user_id,
        )
    except (PostgresError, OSError, RuntimeError) as exc:
        logger.warning("Target entitlement lookup failed type=%s", type(exc).__name__)
        raise HTTPException(503, "Kullanıcı üyeliği doğrulanamadı.") from None
    canonical = {"subscriptions": [
        {key: value.isoformat() if isinstance(value, datetime) else value for key, value in dict(row).items()}
        for row in rows
    ]}
    return await resolve(conn, canonical, user_id)
