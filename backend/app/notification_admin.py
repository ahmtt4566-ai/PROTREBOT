import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .approval_service import owner_connection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v22/admin/approvals/notifications", tags=["Owner notifications"])


class NotificationSummary(BaseModel):
    pending: int | None
    failed: int | None


@router.get("/summary", response_model=NotificationSummary)
async def notification_summary(request: Request):
    async with owner_connection(request) as (conn, _):
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """SELECT COUNT(*) FILTER (WHERE status IN ('pending','sending')) AS pending,
                       COUNT(*) FILTER (WHERE status IN ('failed','dead')) AS failed
                       FROM notification_outbox WHERE recipient_user_id <> 'ERASED'""",
                )
                return NotificationSummary(pending=row["pending"], failed=row["failed"])
        except Exception as exc:
            logger.warning("Notification summary unavailable (%s)", type(exc).__name__)
            return NotificationSummary(pending=None, failed=None)
