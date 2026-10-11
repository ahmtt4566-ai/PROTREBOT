"""OWNER bell projection of persisted LIVE safety events."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

KINDS = {
    "PROTECTION_FAIL", "PROTECTION_BLOCK", "TP_MONITORING_PERSISTED",
    "LIVE_ORDER_BLOCKED", "LIVE_ORDER_EXCEPTION_DIAGNOSTIC",
    "RECONCILIATION_FAILURE_DIAGNOSTIC", "LIVE_STREAM_RECONNECT",
    "LIVE_MONITORING_CREDENTIALS_STALE", "LIVE_UNKNOWN_EXECUTION",
}


def merge_bell(request: Request, items: list[dict[str, Any]], *, read_id: str | None = None,
               read_all: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    from .v22_commercial import authenticated_user
    from . import v25_execution as live

    application = getattr(request, "app", None)
    state = getattr(getattr(application, "state", None), "v25_execution", {})
    events = [row for row in state.get("events", []) if row.get("kind") in KINDS and row.get("recipient_user_id")]
    if not events:
        if read_id:
            raise HTTPException(404, "Bildirim bulunamadı.")
        return items
    user = authenticated_user(request)
    if user.get("role") != "OWNER":
        if read_id:
            raise HTTPException(404, "Bildirim bulunamadı.")
        return items
    events = [row for row in events if row["recipient_user_id"] == user["id"]]
    ids = {f"live:{row['id']}" for row in events}
    if read_id and read_id not in ids:
        raise HTTPException(404, "Bildirim bulunamadı.")
    read_ids = set(state.get("live_notification_read_ids", []))
    if read_all:
        read_ids.update(ids)
    if read_id:
        read_ids.add(read_id)
    if read_id or read_all:
        state["live_notification_read_ids"] = sorted(read_ids & ids)
        live.persist_state(state)
    alerts = [{
        "id": f"live:{row['id']}", "type": row["kind"], "severity": "CRITICAL",
        "title": "LIVE işlem güvenlik uyarısı", "message": live.sanitized_exception_message(row["message"]),
        "timestamp": row["created_at"], "target": "master-trade", "read": f"live:{row['id']}" in read_ids,
    } for row in events]
    return sorted([*items, *alerts], key=lambda item: str(item.get("timestamp") or ""), reverse=True)[:limit]
