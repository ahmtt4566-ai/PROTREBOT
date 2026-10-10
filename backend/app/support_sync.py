"""One-way persisted snapshot reads; moderator state is never copied back."""
import json
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LegacyTicket(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
    user_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
    subject: str = Field(min_length=3, max_length=160)
    message: str = Field(min_length=5, max_length=2000)
    priority: Literal["LOW", "NORMAL", "HIGH"]
    status: Literal["OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"]
    response_note: str = Field(default="", max_length=1000)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def aware_date(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Legacy date requires timezone")
        return value


async def sync_support_cases(conn) -> None:
    if not conn.is_in_transaction():
        raise RuntimeError("Support sync requires a transaction")
    # Also used by the erasure trigger: prevents a stale import after scrubbing.
    await conn.execute("SELECT pg_advisory_xact_lock(61010004)")
    await conn.execute("INSERT INTO support_sync_state (id) VALUES (1) ON CONFLICT DO NOTHING")
    cache = await conn.fetchrow(
        """SELECT synced_at > clock_timestamp() - INTERVAL '30 seconds' AS fresh
           FROM support_sync_state WHERE id = 1 FOR UPDATE"""
    )
    if cache and cache["fresh"] is True:
        return
    snapshot = await conn.fetchrow(
        "SELECT payload FROM application_state_snapshots WHERE state_key = 'v22-commercial'"
    )
    if snapshot is None:
        raise RuntimeError("Support snapshot unavailable")
    payload = snapshot["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict) or not isinstance(payload.get("support_tickets"), list):
        raise ValueError("Invalid legacy support snapshot")
    seen = set()
    for value in payload["support_tickets"]:
        if not isinstance(value, dict):
            raise ValueError("Invalid legacy ticket")
        uid = value.get("user_id")
        if not isinstance(uid, str):
            raise ValueError("Invalid legacy owner")
        visible = await conn.fetchrow(
            """SELECT user_id FROM commercial_auth_users
               WHERE user_id = $1 AND security->>'role' = 'CUSTOMER'
                 AND NOT EXISTS (SELECT 1 FROM commercial_erased_users e
                   WHERE e.user_hash = encode(sha256(convert_to(user_id, 'UTF8')), 'hex'))
               FOR SHARE""", uid,
        )
        if visible is None:
            continue
        ticket = LegacyTicket.model_validate(value)
        if ticket.id in seen:
            raise ValueError("Duplicate legacy ticket identifier")
        seen.add(ticket.id)
        result = await conn.execute(
            """INSERT INTO support_cases
               (id, legacy_ticket_id, user_id, subject, message, priority,
                legacy_status, legacy_response_note, created_at_legacy)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               ON CONFLICT (legacy_ticket_id) DO UPDATE SET
                   subject = EXCLUDED.subject, message = EXCLUDED.message,
                   priority = EXCLUDED.priority, legacy_status = EXCLUDED.legacy_status,
                   legacy_response_note = EXCLUDED.legacy_response_note,
                   synced_at = clock_timestamp()
               WHERE support_cases.user_id = EXCLUDED.user_id""",
            uuid.uuid4().hex, ticket.id, ticket.user_id, ticket.subject, ticket.message,
            ticket.priority, ticket.status, ticket.response_note, ticket.created_at,
        )
        if result != "INSERT 0 1":
            raise RuntimeError("Legacy ticket ownership changed")
    await conn.execute("UPDATE support_sync_state SET synced_at = clock_timestamp() WHERE id = 1")
