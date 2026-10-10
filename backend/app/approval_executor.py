"""Single-use canonical execution, followed by explicit, idempotent agent cleanup."""
import asyncio
import json
import logging

from fastapi import HTTPException

from . import v22_commercial as auth
from .account_settings import close_blocker
from .approval_service import (
    approval_item, approval_row, owner_connection, security, stored_snapshot, target_row,
    target_snapshot, transition,
)

logger = logging.getLogger(__name__)


async def requester_valid(conn, row):
    grant = await conn.fetchrow(
        "SELECT user_id FROM moderator_permissions WHERE user_id = $1 AND permission = $2 FOR SHARE",
        row["requester_user_id"], "approvals.create",
    )
    requester = await conn.fetchrow(
        """SELECT u.security->>'role' AS role, u.security->'active' = 'true'::jsonb AS active,
           EXISTS (SELECT 1 FROM moderator_permissions p WHERE p.user_id = u.user_id
                   AND p.permission = 'approvals.create') AS allowed
           FROM commercial_auth_users u WHERE u.user_id = $1 FOR SHARE OF u /* AS requester_allowed */""",
        row["requester_user_id"],
    )
    return grant is not None and requester is not None and requester["role"] == "MODERATOR" and requester["active"] is True and requester["allowed"] is True


async def canonical_execution(request, approval_id):
    """No runtime security is projected before the canonical transaction commits."""
    projection = None
    rt = auth.runtime(request)
    async with rt["lock"]:
        async with owner_connection(request) as (conn, actor):
            row = await approval_row(conn, approval_id)
            if row["status"] != "executing" or row["result_code"] is not None:
                raise HTTPException(409, "Approval cannot be executed again")
            if not await requester_valid(conn, row):
                row = await transition(conn, actor, row, "stale", "approval.stale", result_code="requester_changed")
            else:
                target = await target_row(conn, row["target_user_id"])
                if target is None or target_snapshot(target) != stored_snapshot(row) or target_snapshot(target).role != "CUSTOMER":
                    row = await transition(conn, actor, row, "stale", "approval.stale", result_code="target_changed")
                else:
                    current = target_snapshot(target)
                    desired = row["action_type"] == "account.reactivate"
                    blocker = None
                    if not desired:
                        try:
                            blocker = await close_blocker(request, {"id": row["target_user_id"], **security(target)})
                        except HTTPException as exc:
                            if exc.status_code != 503:
                                raise
                            row = await transition(conn, actor, row, "failed", "approval.failed", result_code="execution_failed")
                    if row["status"] == "executing":
                        if blocker:
                            row = await transition(conn, actor, row, "failed", "approval.failed", result_code="protected_positions")
                        else:
                            if current.active != desired:
                                # Direct SQL avoids the existing helper's pre-commit runtime mutation.
                                changed = await conn.fetchrow(
                                    """UPDATE commercial_auth_users
                                       SET auth_version = auth_version + 1, security = security || $2::jsonb
                                       WHERE user_id = $1 AND auth_version = $3 RETURNING auth_version, security""",
                                    row["target_user_id"], json.dumps({"active": desired}), current.auth_version,
                                )
                                if changed is None:
                                    raise HTTPException(409, "Target changed")
                                projection = (row["target_user_id"], changed)
                            row = await transition(conn, actor, row, "executing", "approval.executed",
                                                   result_code="canonical_applied", phase="canonical")
            result = approval_item(row)
        if projection:
            uid, canonical = projection
            local = next((u for u in rt["state"]["users"] if u["id"] == uid), None)
            if local is not None:
                local.update(security(canonical), auth_version=canonical["auth_version"])
                rt.setdefault("auth_baseline", {})[uid] = {"auth_version": local["auth_version"], **auth.auth_security(local)}
    return result


async def revoke_agents(request, user_id):
    """Gate the existing non-idempotent helper; always persist on conscious retry."""
    rt = auth.runtime(request)
    lock = rt.setdefault("approval_agent_lock", asyncio.Lock())
    async with lock:
        ids = [agent["id"] for agent in rt["state"]["agents"]
               if agent.get("user_id") == user_id and agent.get("status") == "ACTIVE"]
        for agent_id in ids:
            await auth.v22_revoke_agent(agent_id, auth.RevokeRequest(
                confirmation="AJAN İPTAL", reason="Onaylanan hesap pasifleştirme",
            ), request)
        async with rt["lock"]:
            auth.save_state(rt["state"])
        if not await auth.persist_v22_commercial(request.app):
            raise HTTPException(503, "Agent revocation persistence unavailable")


async def complete_execution(request, approval_id, *, agent_only=False):
    if agent_only:
        async with owner_connection(request) as (conn, _):
            row = await approval_row(conn, approval_id)
            if row["status"] != "executing" or row["result_code"] != "canonical_applied":
                raise HTTPException(409, "Agent cleanup already claimed")
            current = approval_item(row)
    else:
        current = await canonical_execution(request, approval_id)
    if current.status != "executing":
        return current
    failed = False
    if current.action_type == "account.deactivate":
        try:
            await revoke_agents(request, current.target_user_id)
        except Exception as exc:
            logger.warning("Approval agent cleanup failed (%s)", type(exc).__name__)
            failed = True
    async with owner_connection(request) as (conn, actor):
        row = await approval_row(conn, approval_id)
        if row["status"] != "executing" or row["result_code"] != "canonical_applied":
            raise HTTPException(409, "Approval changed")
        row = await transition(conn, actor, row, "failed" if failed else "executed",
                               "approval.failed" if failed else "approval.executed",
                               result_code="agents_revoke_pending" if failed else "ok",
                               phase=None if failed else "completed")
        return approval_item(row)
