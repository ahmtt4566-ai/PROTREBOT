import hashlib
import json
import logging
import uuid
import os
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse

from .audit_log import AuditActor
from .campaign_content import CampaignDraft, ID, token_user, validated
from .campaign_service import (
    CampaignItem, SendCampaign, UpdateCampaign, audit, campaign_connection, campaign_row,
    cancel, create, direct_send, item, preview, request_send, update,
)
from .moderator_access import ModeratorIdentity, require_permission

router = APIRouter(tags=["Announcements"])
logger = logging.getLogger(__name__)


@router.get("/api/mod/campaigns")
async def list_campaigns(request: Request, identity: ModeratorIdentity = Depends(require_permission("campaigns.manage")),
                         limit: int = Query(25, ge=1, le=50), offset: int = Query(0, ge=0)):
    async with campaign_connection(request, identity) as (conn, actor):
        args = (actor.role == "OWNER", actor.user_id)
        total = await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE ($1 OR created_by=$2)", *args)
        rows = await conn.fetch(
            """SELECT * FROM campaigns WHERE ($1 OR created_by=$2) ORDER BY created_at DESC,id LIMIT $3 OFFSET $4""",
            *args, limit, offset,
        )
        return {"items": [item(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.post("/api/mod/campaigns", response_model=CampaignItem)
async def create_campaign(draft: CampaignDraft, request: Request,
                          identity: ModeratorIdentity = Depends(require_permission("campaigns.manage"))):
    try:
        async with campaign_connection(request, identity) as (conn, actor):
            return await create(conn, actor, draft)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/api/mod/campaigns/preview")
async def draft_preview(draft: CampaignDraft, request: Request,
                        identity: ModeratorIdentity = Depends(require_permission("campaigns.manage"))):
    try:
        values = validated(draft)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    async with campaign_connection(request, identity) as (conn, actor):
        from .campaign_content import email_text, unsubscribe_token, warnings
        from .campaign_service import audience
        users = await audience(conn, values["audience"])
        token = unsubscribe_token(request.app.state.v22_commercial["secret"], actor.user_id)
        return {"subject": values["subject"], "text": email_text(values, token),
                "audience": values["audience"],
                "content_hash": values["content_hash"], "recipient_count": len(users),
                "opt_out_count": sum(u["opt_out"] for u in users), "warnings": warnings(values),
                "approximate_schedule": values["scheduled_at"] is not None, "personalized_footer": True,
                "delivery_enabled": os.getenv("CAMPAIGN_EMAIL_ENABLED", "false").lower() in ("true", "1", "yes")}

@router.get("/api/mod/campaigns/{campaign_id}", response_model=CampaignItem)
async def get_campaign(request: Request, campaign_id: str = Path(pattern=ID),
                       identity: ModeratorIdentity = Depends(require_permission("campaigns.manage"))):
    async with campaign_connection(request, identity) as (conn, actor):
        return item(await campaign_row(conn, campaign_id, actor))


@router.put("/api/mod/campaigns/{campaign_id}", response_model=CampaignItem)
async def update_campaign(draft: UpdateCampaign, request: Request, campaign_id: str = Path(pattern=ID),
                          identity: ModeratorIdentity = Depends(require_permission("campaigns.manage"))):
    try:
        async with campaign_connection(request, identity) as (conn, actor):
            return await update(conn, actor, await campaign_row(conn, campaign_id, actor), draft)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/api/mod/campaigns/{campaign_id}/preview")
async def campaign_preview(request: Request, campaign_id: str = Path(pattern=ID),
                           identity: ModeratorIdentity = Depends(require_permission("campaigns.manage"))):
    async with campaign_connection(request, identity) as (conn, actor):
        return await preview(conn, request, await campaign_row(conn, campaign_id, actor), actor.user_id)


@router.post("/api/mod/campaigns/{campaign_id}/test")
async def test_campaign(request: Request, campaign_id: str = Path(pattern=ID),
                        identity: ModeratorIdentity = Depends(require_permission("campaigns.manage"))):
    async with campaign_connection(request, identity) as (conn, actor):
        row = await campaign_row(conn, campaign_id, actor)
        if row["status"] != "draft":
            raise HTTPException(409, "Deneme gönderimi yalnız taslak için kullanılabilir.")
        counter = await conn.fetchrow(
            """INSERT INTO campaign_test_limits(user_id,window_start,attempts) VALUES ($1,clock_timestamp(),1)
               ON CONFLICT(user_id) DO UPDATE SET
                attempts=CASE WHEN campaign_test_limits.window_start<=clock_timestamp()-INTERVAL '1 hour' THEN 1 ELSE campaign_test_limits.attempts+1 END,
                window_start=CASE WHEN campaign_test_limits.window_start<=clock_timestamp()-INTERVAL '1 hour' THEN clock_timestamp() ELSE campaign_test_limits.window_start END
               WHERE campaign_test_limits.attempts<5 OR campaign_test_limits.window_start<=clock_timestamp()-INTERVAL '1 hour'
               RETURNING attempts""", actor.user_id,
        )
        if counter is None:
            raise HTTPException(429, "Saatte en çok 5 deneme gönderimi yapılabilir.")
        outbox_id = uuid.uuid4().hex
        await conn.execute(
            """INSERT INTO notification_outbox(id,kind,priority,recipient_user_id,payload,dedupe_key)
               VALUES ($1,'campaign.info',10,$2,$3::jsonb,$4)""",
            outbox_id, actor.user_id, json.dumps({"campaign_id": row["id"], "test": 1}), "campaign-test:" + outbox_id,
        )
        await audit(conn, actor, row, "campaign.test_sent")
        return {"queued": True, "message": "Deneme yalnız kendi hesabınız için kuyruğa alındı."}


@router.post("/api/mod/campaigns/{campaign_id}/request-send", response_model=CampaignItem)
async def campaign_request_send(request: Request, campaign_id: str = Path(pattern=ID),
                                identity: ModeratorIdentity = Depends(require_permission("campaigns.manage"))):
    async with campaign_connection(request, identity) as (conn, actor):
        return await request_send(conn, actor, await campaign_row(conn, campaign_id, actor))


@router.post("/api/mod/campaigns/{campaign_id}/send", response_model=CampaignItem)
async def campaign_send(submission: SendCampaign, request: Request, campaign_id: str = Path(pattern=ID),
                        identity: ModeratorIdentity = Depends(require_permission("campaigns.manage"))):
    if identity.role != "OWNER":
        raise HTTPException(403, "Yalnız yönetici doğrudan gönderebilir.")
    async with campaign_connection(request, identity) as (conn, actor):
        return await direct_send(conn, actor, await campaign_row(conn, campaign_id, actor), submission)


@router.post("/api/mod/campaigns/{campaign_id}/cancel", response_model=CampaignItem)
async def campaign_cancel(request: Request, campaign_id: str = Path(pattern=ID),
                          identity: ModeratorIdentity = Depends(require_permission("campaigns.manage")),
                          withdraw: bool = False):
    async with campaign_connection(request, identity) as (conn, actor):
        return await cancel(conn, actor, await campaign_row(conn, campaign_id, actor), withdraw)


@router.get("/announcements/unsubscribe", response_class=HTMLResponse)
async def preference_page():
    # Identical page for all tokens; GET never reads or writes preferences.
    return """<!doctype html><html lang="tr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KaisTrade duyuru tercihi</title><body style="font-family:Arial;max-width:520px;margin:48px auto;padding:24px">
<h1>Duyuru tercihiniz</h1><p>Bu tercih yalnız bilgilendirme duyurularını etkiler. Hesap ve güvenlik e-postaları devam eder.</p>
<form method="post"><button name="action" value="unsubscribe">Bu tür duyuruları almak istemiyorum</button>
<button name="action" value="subscribe">Tekrar abone ol</button></form></body></html>"""


@router.post("/announcements/unsubscribe")
async def preference_change(request: Request, token: str = Query("", max_length=512)):
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Tercih servisi kullanılamıyor.")
    body = await request.body()
    if len(body) > 1024:
        raise HTTPException(413, "İstek çok büyük.")
    form = parse_qs(body.decode("utf-8", errors="replace"))
    action = form.get("action", ["unsubscribe"])[0]
    if action not in ("unsubscribe", "subscribe"):
        raise HTTPException(422, "Geçersiz tercih.")
    bucket = "campaign-unsubscribe:" + hashlib.sha256((request.client.host if request.client else "unknown").encode()).hexdigest()
    try:
        counter = await pool.fetchrow(
            """INSERT INTO commercial_auth_limits(bucket,window_start,attempts) VALUES ($1,clock_timestamp(),1)
               ON CONFLICT(bucket) DO UPDATE SET attempts=CASE WHEN commercial_auth_limits.window_start<=clock_timestamp()-INTERVAL '60 seconds'
               THEN 1 ELSE commercial_auth_limits.attempts+1 END,window_start=CASE WHEN commercial_auth_limits.window_start<=clock_timestamp()-INTERVAL '60 seconds'
               THEN clock_timestamp() ELSE commercial_auth_limits.window_start END RETURNING attempts""", bucket,
        )
        if counter is None:
            raise HTTPException(503, "Tercih servisi kullanılamıyor.")
        if counter["attempts"] > 30:
            raise HTTPException(429, "Bir dakika bekleyip yeniden deneyin.", headers={"Retry-After": "60"})
        uid = token_user(request.app.state.v22_commercial["secret"], token)
        if uid:
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('campaign-pref:' || $1,0))", uid)
                await conn.execute(
                    """INSERT INTO email_preferences(user_id,announcements_opt_out,updated_at)
                       SELECT user_id,$2,clock_timestamp() FROM commercial_auth_users WHERE user_id=$1
                       AND NOT EXISTS (SELECT 1 FROM commercial_erased_users WHERE user_hash =
                        encode(sha256(convert_to(user_id,'UTF8')),'hex'))
                       ON CONFLICT(user_id) DO UPDATE SET announcements_opt_out=$2,updated_at=clock_timestamp()""",
                    uid, action == "unsubscribe",
                )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Announcement preference failed (%s)", type(exc).__name__)
        raise HTTPException(503, "Tercih servisi kullanılamıyor.") from None
    return {"message": "İstek alındı. Geçerli bağlantı için duyuru tercihiniz güncellenir."}
