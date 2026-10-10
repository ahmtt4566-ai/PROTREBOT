import asyncio
import os

from .campaign_content import unsubscribe_token
from .campaign_email import send_campaign
from .campaign_service import campaign_row, payload, refresh_counts
from .email_service import EmailDeliveryError


def daily_limit():
    raw = os.getenv("CAMPAIGN_DAILY_LIMIT", "50")
    if not raw.isdecimal() or not 1 <= int(raw) <= 100000:
        raise ValueError("Invalid campaign daily limit")
    return int(raw)


async def claim_campaign(conn):
    candidate = await conn.fetchrow(
        """SELECT * FROM notification_outbox WHERE kind='campaign.info' AND status IN ('pending','failed')
           AND attempts<8 AND next_attempt_at<=clock_timestamp()
           ORDER BY priority,next_attempt_at,id FOR UPDATE SKIP LOCKED LIMIT 1""",
    )
    if candidate is None:
        return None
    budget = await conn.fetchrow(
        """UPDATE campaign_delivery_budget SET
           attempts=CASE WHEN day<(clock_timestamp() AT TIME ZONE 'UTC')::date THEN 1 ELSE attempts+1 END,
           day=(clock_timestamp() AT TIME ZONE 'UTC')::date,next_send_at=clock_timestamp()+INTERVAL '0.5 seconds'
           WHERE id=1 AND (day<(clock_timestamp() AT TIME ZONE 'UTC')::date OR attempts<$1)
           AND next_send_at<=clock_timestamp() RETURNING id""", daily_limit(),
    )
    if budget is None:
        await conn.execute(
            """UPDATE notification_outbox SET next_attempt_at=GREATEST(clock_timestamp()+INTERVAL '0.5 seconds',
               (SELECT CASE WHEN day=(clock_timestamp() AT TIME ZONE 'UTC')::date AND attempts >= $2
                 THEN ((day+1)::timestamp AT TIME ZONE 'UTC') ELSE next_send_at END
                 FROM campaign_delivery_budget WHERE id=1))
               WHERE id=$1 AND status IN ('pending','failed')""", candidate["id"], daily_limit(),
        )
        return None
    return await conn.fetchrow(
        """UPDATE notification_outbox SET status='sending',attempts=attempts+1,
           next_attempt_at=clock_timestamp()+INTERVAL '120 seconds' WHERE id=$1 RETURNING *""", candidate["id"],
    )


async def recipient_result(conn, row, status, reason=None):
    if payload(row).get("test"):
        return
    await conn.execute(
        """UPDATE campaign_recipients SET status=$2,skip_reason=$3 WHERE outbox_id=$1 AND status='queued'""",
        row["id"], status, reason,
    )


async def process_campaign_due(application, *, limit):
    pool = application.state.db_pool
    for _ in range(limit):
        async with pool.acquire() as conn:
            locked = False
            try:
                async with conn.transaction():
                    locked = await conn.fetchval("SELECT pg_try_advisory_lock(71010011)")
                    if not locked:
                        break
                    lease = await claim_campaign(conn)
                if lease is None:
                    break
                async with conn.transaction():
                    await deliver_campaign(conn, lease, application)
            finally:
                if locked:
                    async with conn.transaction():
                        await conn.execute("SELECT pg_advisory_unlock(71010011)")


async def reconcile(conn):
    candidates = await conn.fetch(
        """SELECT id FROM campaigns c WHERE status='sending' AND (
             EXISTS (SELECT 1 FROM campaign_recipients r JOIN notification_outbox o ON o.id=r.outbox_id
               WHERE r.campaign_id=c.id AND r.status='queued' AND o.status IN ('sent','dead'))
             OR recipient_count <> (SELECT COUNT(*) FROM campaign_recipients r WHERE r.campaign_id=c.id))
           ORDER BY updated_at,id LIMIT 10""",
    )
    for candidate in candidates:
        campaign = await campaign_row(conn, candidate["id"])
        rows = await conn.fetch(
            """SELECT r.outbox_id,o.status,o.last_error_code FROM campaign_recipients r
               JOIN notification_outbox o ON o.id=r.outbox_id WHERE r.campaign_id=$1
                 AND r.status='queued' AND o.status IN ('sent','dead')""", campaign["id"],
        )
        for row in rows:
            code = row["last_error_code"]
            reason = {"recipient_erased": "recipient_erased", "recipient_unavailable": "recipient_unavailable",
                      "campaign_cancelled": "cancelled", "opt_out": "opt_out"}.get(code)
            await conn.execute(
                """UPDATE campaign_recipients SET status=$2,skip_reason=$3 WHERE outbox_id=$1 AND status='queued'""",
                row["outbox_id"], "sent" if row["status"] == "sent" else "skipped" if reason else "failed", reason,
            )
        await refresh_counts(conn, campaign)


async def deliver_campaign(conn, lease, application):
    from .notification_worker import ERROR_CODES, finish, logger
    await conn.execute("SELECT pg_advisory_xact_lock(71010011)")
    data = payload(lease)
    campaign = await campaign_row(conn, data["campaign_id"])
    row = await conn.fetchrow(
        "SELECT * FROM notification_outbox WHERE id = $1 AND status = 'sending' AND attempts = $2 FOR UPDATE",
        lease["id"], lease["attempts"],
    )
    if row is None:
        return
    recipient = await conn.fetchrow(
        """SELECT u.security->>'email' AS email,COALESCE(p.announcements_opt_out,FALSE) AS opt_out
           FROM commercial_auth_users u LEFT JOIN email_preferences p ON p.user_id=u.user_id
           WHERE u.user_id=$1 AND u.security->'active'='true'::jsonb AND u.security->'email_verified'='true'::jsonb
             AND NOT EXISTS (SELECT 1 FROM commercial_erased_users e WHERE e.user_hash =
               encode(sha256(convert_to(u.user_id,'UTF8')),'hex')) FOR SHARE OF u""", row["recipient_user_id"],
    )
    # Serialize opt-out changes with the last delivery eligibility check.
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('campaign-pref:' || $1,0))", row["recipient_user_id"])
    opt_out = await conn.fetchval("SELECT announcements_opt_out FROM email_preferences WHERE user_id=$1", row["recipient_user_id"])
    test = bool(data.get("test"))
    reason = None
    if campaign["status"] == "cancelled" or not test and campaign["status"] != "sending":
        reason = "cancelled"
    elif recipient is None or not recipient["email"]:
        reason = "recipient_unavailable"
    elif opt_out:
        reason = "opt_out"
    if reason:
        await finish(conn, row, "dead", "campaign_cancelled" if reason == "cancelled" else reason)
        await recipient_result(conn, row, "skipped", reason)
    else:
        # Fence provider starts across processes, not just earlier queue claims.
        await conn.execute(
            """SELECT pg_sleep(GREATEST(0,extract(epoch FROM next_send_at-clock_timestamp())))
               FROM campaign_delivery_budget WHERE id=1 FOR UPDATE""",
        )
        await conn.execute(
            """UPDATE campaign_delivery_budget SET next_send_at=clock_timestamp()+INTERVAL '0.5 seconds',
               attempts=CASE WHEN day<(clock_timestamp() AT TIME ZONE 'UTC')::date THEN 1 ELSE attempts END,
               day=(clock_timestamp() AT TIME ZONE 'UTC')::date WHERE id=1""",
        )
        stamp = row["created_at"] if test else campaign["started_at"]
        token = unsubscribe_token(application.state.v22_commercial["secret"], row["recipient_user_id"], int(stamp.timestamp()))
        try:
            await asyncio.to_thread(send_campaign, to_email=recipient["email"], campaign=dict(campaign), token=token,
                                    dedupe_key=row["dedupe_key"], test=test)
        except Exception as exc:
            code = exc.code if isinstance(exc, EmailDeliveryError) and exc.code in ERROR_CODES | {"permanent_rejection"} else "delivery_error"
            logger.warning("Campaign delivery failed code=%s type=%s", code, type(exc).__name__)
            dead = code == "permanent_rejection" or row["attempts"] >= 8
            await finish(conn, row, "dead" if dead else "failed", code)
            if dead:
                await recipient_result(conn, row, "failed")
        else:
            await finish(conn, row, "sent")
            await recipient_result(conn, row, "sent")
    if not test:
        await refresh_counts(conn, campaign)
