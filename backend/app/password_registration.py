"""Publish password accounts only after their complete registration commits."""

from __future__ import annotations

import copy
import json
import uuid
from contextlib import asynccontextmanager

from fastapi import HTTPException

from . import account_store, email_verification, v22_commercial as auth
from .error_monitoring import log_database_failure
from .registration_consent import append_registration, require_terms


@asynccontextmanager
async def transaction(request, existing):
    if existing is not None:
        yield existing
    else:
        async with request.app.state.db_pool.acquire() as db, db.transaction():
            yield db


async def fill(request, payload, response, state, doc, user):
    uid = user["id"]
    state["users"].append(user)
    state["profiles"].append(
        {
            "id": uuid.uuid4().hex,
            "user_id": uid,
            "full_name": user["display_name"],
            "avatar_url": None,
            "role": "user",
            "preferences": {},
            "created_at": user["created_at"],
            "updated_at": user["created_at"],
        }
    )
    state["subscriptions"].append(
        {
            "id": uuid.uuid4().hex,
            "user_id": uid,
            "plan": "FREE",
            "status": "inactive",
            "started_at": user["created_at"],
            "expires_at": None,
            "created_at": user["created_at"],
            "updated_at": user["created_at"],
        }
    )
    state["acceptances"].extend(
        append_registration(
            doc, uid, payload.marketing_accepted, "PASSWORD_REGISTRATION"
        )
    )
    if email_verification.enabled():
        user["email_verified_at"] = None
        token = await email_verification.registration_link(request, user)
        await email_verification.complete_registration(request, response, user)
    else:
        token = auth.issue_one_time_token(
            state, user, auth.runtime(request)["secret"], kind="EMAIL_VERIFY"
        )
        await account_store.save_action_token(request, token, user, "EMAIL_VERIFY")
    auth.add_audit(
        state,
        "USER_REGISTERED",
        "Yeni kullanıcı hesabı oluşturuldu.",
        actor=uid,
        subject=uid,
    )
    return token


async def create(request, payload, response):
    from .google_oauth import registration_guard

    require_terms(payload.terms_accepted)
    rt = auth.runtime(request)
    pool = getattr(request.app.state, "db_pool", None)
    email = auth.normalize_email(payload.email)
    user = {
        "id": uuid.uuid4().hex,
        "email": email,
        "display_name": payload.display_name.strip(),
        "role": "CUSTOMER",
        "active": True,
        "auth_version": 1,
        "email_verified": False,
        "password": auth.hash_password(payload.password),
        "created_at": auth.now_iso(),
    }
    stage = "registration_transaction"
    try:
        async with rt["storage_lock"]:
            async with registration_guard(request, email) as existing:
                if pool is not None:
                    async with transaction(request, existing) as db:
                        if existing is None:
                            await db.execute(
                                "SELECT pg_advisory_xact_lock(hashtext($1))",
                                "google-email:" + email,
                            )
                        await db.execute(
                            "SELECT pg_advisory_xact_lock(hashtext($1))",
                            auth.COMMERCIAL_STATE_KEY,
                        )
                        row = await db.fetchrow(
                            "SELECT payload FROM application_state_snapshots "
                            "WHERE state_key=$1 FOR UPDATE",
                            auth.COMMERCIAL_STATE_KEY,
                        )
                        state = auth.sanitize_state(
                            json.loads(row["payload"])
                            if row and isinstance(row["payload"], str)
                            else copy.deepcopy(row["payload"] if row else rt["state"])
                        )
                        duplicate = await db.fetchrow(
                            "SELECT user_id, auth_version, security "
                            "FROM commercial_auth_users "
                            "WHERE lower(security->>'email') = $1",
                            email,
                        )
                        if duplicate or any(
                            item.get("email") == email for item in state["users"]
                        ):
                            return None, None
                        doc = account_store.defaults()
                        marker = account_store.ACTIVE.set(
                            {
                                **(account_store.ACTIVE.get() or {}),
                                (id(request.app), user["id"]): (doc, db),
                            }
                        )
                        try:
                            token = await fill(
                                request, payload, response, state, doc, user
                            )
                            stage = "registration_auth_user"
                            await db.execute(
                                "INSERT INTO commercial_auth_users"
                                "(user_id,auth_version,security) "
                                "VALUES ($1,$2,$3::jsonb)",
                                user["id"],
                                1,
                                json.dumps(auth.auth_security(user)),
                            )
                            stage = "registration_consent"
                            await db.execute(
                                "INSERT INTO commercial_account_settings"
                                "(user_id,payload) VALUES ($1,$2::jsonb)",
                                user["id"],
                                json.dumps(doc),
                            )
                            stage = "registration_snapshot"
                            await db.execute(
                                """INSERT INTO application_state_snapshots
                                   (state_key,updated_at,payload)
                                   VALUES ($1,NOW(),$2::jsonb)
                                   ON CONFLICT(state_key) DO UPDATE
                                   SET updated_at=NOW(),payload=EXCLUDED.payload""",
                                auth.COMMERCIAL_STATE_KEY,
                                json.dumps(state),
                            )
                        finally:
                            account_store.ACTIVE.reset(marker)
                else:
                    state = copy.deepcopy(rt["state"])
                    if any(item.get("email") == email for item in state["users"]):
                        return None, None
                    async with account_store.edit(request, user["id"]) as doc:
                        doc["auth_overlay"] = {
                            "auth_version": 1,
                            **auth.auth_security(user),
                        }
                        token = await fill(request, payload, response, state, doc, user)
            rt["state"] = state
            rt.setdefault("auth_baseline", {})[user["id"]] = {
                "auth_version": 1,
                **auth.auth_security(user),
            }
            if pool is None:
                auth.save_state(state)
            else:
                rt["storage_status"] = "POSTGRESQL_KALICI"
        return user, token
    except HTTPException:
        raise
    except Exception as exc:
        log_database_failure(auth.logger, stage, exc)
        raise HTTPException(
            503,
            "Kalıcı hesap veritabanı hazır değil; kayıt güvenli şekilde tamamlanamadı",
        ) from None


async def rollback(request, uid):
    rt = auth.runtime(request)
    pool = getattr(request.app.state, "db_pool", None)
    try:
        async with rt["storage_lock"]:
            state = copy.deepcopy(rt["state"])
            if pool is not None:
                async with pool.acquire() as db, db.transaction():
                    await db.execute(
                        "SELECT pg_advisory_xact_lock(hashtext($1))",
                        auth.COMMERCIAL_STATE_KEY,
                    )
                    row = await db.fetchrow(
                        "SELECT payload FROM application_state_snapshots "
                        "WHERE state_key=$1 FOR UPDATE",
                        auth.COMMERCIAL_STATE_KEY,
                    )
                    if row:
                        state = (
                            json.loads(row["payload"])
                            if isinstance(row["payload"], str)
                            else copy.deepcopy(row["payload"])
                        )
                    remove_user(state, uid)
                    for table in (
                        "commercial_account_tokens",
                        "commercial_account_settings",
                        "commercial_auth_users",
                    ):
                        await db.execute(f"DELETE FROM {table} WHERE user_id=$1", uid)
                    await db.execute(
                        """INSERT INTO application_state_snapshots
                           (state_key,updated_at,payload)
                           VALUES ($1,NOW(),$2::jsonb)
                           ON CONFLICT(state_key) DO UPDATE
                           SET updated_at=NOW(),payload=EXCLUDED.payload""",
                        auth.COMMERCIAL_STATE_KEY,
                        json.dumps(state),
                    )
            else:
                db = account_store.connection(request)
                try:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "DELETE FROM commercial_account_tokens WHERE user_id=?", (uid,)
                    )
                    db.execute(
                        "DELETE FROM commercial_account_settings WHERE user_id=?",
                        (uid,),
                    )
                    db.commit()
                finally:
                    db.close()
                remove_user(state, uid)
            rt["state"] = state
            rt.setdefault("auth_baseline", {}).pop(uid, None)
            if pool is None:
                auth.save_state(state)
    except Exception as exc:
        log_database_failure(auth.logger, "registration_cleanup", exc)
        raise HTTPException(
            503, "Başarısız kayıt güvenli şekilde temizlenemedi"
        ) from None


def remove_user(state, uid):
    for key, field in (
        ("users", "id"),
        ("profiles", "user_id"),
        ("subscriptions", "user_id"),
        ("auth_tokens", "user_id"),
        ("acceptances", "user_id"),
    ):
        state[key] = [item for item in state.get(key, []) if item.get(field) != uid]
