"""Serialized, cross-worker account settings. Snapshots are not this store's authority."""
from __future__ import annotations

import copy
import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from fastapi import HTTPException

from .local_storage import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS commercial_account_settings (
 user_id TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS commercial_account_tokens (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
 auth_version BIGINT NOT NULL, email TEXT, expires DOUBLE PRECISION NOT NULL, used BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS commercial_auth_failures (
 bucket TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
DROP TRIGGER IF EXISTS commercial_erasure_guard ON commercial_account_settings;
CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON commercial_account_settings
 FOR EACH ROW EXECUTE FUNCTION commercial_erasure_user_guard();
DROP TRIGGER IF EXISTS commercial_erasure_guard ON commercial_account_tokens;
CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON commercial_account_tokens
 FOR EACH ROW EXECUTE FUNCTION commercial_erasure_user_guard();
"""
ACTIVE = ContextVar("account_settings_transactions", default=None)


def active_connection(request, user_id):
    active = ACTIVE.get() or {}
    entry = active.get((id(request.app), user_id))
    return entry[1] if entry else None


def active_local_connection(request, user_id):
    entry = (ACTIVE.get() or {}).get((id(request.app), user_id))
    return entry[2] if entry and len(entry) > 2 else None


def defaults() -> dict:
    return {"preferences": {}, "sessions": {}, "challenges": {}, "activity": [],
            "two_factor_enabled": False, "recovery_hashes": [], "last_totp_step": -1}


def local_path(request) -> Path:
    return Path(getattr(request.app.state, "account_settings_path", DATA_DIR / "account_settings.sqlite3"))


def connection(request):
    path = local_path(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("CREATE TABLE IF NOT EXISTS commercial_account_settings (user_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS commercial_account_tokens (
        token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
        auth_version INTEGER NOT NULL, email TEXT, expires REAL NOT NULL, used BOOLEAN NOT NULL DEFAULT FALSE)""")
    db.execute("CREATE TABLE IF NOT EXISTS commercial_auth_failures (bucket TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    return db


async def read(request, user_id: str) -> dict | None:
    pool = getattr(request.app.state, "db_pool", None)
    active = (ACTIVE.get() or {}).get((id(request.app), user_id))
    if active:
        return copy.deepcopy(active[0])
    try:
        if pool is not None:
            row = await pool.fetchrow("SELECT payload FROM commercial_account_settings WHERE user_id = $1", user_id)
            value = row["payload"] if row else None
        else:
            from . import v22_commercial as auth
            if auth.DURABLE_AUTH_REQUIRED or auth.runtime(request).get("authoritative_auth_enabled"):
                raise HTTPException(503, "Kalıcı hesap deposu hazır değil")
            if not local_path(request).exists():
                return None
            db = connection(request)
            try:
                row = db.execute("SELECT payload FROM commercial_account_settings WHERE user_id = ?", (user_id,)).fetchone()
                value = row[0] if row else None
            finally:
                db.close()
        return json.loads(value) if isinstance(value, str) else copy.deepcopy(value)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Hesap deposu kullanılamıyor") from None


@asynccontextmanager
async def edit(request, user_id: str):
    """Rollback on exceptions, including failed delivery and failed durable writes."""
    pool = getattr(request.app.state, "db_pool", None)
    key = (id(request.app), user_id)
    active = ACTIVE.get() or {}
    if key in active:
        yield active[key][0]
        return
    marker = None
    committed = False
    rt = request.app.state.v22_commercial
    user = next((row for row in rt["state"].get("users", []) if row.get("id") == user_id), None)
    previous_user = copy.deepcopy(user)
    previous_baseline = copy.deepcopy(rt.get("auth_baseline", {}).get(user_id))
    try:
        if pool is not None:
            async with pool.acquire() as db, db.transaction():
                await db.execute("SELECT pg_advisory_xact_lock(hashtext($1))", "account-settings:" + user_id)
                row = await db.fetchrow("SELECT payload FROM commercial_account_settings WHERE user_id = $1 FOR UPDATE", user_id)
                doc = defaults() if row is None else row["payload"]
                if isinstance(doc, str):
                    doc = json.loads(doc)
                marker = ACTIVE.set({**active, key: (doc, db)})
                yield doc
                result = await db.execute("""INSERT INTO commercial_account_settings (user_id,payload) VALUES ($1,$2::jsonb)
                    ON CONFLICT (user_id) DO UPDATE SET payload=EXCLUDED.payload, updated_at=NOW()""",
                                 user_id, json.dumps(doc))
                if result == "INSERT 0 0":
                    raise HTTPException(401, "Hesap etkin değil")
            committed = True
        else:
            from . import v22_commercial as auth
            if auth.DURABLE_AUTH_REQUIRED or auth.runtime(request).get("authoritative_auth_enabled"):
                raise HTTPException(503, "Kalıcı hesap deposu hazır değil")
            lock = getattr(request.app.state, "account_settings_lock", None)
            if lock is None:
                lock = asyncio.Lock()
                request.app.state.account_settings_lock = lock
            async with lock:
                db = connection(request)
                try:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute("SELECT payload FROM commercial_account_settings WHERE user_id = ?", (user_id,)).fetchone()
                    doc = json.loads(row[0]) if row else defaults()
                    marker = ACTIVE.set({**active, key: (doc, None, db)})
                    yield doc
                    db.execute("INSERT OR REPLACE INTO commercial_account_settings VALUES (?,?)", (user_id, json.dumps(doc)))
                    db.commit()
                    committed = True
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    db.close()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Hesap değişikliği kalıcı depoya yazılamadı") from None
    finally:
        if not committed:
            if user is not None and any(row is user for row in rt["state"].get("users", [])):
                user.clear()
                user.update(previous_user)
            if previous_baseline is None:
                rt.setdefault("auth_baseline", {}).pop(user_id, None)
            else:
                rt.setdefault("auth_baseline", {})[user_id] = previous_baseline
        if marker is not None:
            ACTIVE.reset(marker)


async def save_action_token(request, token, user, kind):
    import hashlib
    from . import v22_commercial as auth
    payload = auth.verify_token(token, auth.runtime(request)["secret"], expected_kind=kind)
    values = (hashlib.sha256(token.encode()).hexdigest(), user["id"], kind,
              int(user.get("auth_version", 1)), user.get("email"), float(payload["exp"]))
    pool = getattr(request.app.state, "db_pool", None)
    try:
        if pool is not None:
            await pool.execute("""INSERT INTO commercial_account_tokens
                (token_hash,user_id,kind,auth_version,email,expires) VALUES ($1,$2,$3,$4,$5,$6)""", *values)
        else:
            if auth.DURABLE_AUTH_REQUIRED or auth.runtime(request).get("authoritative_auth_enabled"):
                raise HTTPException(503, "Kalıcı doğrulama deposu hazır değil")
            db = connection(request)
            try:
                db.execute("""INSERT INTO commercial_account_tokens
                    (token_hash,user_id,kind,auth_version,email,expires) VALUES (?,?,?,?,?,?)""", values)
            finally:
                db.close()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Doğrulama isteği kalıcı depoya yazılamadı") from None


async def local_user_by_email(request, email):
    if not local_path(request).exists():
        return None
    try:
        db = connection(request)
        try:
            for uid, body in db.execute("SELECT user_id,payload FROM commercial_account_settings"):
                doc = json.loads(body)
                security = doc.get("auth_overlay", {})
                if security.get("email") == email:
                    return {"id": uid, **security}
        finally:
            db.close()
    except Exception:
        raise HTTPException(503, "Hesap deposu kullanılamıyor") from None


async def consume_action_token(request, token, user, kind):
    import hashlib
    import time
    from . import v22_commercial as auth
    values = (hashlib.sha256(token.encode()).hexdigest(), user["id"], kind,
              int(user.get("auth_version", 1)), user.get("email"), time.time())
    pool = getattr(request.app.state, "db_pool", None)
    try:
        if pool is not None:
            db = active_connection(request, user["id"]) or pool
            row = await db.fetchrow("""UPDATE commercial_account_tokens SET used=TRUE
                WHERE token_hash=$1 AND user_id=$2 AND kind=$3 AND auth_version=$4
                  AND email=$5 AND expires>$6 AND used=FALSE RETURNING token_hash""", *values)
            if row is None:
                exists = await db.fetchrow("SELECT token_hash FROM commercial_account_tokens WHERE token_hash=$1", values[0])
                # Pre-migration links retain their existing local one-use guard.
                # Atomically claim their hash too, so stale worker snapshots
                # cannot replay a grandfathered link.
                if exists is None:
                    payload = auth.consume_one_time_token(auth.runtime(request)["state"], token, auth.runtime(request)["secret"], kind=kind)
                    row = await db.fetchrow("""INSERT INTO commercial_account_tokens
                        (token_hash,user_id,kind,auth_version,email,expires,used)
                        VALUES ($1,$2,$3,$4,$5,$6,TRUE) ON CONFLICT (token_hash) DO NOTHING
                        RETURNING token_hash""", *values[:5], float(payload["exp"]))
                    if row is not None:
                        return payload
                raise HTTPException(400, "Güvenlik bağlantısı geçersiz veya kullanılmış")
        else:
            active = active_local_connection(request, user["id"])
            db = active or connection(request)
            try:
                result = db.execute("""UPDATE commercial_account_tokens SET used=TRUE
                    WHERE token_hash=? AND user_id=? AND kind=? AND auth_version=?
                    AND email=? AND expires>? AND used=FALSE""", values)
                if result.rowcount != 1:
                    exists = db.execute("SELECT token_hash FROM commercial_account_tokens WHERE token_hash=?", (values[0],)).fetchone()
                    if exists is None:
                        payload = auth.consume_one_time_token(auth.runtime(request)["state"], token, auth.runtime(request)["secret"], kind=kind)
                        claimed = db.execute("""INSERT INTO commercial_account_tokens
                            (token_hash,user_id,kind,auth_version,email,expires,used)
                            VALUES (?,?,?,?,?,?,TRUE) ON CONFLICT (token_hash) DO NOTHING""",
                            (*values[:5], float(payload["exp"])))
                        if claimed.rowcount == 1:
                            return payload
                    raise HTTPException(400, "Güvenlik bağlantısı geçersiz veya kullanılmış")
            finally:
                if active is None:
                    db.close()
        return auth.verify_token(token, auth.runtime(request)["secret"], expected_kind=kind)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Doğrulama deposu kullanılamıyor") from None
