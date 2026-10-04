"""Account erasure with anonymized financial evidence and resurrection guards.

Only application-controlled stores are covered. Provider logs, external backups
and other browsers/devices cannot be erased by this service.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import HTTPException

ERASED = "ERASED"
PERSONAL_LISTS = (
    "users", "profiles", "licenses", "agents", "auth_tokens", "pairing_codes",
    "leads", "support_tickets", "acceptances", "stripe_checkout_sessions",
)
RETAINED_LISTS = ("subscriptions", "demo_invoices", "audit")
RETENTION_FIELDS = frozenset({
    "plan", "status", "kind", "created_at", "updated_at", "occurred_at", "event_time",
    "started_at", "expires_at", "current_period_start", "current_period_end",
    "amount", "amount_usd", "amount_usdt", "currency", "cost_usd", "fee", "fees",
    "price", "entry_price", "exit_price", "quantity", "symbol", "side", "direction",
    "realized_pnl", "pnl", "profit", "net_usdt", "gross_usdt", "notional_usdt",
    "input_tokens", "output_tokens", "called_at", "month_key", "demo_only",
    "verified_realized", "reduce_only", "executed_at", "closed_at", "opened_at",
})
PERSONAL_TABLES = (
    "trading_accounts", "protrebot_exchange_session_vault", "assistant_usage",
    "assistant_proactive_state", "analyst_credits", "analyst_requests", "analyst_cache",
)


def user_hash(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def owned(row: Any, user: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    uid = str(user["id"])
    email = str(user.get("email") or "").casefold()
    if any(str(row.get(key) or "") == uid for key in (
        "id", "user_id", "userId", "_user_id", "owner_user_id", "ownerUserId",
        "actor", "subject", "customer_id",
    )):
        return True
    if any(row.get(key) for key in ("user_id", "userId", "_user_id", "owner_user_id", "ownerUserId")):
        return False
    if row.get("role") in {"OWNER", "CUSTOMER"} and row.get("id"):
        return False
    return bool(email and str(row.get("email") or "").casefold() == email)


def retained(row: dict[str, Any]) -> dict[str, Any]:
    # Free-form messages, IDs, provider references and nested payloads may
    # contain account data; retain only financial/trade/audit scalar evidence.
    result = {
        key: value for key, value in row.items()
        if key in RETENTION_FIELDS and isinstance(value, (int, float, bool, type(None)))
    }
    for key, value in row.items():
        if key in RETENTION_FIELDS and isinstance(value, str):
            if key in {"symbol", "side", "direction", "plan", "status", "kind", "currency"}:
                if value.replace("_", "").replace("-", "").replace("/", "").isalnum() and len(value) <= 40:
                    result[key] = value
            elif key.endswith("_at") or key in {"event_time", "month_key", "current_period_start", "current_period_end"}:
                if all(character in "0123456789-:.+TZ " for character in value):
                    result[key] = value
            elif key in {"amount", "amount_usd", "amount_usdt", "cost_usd", "fee", "fees", "price", "quantity", "realized_pnl", "pnl", "profit"}:
                try:
                    if Decimal(value).is_finite():
                        result[key] = value
                except InvalidOperation:
                    pass
    result["user_id"] = ERASED
    return result


def scrub_payload(payload: Any, user: dict[str, Any]) -> Any:
    """Scrub one account without modifying unrelated tenants' records."""
    uid, email = str(user["id"]), str(user.get("email") or "")
    if isinstance(payload, list):
        return [scrub_payload(item, user) for item in payload]
    if not isinstance(payload, dict):
        if isinstance(payload, str):
            for identifier in (uid, email):
                if identifier:
                    payload = payload.replace(identifier, ERASED)
        return payload
    if owned(payload, user):
        return retained(payload)
    result = {}
    for key, value in payload.items():
        if key == uid:
            continue
        if key in PERSONAL_LISTS and isinstance(value, list):
            result[key] = [scrub_payload(item, user) for item in value if not owned(item, user)]
        elif key in RETAINED_LISTS and isinstance(value, list):
            result[key] = [retained(item) if owned(item, user) else scrub_payload(item, user) for item in value]
        else:
            result[key] = scrub_payload(value, user)
    return result


def scrub_commercial_state(state: dict[str, Any], user: dict[str, Any]) -> None:
    cleaned = scrub_payload(state, user)
    state.clear()
    state.update(cleaned)


def tombstoned_subjects(payload: Any, hashes: set[str]) -> list[dict[str, Any]]:
    """Find erased references even when a stale writer omitted the users list."""
    subjects: dict[str, dict[str, Any]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, str):
            if user_hash(value) in hashes:
                subjects.setdefault(value, {"id": value})
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for field in ("id", "user_id", "_user_id", "owner_user_id", "actor", "subject"):
                uid = str(value.get(field) or "")
                if uid and user_hash(uid) in hashes:
                    subject = subjects.setdefault(uid, {"id": uid})
                    if value.get("email") and any(
                        str(value.get(key) or "") == uid for key in ("id", "user_id", "_user_id")
                    ):
                        subject["email"] = str(value["email"])
            for key, item in value.items():
                if user_hash(str(key)) in hashes:
                    subjects.setdefault(str(key), {"id": str(key)})
                visit(item)

    visit(payload)
    return list(subjects.values())


async def apply_erasure_tombstones(application: Any) -> None:
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        return
    rows = await pool.fetch("SELECT user_hash FROM commercial_erased_users")
    hashes = {row["user_hash"] for row in rows}
    rt = application.state.v22_commercial
    erased = tombstoned_subjects(rt["state"], hashes)
    for user in erased:
        scrub_commercial_state(rt["state"], user)
        rt.setdefault("auth_baseline", {}).pop(user["id"], None)
    for user in erased:
        await erase_runtime(application, user, [])
    if erased:
        rt["state"]["_database_dirty"] = True
        rt["state"]["_database_revision"] = int(rt["state"].get("_database_revision", 0)) + 1
    if hashes and (erased or rt.get("erasure_tombstones_seen") != hashes):
        from .local_storage import DATA_DIR, PROJECT_DATA_DIR

        paths = [
            directory / name for directory in {DATA_DIR, PROJECT_DATA_DIR}
            for name in (
                "v22_commercial_state.json", "v22_commercial_state.backup.json",
                "binance_demo_runtime.json", "v21_demo_state.json", "v21_demo_state.backup.json",
            )
        ]
        await asyncio.to_thread(scrub_tombstoned_local_snapshots, paths, hashes)
    rt["erasure_tombstones_seen"] = hashes


async def ensure_erasure_schema(pool: Any) -> None:
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS commercial_erased_users (
          user_hash TEXT PRIMARY KEY, erased_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE OR REPLACE FUNCTION commercial_erasure_user_guard() RETURNS trigger AS $$
        BEGIN
          IF EXISTS (SELECT 1 FROM commercial_erased_users
                     WHERE user_hash = encode(sha256(convert_to(NEW.user_id, 'UTF8')), 'hex')) THEN
            IF TG_TABLE_NAME = 'assistant_calls' THEN
              NEW.user_id := 'ERASED';
              RETURN NEW;
            ELSIF TG_TABLE_NAME = 'subscriptions' THEN
              NEW.user_id := 'ERASED';
              NEW.stripe_customer_id := NULL;
              NEW.stripe_subscription_id := NULL;
              NEW.stripe_price_id := NULL;
              RETURN NEW;
            ELSIF TG_TABLE_NAME = 'error_events' THEN
              NEW.user_id := NULL; NEW.request_id := NULL;
              NEW.message := 'Account erased'; NEW.context := '{}'::jsonb;
              NEW.details := '{}'::jsonb; NEW.stack := NULL; NEW.notes := NULL;
              NEW.route := NULL; NEW.fingerprint := 'ERASED';
              RETURN NEW;
            END IF;
            RETURN NULL;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        DROP TRIGGER IF EXISTS commercial_erasure_guard ON commercial_auth_users;
        CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON commercial_auth_users
          FOR EACH ROW EXECUTE FUNCTION commercial_erasure_user_guard();
        CREATE OR REPLACE FUNCTION commercial_erasure_json(data jsonb) RETURNS jsonb AS $$
        DECLARE field text; item jsonb; result jsonb; identity text;
        BEGIN
          IF jsonb_typeof(data) = 'array' THEN
            SELECT COALESCE(jsonb_agg(commercial_erasure_json(value)), '[]'::jsonb)
              INTO result FROM jsonb_array_elements(data);
            RETURN result;
          ELSIF jsonb_typeof(data) <> 'object' THEN
            RETURN data;
          END IF;
          FOREACH field IN ARRAY ARRAY['id','user_id','_user_id','owner_user_id','actor','subject']
          LOOP
            identity := data->>field;
            IF identity IS NOT NULL AND EXISTS (SELECT 1 FROM commercial_erased_users
                WHERE user_hash = encode(sha256(convert_to(identity, 'UTF8')), 'hex')) THEN
              SELECT COALESCE(jsonb_object_agg(key,value), '{}'::jsonb) INTO result
              FROM jsonb_each(data) WHERE key IN ('plan','status','kind','created_at','updated_at',
                'amount','amount_usd','amount_usdt','currency','price','quantity','symbol','side',
                'realized_pnl','pnl','profit','fee','fees','cost_usd');
              RETURN result || '{"user_id":"ERASED"}'::jsonb;
            END IF;
          END LOOP;
          result := '{}'::jsonb;
          FOR field,item IN SELECT key,value FROM jsonb_each(data) LOOP
            IF NOT EXISTS (SELECT 1 FROM commercial_erased_users
                WHERE user_hash = encode(sha256(convert_to(field, 'UTF8')), 'hex')) THEN
              result := result || jsonb_build_object(field,commercial_erasure_json(item));
            END IF;
          END LOOP;
          RETURN result;
        END;
        $$ LANGUAGE plpgsql;
        CREATE OR REPLACE FUNCTION commercial_erasure_payload_guard() RETURNS trigger AS $$
        DECLARE cleaned jsonb;
        BEGIN
          cleaned := commercial_erasure_json(NEW.payload);
          IF TG_TABLE_NAME = 'protrebot_cloud_evidence' AND cleaned IS DISTINCT FROM NEW.payload THEN
            NEW.event_key := 'erased-event:' || md5(random()::text || clock_timestamp()::text);
          END IF;
          NEW.payload := cleaned;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE OR REPLACE FUNCTION commercial_erasure_snapshot_guard() RETURNS trigger AS $$
        DECLARE erased_user jsonb; uid text; field text; items jsonb;
        BEGIN
          IF NEW.state_key LIKE 'binance_demo:user:%' OR NEW.state_key LIKE 'v21_demo:user:%' THEN
            IF EXISTS (SELECT 1 FROM commercial_erased_users
                       WHERE user_hash = encode(sha256(convert_to(split_part(NEW.state_key, ':', 3), 'UTF8')), 'hex')) THEN
              RETURN NULL;
            END IF;
          END IF;
          IF NEW.state_key = 'v22-commercial' THEN
            FOR erased_user IN SELECT value FROM jsonb_array_elements(COALESCE(NEW.payload->'users', '[]'::jsonb))
              WHERE EXISTS (SELECT 1 FROM commercial_erased_users
                WHERE user_hash = encode(sha256(convert_to(value->>'id', 'UTF8')), 'hex'))
            LOOP
              uid := erased_user->>'id';
              FOREACH field IN ARRAY ARRAY['users','profiles','licenses','agents','auth_tokens',
                  'pairing_codes','leads','support_tickets','acceptances','stripe_checkout_sessions']
              LOOP
                SELECT COALESCE(jsonb_agg(value), '[]'::jsonb) INTO items
                FROM jsonb_array_elements(COALESCE(NEW.payload->field, '[]'::jsonb))
                WHERE COALESCE(value->>'id','') <> uid AND COALESCE(value->>'user_id','') <> uid
                  AND COALESCE(value->>'email','') <> COALESCE(erased_user->>'email', '');
                NEW.payload := jsonb_set(NEW.payload, ARRAY[field], items);
              END LOOP;
              FOREACH field IN ARRAY ARRAY['subscriptions','demo_invoices','audit']
              LOOP
                SELECT COALESCE(jsonb_agg(CASE
                  WHEN value->>'user_id' = uid OR value->>'actor' = uid OR value->>'subject' = uid
                  THEN (SELECT COALESCE(jsonb_object_agg(key, val), '{}'::jsonb)
                        FROM jsonb_each(value) AS entry(key,val)
                        WHERE key IN ('plan','status','kind','created_at','updated_at',
                          'amount','amount_usd','amount_usdt','currency','price','quantity','symbol','side','realized_pnl'))
                        || '{"user_id":"ERASED"}'::jsonb
                  ELSE value END), '[]'::jsonb) INTO items
                FROM jsonb_array_elements(COALESCE(NEW.payload->field, '[]'::jsonb));
                NEW.payload := jsonb_set(NEW.payload, ARRAY[field], items);
              END LOOP;
            END LOOP;
          END IF;
          IF NEW.state_key = 'v22-commercial' THEN
            SELECT COALESCE(jsonb_agg(value || jsonb_build_object('auth_version',auth.auth_version)
                       || auth.security), '[]'::jsonb) INTO items
            FROM jsonb_array_elements(COALESCE(NEW.payload->'users','[]'::jsonb))
            JOIN commercial_auth_users AS auth ON auth.user_id = value->>'id';
            NEW.payload := jsonb_set(NEW.payload, ARRAY['users'], items);
          END IF;
          NEW.payload := commercial_erasure_json(NEW.payload);
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        DROP TRIGGER IF EXISTS commercial_erasure_guard ON application_state_snapshots;
        CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON application_state_snapshots
          FOR EACH ROW EXECUTE FUNCTION commercial_erasure_snapshot_guard();
    """)


async def table_exists(connection: Any, name: str) -> bool:
    return bool(await connection.fetchval("SELECT to_regclass($1)", name))


async def erase_database(application: Any, user: dict[str, Any]) -> list[str]:
    from . import v22_commercial as auth

    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        return []
    uid = user["id"]
    session_ids = []
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "INSERT INTO commercial_erased_users (user_hash) VALUES ($1) ON CONFLICT DO NOTHING", user_hash(uid),
        )
        for table in PERSONAL_TABLES:
            if not await table_exists(connection, table):
                continue
            await connection.execute(f"""
                DROP TRIGGER IF EXISTS commercial_erasure_guard ON {table};
                CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION commercial_erasure_user_guard();
            """)
            if table == "protrebot_exchange_session_vault":
                session_ids = [row["session_id"] for row in await connection.fetch(
                    "SELECT session_id FROM protrebot_exchange_session_vault WHERE user_id = $1", uid,
                )]
            await connection.execute(f"DELETE FROM {table} WHERE user_id = $1", uid)
        if await table_exists(connection, "assistant_calls"):
            await connection.execute("""
                DROP TRIGGER IF EXISTS commercial_erasure_guard ON assistant_calls;
                CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON assistant_calls
                FOR EACH ROW EXECUTE FUNCTION commercial_erasure_user_guard();
            """)
            await connection.execute("UPDATE assistant_calls SET user_id = $1 WHERE user_id = $2", ERASED, uid)
        if await table_exists(connection, "subscriptions"):
            await connection.execute("""
                DROP TRIGGER IF EXISTS commercial_erasure_guard ON subscriptions;
                CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON subscriptions
                FOR EACH ROW EXECUTE FUNCTION commercial_erasure_user_guard();
            """)
            await connection.execute("""
                UPDATE subscriptions SET user_id = $1, stripe_customer_id = NULL,
                  stripe_subscription_id = NULL, stripe_price_id = NULL WHERE user_id = $2
            """, ERASED, uid)
        if await table_exists(connection, "error_events"):
            await connection.execute("""
                DROP TRIGGER IF EXISTS commercial_erasure_guard ON error_events;
                CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON error_events
                FOR EACH ROW EXECUTE FUNCTION commercial_erasure_user_guard();
            """)
            await connection.execute("""
                UPDATE error_events SET user_id = NULL, request_id = NULL, message = 'Account erased',
                  context = '{}'::jsonb, details = '{}'::jsonb, stack = NULL, notes = NULL,
                  route = NULL, fingerprint = 'ERASED' WHERE user_id = $1
            """, uid)
        if await table_exists(connection, "commercial_auth_limits"):
            accounts = {uid, auth.normalize_email(str(user.get("email") or ""))}
            buckets = [
                f"{action}:account:{hashlib.sha256(account.encode('utf-8')).hexdigest()}"
                for action in auth.AUTH_LIMITS for account in accounts if account
            ]
            await connection.execute("DELETE FROM commercial_auth_limits WHERE bucket = ANY($1::text[])", buckets)
        for table, key in (
            ("application_state_snapshots", "state_key"), ("protrebot_cloud_state", "state_key"),
            ("protrebot_cloud_evidence", "event_key"), ("paper_account_snapshots", "account_key"),
        ):
            if not await table_exists(connection, table):
                continue
            if table != "application_state_snapshots":
                await connection.execute(f"""
                    DROP TRIGGER IF EXISTS commercial_erasure_guard ON {table};
                    CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON {table}
                    FOR EACH ROW EXECUTE FUNCTION commercial_erasure_payload_guard();
                """)
            rows = await connection.fetch(f"SELECT {key}, payload FROM {table} FOR UPDATE")
            for row in rows:
                payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                identity = row[key]
                if table == "application_state_snapshots" and identity in {f"v21_demo:user:{uid}", f"binance_demo:user:{uid}"}:
                    # Keep trade-only evidence under an unrelated identifier.
                    evidence = {}
                    for category in ("journal", "events", "trades", "automation_trades", "paper_positions"):
                        value = payload.get(category, [])
                        if isinstance(value, list):
                            evidence[category] = [retained(item) for item in value if isinstance(item, dict)]
                    await connection.execute(f"DELETE FROM {table} WHERE {key} = $1", identity)
                    if any(evidence.values()):
                        await connection.execute("""
                            INSERT INTO application_state_snapshots (state_key, payload)
                            VALUES ($1, $2::jsonb)
                        """, f"erased-trades:{uuid.uuid4().hex}", json.dumps(evidence))
                    continue
                cleaned = scrub_payload(payload, user)
                if cleaned != payload:
                    if table == "protrebot_cloud_evidence":
                        await connection.execute(
                            f"UPDATE {table} SET payload = $2::jsonb, {key} = $3 WHERE {key} = $1",
                            identity, json.dumps(cleaned), f"erased-event:{uuid.uuid4().hex}",
                        )
                        continue
                    await connection.execute(
                        f"UPDATE {table} SET payload = $2::jsonb WHERE {key} = $1", identity, json.dumps(cleaned),
                    )
        await connection.execute("DELETE FROM commercial_auth_users WHERE user_id = $1", uid)
    return session_ids


async def erase_runtime(application: Any, user: dict[str, Any], session_ids: list[str]) -> None:
    from . import exchange_connections as vault
    from . import v22_commercial as auth

    uid = user["id"]
    accounts = {uid, auth.normalize_email(str(user.get("email") or ""))}
    for action in auth.AUTH_LIMITS:
        for account in accounts:
            if account:
                bucket = f"{action}:account:{hashlib.sha256(account.encode('utf-8')).hexdigest()}"
                auth.LOGIN_ATTEMPTS.pop(bucket, None)
    pending = getattr(application.state, "_binance_demo_persistence_tasks", {})
    tasks = list(pending.pop(uid, [])) if isinstance(pending, dict) else []
    assistant = getattr(application.state, "assistant_service", None)
    candidates = list(getattr(assistant, "pending", ()))
    candidates.extend(getattr(application.state, "_v21_persistence_tasks", ()))
    for task in candidates:
        frame = getattr(task.get_coro(), "cr_frame", None)
        if frame is not None and str(frame.f_locals.get("user_id") or "") == uid:
            tasks.append(task)
    tasks = [task for task in set(tasks) if task is not asyncio.current_task() and not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for name in ("_binance_demo_user_state", "_v21_demo_user_state"):
        store = getattr(application.state, name, {})
        if isinstance(store, dict):
            personal = store.pop(uid, None)
            if isinstance(personal, dict):
                personal.clear()
                personal.update({
                    "_persistence_blocked": True, "_user_id": ERASED, "_session_id": "",
                    "connected": False, "armed_until": 0, "auto": {"enabled": False},
                })
    vault.clear_session_vault_for_user_cache(application, uid, session_ids)
    for name in ("binance_demo", "v21_demo", "v25_execution", "v27_cloud", "paper", "paper_bot", "error_events"):
        state = getattr(application.state, name, None)
        if isinstance(state, dict):
            cleaned = scrub_payload(state, user)
            state.clear()
            state.update(cleaned)


def scrub_local_files(paths: list[Path], user: dict[str, Any]) -> None:
    for path in paths:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        cleaned = scrub_payload(payload, user)
        if cleaned != payload:
            temporary = path.with_name(path.name + ".erasing")
            temporary.write_text(json.dumps(cleaned, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)


def scrub_tombstoned_local_snapshots(paths: list[Path], hashes: set[str]) -> None:
    """Scrub old local backups even if the current snapshot no longer has users."""
    for path in paths:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        erased = tombstoned_subjects(payload, hashes)
        if not erased:
            continue
        for user in erased:
            payload = scrub_payload(payload, user)
        temporary = path.with_name(path.name + ".erasing")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)


def scrub_sqlite_file(path: Path, user_id: str) -> None:
    if not path.exists():
        return
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in PERSONAL_TABLES:
            if table in tables:
                connection.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        if "assistant_calls" in tables:
            connection.execute("UPDATE assistant_calls SET user_id = ? WHERE user_id = ?", (ERASED, user_id))
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")


async def erase_user_account(request: Any, user: dict[str, Any]) -> dict[str, Any]:
    from . import v22_commercial as auth
    from .local_storage import DATA_DIR, PROJECT_DATA_DIR

    rt = auth.runtime(request)
    if user.get("role") == "OWNER":
        raise HTTPException(409, "OWNER hesabı silinemez")
    async with rt["lock"]:
        # Revocation is deliberately committed before the deletion transaction.
        # Any later failure leaves the account disabled, never authenticated.
        pool = getattr(request.app.state, "db_pool", None)
        already_erased = False
        if pool is not None:
            try:
                already_erased = bool(await pool.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM commercial_erased_users WHERE user_hash = $1)", user_hash(user["id"]),
                ))
            except Exception as exc:
                raise HTTPException(503, "Account erasure storage is unavailable") from exc
        if not already_erased:
            await auth.refresh_auth_security(request, user)
            if user.get("role") == "OWNER":
                raise HTTPException(409, "OWNER hesabı silinemez")
            await auth.invalidate_user_sessions(
                request, user, security_updates={"active": False, "password": {}},
                expected_version=int(user.get("auth_version", 1)),
            )
        try:
            await erase_runtime(request.app, user, [])
            session_ids = await erase_database(request.app, user)
            await erase_runtime(request.app, user, session_ids)
            scrub_commercial_state(rt["state"], user)
            rt.setdefault("auth_baseline", {}).pop(user["id"], None)
            auth.add_audit(rt["state"], "ACCOUNT_ERASED", "Account data erased; evidence anonymized.", actor=ERASED, subject=ERASED)
            auth.save_state(rt["state"])
            persisted = await auth.persist_v22_commercial(request.app)
            if auth.DURABLE_AUTH_REQUIRED and not persisted:
                raise RuntimeError("Erased commercial state could not be persisted")
            paths = [auth.STATE_PATH, auth.BACKUP_PATH, auth.STATE_PATH.with_suffix(".tmp")]
            for directory in {DATA_DIR, PROJECT_DATA_DIR}:
                for name in (
                    "v22_commercial_state.json", "v22_commercial_state.backup.json",
                    "binance_demo_runtime.json", "v21_demo_state.json", "v21_demo_state.backup.json",
                ):
                    paths.append(directory / name)
            await asyncio.to_thread(scrub_local_files, paths, user)
            sqlite_paths = {DATA_DIR / "assistant_usage.sqlite3", DATA_DIR / "analyst_credits.sqlite3"}
            for service_name in ("assistant_service", "analyst_credits"):
                service = getattr(request.app.state, service_name, None)
                path = getattr(getattr(service, "store", None), "path", None)
                if isinstance(path, Path):
                    sqlite_paths.add(path)
            for path in sqlite_paths:
                await asyncio.to_thread(scrub_sqlite_file, path, user["id"])
        except Exception as exc:
            raise HTTPException(503, "Account disabled; application data erasure is incomplete. Retry or contact support.") from exc
    return {
        "ok": True, "message": "Account data erased; financial/trade/audit evidence retained anonymously.",
        "erasure": {
            "application_stores": "erased", "retention": "unlinked_financial_trade_audit",
            "external_stores": "not_erased",
            "limitations": [
                "provider_logs_and_backups", "external_backups", "other_browsers_and_devices",
                "other_process_memory_and_unmanaged_local_files", "unattributed_environment_or_desktop_credentials",
                "external_billing_subscriptions_not_cancelled",
            ],
        },
    }
