import asyncio
import json
import logging

from .email_service import EmailDeliveryError
from .notification_email import send_notification

logger = logging.getLogger(__name__)
BACKOFF_SECONDS = (60, 300, 900, 3600, 21600)
ERROR_CODES = {"provider_unavailable", "provider_timeout", "provider_rejected", "invalid_response", "delivery_error"}
LEASE_SECONDS = 120
SWEEP_COOLDOWN_SECONDS = 15


async def recover_leases(conn):
    await conn.execute(
        """WITH expired AS (
             SELECT id FROM notification_outbox WHERE status = 'sending' AND next_attempt_at <= clock_timestamp()
               ORDER BY next_attempt_at,id FOR UPDATE SKIP LOCKED LIMIT 10
           )
           UPDATE notification_outbox SET status = CASE WHEN attempts >= 8 THEN 'dead' ELSE 'pending' END,
           last_error_code = CASE WHEN attempts >= 8 THEN 'attempts_exhausted' ELSE 'lease_expired' END
           FROM expired WHERE notification_outbox.id = expired.id""",
    )


async def claim(conn):
    return await conn.fetchrow(
        """WITH due AS (
             SELECT id FROM notification_outbox WHERE status IN ('pending','failed') AND attempts < 8
               AND next_attempt_at <= clock_timestamp() ORDER BY next_attempt_at,id FOR UPDATE SKIP LOCKED LIMIT 1
           )
           UPDATE notification_outbox o SET status = 'sending', attempts = attempts + 1,
             next_attempt_at = clock_timestamp() + $1 * INTERVAL '1 second',
             payload = CASE WHEN o.kind = 'approval.pending_digest' AND NOT o.payload ? 'count'
               THEN o.payload || jsonb_build_object('count',(
                 SELECT COUNT(*) FROM approval_requests WHERE status = 'pending' AND expires_at > clock_timestamp()))
               ELSE o.payload END
           FROM due WHERE o.id = due.id RETURNING o.*""", LEASE_SECONDS,
    )


async def finish(conn, row, status, code=None):
    delay = BACKOFF_SECONDS[min(row["attempts"] - 1, len(BACKOFF_SECONDS) - 1)]
    result = await conn.execute(
        """UPDATE notification_outbox SET status = $3, last_error_code = $4,
           next_attempt_at = clock_timestamp() + $5 * INTERVAL '1 second',
           sent_at = CASE WHEN $3 = 'sent' THEN clock_timestamp() ELSE NULL END
           WHERE id = $1 AND attempts = $2 AND status = 'sending'""",
        row["id"], row["attempts"], status, code, delay,
    )
    if result != "UPDATE 1":
        raise RuntimeError("Outbox delivery lease changed")


async def deliver(conn, lease):
    # Hold the outbox row through delivery: erasure cannot race a completed recipient check.
    row = await conn.fetchrow(
        "SELECT * FROM notification_outbox WHERE id = $1 AND status = 'sending' AND attempts = $2 FOR UPDATE",
        lease["id"], lease["attempts"],
    )
    if row is None:
        return
    recipient = await conn.fetchrow(
        """SELECT security->>'email' AS email FROM commercial_auth_users
           WHERE user_id = $1 AND security->'active' = 'true'::jsonb
             AND security->'email_verified' = 'true'::jsonb
             AND ($2 = 'approval.decision' OR security->>'role' = 'OWNER')
             AND NOT EXISTS (SELECT 1 FROM commercial_erased_users e
               WHERE e.user_hash = encode(sha256(convert_to(user_id,'UTF8')),'hex'))
           FOR SHARE""", row["recipient_user_id"], row["kind"],
    )
    if recipient is None or not recipient["email"]:
        await finish(conn, row, "dead", "recipient_unavailable")
        return
    payload = row["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    if row["kind"] == "approval.pending_digest" and payload["count"] == 0:
        await finish(conn, row, "dead", "nothing_to_notify")
        return
    try:
        await asyncio.to_thread(send_notification, to_email=recipient["email"], kind=row["kind"],
                                payload=payload, dedupe_key=row["dedupe_key"])
    except Exception as exc:
        code = exc.code if isinstance(exc, EmailDeliveryError) and exc.code in ERROR_CODES else "delivery_error"
        logger.warning("Notification delivery failed code=%s type=%s", code, type(exc).__name__)
        await finish(conn, row, "dead" if row["attempts"] >= 8 else "failed", code)
    else:
        await finish(conn, row, "sent")


async def process_due(application, *, limit=10):
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        logger.warning("Notification sweep unavailable code=storage_unavailable")
        return
    try:
        async with pool.acquire() as conn, conn.transaction():
            await recover_leases(conn)
        for _ in range(min(max(limit, 0), 10)):
            async with pool.acquire() as conn, conn.transaction():
                row = await claim(conn)
            if row is None:
                break
            async with pool.acquire() as conn, conn.transaction():
                await deliver(conn, row)
    except Exception as exc:
        logger.warning("Notification sweep failed (%s)", type(exc).__name__)


def schedule_sweep(application):
    """Coalesce endpoint/startup work without delaying or failing approval responses."""
    try:
        loop = asyncio.get_running_loop()
        current = getattr(application.state, "notification_task", None)
        last = getattr(application.state, "notification_last_sweep", float("-inf"))
        if current is not None and not current.done() or loop.time() - last < SWEEP_COOLDOWN_SECONDS:
            return
        application.state.notification_last_sweep = loop.time()
        application.state.notification_task = loop.create_task(process_due(application))
    except Exception as exc:
        logger.warning("Notification scheduling failed (%s)", type(exc).__name__)


async def shutdown_notifications(application):
    task = getattr(application.state, "notification_task", None)
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
