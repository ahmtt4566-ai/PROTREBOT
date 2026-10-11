"""Unwired manual-payment storage. No routes, grants, Stripe calls or mail."""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol

from asyncpg import PostgresError, UniqueViolationError
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)
AMOUNT = Decimal("119.90")
REFERENCE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
TRANSACTION_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)
Status = Literal["created", "pending", "approved", "rejected", "expired"]


class PaymentConnection(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Any]: ...
    async def execute(self, query: str, *args: Any) -> str: ...
    async def fetchrow(self, query: str, *args: Any) -> Mapping[str, Any] | None: ...
    async def fetch(self, query: str, *args: Any) -> list[Mapping[str, Any]]: ...
    async def fetchval(self, query: str, *args: Any) -> Any: ...


class PaymentRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    user_id: str
    reference_code: str
    amount: Decimal
    currency: Literal["USDT"]
    method: Literal["binance_manual"]
    status: Status
    transaction_number: str | None
    created_at: datetime
    expires_at: datetime
    submitted_at: datetime | None
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None
    version: int


def new_reference() -> str:
    return "KT-" + "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(6))


def normalize_transaction(value: str) -> str:
    normalized = value.strip()
    if not TRANSACTION_PATTERN.fullmatch(normalized):
        raise HTTPException(422, "Binance işlem numarası 1–128 harf, rakam, tire veya alt çizgi içermeli.")
    return normalized


def transaction_fingerprint(value: str) -> str:
    return hashlib.sha256(("binance_manual:" + value).encode("ascii")).hexdigest()


def period_start(now: datetime, *, trial_end: datetime | None = None,
                 manual_end: datetime | None = None) -> datetime:
    dates = [now, *([trial_end] if trial_end is not None else []),
             *([manual_end] if manual_end is not None else [])]
    if any(date.tzinfo is None or date.utcoffset() is None for date in dates):
        raise ValueError("Payment period dates must be timezone-aware")
    return max(date.astimezone(timezone.utc) for date in dates)


def payment_period(now: datetime, *, trial_end: datetime | None = None,
                   manual_end: datetime | None = None) -> tuple[datetime, datetime]:
    start = period_start(now, trial_end=trial_end, manual_end=manual_end)
    return start, start + timedelta(days=30)


@asynccontextmanager
async def storage(conn: PaymentConnection):
    try:
        async with conn.transaction():
            yield
    except HTTPException:
        raise
    except (PostgresError, OSError, RuntimeError, ValidationError) as exc:
        logger.warning("Manual Binance payment storage failed type=%s", type(exc).__name__)
        raise HTTPException(503, "Binance ödeme kaydı şu anda kullanılamıyor.") from None


async def lock_user(conn: PaymentConnection, user_id: str) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 71011001))", user_id)
    valid = await conn.fetchval(
        """SELECT EXISTS (
             SELECT 1 FROM commercial_auth_users WHERE user_id = $1
               AND security->'active' = 'true'::jsonb AND security->'email_verified' = 'true'::jsonb
               AND NOT EXISTS (SELECT 1 FROM commercial_erased_users e WHERE e.user_hash =
                 encode(sha256(convert_to(user_id,'UTF8')),'hex'))
               FOR SHARE)""", user_id,
    )
    if not valid:
        raise HTTPException(403, "Aktif ve e-postası doğrulanmış kullanıcı gerekli.")


async def ensure_no_paid_stripe(conn: PaymentConnection, user_id: str) -> None:
    active = await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM subscriptions WHERE user_id = $1
           AND stripe_subscription_id IS NOT NULL AND plan = 'MASTER_MODE' AND status = 'ACTIVE'
           AND current_period_end > clock_timestamp())""", user_id,
    )
    if active:
        raise HTTPException(409, "Ücretli Stripe aboneliğiniz aktifken Binance ödeme talebi oluşturamazsınız.")


async def transition(conn: PaymentConnection, row: Mapping[str, Any], status: Literal["pending", "expired"],
                     *, transaction_number: str | None = None) -> PaymentRequest:
    """Only submission/expiry are enabled; audited approval and grants are step 3."""
    if row["status"] != "created":
        raise HTTPException(409, "Ödeme talebi bu durum geçişine uygun değil.")
    if status == "pending" and transaction_number is None:
        raise HTTPException(422, "Binance işlem numarası gerekli.")
    if status == "expired" and transaction_number is not None:
        raise HTTPException(422, "Süresi dolan talebe işlem numarası eklenemez.")
    changed = await conn.fetchrow(
        """UPDATE binance_manual_payment_requests SET status = $3,
             transaction_number = $4, transaction_fingerprint = $5,
             submitted_at = CASE WHEN $3 = 'pending' THEN clock_timestamp() ELSE NULL END,
             version = version + 1
           WHERE id = $1 AND version = $2 AND status = 'created'
             AND (($3 = 'pending' AND expires_at > clock_timestamp())
               OR ($3 = 'expired' AND expires_at <= clock_timestamp()))
           RETURNING *""", row["id"], row["version"], status, transaction_number,
        transaction_fingerprint(transaction_number) if transaction_number is not None else None,
    )
    if changed is None:
        raise HTTPException(409, "Ödeme talebi değişti veya işlem süresi doldu; yenileyin.")
    return PaymentRequest.model_validate(dict(changed))


async def expire_due(conn: PaymentConnection, *, user_id: str | None = None) -> int:
    """Bounded durable sweep; future lifecycle wiring will call this periodically."""
    async with storage(conn):
        rows = await conn.fetch(
            """SELECT * FROM binance_manual_payment_requests WHERE status = 'created'
               AND erased_at IS NULL AND expires_at <= clock_timestamp() AND ($1::text IS NULL OR user_id = $1)
               ORDER BY expires_at,id LIMIT 100 FOR UPDATE SKIP LOCKED""", user_id,
        )
        for row in rows:
            await transition(conn, row, "expired")
        return len(rows)


async def create_request(conn: PaymentConnection, user_id: str) -> PaymentRequest:
    async with storage(conn):
        await lock_user(conn, user_id)
        await ensure_no_paid_stripe(conn, user_id)
        await expire_due(conn, user_id=user_id)
        existing = await conn.fetchrow(
            """SELECT * FROM binance_manual_payment_requests WHERE user_id = $1
               AND status IN ('created','pending') FOR UPDATE""", user_id,
        )
        if existing is not None:
            return PaymentRequest.model_validate(dict(existing))
        for attempt in range(5):
            try:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        """INSERT INTO binance_manual_payment_requests (id,user_id,reference_code)
                           VALUES ($1,$2,$3) RETURNING *""", uuid.uuid4().hex, user_id, new_reference(),
                    )
                    if row is None:
                        raise RuntimeError("Payment insert did not return a row")
                    return PaymentRequest.model_validate(dict(row))
            except UniqueViolationError as exc:
                if getattr(exc, "constraint_name", None) != "binance_manual_reference_unique":
                    raise
                logger.warning("Manual Binance reference collision attempt=%s", attempt + 1)
        raise RuntimeError("Unique payment reference could not be generated")


async def current_request(conn: PaymentConnection, user_id: str) -> PaymentRequest | None:
    async with storage(conn):
        await lock_user(conn, user_id)
        await expire_due(conn, user_id=user_id)
        row = await conn.fetchrow(
            """SELECT * FROM binance_manual_payment_requests WHERE user_id = $1 AND erased_at IS NULL
               ORDER BY created_at DESC,id DESC LIMIT 1""", user_id,
        )
        return PaymentRequest.model_validate(dict(row)) if row is not None else None


async def submit_transaction(conn: PaymentConnection, user_id: str, request_id: str,
                             transaction_number: str) -> PaymentRequest:
    number = normalize_transaction(transaction_number)
    expired = False
    result = None
    async with storage(conn):
        await lock_user(conn, user_id)
        row = await conn.fetchrow(
            """SELECT * FROM binance_manual_payment_requests WHERE id = $1 AND user_id = $2
               AND erased_at IS NULL FOR UPDATE""", request_id, user_id,
        )
        if row is None:
            raise HTTPException(404, "Ödeme talebi bulunamadı.")
        if row["status"] == "pending" and row["transaction_number"] == number:
            return PaymentRequest.model_validate(dict(row))
        if row["status"] != "created":
            raise HTTPException(410 if row["status"] == "expired" else 409, "Ödeme talebi işlem numarası kabul etmiyor.")
        if await conn.fetchval("SELECT $1::timestamptz <= clock_timestamp()", row["expires_at"]):
            await transition(conn, row, "expired")
            expired = True
        else:
            await ensure_no_paid_stripe(conn, user_id)
            try:
                async with conn.transaction():
                    result = await transition(conn, row, "pending", transaction_number=number)
            except UniqueViolationError as exc:
                if getattr(exc, "constraint_name", None) not in (
                    "binance_manual_transaction_unique", "binance_manual_fingerprint_unique",
                ):
                    raise
                raise HTTPException(409, "Bu Binance işlem numarası başka bir talepte kullanılmış.") from None
    if expired:
        raise HTTPException(410, "48 saat içinde işlem numarası girilmedi; ödeme talebinin süresi doldu.")
    if result is None:
        raise RuntimeError("Payment submission did not produce a result")
    return result
