"""Canonical approval storage; no account changes happen on request creation."""
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import v22_commercial as auth
from .audit_log import AuditActor, write_audit
from .moderator_access import ModeratorIdentity
from .moderator_customers import customer_read_connection
from .moderator_support import mask_support_text
from .notification_outbox import enqueue_decision

logger = logging.getLogger(__name__)
Action = Literal["account.deactivate", "account.reactivate"]
Status = Literal["pending", "approved", "rejected", "cancelled", "expired", "stale", "executing", "executed", "failed"]
ResultCode = Literal["ok", "already_target_state", "canonical_applied", "protected_positions",
                     "agents_revoke_pending", "execution_failed", "target_changed", "requester_changed"]
ID = r"^[A-Za-z0-9_-]+$"


class CreateApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_type: Action
    target_user_id: str = Field(min_length=1, max_length=160, pattern=ID)
    reason: str = Field(min_length=10, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value):
        if len(value.strip()) < 10:
            raise ValueError("Reason requires 10 characters")
        return value


class RejectApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_note: str = Field(min_length=1, max_length=500)

    @field_validator("decision_note")
    @classmethod
    def note_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Decision note is required")
        return value


class TargetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool = Field(strict=True)
    role: Literal["OWNER", "MODERATOR", "CUSTOMER"]
    auth_version: int = Field(ge=1, strict=True)


class ApprovalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    action_type: Action
    target_user_id: str
    requester_user_id: str
    requester_role: Literal["MODERATOR"]
    target_snapshot: TargetSnapshot
    reason: str
    status: Status
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None
    executed_at: datetime | None
    result_code: ResultCode | None
    expires_at: datetime
    version: int
    created_at: datetime
    needs_review: bool


class ApprovalPage(BaseModel):
    items: list[ApprovalItem]
    total: int
    limit: int
    offset: int
    pending_count: int


class ApprovalDetail(BaseModel):
    request: ApprovalItem
    current_target: TargetSnapshot | None
    target_changed: bool
    active_subscription: bool


def security(row):
    value = row["security"]
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, dict):
        raise ValueError("Invalid canonical security")
    return value


def target_snapshot(row) -> TargetSnapshot:
    value = security(row)
    return TargetSnapshot(active=value["active"], role=value["role"], auth_version=row["auth_version"])


def stored_snapshot(row) -> TargetSnapshot:
    value = row["target_snapshot"]
    return TargetSnapshot.model_validate(json.loads(value) if isinstance(value, str) else value)


def approval_item(row) -> ApprovalItem:
    fields = {name: row[name] for name in ApprovalItem.model_fields if name not in ("needs_review", "target_snapshot")}
    fields["reason"] = mask_support_text(fields["reason"])
    if fields["decision_note"] is not None:
        fields["decision_note"] = mask_support_text(fields["decision_note"])
    return ApprovalItem(**fields, target_snapshot=stored_snapshot(row),
                        needs_review=row["status"] == "executing" or row["result_code"] == "agents_revoke_pending")


async def target_row(conn, user_id):
    return await conn.fetchrow(
        """SELECT user_id, auth_version, security FROM commercial_auth_users
           WHERE user_id = $1 AND NOT EXISTS (SELECT 1 FROM commercial_erased_users e
             WHERE e.user_hash = encode(sha256(convert_to(user_id,'UTF8')),'hex'))
           FOR UPDATE""", user_id,
    )


async def approval_row(conn, approval_id):
    row = await conn.fetchrow("SELECT * FROM approval_requests WHERE id = $1 FOR UPDATE", approval_id)
    if row is None:
        raise HTTPException(404, "Approval not found")
    return row


async def audit_approval(conn, actor, row, action, *, phase=None):
    fields = {"phase": phase} if phase is not None else {}
    await write_audit(conn, actor, action, "APPROVAL_REQUEST", row["id"], fields, fields,
                      approval_request_id=row["id"])


async def transition(conn, actor, row, status, action=None, *, result_code=None, decider=None, note=None, phase=None):
    changed = await conn.fetchrow(
        """UPDATE approval_requests SET status = $2, decided_by = $3,
           decided_at = CASE WHEN $3::text IS NOT NULL THEN COALESCE(decided_at,clock_timestamp()) ELSE decided_at END,
           decision_note = $4, result_code = $5,
           executed_at = CASE WHEN $2 = 'executed' THEN clock_timestamp() ELSE executed_at END,
           version = version + 1 WHERE id = $1 AND version = $6 RETURNING *""",
        row["id"], status, decider if decider is not None else row["decided_by"],
        note if note is not None else row["decision_note"], result_code, row["version"],
    )
    if changed is None:
        raise HTTPException(409, "Approval changed")
    if action:
        await audit_approval(conn, actor, changed, action, phase=phase)
    await enqueue_decision(conn, changed)
    return changed


async def expire_pending(conn, actor, *, owner=False, target=None):
    rows = await conn.fetch(
        """SELECT * FROM approval_requests WHERE status = 'pending' AND expires_at <= clock_timestamp()
           AND ($1::boolean OR requester_user_id = $2 OR target_user_id = $3) ORDER BY id FOR UPDATE""",
        owner, actor.user_id, target,
    )
    for row in rows:
        await transition(conn, actor, row, "expired", "approval.expired")


@asynccontextmanager
async def mod_connection(request, identity: ModeratorIdentity):
    if identity.role != "MODERATOR":
        raise HTTPException(403, "Only moderators may request approvals")
    async with customer_read_connection(request, identity, "approvals.create") as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(71010006)")
        yield conn, request.state.mod_read_actor
    from .notification_worker import schedule_sweep
    schedule_sweep(request.app)


@asynccontextmanager
async def owner_connection(request: Request):
    owner = await auth.authenticated_user_async(request, owner=True)
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Approval storage unavailable")
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(71010006)")
            row = await conn.fetchrow(
                "SELECT auth_version, security FROM commercial_auth_users WHERE user_id = $1",
                owner["id"],
            )
            if not row or row["auth_version"] != owner["auth_version"]:
                raise HTTPException(401, "Session revoked")
            value = security(row)
            if value.get("role") != "OWNER" or value.get("active") is not True:
                raise HTTPException(403, "Owner required")
            actor = AuditActor.from_request(request, {"id": owner["id"], "role": "OWNER"})
            yield conn, actor
        from .notification_worker import schedule_sweep
        schedule_sweep(request.app)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Approval operation failed (%s)", type(exc).__name__)
        raise HTTPException(503, "Approval storage unavailable") from None
