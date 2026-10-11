"""OWNER-only payment decisions, periods and audit in one transaction."""
import json
import logging
from contextlib import asynccontextmanager
from typing import Literal

from asyncpg import PostgresError
from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from . import binance_manual_payments as payments
from .audit_log import AuditActor, write_audit
from .member_entitlements import enabled
from .v22_commercial import authenticated_user, authenticated_user_async

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v22/admin/binance-payments", tags=["OWNER Binance payments"])


class DisabledPayments(BaseModel):
    enabled: Literal[False] = False
    message: Literal["Binance ödemeleri şu an kapalı"] = "Binance ödemeleri şu an kapalı"


def disabled_payments(request: Request) -> DisabledPayments | None:
    authenticated_user(request, owner=True)
    return None if enabled() else DisabledPayments()


class ApprovePayment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checked_in_binance: StrictBool

    @field_validator("checked_in_binance")
    @classmethod
    def must_confirm(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Binance hesabınızda ödemeyi kontrol ettiğinizi onaylayın.")
        return value


class RejectPayment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Red gerekçesi gerekli.")
        return value.strip()


@asynccontextmanager
async def owner_connection(request: Request):
    owner = await authenticated_user_async(request, owner=True)
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Binance ödeme kaydı kullanılamıyor.")
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(71011002)")
            row = await conn.fetchrow(
                "SELECT auth_version,security FROM commercial_auth_users WHERE user_id=$1 FOR SHARE", owner["id"],
            )
            if row is None or row["auth_version"] != owner["auth_version"]:
                raise HTTPException(401, "Oturum iptal edilmiş.")
            security = json.loads(row["security"]) if isinstance(row["security"], str) else row["security"]
            if security.get("role") != "OWNER" or security.get("active") is not True:
                raise HTTPException(403, "Bu işlem yalnız OWNER tarafından yapılabilir.")
            yield conn, AuditActor.from_request(request, owner)
    except HTTPException:
        raise
    except (PostgresError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("Binance OWNER operation failed type=%s", type(exc).__name__)
        raise HTTPException(503, "Binance ödeme işlemi kaydedilemedi.") from None


async def decision(conn, actor: AuditActor, request_id: str, status: Literal["approved", "rejected"],
                   *, reason: str | None = None) -> payments.PaymentRequest:
    if actor.role != "OWNER":
        raise HTTPException(403, "Bu işlem yalnız OWNER tarafından yapılabilir.")
    if not conn.is_in_transaction():
        raise RuntimeError("Payment decision must share the grant/audit transaction")
    if status == "rejected" and (not reason or not reason.strip() or len(reason) > 500):
        raise HTTPException(422, "Red gerekçesi gerekli.")
    row = await conn.fetchrow(
        "SELECT * FROM binance_manual_payment_requests WHERE id=$1 AND erased_at IS NULL FOR UPDATE", request_id,
    )
    if row is None:
        raise HTTPException(404, "Ödeme talebi bulunamadı.")
    if row["status"] == status:
        if status == "approved" and not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM binance_manual_periods WHERE payment_request_id=$1)", request_id,
        ):
            raise HTTPException(503, "Onaylı ödeme dönemi doğrulanamadı.")
        return payments.PaymentRequest.model_validate(dict(row))
    if row["status"] != "pending":
        raise HTTPException(409, "Yalnız bekleyen ödeme talebi karara bağlanabilir.")
    await payments.lock_user(conn, row["user_id"])
    period = None
    if status == "approved":
        await payments.ensure_no_paid_stripe(conn, row["user_id"])
        trial_end = await conn.fetchval(
            """SELECT MAX(COALESCE(trial_end,current_period_end)) FROM subscriptions WHERE user_id=$1
               AND plan='TRIAL' AND status='TRIALING'
               AND COALESCE(trial_end,current_period_end) > clock_timestamp()""", row["user_id"],
        )
        from .free_trial import enabled as trial_enabled
        if trial_enabled():
            free_end = await conn.fetchval(
                "SELECT ends_at FROM member_free_trials WHERE user_id=$1 AND revoked_at IS NULL AND ends_at>clock_timestamp()",
                row["user_id"],
            )
            if free_end is not None:
                trial_end = max(trial_end, free_end) if trial_end is not None else free_end
        manual_end = await conn.fetchval(
            """SELECT MAX(p.period_end) FROM binance_manual_periods p
               JOIN binance_manual_payment_requests r ON r.id=p.payment_request_id
               WHERE p.user_id=$1 AND r.user_id=$1 AND r.status='approved' AND r.erased_at IS NULL
                 AND p.revoked_at IS NULL""", row["user_id"],
        )
        now = await conn.fetchval("SELECT clock_timestamp()")
        period = payments.payment_period(now, trial_end=trial_end, manual_end=manual_end)
    changed = await conn.fetchrow(
        """UPDATE binance_manual_payment_requests SET status=$3,decided_by=$4,
             decided_at=clock_timestamp(),decision_note=$5,version=version+1
           WHERE id=$1 AND version=$2 AND status='pending' RETURNING *""",
        request_id, row["version"], status, actor.user_id, reason,
    )
    if changed is None:
        raise HTTPException(409, "Ödeme talebi başka bir işlemde değişti.")
    if period is not None:
        result = await conn.execute(
            """INSERT INTO binance_manual_periods (payment_request_id,user_id,period_start,period_end)
               VALUES ($1,$2,$3,$4)""", request_id, row["user_id"], *period,
        )
        if result != "INSERT 0 1":
            raise RuntimeError("Binance period did not persist")
    await write_audit(
        conn, actor, "payment.binance.approved" if status == "approved" else "payment.binance.rejected",
        "BINANCE_PAYMENT", request_id, {"status": "pending"}, {"status": status}, reason=reason,
    )
    return payments.PaymentRequest.model_validate(dict(changed))


@router.get("")
async def list_payments(request: Request, status: payments.Status | None = None,
                        limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0),
                        request_id: str | None = Query(None, pattern=r"^[a-f0-9]{32}$")):
    disabled = disabled_payments(request)
    if disabled is not None:
        return disabled
    async with owner_connection(request) as (conn, _):
        await payments.expire_due(conn)
        rows = await conn.fetch(
            """SELECT r.*,u.security->>'email' AS email,u.security->>'display_name' AS display_name
               FROM binance_manual_payment_requests r LEFT JOIN commercial_auth_users u ON u.user_id=r.user_id
               WHERE r.erased_at IS NULL AND ($1::text IS NULL OR r.status=$1)
                 AND ($4::text IS NULL OR r.id=$4)
               ORDER BY r.created_at DESC,r.id DESC LIMIT $2 OFFSET $3""", status, limit, offset, request_id,
        )
        total = await conn.fetchval(
            """SELECT COUNT(*) FROM binance_manual_payment_requests
               WHERE erased_at IS NULL AND ($1::text IS NULL OR status=$1)
                 AND ($2::text IS NULL OR id=$2)""", status, request_id,
        )
        summary = await conn.fetchrow(
            """SELECT COUNT(*) FILTER (WHERE status='pending') AS pending,
                 COUNT(*) FILTER (WHERE status='approved' AND decided_at >=
                   date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS approved_today,
                 COUNT(*) FILTER (WHERE status='rejected') AS rejected
               FROM binance_manual_payment_requests WHERE erased_at IS NULL""",
        )
        return {"items": [{**payments.PaymentRequest.model_validate(dict(row)).model_dump(mode="json"),
                           "email": row["email"], "display_name": row["display_name"]} for row in rows],
                "total": total, "limit": limit, "offset": offset, "summary": dict(summary)}


@router.post("/{request_id}/approve", response_model=payments.PaymentRequest | DisabledPayments)
async def approve(body: ApprovePayment, request: Request, request_id: str = Path(pattern=r"^[a-f0-9]{32}$")):
    disabled = disabled_payments(request)
    if disabled is not None:
        return disabled
    if not body.checked_in_binance:
        raise HTTPException(422, "Binance hesabınızda ödemeyi kontrol ettiğinizi onaylayın.")
    async with owner_connection(request) as (conn, actor):
        result = await decision(conn, actor, request_id, "approved")
    from .binance_notifications import wake
    wake(request.app)
    return result


@router.post("/{request_id}/reject", response_model=payments.PaymentRequest | DisabledPayments)
async def reject(body: RejectPayment, request: Request, request_id: str = Path(pattern=r"^[a-f0-9]{32}$")):
    disabled = disabled_payments(request)
    if disabled is not None:
        return disabled
    async with owner_connection(request) as (conn, actor):
        result = await decision(conn, actor, request_id, "rejected", reason=body.reason)
    from .binance_notifications import wake
    wake(request.app)
    return result
