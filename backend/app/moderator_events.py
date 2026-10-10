import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from .audit_log import write_audit
from .customer_event_writer import CODES, FEATURES, KINDS, schedule_cleanup
from .moderator_access import ModeratorIdentity, require_permission
from .moderator_customers import customer_read_connection, customer_row

router = APIRouter(prefix="/api/mod/customers", tags=["Customer technical history"])
Kind = Literal[
    "auth.login_failed", "auth.login_succeeded", "auth.mfa_failed", "auth.password_reset_requested",
    "auth.password_reset_completed", "auth.email_verification_sent", "auth.email_verification_failed",
    "auth.email_verified", "account.session_revoked", "api.error",
]


class EventItem(BaseModel):
    kind: Kind
    code: str
    feature: str
    http_status: int | None = Field(ge=100, le=599)
    count: int = Field(ge=1)
    first_at: datetime
    last_at: datetime
    request_ref: str | None = Field(pattern=r"^[0-9a-f]{8}$")
    source: Literal["customer_event", "system_error"]
    severity: Literal["INFO", "WARNING", "ERROR", "CRITICAL"]
    resolved: bool | None


class EventsPage(BaseModel):
    items: list[EventItem]
    total: int
    limit: int
    offset: int


HISTORY = """
    SELECT kind,code,feature,http_status,count,first_at,last_at,request_id::text,
      'customer_event' AS source,
      CASE WHEN kind IN ('auth.login_failed','auth.mfa_failed','auth.email_verification_failed','api.error')
           THEN 'WARNING' ELSE 'INFO' END AS severity, NULL::boolean AS resolved
    FROM customer_events WHERE user_id = $1 AND last_at >= clock_timestamp() - INTERVAL '90 days'
    UNION ALL
    SELECT 'api.error',
      CASE code WHEN 'TOKEN_INVALID' THEN 'invalid_credentials' WHEN 'SESSION_STALE' THEN 'session_revoked'
                ELSE 'server_error' END,
      CASE WHEN route LIKE '/api/v22/auth/%' THEN 'auth'
           WHEN route LIKE '/api/v22/account/%' THEN 'account' ELSE 'api' END,
      NULL::integer, 1::bigint, first_seen,last_seen,NULL::text,'system_error',
      CASE WHEN severity IN ('INFO','WARNING','ERROR','CRITICAL') THEN severity ELSE 'ERROR' END,
      (status = 'RESOLVED' OR resolved_at IS NOT NULL)
    FROM error_events WHERE user_id = $1 AND last_seen >= clock_timestamp() - INTERVAL '90 days'
"""
FILTERS = "WHERE ($2::text IS NULL OR kind = $2) AND ($3::timestamptz IS NULL OR last_at >= $3) AND ($4::timestamptz IS NULL OR last_at <= $4)"


def safe_item(row):
    if row["kind"] not in KINDS or row["code"] not in CODES or row["feature"] not in FEATURES:
        raise ValueError("Invalid stored event enum")
    ref = uuid.UUID(str(row["request_id"])).hex[:8] if row["request_id"] else None
    return EventItem(**{key: row[key] for key in EventItem.model_fields if key != "request_ref"}, request_ref=ref)


@router.get("/{user_id}/events", response_model=EventsPage)
async def customer_events(request: Request,
                          user_id: str = Path(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$"),
                          identity: ModeratorIdentity = Depends(require_permission("events.view")),
                          kind: Kind | None = None, start: datetime | None = None, end: datetime | None = None,
                          limit: int = Query(25, ge=1, le=50), offset: int = Query(0, ge=0)):
    if any(value is not None and value.utcoffset() is None for value in (start, end)):
        raise HTTPException(422, "Dates require a timezone")
    if start is not None and end is not None and start > end:
        raise HTTPException(422, "Invalid date range")
    schedule_cleanup(request.app)
    async with customer_read_connection(request, identity, "events.view") as conn:
        await customer_row(conn, identity, user_id)
        args = (user_id, kind, start, end)
        total = await conn.fetchval(f"SELECT COUNT(*) FROM ({HISTORY}) history {FILTERS}", *args)
        rows = await conn.fetch(
            f"SELECT * FROM ({HISTORY}) history {FILTERS} ORDER BY last_at DESC,source,first_at LIMIT $5 OFFSET $6",
            *args, limit, offset,
        )
        result = EventsPage(items=[safe_item(row) for row in rows], total=total, limit=limit, offset=offset)
        await write_audit(conn, request.state.mod_read_actor, "customer.events.viewed", "USER", user_id, {}, {})
        return result
