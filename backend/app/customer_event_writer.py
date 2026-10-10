"""Bounded, fail-open telemetry; authentication never waits for event storage."""
import asyncio
import logging
import re
import uuid

logger = logging.getLogger(__name__)
EVENT_CODES = {
    "auth.login_failed": "invalid_credentials", "auth.login_succeeded": "login_ok",
    "auth.mfa_failed": "invalid_mfa", "auth.password_reset_requested": "reset_requested",
    "auth.password_reset_completed": "reset_completed", "auth.email_verification_sent": "verification_sent",
    "auth.email_verification_failed": "verification_failed", "auth.email_verified": "email_verified",
    "account.session_revoked": "session_revoked", "api.error": "server_error",
}
KINDS = tuple(EVENT_CODES)
CODES = tuple(EVENT_CODES.values()) + ("request_failed", "event_limit")
FEATURES = ("auth", "security", "verification", "account", "api", "events")


async def write_event(application, user_id, kind, code, feature, http_status, request_id):
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        logger.warning("Customer event storage unavailable")
        return
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """INSERT INTO customer_events (id,user_id,kind,code,feature,http_status,request_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                uuid.uuid4().hex, user_id, kind, code, feature, http_status, request_id,
            )
    except Exception as exc:
        logger.warning("Customer event write failed (%s)", type(exc).__name__)


def start_task(application, operation, *args):
    try:
        tasks = getattr(application.state, "customer_event_tasks", None)
        if tasks is None:
            tasks = application.state.customer_event_tasks = set()
        if len(tasks) >= 64:
            logger.warning("Customer event queue capacity reached")
            return
        coroutine = operation(application, *args)
        try:
            task = asyncio.create_task(coroutine)
        except Exception:
            coroutine.close()
            raise
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    except Exception as exc:
        logger.warning("Customer event scheduling failed (%s)", type(exc).__name__)


def _schedule_event(request, user, kind, code, feature, http_status=None):
    uid = user.get("id") if isinstance(user, dict) else user
    if uid is None:
        return
    if not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", uid):
        raise ValueError("Invalid event user")
    if kind not in KINDS or code not in CODES or feature not in FEATURES:
        raise ValueError("Invalid event enum")
    if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
        raise ValueError("Invalid event status")
    raw_id = getattr(request.state, "request_id", None)
    try:
        request_id = uuid.UUID(str(raw_id)) if raw_id else None
    except ValueError:
        request_id = None
    asyncio.get_running_loop().call_soon(
        start_task, request.app, write_event, uid, kind, code, feature, http_status, request_id,
    )


def record_customer_event(request, user, kind, code, feature, http_status=None):
    try:
        _schedule_event(request, user, kind, code, feature, http_status)
    except Exception as exc:
        logger.warning("Customer event recording failed (%s)", type(exc).__name__)


def record_event(request, user, kind, code, feature, http_status=None):
    try:
        record_customer_event(request, user, kind, code, feature, http_status)
    except Exception as exc:
        logger.warning("Customer event recording failed (%s)", type(exc).__name__)


async def cleanup_events(application):
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        return
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """DELETE FROM customer_events WHERE id IN (
                     SELECT id FROM customer_events WHERE last_at < clock_timestamp() - INTERVAL '90 days'
                     ORDER BY last_at,id LIMIT 500)""",
            )
    except Exception as exc:
        logger.warning("Customer event cleanup failed (%s)", type(exc).__name__)


def schedule_cleanup(application):
    try:
        loop = asyncio.get_running_loop()
        last = getattr(application.state, "customer_event_cleanup_at", float("-inf"))
        if loop.time() - last >= 60:
            application.state.customer_event_cleanup_at = loop.time()
            loop.call_soon(start_task, application, cleanup_events)
    except Exception as exc:
        logger.warning("Customer event cleanup scheduling failed (%s)", type(exc).__name__)


async def shutdown_events(application):
    tasks = tuple(getattr(application.state, "customer_event_tasks", ()))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
