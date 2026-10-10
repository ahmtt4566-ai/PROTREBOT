"""Atomic campaign authorization; never invoke the account-status executor."""
from fastapi import HTTPException

from .approval_executor import requester_valid
from .approval_service import transition
from .campaign_content import content_hash
from .campaign_service import audience, begin_send, campaign_row, payload, preview


async def valid_requester(conn, row):
    if not await requester_valid(conn, row):
        return False
    return await conn.fetchrow(
        "SELECT user_id FROM moderator_permissions WHERE user_id=$1 AND permission='campaigns.manage' FOR SHARE",
        row["requester_user_id"],
    ) is not None


async def detail(conn, request, row, actor):
    data = payload(row)
    campaign = await campaign_row(conn, data["campaign_id"])
    changed = campaign["content_hash"] != data["content_hash"] or content_hash(dict(campaign)) != data["content_hash"] or campaign["approval_request_id"] != row["id"]
    return {"request": row, "campaign_preview": await preview(conn, request, campaign, actor.user_id),
            "target_changed": changed, "current_target": None, "active_subscription": False}


async def approve(conn, actor, row):
    if not await valid_requester(conn, row):
        return await transition(conn, actor, row, "stale", "approval.stale", result_code="requester_changed")
    data = payload(row)
    campaign = await campaign_row(conn, data["campaign_id"])
    if campaign["status"] != "pending_approval" or campaign["approval_request_id"] != row["id"] or campaign["content_hash"] != data["content_hash"] or content_hash(dict(campaign)) != data["content_hash"]:
        return await transition(conn, actor, row, "stale", "approval.stale", result_code="campaign_changed")
    row = await transition(conn, actor, row, "approved", "approval.approved", decider=actor.user_id)
    row = await transition(conn, actor, row, "executing")
    await begin_send(conn, actor, campaign, await audience(conn, campaign["audience"]))
    return await transition(conn, actor, row, "executed", "approval.executed", result_code="ok", phase="completed")
