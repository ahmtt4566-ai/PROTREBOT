"""Append-only administrative evidence, independent of erasable snapshots.

Install 20261010_002_audit_log.sql separately before enabling audited writes.
write_audit requires the mutation's active connection/transaction. JSON fields
are action-allowlisted and value-validated, not general payload logging.
Free-form reasons are stored as [REDACTED]; request IDs must be UUIDs.
Peer IPs use /24 (IPv4) or /48 (IPv6); forwarded headers are not trusted here.
GET /api/v22/admin/audit is OWNER-only, ordered by created_at/id descending.
Filters: actor, target, action, created_from/created_to (timezone-aware ISO).
Pagination: limit 1..100, offset >= 0. No update/delete endpoint exists.
"""
import ipaddress
import json
import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, get_args

from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Administrative audit"])

AuditAction = Literal[
    "ROLE_CHANGED", "PERMISSION_GRANTED", "PERMISSION_REVOKED",
    "customer.viewed", "customer.subscription.viewed", "customer.payments.viewed",
    "support.case.viewed", "support.case.taken", "support.case.released",
    "support.case.status_changed", "support.note.added",
    "approval.requested", "approval.approved", "approval.rejected", "approval.cancelled",
    "approval.expired", "approval.stale", "approval.executed", "approval.failed",
]
AUDIT_ACTIONS: tuple[str, ...] = get_args(AuditAction)
ACTION_FIELDS = {
    "ROLE_CHANGED": frozenset({"role"}),
    "PERMISSION_GRANTED": frozenset({"permission", "granted"}),
    "PERMISSION_REVOKED": frozenset({"permission", "granted"}),
    "customer.viewed": frozenset(),
    "customer.subscription.viewed": frozenset(),
    "customer.payments.viewed": frozenset(),
    "support.case.viewed": frozenset(),
    "support.case.taken": frozenset(),
    "support.case.released": frozenset(),
    "support.case.status_changed": frozenset(),
    "support.note.added": frozenset(),
    "approval.requested": frozenset(),
    "approval.approved": frozenset(),
    "approval.rejected": frozenset(),
    "approval.cancelled": frozenset(),
    "approval.expired": frozenset(),
    "approval.stale": frozenset(),
    "approval.executed": frozenset({"phase"}),
    "approval.failed": frozenset(),
}


class AuditConnection(Protocol):
    def is_in_transaction(self) -> bool: ...

    async def execute(self, query: str, *args: Any) -> str: ...


@dataclass(frozen=True)
class AuditActor:
    user_id: str
    role: str
    request_id: str
    ip_masked: str | None = None

    @classmethod
    def from_request(cls, request: Request, user: Mapping[str, Any]) -> "AuditActor":
        request_id = getattr(request.state, "request_id", None)
        try:
            request_id = str(uuid.UUID(str(request_id)))
        except ValueError as exc:
            if request_id is not None:
                logger.warning("Audit request ID normalized (%s)", type(exc).__name__)
            request_id = str(uuid.uuid4())
        masked = None
        if request.client:
            try:
                address = ipaddress.ip_address(request.client.host)
                masked = str(ipaddress.ip_network(
                    f"{address}/{24 if address.version == 4 else 48}", strict=False,
                ))
            except ValueError as exc:
                logger.warning("Audit peer address unavailable (%s)", type(exc).__name__)
        return cls(user["id"], user["role"], request_id, masked)


def opaque_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise ValueError("Audit identity must be an opaque ID")
    return value


def allowed_snapshot(action: AuditAction, value: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value[key] for key in ACTION_FIELDS[action] if key in value}
    if result.keys() != ACTION_FIELDS[action]:
        raise ValueError("Missing audit snapshot fields")
    if action == "ROLE_CHANGED":
        if result["role"] not in ("OWNER", "MODERATOR", "CUSTOMER"):
            raise ValueError("Invalid audit role")
    elif action in ("PERMISSION_GRANTED", "PERMISSION_REVOKED"):
        from .moderator_access import PERMISSIONS
        if result["permission"] not in PERMISSIONS or type(result["granted"]) is not bool:
            raise ValueError("Invalid audit permission state")
    elif action == "approval.executed":
        if result["phase"] not in ("canonical", "completed"):
            raise ValueError("Invalid approval execution phase")
    return result


async def write_audit(
    conn: AuditConnection, actor: AuditActor, action: AuditAction,
    target_type: str, target_id: str, before: Mapping[str, Any], after: Mapping[str, Any],
    reason: str | None = None, approval_request_id: str | None = None,
) -> None:
    if action not in AUDIT_ACTIONS:
        raise ValueError("Unsupported audit action")
    if not conn.is_in_transaction():
        raise RuntimeError("Audit must share the mutation transaction")
    if actor.role not in ("OWNER", "MODERATOR", "CUSTOMER"):
        raise ValueError("Invalid audit actor role")
    expected_target = "MODERATOR_PERMISSION" if action in (
        "PERMISSION_GRANTED", "PERMISSION_REVOKED",
    ) else "SUPPORT_CASE" if action.startswith("support.") else "APPROVAL_REQUEST" if action.startswith("approval.") else "USER"
    if target_type != expected_target:
        raise ValueError("Invalid audit target type")
    request_id = str(uuid.UUID(actor.request_id))
    if actor.ip_masked is not None:
        network = ipaddress.ip_network(actor.ip_masked)
        if network.prefixlen != (24 if network.version == 4 else 48):
            raise ValueError("Audit IP must be masked")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("Invalid audit reason")
    values = (
        opaque_id(actor.user_id), actor.role, action, target_type, opaque_id(target_id), request_id,
        opaque_id(approval_request_id) if approval_request_id is not None else None,
        "[REDACTED]" if reason else None,
        json.dumps(allowed_snapshot(action, before)),
        json.dumps(allowed_snapshot(action, after)), actor.ip_masked,
    )
    try:
        result = await conn.execute(
            """INSERT INTO audit_log
               (actor_user_id, actor_role, action, target_type, target_id, request_id,
                approval_request_id, reason, "before", "after", ip_masked)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11)""",
            *values,
        )
        if result != "INSERT 0 1":
            raise RuntimeError("Audit insert did not persist")
    except Exception as exc:
        logger.warning("Audit write failed (%s)", type(exc).__name__)
        raise HTTPException(503, "Audit storage unavailable") from None


@router.get("/api/v22/admin/audit")
async def read_audit(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    actor: str | None = Query(None, min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$"),
    target: str | None = Query(None, min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$"),
    action: AuditAction | None = Query(None),
    created_from: datetime | None = Query(None),
    created_to: datetime | None = Query(None),
):
    from . import v22_commercial as auth

    await auth.authenticated_user_async(request, owner=True)
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Audit storage unavailable")
    if any(value is not None and value.utcoffset() is None for value in (created_from, created_to)):
        raise HTTPException(422, "Audit date filters require a timezone")
    if created_from is not None and created_to is not None and created_from > created_to:
        raise HTTPException(422, "Audit date range is reversed")
    clauses = []
    args = []
    for column, value, operator in (
        ("actor_user_id", actor, "="), ("target_id", target, "="), ("action", action, "="),
        ("created_at", created_from, ">="), ("created_at", created_to, "<="),
    ):
        if value is not None:
            args.append(value.astimezone(timezone.utc) if isinstance(value, datetime) else value)
            clauses.append(f"{column} {operator} ${len(args)}")
    where = " AND ".join(clauses) or "TRUE"
    try:
        total = await pool.fetchval(f"SELECT COUNT(*) FROM audit_log WHERE {where}", *args)
        rows = await pool.fetch(
            f"""SELECT id, created_at, actor_user_id, actor_role, action, target_type,
                       target_id, request_id, approval_request_id, reason, "before", "after", ip_masked
                FROM audit_log WHERE {where} ORDER BY created_at DESC, id DESC
                LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}""",
            *args, limit, offset,
        )
        items = []
        for row in rows:
            item = dict(row)
            for field in ("before", "after"):
                if isinstance(item[field], str):
                    item[field] = json.loads(item[field])
            items.append(item)
        return {"items": items, "total": int(total), "limit": limit, "offset": offset}
    except Exception as exc:
        logger.warning("Audit read failed (%s)", type(exc).__name__)
        raise HTTPException(503, "Audit storage unavailable") from None
