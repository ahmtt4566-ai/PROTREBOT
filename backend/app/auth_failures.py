"""Serialized failure-only limits, shared by workers and independent of challenges."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from typing import TypedDict

import asyncpg
from fastapi import HTTPException

from . import account_store
from .commercial_core import normalize_email

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FailurePolicy:
    attempts: int
    window: int
    waits: tuple[int, ...]
    reset_after: int = 24 * 60 * 60


class FailureState(TypedDict):
    failures: list[float]
    blocked_until: float
    level: int
    last_failure: float


LOGIN_ACCOUNT = FailurePolicy(5, 900, (900, 1800, 3600))
LOGIN_IP = FailurePolicy(20, 900, (900, 1800, 3600))
MFA_ACCOUNT = FailurePolicy(10, 3600, (3600,))


def new_state() -> FailureState:
    return {"failures": [], "blocked_until": 0.0, "level": 0, "last_failure": 0.0}


def decode_state(value: str | dict) -> FailureState:
    row = json.loads(value) if isinstance(value, str) else value
    if not isinstance(row, dict):
        raise TypeError("Invalid authentication failure state")
    failures = row.get("failures")
    numeric = ("blocked_until", "last_failure")
    if (
        not isinstance(failures, list)
        or any(not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(item) for item in failures)
        or any(not isinstance(row.get(key), (int, float)) or isinstance(row[key], bool) or not math.isfinite(row[key]) for key in numeric)
        or not isinstance(row.get("level"), int) or isinstance(row["level"], bool) or row["level"] < 0
    ):
        raise ValueError("Invalid authentication failure state")
    return {
        "failures": [float(item) for item in failures],
        "blocked_until": float(row["blocked_until"]),
        "level": row["level"],
        "last_failure": float(row["last_failure"]),
    }


def retry_after(state: FailureState, now: float) -> int:
    return max(0, math.ceil(state["blocked_until"] - now))


def record_failure(state: FailureState, policy: FailurePolicy, now: float) -> None:
    if now - state["last_failure"] >= policy.reset_after:
        state["level"] = 0
    state["last_failure"] = max(state["last_failure"], now)
    if retry_after(state, now):
        return
    state["failures"] = [stamp for stamp in state["failures"] if now - stamp < policy.window]
    state["failures"].append(now)
    if len(state["failures"]) >= policy.attempts:
        state["level"] = min(len(policy.waits), state["level"] + 1)
        state["blocked_until"] = now + policy.waits[state["level"] - 1]
        state["failures"] = []


def enforce_failure_wait(state: FailureState, now: float) -> None:
    seconds = retry_after(state, now)
    if seconds:
        raise HTTPException(
            429, "Çok fazla yanlış deneme; bekleme süresinden sonra tekrar deneyin.",
            headers={"Retry-After": str(seconds)},
        )


async def login_failure_limits(request, email: str, *, failed: bool = False) -> None:
    from . import v22_commercial as auth
    rt = auth.runtime(request)
    if rt.get("auth_storage_sync_failed"):
        raise HTTPException(503, "Kimlik doğrulama depolama eşitlemesi başarısız")
    host = request.client.host if request.client else "unknown"
    buckets = sorted([
        ("password-account:" + hashlib.sha256(normalize_email(email).encode()).hexdigest(), LOGIN_ACCOUNT),
        ("password-ip:" + hashlib.sha256(host.encode()).hexdigest(), LOGIN_IP),
    ], key=lambda item: item[0])
    pool = getattr(request.app.state, "db_pool", None)
    try:
        if pool is not None:
            async with pool.acquire() as db, db.transaction():
                for bucket, policy in buckets:
                    await db.execute("SELECT pg_advisory_xact_lock(hashtext($1))", "auth-failure:" + bucket)
                    row = await db.fetchrow(
                        "SELECT payload FROM commercial_auth_failures WHERE bucket = $1", bucket)
                    state = decode_state(row["payload"]) if row else new_state()
                    now = time.time()
                    if failed:
                        record_failure(state, policy, now)
                        await db.execute(
                            """INSERT INTO commercial_auth_failures (bucket, payload) VALUES ($1,$2::jsonb)
                               ON CONFLICT (bucket) DO UPDATE SET payload=EXCLUDED.payload, updated_at=NOW()""",
                            bucket, json.dumps(state),
                        )
                    else:
                        enforce_failure_wait(state, now)
        else:
            if auth.DURABLE_AUTH_REQUIRED or rt.get("authoritative_auth_enabled"):
                raise HTTPException(503, "Kalıcı güvenlik sınırı deposu hazır değil")
            db = account_store.connection(request)
            try:
                db.execute("BEGIN IMMEDIATE")
                for bucket, policy in buckets:
                    row = db.execute("SELECT payload FROM commercial_auth_failures WHERE bucket=?", (bucket,)).fetchone()
                    state = decode_state(row[0]) if row else new_state()
                    now = time.time()
                    if failed:
                        record_failure(state, policy, now)
                        db.execute(
                            """INSERT INTO commercial_auth_failures (bucket,payload) VALUES (?,?)
                               ON CONFLICT(bucket) DO UPDATE SET payload=excluded.payload""",
                            (bucket, json.dumps(state)),
                        )
                    else:
                        enforce_failure_wait(state, now)
                db.commit()
            finally:
                db.close()
    except HTTPException:
        raise
    except (asyncpg.PostgresError, asyncpg.InterfaceError, sqlite3.Error, OSError, ValueError, TypeError, RuntimeError) as exc:
        logger.error("Authentication failure store unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "Güvenlik sınırı deposu kullanılamıyor") from exc
