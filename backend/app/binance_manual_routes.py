"""Authenticated manual-payment UI API; no approval or entitlement mutation."""
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from asyncpg import PostgresError
from fastapi import APIRouter, HTTPException, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import binance_manual_payments as payments
from .member_entitlements import enabled
from .v22_commercial import authenticated_user_async

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v22/payments/binance-manual", tags=["Manual Binance payments"])


class DisabledMethod(BaseModel):
    enabled: Literal[False] = False


class EnabledMethod(BaseModel):
    enabled: Literal[True] = True
    amount: Decimal
    currency: Literal["USDT"] = "USDT"
    pay_id: str


class PublicPayment(BaseModel):
    id: str
    reference_code: str
    amount: Decimal
    currency: Literal["USDT"]
    method: Literal["binance_manual"]
    status: payments.Status
    transaction_number: str | None
    expires_at: datetime


class PublicPeriod(BaseModel):
    period_start: datetime
    period_end: datetime


class CurrentPayment(BaseModel):
    request: PublicPayment | None
    period: PublicPeriod | None = None


class SubmitPayment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction_number: str = Field(min_length=1, max_length=128)


def method_configuration() -> EnabledMethod | DisabledMethod:
    pay_id = os.getenv("BINANCE_MANUAL_PAY_ID", "").strip()
    if not pay_id:
        return DisabledMethod()
    if len(pay_id) > 128 or not pay_id.isascii() or any(char.isspace() or ord(char) < 32 for char in pay_id):
        logger.warning("Manual Binance Pay ID configuration is invalid")
        raise HTTPException(503, "Binance ödeme yöntemi ayarı geçersiz.")
    return EnabledMethod(amount=payments.AMOUNT, pay_id=pay_id)


def require_enabled() -> None:
    if not method_configuration().enabled:
        raise HTTPException(503, "Binance ödeme yöntemi şu anda kullanılamıyor.")


@asynccontextmanager
async def connection(request: Request):
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Binance ödeme kaydı şu anda kullanılamıyor.")
    try:
        async with pool.acquire() as conn:
            yield conn
    except HTTPException:
        raise
    except (PostgresError, OSError, RuntimeError) as exc:
        logger.warning("Manual Binance payment connection failed type=%s", type(exc).__name__)
        raise HTTPException(503, "Binance ödeme kaydı şu anda kullanılamıyor.") from None


def public(row: payments.PaymentRequest) -> PublicPayment:
    return PublicPayment.model_validate(row.model_dump())


@router.get("/config", response_model=EnabledMethod | DisabledMethod)
async def configuration(request: Request, response: Response):
    await authenticated_user_async(request)
    response.headers["Cache-Control"] = "no-store"
    return method_configuration()


@router.get("/requests/current", response_model=CurrentPayment)
async def current(request: Request, response: Response):
    user = await authenticated_user_async(request)
    require_enabled()
    response.headers["Cache-Control"] = "no-store"
    async with connection(request) as conn:
        row = await payments.current_request(conn, user["id"])
        period = None
        if row is not None and row.status == "approved" and enabled():
            stored = await conn.fetchrow(
                """SELECT p.period_start,p.period_end FROM binance_manual_periods p
                   JOIN binance_manual_payment_requests r ON r.id=p.payment_request_id
                   WHERE p.payment_request_id=$1 AND p.user_id=$2 AND r.user_id=$2
                     AND r.status='approved' AND r.erased_at IS NULL AND p.revoked_at IS NULL""",
                row.id, user["id"],
            )
            if stored is None or not isinstance(stored["period_start"], datetime) or not isinstance(stored["period_end"], datetime) \
                    or stored["period_end"] - stored["period_start"] != timedelta(days=30):
                logger.warning("Approved Binance payment period failed display validation")
                raise HTTPException(503, "Onaylı ödeme dönemi doğrulanamadı.")
            period = PublicPeriod(**dict(stored))
    return CurrentPayment(request=public(row) if row is not None else None, period=period)


@router.post("/requests", response_model=PublicPayment)
async def create(request: Request, response: Response):
    user = await authenticated_user_async(request)
    require_enabled()
    response.headers["Cache-Control"] = "no-store"
    async with connection(request) as conn:
        return public(await payments.create_request(conn, user["id"]))


@router.post("/requests/{request_id}/submit", response_model=PublicPayment)
async def submit(body: SubmitPayment, request: Request, response: Response,
                 request_id: str = Path(pattern=r"^[a-f0-9]{32}$")):
    user = await authenticated_user_async(request)
    require_enabled()
    response.headers["Cache-Control"] = "no-store"
    async with connection(request) as conn:
        result = public(await payments.submit_transaction(conn, user["id"], request_id, body.transaction_number))
    from .binance_notifications import wake
    wake(request.app)
    return result
