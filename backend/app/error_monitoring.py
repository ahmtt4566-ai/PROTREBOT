"""Centralized, secret-safe application error monitoring."""

from __future__ import annotations

import hashlib
import asyncio
import json
import os
import re
import traceback
import time
from datetime import datetime, timezone
from typing import Any

import httpx

SENSITIVE_KEY_RE = re.compile(r"(?:password|passwd|secret|token|api[_-]?key|apikey|authorization|cookie|credential|private[_-]?key|signature|x-mbx-apikey|listen[_-]?key)", re.I)
SENSITIVE_VALUE_RE = re.compile(
    r"(?P<key>signature|secret(?:[_-]?key)?|api[_-]?key|apikey|token|password|passwd|authorization|cookie|x-mbx-apikey|listen[_-]?key)"
    r"(?P<separator>\s*[:=]\s*|%3d)(?P<value>[^&\s,;\}\]\"']+)",
    re.IGNORECASE,
)
MAX_TEXT = 2_000
MAX_STACK = 8_000
MAX_CONTEXT_KEYS = 32
RETENTION_DAYS = max(7, int(os.getenv("ERROR_EVENT_RETENTION_DAYS", "90")))
_telegram_alerts: dict[str, float] = {}


def schedule_log_event(application: Any, event: dict[str, Any]) -> None:
    pool = getattr(getattr(application, "state", None), "db_pool", None)
    if pool is None:
        return
    try:
        asyncio.create_task(log_event(pool, event))
    except RuntimeError:
        return


def sanitize_text(value: Any, *, max_length: int = MAX_TEXT) -> str:
    text = str(value)
    text = re.sub(r"(authorization\s*[:=]\s*(?:Bearer\s+)?)\S+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    return SENSITIVE_VALUE_RE.sub(lambda match: f"{match.group('key')}{match.group('separator')}[REDACTED]", text)[:max_length]


def redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[REDACTED]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:MAX_CONTEXT_KEYS]:
            key_text = str(key)
            result[key_text] = "[REDACTED]" if SENSITIVE_KEY_RE.search(key_text) else redact(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [redact(item, depth=depth + 1) for item in list(value)[:MAX_CONTEXT_KEYS]]
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:MAX_TEXT]


def normalize_error_message(message: str) -> str:
    value = re.sub(r"Bearer\s+[^\s]+", "Bearer [REDACTED]", str(message), flags=re.I)
    return sanitize_text(value)


def error_fingerprint(*, source: str, kind: str, message: str, route: str | None = None) -> str:
    material = "|".join((source.strip().lower(), kind.strip().lower(), normalize_error_message(message).lower(), normalize_error_message(route or "").lower()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def build_error_event(*, source: str, kind: str, message: str, severity: str = "ERROR", service: str | None = None, code: str | None = None, route: str | None = None, method: str | None = None, request_id: str | None = None, user_id: str | None = None, context: dict[str, Any] | None = None, details: dict[str, Any] | None = None, stack: str | None = None) -> dict[str, Any]:
    clean_message = normalize_error_message(message)
    return {
        "source": source[:40], "service": (service or source)[:40], "kind": kind[:80], "code": (code or "")[:64] or None, "severity": severity.upper()[:16],
        "message": clean_message, "route": sanitize_text(route or "")[:300] or None,
        "method": (method or "")[:12] or None, "request_id": (request_id or "")[:100] or None,
        "user_id": (user_id or "")[:100] or None,
        "context": redact(context or {}), "details": redact(details or {}), "stack": sanitize_text(stack or "", max_length=MAX_STACK) or None,
        "fingerprint": error_fingerprint(source=source, kind=kind, message=clean_message, route=route),
        "occurred_at": datetime.now(timezone.utc),
    }


def exception_event(exc: BaseException, *, route: str | None = None, method: str | None = None, request_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    return build_error_event(
        source="backend", kind=type(exc).__name__, message=str(exc) or type(exc).__name__,
        route=route, method=method, request_id=request_id, user_id=user_id,
        stack=traceback.format_exc(),
    )


async def ensure_error_schema(pool: Any) -> None:
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS error_events (
          id BIGSERIAL PRIMARY KEY,
          fingerprint TEXT NOT NULL,
          source TEXT NOT NULL,
          service TEXT NOT NULL DEFAULT 'backend',
          kind TEXT NOT NULL,
          code TEXT,
          severity TEXT NOT NULL DEFAULT 'ERROR',
          status TEXT NOT NULL DEFAULT 'OPEN',
          message TEXT NOT NULL,
          route TEXT,
          method TEXT,
          request_id TEXT,
          user_id TEXT,
          context JSONB NOT NULL DEFAULT '{}'::jsonb,
          details JSONB NOT NULL DEFAULT '{}'::jsonb,
          stack TEXT,
          occurrences INTEGER NOT NULL DEFAULT 1,
          first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          acknowledged_at TIMESTAMPTZ,
          resolved_at TIMESTAMPTZ,
          notes TEXT
        )
    """)
    await pool.execute("ALTER TABLE error_events ADD COLUMN IF NOT EXISTS service TEXT NOT NULL DEFAULT 'backend'")
    await pool.execute("ALTER TABLE error_events ADD COLUMN IF NOT EXISTS code TEXT")
    await pool.execute("ALTER TABLE error_events ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb")
    await pool.execute("ALTER TABLE error_events ADD COLUMN IF NOT EXISTS notes TEXT")
    await pool.execute("CREATE UNIQUE INDEX IF NOT EXISTS error_events_fingerprint_open_idx ON error_events (fingerprint) WHERE status = 'OPEN'")
    await pool.execute("CREATE INDEX IF NOT EXISTS error_events_last_seen_idx ON error_events (last_seen DESC)")
    await pool.execute("CREATE INDEX IF NOT EXISTS error_events_status_severity_idx ON error_events (status, severity)")


async def log_event(pool: Any, event: dict[str, Any]) -> None:
    if pool is None:
        return
    try:
        await pool.execute("""
            INSERT INTO error_events (fingerprint, source, service, kind, code, severity, message, route, method, request_id, user_id, context, details, stack)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb,$14)
            ON CONFLICT (fingerprint) WHERE status = 'OPEN' DO UPDATE SET
              occurrences = error_events.occurrences + 1, last_seen = NOW(), stack = COALESCE(EXCLUDED.stack, error_events.stack), context = EXCLUDED.context
        """, event["fingerprint"], event["source"], event.get("service", event["source"]), event["kind"], event.get("code"), event["severity"], event["message"], event["route"], event["method"], event["request_id"], event["user_id"], json.dumps(event["context"]), json.dumps(event.get("details", {})), event["stack"])
        await maybe_alert_critical(event)
        await pool.execute("DELETE FROM error_events WHERE last_seen < NOW() - ($1 * INTERVAL '1 day')", RETENTION_DAYS)
    except Exception:
        return


async def maybe_alert_critical(event: dict[str, Any]) -> None:
    if str(event.get("severity") or "").upper() != "CRITICAL":
        return
    token = os.getenv("ALERT_TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("ALERT_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    fingerprint = str(event.get("fingerprint") or "")
    now = time.monotonic()
    if now - _telegram_alerts.get(fingerprint, 0.0) < 600:
        return
    _telegram_alerts[fingerprint] = now
    text = sanitize_text(f"CRITICAL {event.get('service', 'backend')} {event.get('code') or event.get('kind')}: {event.get('message', '')[:500]}")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text})
    except Exception:
        return