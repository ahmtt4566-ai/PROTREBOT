"""Canonical announcement lifecycle; all mail addresses stay inside delivery."""
import hashlib
import json
import uuid
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .audit_log import AuditActor, write_audit
from .campaign_content import CampaignDraft, content_hash, email_text, unsubscribe_token, validated, warnings
from .moderator_customers import customer_read_connection
from .subscription_core import entitlement_snapshot

CampaignStatus = Literal["draft", "pending_approval", "sending", "sent", "cancelled", "failed"]


class CampaignItem(BaseModel):
    id: str
    title: str
    subject: str
    body: str
    kind: Literal["info"]
    audience: Literal["all_users", "premium_users", "team_only"]
    status: CampaignStatus
    created_by: str
    created_by_role: Literal["OWNER", "MODERATOR"]
    content_hash: str
    scheduled_at: datetime | None
    approval_request_id: str | None
    recipient_count: int
    queued_count: int
    sent_count: int
    failed_count: int
    skipped_count: int
    version: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class SendCampaign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=16, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
    confirmed_recipient_count: int = Field(ge=0, strict=True)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class UpdateCampaign(CampaignDraft):
    version: int = Field(ge=1, strict=True)


def item(row) -> CampaignItem:
    return CampaignItem(**{name: row[name] for name in CampaignItem.model_fields})


def payload(row) -> dict:
    value = row["payload"]
    return json.loads(value) if isinstance(value, str) else value


@asynccontextmanager
async def campaign_connection(request, identity):
    async with customer_read_connection(request, identity, "campaigns.manage") as conn:
        # Match approval lock order before taking any campaign row lock.
        await conn.execute("SELECT pg_advisory_xact_lock(71010006)")
        yield conn, request.state.mod_read_actor
    from .notification_worker import schedule_sweep
    schedule_sweep(request.app)


async def campaign_row(conn, campaign_id, actor=None):
    row = await conn.fetchrow("SELECT * FROM campaigns WHERE id=$1 FOR UPDATE", campaign_id)
    if row is None or actor is not None and actor.role != "OWNER" and row["created_by"] != actor.user_id:
        raise HTTPException(404, "Duyuru bulunamadı.")
    return row


async def audit(conn, actor, row, action):
    await write_audit(conn, actor, action, "CAMPAIGN", row["id"], {}, {})


async def audience(conn, group: str):
    users = await conn.fetch(
        """SELECT u.user_id,u.security->>'role' AS role,
             COALESCE(p.announcements_opt_out,FALSE) AS opt_out
           FROM commercial_auth_users u LEFT JOIN email_preferences p ON p.user_id=u.user_id
           WHERE u.security->'active'='true'::jsonb AND u.security->'email_verified'='true'::jsonb
             AND NOT EXISTS (SELECT 1 FROM commercial_erased_users e WHERE e.user_hash =
               encode(sha256(convert_to(u.user_id,'UTF8')),'hex'))
           ORDER BY u.user_id FOR SHARE OF u""",
    )
    if group == "team_only":
        return [u for u in users if u["role"] in ("OWNER", "MODERATOR")]
    if group == "premium_users":
        rows = await conn.fetch(
            """SELECT user_id,plan,status,current_period_end,trial_end,grace_until,updated_at
               FROM subscriptions WHERE user_id=ANY($1::text[]) FOR SHARE""", [u["user_id"] for u in users],
        )
        # Reuse the canonical entitlement decision, including trial and past-due grace.
        state = {"subscriptions": [
            {key: value.isoformat() if isinstance(value, datetime) else value for key, value in dict(r).items()}
            for r in rows
        ]}
        return [u for u in users if entitlement_snapshot(state, u["user_id"])["master_trade_access"]]
    if group != "all_users":
        raise ValueError("Unsupported audience")
    return users


async def preview(conn, request: Request, row, uid):
    users = await audience(conn, row["audience"])
    token = unsubscribe_token(request.app.state.v22_commercial["secret"], uid,
                              int(row["started_at"].timestamp()) if row["started_at"] else None)
    return {"campaign": item(row), "subject": row["subject"], "text": email_text(dict(row), token),
            "audience": row["audience"],
            "content_hash": row["content_hash"],
            "recipient_count": len(users), "opt_out_count": sum(u["opt_out"] for u in users),
            "warnings": warnings(dict(row)),
            "approximate_schedule": row["scheduled_at"] is not None,
            "personalized_footer": True,
            "delivery_enabled": os.getenv("CAMPAIGN_EMAIL_ENABLED", "false").lower() in ("true", "1", "yes")}


async def create(conn, actor, draft):
    values = validated(draft)
    row = await conn.fetchrow(
        """INSERT INTO campaigns(id,title,subject,body,audience,created_by,created_by_role,content_hash,scheduled_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *""",
        uuid.uuid4().hex, values["title"], values["subject"], values["body"], values["audience"],
        actor.user_id, actor.role, values["content_hash"], values["scheduled_at"],
    )
    await audit(conn, actor, row, "campaign.created")
    return item(row)


async def update(conn, actor, row, draft):
    if row["status"] != "draft" or row["version"] != draft.version:
        raise HTTPException(409, "Önce talebi geri çekin ve taslağı yenileyin.")
    values = validated(draft)
    changed = await conn.fetchrow(
        """UPDATE campaigns SET title=$2,subject=$3,body=$4,audience=$5,scheduled_at=$6,content_hash=$7,
           version=version+1,updated_at=clock_timestamp() WHERE id=$1 RETURNING *""",
        row["id"], values["title"], values["subject"], values["body"], values["audience"], values["scheduled_at"], values["content_hash"],
    )
    await audit(conn, actor, changed, "campaign.updated")
    return item(changed)


async def begin_send(conn, actor, row, users, send_key_hash=None):
    if row["status"] not in ("draft", "pending_approval"):
        raise HTTPException(409, "Duyuru zaten başlatıldı veya iptal edildi.")
    changed = await conn.fetchrow(
        """UPDATE campaigns SET status='sending',send_key_hash=$2,started_at=clock_timestamp(),
           updated_at=clock_timestamp(),version=version+1 WHERE id=$1 RETURNING *""", row["id"], send_key_hash,
    )
    for user in users:
        outbox_id = None if user["opt_out"] else uuid.uuid4().hex
        await conn.execute(
            """INSERT INTO campaign_recipients(campaign_id,user_id,status,skip_reason,outbox_id)
               VALUES ($1,$2,$3,$4,$5)""",
            row["id"], user["user_id"], "skipped" if user["opt_out"] else "queued",
            "opt_out" if user["opt_out"] else None, outbox_id,
        )
        if outbox_id:
            await conn.execute(
                """INSERT INTO notification_outbox(id,kind,priority,recipient_user_id,payload,dedupe_key,next_attempt_at)
                   VALUES ($1,'campaign.info',10,$2,$3::jsonb,$4,COALESCE($5,clock_timestamp()))""",
                outbox_id, user["user_id"], json.dumps({"campaign_id": row["id"]}),
                f"campaign:{row['id']}:{user['user_id']}", row["scheduled_at"],
            )
    await audit(conn, actor, changed, "campaign.send_started")
    return await refresh_counts(conn, changed, actor)


async def refresh_counts(conn, row, actor=None):
    counts = await conn.fetchrow(
        """SELECT COUNT(*)::integer AS recipient,COUNT(*) FILTER (WHERE status='queued')::integer AS queued,
           COUNT(*) FILTER (WHERE status='sent')::integer AS sent,COUNT(*) FILTER (WHERE status='failed')::integer AS failed,
           COUNT(*) FILTER (WHERE status='skipped')::integer AS skipped FROM campaign_recipients WHERE campaign_id=$1""", row["id"],
    )
    final = row["status"] == "sending" and counts["queued"] == 0
    status = ("failed" if counts["failed"] else "sent") if final else row["status"]
    if row["status"] in ("sent", "failed", "cancelled"):
        return row
    changed = await conn.fetchrow(
        """UPDATE campaigns SET recipient_count=$2,queued_count=$3,sent_count=$4,failed_count=$5,skipped_count=$6,
           status=$7,finished_at=CASE WHEN $8 THEN clock_timestamp() ELSE finished_at END,
           updated_at=clock_timestamp(),version=version+1 WHERE id=$1 RETURNING *""",
        row["id"], counts["recipient"], counts["queued"], counts["sent"], counts["failed"], counts["skipped"], status, final,
    )
    if final:
        if actor is None:
            actor = AuditActor(row["created_by"], row["created_by_role"], str(uuid.uuid4()), None)
        await audit(conn, actor, changed, "campaign.completed")
    return changed


async def direct_send(conn, actor, row, submission):
    if actor.role != "OWNER":
        raise HTTPException(403, "Yalnız yönetici doğrudan gönderebilir.")
    key = hashlib.sha256(submission.idempotency_key.encode()).hexdigest()
    if row["send_key_hash"] == key and row["status"] in ("sending", "sent", "failed"):
        return item(row)
    if row["status"] != "draft" or row["content_hash"] != submission.content_hash or content_hash(dict(row)) != row["content_hash"]:
        raise HTTPException(409, "İçerik değişti veya gönderim başladı; yeniden önizleyin.")
    users = await audience(conn, row["audience"])
    if len(users) != submission.confirmed_recipient_count:
        raise HTTPException(409, "Alıcı sayısı değişti; yeniden önizleyin.")
    return item(await begin_send(conn, actor, row, users, key))


async def request_send(conn, actor, row):
    from .approval_service import audit_approval
    from .notification_outbox import enqueue_digest
    if actor.role != "MODERATOR":
        raise HTTPException(403, "Yönetici doğrudan gönderim ekranını kullanmalı.")
    if row["status"] != "draft":
        raise HTTPException(409, "Taslak zaten onayda veya gönderimde.")
    await conn.execute("SELECT pg_advisory_xact_lock(71010006)")
    grant = await conn.fetchrow(
        "SELECT user_id FROM moderator_permissions WHERE user_id=$1 AND permission='approvals.create' FOR SHARE", actor.user_id,
    )
    if grant is None:
        raise HTTPException(403, "Onay talebi oluşturma izni gerekli.")
    counts = await conn.fetchrow(
        """SELECT COUNT(*) FILTER (WHERE status='pending') AS pending,
           COUNT(*) FILTER (WHERE created_at >= date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS today
           FROM approval_requests WHERE requester_user_id=$1""", actor.user_id,
    )
    if counts["pending"] >= 5 or counts["today"] >= 20:
        raise HTTPException(429, "Onay talebi sınırına ulaşıldı.")
    approval = await conn.fetchrow(
        """INSERT INTO approval_requests(id,action_type,target_user_id,requester_user_id,requester_role,payload,target_snapshot,reason,expires_at)
           VALUES ($1,'campaign.send',NULL,$2,'MODERATOR',$3::jsonb,'{}'::jsonb,$4,clock_timestamp()+INTERVAL '72 hours') RETURNING *""",
        uuid.uuid4().hex, actor.user_id, json.dumps({"campaign_id": row["id"], "content_hash": row["content_hash"]}),
        "Bilgilendirme duyurusu gönderim talebi",
    )
    changed = await conn.fetchrow(
        """UPDATE campaigns SET status='pending_approval',approval_request_id=$2,version=version+1,
           updated_at=clock_timestamp() WHERE id=$1 RETURNING *""", row["id"], approval["id"],
    )
    await audit_approval(conn, actor, approval, "approval.requested")
    await audit(conn, actor, changed, "campaign.send_requested")
    await enqueue_digest(conn)
    return item(changed)


async def cancel(conn, actor, row, withdraw=False):
    from .approval_service import approval_row, transition
    if row["status"] not in ("draft", "pending_approval", "sending"):
        raise HTTPException(409, "Duyuru artık değiştirilemez.")
    if row["status"] == "sending" and actor.role != "OWNER":
        raise HTTPException(403, "Gönderimi yalnız yönetici iptal edebilir.")
    if row["status"] == "pending_approval":
        approval = await approval_row(conn, row["approval_request_id"])
        if approval["status"] == "pending":
            await transition(conn, actor, approval, "cancelled", "approval.cancelled")
        elif approval["status"] not in ("rejected", "expired", "stale", "cancelled"):
            raise HTTPException(409, "Onay işlemi sürüyor; yenileyin.")
    if withdraw and row["status"] != "pending_approval":
        raise HTTPException(409, "Yalnız onaydaki taslak geri çekilebilir.")
    await conn.execute(
        """UPDATE notification_outbox SET status='dead',last_error_code='campaign_cancelled'
           WHERE kind='campaign.info' AND payload->>'campaign_id'=$1 AND status IN ('pending','failed')""", row["id"],
    )
    await conn.execute(
        """UPDATE campaign_recipients SET status='skipped',skip_reason='cancelled'
           WHERE campaign_id=$1 AND status='queued'""", row["id"],
    )
    row = await refresh_counts(conn, row, actor) if row["status"] != "sending" else row
    changed = await conn.fetchrow(
        """UPDATE campaigns SET status=$2,approval_request_id=CASE WHEN $2='draft' THEN NULL ELSE approval_request_id END,
           queued_count=0,skipped_count=(SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id=$1 AND status='skipped'),
           version=version+1,updated_at=clock_timestamp(),
           finished_at=CASE WHEN $2='cancelled' THEN clock_timestamp() ELSE NULL END WHERE id=$1 RETURNING *""",
        row["id"], "draft" if withdraw else "cancelled",
    )
    await audit(conn, actor, changed, "campaign.cancelled")
    return item(changed)
