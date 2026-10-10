"""Best-effort enqueue inside a savepoint of the approval transaction."""
import json
import logging
import uuid

logger = logging.getLogger(__name__)
DECISION_RESULTS = {"approved": 1, "rejected": 2, "stale": 3, "failed": 4}


async def enqueue_digest(conn):
    try:
        async with conn.transaction():
            owners = await conn.fetch(
                """SELECT user_id FROM commercial_auth_users WHERE security->>'role' = 'OWNER'
                   AND security->'active' = 'true'::jsonb
                   AND NOT EXISTS (SELECT 1 FROM commercial_erased_users e
                     WHERE e.user_hash = encode(sha256(convert_to(user_id,'UTF8')),'hex'))""",
            )
            bucket = await conn.fetchval("SELECT floor(extract(epoch FROM clock_timestamp()) / 600)::bigint")
            for owner in owners:
                uid = owner["user_id"]
                await conn.execute(
                    """INSERT INTO notification_outbox (id,kind,recipient_user_id,payload,dedupe_key,next_attempt_at)
                       VALUES ($1,'approval.pending_digest',$2,$3::jsonb,$4,to_timestamp(($5+1)*600))
                       ON CONFLICT (dedupe_key) DO NOTHING""",
                    uuid.uuid4().hex, uid, json.dumps({"bucket": bucket}),
                    f"approval.pending_digest:{uid}:{bucket}", bucket,
                )
    except Exception as exc:
        logger.warning("Notification enqueue failed (%s)", type(exc).__name__)


async def enqueue_decision(conn, row):
    result = DECISION_RESULTS.get(row["status"])
    if result is None:
        return
    try:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO notification_outbox (id,kind,recipient_user_id,payload,dedupe_key)
                   VALUES ($1,'approval.decision',$2,$3::jsonb,$4)
                   ON CONFLICT (dedupe_key) DO NOTHING""",
                uuid.uuid4().hex, row["requester_user_id"],
                json.dumps({"approval_id": row["id"], "result": result}),
                f"approval.decision:{row['id']}:{row['status']}",
            )
    except Exception as exc:
        logger.warning("Notification enqueue failed (%s)", type(exc).__name__)
