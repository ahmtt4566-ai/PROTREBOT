"""Permission-gated customer summaries from existing PostgreSQL stores.

Only GET routes are exposed. Registration time alone comes from the persisted
commercial snapshot; security and billing fields never use worker snapshots.
Search is exact user ID or case-insensitive full email equality, never LIKE.
No customer/commerce/exchange writes or provider calls belong in this module.
"""
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .email_service import masked_recipient
from .moderator_access import ModeratorIdentity, require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mod/customers", tags=["Moderator customer reads"])


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomerSummary(ReadModel):
    user_id: str
    email_masked: str
    role: Literal["CUSTOMER"]
    active: bool = Field(strict=True)
    created_at: datetime | None
    email_verified: bool = Field(strict=True)
    mfa_enabled: bool = Field(strict=True)


class CustomerPage(ReadModel):
    items: list[CustomerSummary]
    total: int
    limit: int
    offset: int


class CustomerSubscription(ReadModel):
    user_id: str
    plan: Literal["TRIAL", "MASTER_MODE"] | None
    subscription_status: Literal["TRIALING", "ACTIVE", "PAST_DUE", "UNPAID", "CANCELLED", "EXPIRED"] | None
    current_period_end: datetime | None
    cancel_at_period_end: bool | None = Field(strict=True)


class CustomerPayments(ReadModel):
    user_id: str
    payment_status: Literal["PAID", "FAILED", "veri yok"]
    last_failed_payment_at: datetime | None


CUSTOMER_WHERE = """
    u.security->>'role' = 'CUSTOMER'
    AND u.user_id <> $1
    AND NOT EXISTS (
        SELECT 1 FROM commercial_erased_users e
        WHERE e.user_hash = encode(sha256(convert_to(u.user_id, 'UTF8')), 'hex')
    )
"""
PROFILE_COLUMNS = """
    u.user_id, u.security->>'email' AS email, u.security->>'role' AS role,
    COALESCE(u.security->'active' = 'true'::jsonb, FALSE) AS active,
    registration.created_at,
    COALESCE(u.security->'email_verified' = 'true'::jsonb, FALSE) AS email_verified,
    COALESCE(a.payload->'two_factor_enabled' = 'true'::jsonb, FALSE) AS mfa_enabled
"""
PROFILE_FROM = """
    FROM commercial_auth_users u
    LEFT JOIN commercial_account_settings a ON a.user_id = u.user_id
    LEFT JOIN LATERAL (
        SELECT (entry->>'created_at')::timestamptz AS created_at
        FROM application_state_snapshots s,
             jsonb_array_elements(COALESCE(s.payload->'users', '[]'::jsonb)) entry
        WHERE s.state_key = 'v22-commercial' AND entry->>'id' = u.user_id
        LIMIT 1
    ) registration ON TRUE
"""
SEARCH_WHERE = """
    AND ($2::text IS NULL OR u.user_id = $2 OR lower(u.security->>'email') = lower($2))
"""
CustomerId = str


@asynccontextmanager
async def customer_read_connection(request: Request):
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Customer read storage unavailable")
    try:
        async with pool.acquire() as conn, conn.transaction():
            yield conn
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Moderator customer read failed (%s)", type(exc).__name__)
        raise HTTPException(503, "Customer read storage unavailable") from None


def customer_summary(row) -> CustomerSummary:
    return CustomerSummary(
        user_id=row["user_id"], email_masked=masked_recipient(row["email"] or ""),
        role=row["role"], active=row["active"], created_at=row["created_at"],
        email_verified=row["email_verified"], mfa_enabled=row["mfa_enabled"],
    )


async def customer_row(conn, identity: ModeratorIdentity, user_id: str):
    row = await conn.fetchrow(
        f"""SELECT {PROFILE_COLUMNS} {PROFILE_FROM}
            WHERE {CUSTOMER_WHERE} AND u.user_id = $2 FOR SHARE OF u""",
        identity.user_id, user_id,
    )
    if row is None:
        raise HTTPException(404, "Customer not found")
    return row


@router.get("", response_model=CustomerPage)
async def customers(
    request: Request,
    identity: ModeratorIdentity = Depends(require_permission("customers.view")),
    limit: int = Query(25, ge=1, le=50),
    offset: int = Query(0, ge=0),
    search: str | None = Query(None, min_length=1, max_length=180),
):
    query = search.strip() if search is not None else None
    async with customer_read_connection(request) as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM commercial_auth_users u WHERE {CUSTOMER_WHERE} {SEARCH_WHERE}",
            identity.user_id, query,
        )
        rows = await conn.fetch(
            f"""SELECT {PROFILE_COLUMNS} {PROFILE_FROM} WHERE {CUSTOMER_WHERE} {SEARCH_WHERE}
                ORDER BY u.user_id LIMIT $3 OFFSET $4""",
            identity.user_id, query, limit, offset,
        )
        return CustomerPage(items=[customer_summary(row) for row in rows],
                            total=total, limit=limit, offset=offset)


@router.get("/{user_id}", response_model=CustomerSummary)
async def customer(
    request: Request,
    user_id: CustomerId = Path(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$"),
    identity: ModeratorIdentity = Depends(require_permission("customers.view")),
):
    async with customer_read_connection(request) as conn:
        return customer_summary(await customer_row(conn, identity, user_id))


@router.get("/{user_id}/subscription", response_model=CustomerSubscription)
async def customer_subscription(
    request: Request,
    user_id: CustomerId = Path(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$"),
    identity: ModeratorIdentity = Depends(require_permission("subscriptions.view")),
):
    async with customer_read_connection(request) as conn:
        await customer_row(conn, identity, user_id)
        row = await conn.fetchrow(
            """SELECT plan, status AS subscription_status, current_period_end, cancel_at_period_end
               FROM subscriptions WHERE user_id = $1 ORDER BY updated_at DESC, id DESC LIMIT 1""",
            user_id,
        )
        return CustomerSubscription(
            user_id=user_id,
            plan=row["plan"] if row else None,
            subscription_status=row["subscription_status"] if row else None,
            current_period_end=row["current_period_end"] if row else None,
            cancel_at_period_end=row["cancel_at_period_end"] if row else None,
        )


@router.get("/{user_id}/payments", response_model=CustomerPayments)
async def customer_payments(
    request: Request,
    user_id: CustomerId = Path(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$"),
    identity: ModeratorIdentity = Depends(require_permission("payments.view")),
):
    async with customer_read_connection(request) as conn:
        await customer_row(conn, identity, user_id)
        row = await conn.fetchrow(
            """SELECT last_payment_status, last_payment_at FROM subscriptions
               WHERE user_id = $1 ORDER BY updated_at DESC, id DESC LIMIT 1""",
            user_id,
        )
        status = row["last_payment_status"] if row else None
        return CustomerPayments(
            user_id=user_id, payment_status=status if status is not None else "veri yok",
            last_failed_payment_at=row["last_payment_at"] if row and status == "FAILED" else None,
        )
