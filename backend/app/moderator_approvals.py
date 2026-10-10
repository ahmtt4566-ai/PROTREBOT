import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from .approval_service import (
    ApprovalItem, ApprovalPage, CreateApproval, ID, Status, approval_item, approval_row,
    audit_approval, expire_pending, mod_connection, target_row, target_snapshot, transition,
)
from .moderator_access import ModeratorIdentity, require_permission
from .notification_outbox import enqueue_digest

router = APIRouter(prefix="/api/mod/approvals", tags=["Moderator approvals"])


@router.post("", response_model=ApprovalItem)
async def create_approval(payload: CreateApproval, request: Request,
                          identity: ModeratorIdentity = Depends(require_permission("approvals.create"))):
    async with mod_connection(request, identity) as (conn, actor):
        await expire_pending(conn, actor, target=payload.target_user_id)
        target = await target_row(conn, payload.target_user_id)
        if target is None or target_snapshot(target).role != "CUSTOMER" or payload.target_user_id == identity.user_id:
            raise HTTPException(404, "Customer not found")
        snapshot = target_snapshot(target)
        desired = payload.action_type == "account.reactivate"
        if snapshot.active == desired:
            raise HTTPException(409, "Account already active" if desired else "Account already inactive")
        pending = await conn.fetchval(
            "SELECT COUNT(*) FROM approval_requests WHERE target_user_id = $1 AND status = 'pending'", payload.target_user_id,
        )
        if pending:
            raise HTTPException(409, "Target already has a pending approval")
        counts = await conn.fetchrow(
            """SELECT COUNT(*) FILTER (WHERE status = 'pending') AS pending,
               COUNT(*) FILTER (WHERE created_at >= date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS today
               FROM approval_requests WHERE requester_user_id = $1""", identity.user_id,
        )
        if counts["pending"] >= 5 or counts["today"] >= 20:
            raise HTTPException(429, "Approval request limit reached")
        row = await conn.fetchrow(
            """INSERT INTO approval_requests (id,action_type,target_user_id,requester_user_id,requester_role,
               target_snapshot,reason,expires_at) VALUES ($1,$2,$3,$4,'MODERATOR',$5::jsonb,$6,clock_timestamp()+INTERVAL '72 hours')
               RETURNING *""", uuid.uuid4().hex, payload.action_type, payload.target_user_id,
            identity.user_id, json.dumps(snapshot.model_dump()), payload.reason,
        )
        result = approval_item(row)
        await audit_approval(conn, actor, row, "approval.requested")
        await enqueue_digest(conn)
        return result


@router.get("", response_model=ApprovalPage)
async def own_approvals(request: Request, identity: ModeratorIdentity = Depends(require_permission("approvals.create")),
                        status: Status | None = None, limit: int = Query(25, ge=1, le=50), offset: int = Query(0, ge=0)):
    async with mod_connection(request, identity) as (conn, actor):
        await expire_pending(conn, actor)
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM approval_requests WHERE requester_user_id = $1 AND ($2::text IS NULL OR status = $2)",
            identity.user_id, status,
        )
        rows = await conn.fetch(
            """SELECT * FROM approval_requests WHERE requester_user_id = $1 AND ($2::text IS NULL OR status = $2)
               ORDER BY created_at DESC,id LIMIT $3 OFFSET $4""", identity.user_id, status, limit, offset,
        )
        pending = await conn.fetchval(
            "SELECT COUNT(*) FROM approval_requests WHERE requester_user_id = $1 AND status = 'pending'", identity.user_id,
        )
        return ApprovalPage(items=[approval_item(row) for row in rows], total=total, limit=limit, offset=offset, pending_count=pending)


@router.post("/{approval_id}/cancel", response_model=ApprovalItem)
async def cancel_approval(request: Request, approval_id: str = Path(pattern=ID, min_length=1, max_length=160),
                          identity: ModeratorIdentity = Depends(require_permission("approvals.create"))):
    expired = False
    async with mod_connection(request, identity) as (conn, actor):
        row = await approval_row(conn, approval_id)
        if row["requester_user_id"] != identity.user_id:
            raise HTTPException(404, "Approval not found")
        if row["status"] != "pending":
            raise HTTPException(409, "Approval is not pending")
        expired = await conn.fetchval("SELECT $1::timestamptz <= clock_timestamp()", row["expires_at"])
        changed = await transition(conn, actor, row, "expired" if expired else "cancelled",
                                   "approval.expired" if expired else "approval.cancelled")
        result = approval_item(changed)
    if expired:
        raise HTTPException(409, "Approval expired")
    return result
