"""Source-backed payment notifications; no provider work in payment transactions."""
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from asyncpg import PostgresError
from fastapi import HTTPException

from . import binance_manual_payments as payments
from .v22_commercial import authenticated_user_async

logger = logging.getLogger(__name__)
EMAIL = "payment.binance.email"
BELL = "payment.binance.bell"
EVENTS = {"pending", "approved", "rejected", "renewal"}
INTERVAL_SECONDS = 60


def enabled():
    value = os.getenv("BINANCE_MANUAL_NOTIFICATIONS_ENABLED", "false").strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError("Invalid Binance notification configuration")
    return value in {"true", "1"}


def key(payment_id, event, recipient, channel):
    return f"payment.binance:{payment_id}:{event}:{recipient}:{channel}"


async def enqueue(conn, payment_id, event, recipient, channel):
    if event not in EVENTS or channel not in {"email", "bell"}:
        raise ValueError("Invalid payment notification")
    await conn.execute(
        """INSERT INTO notification_outbox(id,kind,recipient_user_id,payload,dedupe_key)
           VALUES ($1,$2,$3,$4::jsonb,$5) ON CONFLICT (dedupe_key) DO NOTHING""",
        uuid.uuid4().hex, EMAIL if channel == "email" else BELL, recipient,
        json.dumps({"payment_id": payment_id, "event": event}), key(payment_id, event, recipient, channel),
    )


async def reconcile(conn):
    await payments.expire_due(conn)
    # Only missing recipient/channel pairs are scanned, so old payments cannot starve newer ones.
    rows = await conn.fetch(
        """WITH sources AS (
           SELECT r.id AS payment_id,'pending'::text AS event,u.user_id AS recipient
             FROM binance_manual_payment_requests r CROSS JOIN commercial_auth_users u
             WHERE r.status='pending' AND r.erased_at IS NULL AND u.security->>'role'='OWNER'
               AND u.security->'active'='true'::jsonb AND u.security->'email_verified'='true'::jsonb
           UNION ALL
           SELECT r.id,r.status,r.user_id FROM binance_manual_payment_requests r
             WHERE r.status IN ('approved','rejected') AND r.erased_at IS NULL
           UNION ALL
           SELECT r.id,'renewal',r.user_id FROM binance_manual_payment_requests r
             JOIN binance_manual_periods p ON p.payment_request_id=r.id AND p.user_id=r.user_id
             WHERE r.status='approved' AND r.erased_at IS NULL AND p.revoked_at IS NULL
               AND p.period_end > clock_timestamp() AND p.period_end <= clock_timestamp()+INTERVAL '72 hours'
               AND NOT EXISTS (SELECT 1 FROM binance_manual_periods later
                 WHERE later.user_id=p.user_id AND later.revoked_at IS NULL AND later.period_end>p.period_end)
           )
           SELECT s.*,c.channel FROM sources s CROSS JOIN (VALUES ('bell'),('email')) AS c(channel)
             JOIN commercial_auth_users u ON u.user_id=s.recipient
             WHERE u.security->'active'='true'::jsonb AND u.security->'email_verified'='true'::jsonb
               AND NOT EXISTS (SELECT 1 FROM commercial_erased_users e WHERE e.user_hash=
                 encode(sha256(convert_to(s.recipient,'UTF8')),'hex'))
               AND NOT EXISTS (SELECT 1 FROM notification_outbox o WHERE o.dedupe_key=
                 'payment.binance:' || s.payment_id || ':' || s.event || ':' || s.recipient || ':' || c.channel)
             ORDER BY s.payment_id,s.event,s.recipient,c.channel LIMIT 100""",
    )
    for row in rows:
        await enqueue(conn, row["payment_id"], row["event"], row["recipient"], row["channel"])
    unresolved = await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM binance_manual_payment_requests WHERE status='pending' AND erased_at IS NULL)
           AND NOT EXISTS (SELECT 1 FROM commercial_auth_users WHERE security->>'role'='OWNER'
             AND security->'active'='true'::jsonb AND security->'email_verified'='true'::jsonb)""",
    )
    if unresolved:
        logger.warning("Binance pending payments have no eligible OWNER; durable requests retained for next sweep")


async def sweep(application):
    if not enabled():
        return
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        logger.warning("Binance lifecycle unavailable code=storage_unavailable")
        return
    try:
        async with pool.acquire() as conn, conn.transaction():
            await reconcile(conn)
    except (PostgresError, OSError, RuntimeError, HTTPException) as exc:
        logger.warning("Binance lifecycle failed type=%s; durable sources retained for retry", type(exc).__name__)


def wake(application):
    from .notification_worker import schedule_sweep
    try:
        if enabled():
            schedule_sweep(application)
    except Exception as exc:  # noqa: BLE001 -- Scheduling cannot fail a committed payment.
        logger.warning("Binance notification scheduling failed type=%s; periodic reconciliation retained", type(exc).__name__)


async def content(conn, recipient, payload):
    if not isinstance(payload, dict) or set(payload) != {"payment_id", "event"} or payload["event"] not in EVENTS:
        raise ValueError("Invalid payment notification payload")
    row = await conn.fetchrow(
        """SELECT r.*,p.period_start,p.period_end,u.security->>'email' AS customer_email,
             u.security->>'display_name' AS customer_name
           FROM binance_manual_payment_requests r
           JOIN commercial_auth_users u ON u.user_id=r.user_id
           LEFT JOIN binance_manual_periods p ON p.payment_request_id=r.id AND p.user_id=r.user_id AND p.revoked_at IS NULL
           WHERE r.id=$1 AND r.erased_at IS NULL AND u.security->'active'='true'::jsonb
             AND u.security->'email_verified'='true'::jsonb
             AND NOT EXISTS (SELECT 1 FROM commercial_erased_users e WHERE e.user_hash=
               encode(sha256(convert_to(r.user_id,'UTF8')),'hex'))
           FOR SHARE OF r,u""", payload["payment_id"],
    )
    event = payload["event"]
    if row is None or row["status"] != ("approved" if event == "renewal" else event):
        return None
    if event != "pending" and row["user_id"] != recipient:
        return None
    if event == "pending":
        owner = await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM commercial_auth_users WHERE user_id=$1 AND security->>'role'='OWNER'
               AND security->'active'='true'::jsonb AND security->'email_verified'='true'::jsonb)""", recipient,
        )
        if not owner:
            return None
    if str(row["amount"]) != "119.90" or row["currency"] != "USDT" or row["method"] != "binance_manual":
        raise ValueError("Invalid payment notification source")
    if event in {"approved", "renewal"}:
        start, end = row["period_start"], row["period_end"]
        if not isinstance(start, datetime) or not isinstance(end, datetime) or end-start != timedelta(days=30):
            raise ValueError("Invalid payment notification period")
    if event == "renewal":
        eligible = await conn.fetchval(
            """SELECT $2::timestamptz > clock_timestamp() AND $2 <= clock_timestamp()+INTERVAL '72 hours'
               AND NOT EXISTS (SELECT 1 FROM binance_manual_periods WHERE user_id=$1 AND revoked_at IS NULL
                 AND period_end > $2)""", recipient, row["period_end"],
        )
        if not eligible:
            return None
    return {**dict(row), "event": event}


def presentation(row):
    event = row["event"]
    reference = row["reference_code"]
    end = row["period_end"].astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC") if row.get("period_end") else None
    if event == "pending":
        return ("info", "Binance ödeme talebi",
                (f"{row.get('customer_name') or row.get('customer_email') or row['user_id']} · "
                 f"119.90 USDT · {reference}. Ödeme incelemenizi bekliyor."),
                f"/admin?section=binance-payments&payment={row['id']}")
    if event == "approved":
        return ("success", "Binance ödemen onaylandı",
                f"Ödemen onaylandı, MASTER MODE {end} tarihine kadar aktif. Referans: {reference}.", "/billing")
    if event == "rejected":
        reason = row["decision_note"]
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Missing rejection reason")
        return ("warning", "Binance ödemen reddedildi", f"Ödemen reddedildi. Gerekçe: {reason} Referans: {reference}.", "/billing")
    if event == "renewal":
        return ("warning", "MASTER MODE yenileme hatırlatması",
                f"MASTER MODE erişimin {end} tarihinde sona eriyor. Yenilemek için Binance ile ödeme yapabilirsin. Referans: {reference}.",
                "/billing")
    raise ValueError("Unknown payment event")


async def bell_items(conn, user_id, limit):
    rows = await conn.fetch(
        """SELECT o.id,o.payload,o.sent_at,(r.outbox_id IS NOT NULL) AS read
           FROM notification_outbox o LEFT JOIN binance_notification_reads r ON r.outbox_id=o.id
           WHERE o.kind='payment.binance.bell' AND o.status='sent' AND o.recipient_user_id=$1
           ORDER BY o.sent_at DESC,o.id DESC LIMIT $2""", user_id, limit,
    )
    result = []
    for row in rows:
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        source = await content(conn, user_id, payload)
        if source is None:
            continue
        severity, title, message, target = presentation(source)
        result.append({"id": "binance:"+row["id"], "type": "BINANCE_"+payload["event"].upper(), "severity": severity,
                       "title": title, "message": message, "target": target, "timestamp": row["sent_at"].isoformat(), "read": row["read"]})
    return result


async def merge_bell(request, legacy, limit=100, *, read_id=None, read_all=False):
    if not enabled():
        if read_id:
            raise HTTPException(404, "Bildirim bulunamadı.")
        return legacy
    user = await authenticated_user_async(request)
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Ödeme bildirimleri okunamadı.")
    try:
        async with pool.acquire() as conn, conn.transaction():
            items = await bell_items(conn, user["id"], 500 if read_all or read_id else limit)
            selected = [item for item in items if read_all or item["id"] == read_id]
            if read_id and not selected:
                raise HTTPException(404, "Bildirim bulunamadı.")
            for item in selected:
                await conn.execute(
                    """INSERT INTO binance_notification_reads(outbox_id) VALUES ($1)
                       ON CONFLICT (outbox_id) DO NOTHING""", item["id"].removeprefix("binance:"),
                )
                item["read"] = True
    except (PostgresError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("Binance bell operation failed type=%s", type(exc).__name__)
        raise HTTPException(503, "Ödeme bildirimleri okunamadı.") from None
    return sorted([*legacy, *items], key=lambda item: item["timestamp"] or "", reverse=True)[:limit]
