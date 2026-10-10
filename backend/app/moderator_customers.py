"""Permission-gated customer summaries from existing PostgreSQL stores.

Only GET routes are exposed. Registration time alone comes from the persisted
commercial snapshot; security and billing fields never use worker snapshots.
Search is exact user ID or case-insensitive full email equality, never LIKE.
No customer/commerce/exchange writes or provider calls belong in this module.
"""
import logging
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .email_service import masked_recipient
from .moderator_access import ModeratorIdentity, require_permission
from .audit_log import AuditAction, AuditActor, write_audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mod/customers", tags=["Moderator customer reads"])


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomerSummary(ReadModel):
    user_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
    email_masked: str = Field(max_length=184, pattern=r"^(?:\*{3}|[A-Za-z0-9*]\*{3}@[A-Za-z0-9.-]+)$")
    role: Literal["CUSTOMER"]
    active: bool = Field(strict=True)
    created_at: datetime | None
    email_verified: bool = Field(strict=True)
    mfa_enabled: bool = Field(strict=True)


class CustomerPage(ReadModel):
    items: list[CustomerSummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=50)
    offset: int = Field(ge=0)


class CustomerSubscription(ReadModel):
    user_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
    plan: Literal["TRIAL", "MASTER_MODE"] | None
    subscription_status: Literal["TRIALING", "ACTIVE", "PAST_DUE", "UNPAID", "CANCELLED", "EXPIRED"] | None
    current_period_end: datetime | None
    cancel_at_period_end: bool | None = Field(strict=True)


class CustomerPayments(ReadModel):
    user_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
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
    COALESCE(u.security->'email_verified' = 'true'::jsonb, FALSE) AS email_verified,
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
@asynccontextmanager
async def customer_read_connection(request: Request, identity: ModeratorIdentity, permission: str):
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Customer read storage unavailable")
    try:
        bucket = "moderator-customer-read:" + hashlib.sha256(identity.user_id.encode("utf-8")).hexdigest()
        counter = await pool.fetchrow(
            """INSERT INTO commercial_auth_limits (bucket, window_start, attempts)
               VALUES ($1, clock_timestamp(), 1)
               ON CONFLICT (bucket) DO UPDATE SET
                   attempts = CASE WHEN commercial_auth_limits.window_start <= clock_timestamp() - INTERVAL '60 seconds'
                                   THEN 1 ELSE commercial_auth_limits.attempts + 1 END,
                   window_start = CASE WHEN commercial_auth_limits.window_start <= clock_timestamp() - INTERVAL '60 seconds'
                                       THEN clock_timestamp() ELSE commercial_auth_limits.window_start END
               RETURNING attempts""",
            bucket,
        )
        if counter is None:
            raise HTTPException(503, "Customer read rate-limit storage unavailable")
        if int(counter["attempts"]) > 30:
            raise HTTPException(429, "Customer read rate limit exceeded", headers={"Retry-After": "60"})
        async with pool.acquire() as conn, conn.transaction():
            actor = await conn.fetchrow(
                """SELECT u.auth_version, u.security->>'role' AS role,
                          COALESCE(u.security->'active' = 'true'::jsonb, FALSE) AS active,
                          COALESCE(u.security->'email_verified' = 'true'::jsonb, FALSE) AS email_verified,
                          COALESCE(a.payload->'two_factor_enabled' = 'true'::jsonb, FALSE) AS mfa_enabled,
                          EXISTS (SELECT 1 FROM moderator_permissions p
                                  WHERE p.user_id = u.user_id AND p.permission = $2) AS allowed
                   FROM commercial_auth_users u
                   LEFT JOIN commercial_account_settings a ON a.user_id = u.user_id
                   WHERE u.user_id = $1""",
                identity.user_id, permission,
            )
            if not actor or actor["auth_version"] != identity.auth_version or actor["active"] is not True:
                raise HTTPException(401, "Session revoked")
            if actor["role"] != "OWNER":
                if actor["role"] != "MODERATOR":
                    raise HTTPException(403, "Moderator access required")
                if actor["email_verified"] is not True:
                    raise HTTPException(403, "Verified email required")
                if actor["mfa_enabled"] is not True:
                    raise HTTPException(403, {"code": "mfa_required"})
                if actor["allowed"] is not True:
                    raise HTTPException(403, "Permission required")
            request.state.mod_read_actor = AuditActor.from_request(
                request, {"id": identity.user_id, "role": actor["role"]},
            )
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


async def audit_customer_read(conn, request: Request, user_id: str, action: AuditAction):
    await write_audit(conn, request.state.mod_read_actor, action, "USER", user_id, {}, {})


@router.get("", response_model=CustomerPage)
async def customers(
    request: Request,
    identity: ModeratorIdentity = Depends(require_permission("customers.view")),
    limit: int = Query(25, ge=1, le=50),
    offset: int = Query(0, ge=0),
    search: str | None = Query(None, min_length=1, max_length=180),
):
    query = search.strip() if search is not None else None
    async with customer_read_connection(request, identity, "customers.view") as conn:
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
    user_id: str = Path(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$"),
    identity: ModeratorIdentity = Depends(require_permission("customers.view")),
):
    async with customer_read_connection(request, identity, "customers.view") as conn:
        result = customer_summary(await customer_row(conn, identity, user_id))
        await audit_customer_read(conn, request, user_id, "customer.viewed")
        return result


@router.get("/{user_id}/subscription", response_model=CustomerSubscription)
async def customer_subscription(
    request: Request,
    user_id: str = Path(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$"),
    identity: ModeratorIdentity = Depends(require_permission("subscriptions.view")),
):
    async with customer_read_connection(request, identity, "subscriptions.view") as conn:
        await customer_row(conn, identity, user_id)
        row = await conn.fetchrow(
            """SELECT plan, status AS subscription_status, current_period_end, cancel_at_period_end
               FROM subscriptions WHERE user_id = $1 ORDER BY updated_at DESC, id DESC LIMIT 1""",
            user_id,
        )
        result = CustomerSubscription(
            user_id=user_id,
            plan=row["plan"] if row else None,
            subscription_status=row["subscription_status"] if row else None,
            current_period_end=row["current_period_end"] if row else None,
            cancel_at_period_end=row["cancel_at_period_end"] if row else None,
        )
        await audit_customer_read(conn, request, user_id, "customer.subscription.viewed")
        return result


@router.get("/{user_id}/payments", response_model=CustomerPayments)
async def customer_payments(
    request: Request,
    user_id: str = Path(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$"),
    identity: ModeratorIdentity = Depends(require_permission("payments.view")),
):
    async with customer_read_connection(request, identity, "payments.view") as conn:
        await customer_row(conn, identity, user_id)
        row = await conn.fetchrow(
            """SELECT last_payment_status, last_payment_at FROM subscriptions
               WHERE user_id = $1 ORDER BY updated_at DESC, id DESC LIMIT 1""",
            user_id,
        )
        status = row["last_payment_status"] if row else None
        result = CustomerPayments(
            user_id=user_id, payment_status=status if status is not None else "veri yok",
            last_failed_payment_at=row["last_payment_at"] if row and status == "FAILED" else None,
        )
        await audit_customer_read(conn, request, user_id, "customer.payments.viewed")
        return result
