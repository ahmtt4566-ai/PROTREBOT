from fastapi import APIRouter, HTTPException, Path, Query, Request

from .approval_executor import complete_execution, requester_valid
from .approval_service import (
    ApprovalDetail, ApprovalItem, ApprovalPage, ID, RejectApproval, Status, approval_item,
    approval_row, expire_pending, owner_connection, stored_snapshot, target_row, target_snapshot, transition,
)

router = APIRouter(prefix="/api/v22/admin/approvals", tags=["Owner approvals"])


@router.get("", response_model=ApprovalPage)
async def list_approvals(request: Request, status: Status | None = None,
                         limit: int = Query(25, ge=1, le=50), offset: int = Query(0, ge=0)):
    async with owner_connection(request) as (conn, actor):
        await expire_pending(conn, actor, owner=True)
        total = await conn.fetchval("SELECT COUNT(*) FROM approval_requests WHERE ($1::text IS NULL OR status = $1)", status)
        rows = await conn.fetch(
            """SELECT * FROM approval_requests WHERE ($1::text IS NULL OR status = $1)
               ORDER BY created_at DESC,id LIMIT $2 OFFSET $3""", status, limit, offset,
        )
        pending = await conn.fetchval("SELECT COUNT(*) FROM approval_requests WHERE status = 'pending'")
        return ApprovalPage(items=[approval_item(r) for r in rows], total=total, limit=limit, offset=offset, pending_count=pending)


@router.get("/summary")
async def approval_summary(request: Request):
    async with owner_connection(request) as (conn, actor):
        await expire_pending(conn, actor, owner=True)
        return {"pending_count": await conn.fetchval("SELECT COUNT(*) FROM approval_requests WHERE status = 'pending'")}


@router.get("/{approval_id}", response_model=ApprovalDetail)
async def approval_detail(request: Request, approval_id: str = Path(pattern=ID, min_length=1, max_length=160)):
    async with owner_connection(request) as (conn, actor):
        await expire_pending(conn, actor, owner=True)
        row = await approval_row(conn, approval_id)
        target = await target_row(conn, row["target_user_id"])
        current = target_snapshot(target) if target else None
        subscription = await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM subscriptions WHERE user_id = $1
               AND status IN ('ACTIVE','TRIALING') AND (current_period_end IS NULL OR current_period_end > clock_timestamp()))""",
            row["target_user_id"],
        )
        return ApprovalDetail(request=approval_item(row), current_target=current,
                              target_changed=current != stored_snapshot(row), active_subscription=subscription)


async def decision_check(conn, actor, row):
    if row["status"] != "pending":
        raise HTTPException(409, "Approval is not pending")
    if row["requester_user_id"] == actor.user_id:
        raise HTTPException(403, "Cannot decide your own request")
    if await conn.fetchval("SELECT $1::timestamptz <= clock_timestamp()", row["expires_at"]):
        return await transition(conn, actor, row, "expired", "approval.expired")
    return None


@router.post("/{approval_id}/reject", response_model=ApprovalItem)
async def reject_approval(payload: RejectApproval, request: Request,
                          approval_id: str = Path(pattern=ID, min_length=1, max_length=160)):
    async with owner_connection(request) as (conn, actor):
        row = await approval_row(conn, approval_id)
        expired = await decision_check(conn, actor, row)
        changed = expired or await transition(conn, actor, row, "rejected", "approval.rejected",
                                             decider=actor.user_id, note=payload.decision_note)
        result = approval_item(changed)
    if expired:
        raise HTTPException(409, "Approval expired")
    return result


@router.post("/{approval_id}/approve", response_model=ApprovalItem)
async def approve_approval(request: Request, approval_id: str = Path(pattern=ID, min_length=1, max_length=160)):
    error = None
    async with owner_connection(request) as (conn, actor):
        row = await approval_row(conn, approval_id)
        if await decision_check(conn, actor, row):
            error = "Approval expired"
        elif not await requester_valid(conn, row):
            await transition(conn, actor, row, "stale", "approval.stale", result_code="requester_changed")
            error = "Requester changed; review again"
        else:
            target = await target_row(conn, row["target_user_id"])
            if not target or target_snapshot(target) != stored_snapshot(row) or target_snapshot(target).role != "CUSTOMER":
                await transition(conn, actor, row, "stale", "approval.stale", result_code="target_changed")
                error = "Target changed; review again"
            else:
                row = await transition(conn, actor, row, "approved", "approval.approved", decider=actor.user_id)
                await transition(conn, actor, row, "executing")
    if error:
        raise HTTPException(409, error)
    result = await complete_execution(request, approval_id)
    if result.status == "stale":
        raise HTTPException(409, "Target or requester changed; review again")
    return result


@router.post("/{approval_id}/retry-agents", response_model=ApprovalItem)
async def retry_agent_cleanup(request: Request, approval_id: str = Path(pattern=ID, min_length=1, max_length=160)):
    async with owner_connection(request) as (conn, actor):
        row = await approval_row(conn, approval_id)
        if row["status"] != "failed" or row["result_code"] != "agents_revoke_pending" or row["action_type"] != "account.deactivate":
            raise HTTPException(409, "Only pending agent cleanup can be retried")
        await transition(conn, actor, row, "executing", "approval.approved", result_code="canonical_applied")
    return await complete_execution(request, approval_id, agent_only=True)
