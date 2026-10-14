"""Real local PostgreSQL only; isolated schemas, never the application's DB URL."""
import asyncio
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import asyncpg
import pytest
from app import binance_manual_admin as admin
from app import binance_manual_payments as payments
from app import binance_notifications as notifications
from app import free_trial
from app.audit_log import AuditActor
from app.v22_commercial import ensure_commercial_schema

MIGRATIONS = Path(__file__).parents[1] / "migrations"
FEATURE = [MIGRATIONS / name for name in (
    "20261011_001_binance_manual_payments.sql",
    "20261011_002_binance_manual_entitlements.sql",
    "20261011_003_binance_notifications.sql",
)]
ROLLBACK = MIGRATIONS / "rollback" / "20261011_binance_empty.sql"
DSN = os.getenv("BINANCE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="Explicit isolated local PostgreSQL test DSN required")


def local_dsn():
    if not DSN:
        raise RuntimeError("Local PostgreSQL DSN required")
    parsed = urlsplit(DSN)
    if parsed.scheme != "postgresql" or parsed.hostname not in {"127.0.0.1", "::1"} \
            or parsed.path != "/binance_stage5_test" or parsed.query or parsed.fragment:
        raise RuntimeError("Only dedicated loopback binance_stage5_test database is allowed")
    return DSN


async def setup():
    conn = await asyncpg.connect(local_dsn())
    schema = "binance_test_" + uuid.uuid4().hex
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}"')
    app = SimpleNamespace(state=SimpleNamespace(db_pool=conn))
    await ensure_commercial_schema(app)
    for file in sorted(MIGRATIONS.glob("20261010_*.sql")):
        await conn.execute(file.read_text(encoding="utf-8"))
    before = await schema_contract(conn)
    for file in FEATURE:
        await conn.execute(file.read_text(encoding="utf-8"))
    for uid, role in (("owner","OWNER"),("user-a","CUSTOMER"),("user-b","CUSTOMER"),("mod","MODERATOR")):
        await conn.execute(
            "INSERT INTO commercial_auth_users(user_id,auth_version,security) VALUES ($1,1,$2::jsonb)",
            uid, json.dumps({"role":role,"active":True,"email_verified":True,"email":uid+"@example.test","display_name":uid}),
        )
    return conn, schema, before


async def schema_contract(conn):
    constraints = await conn.fetch(
        """SELECT c.relname,p.conname,pg_get_constraintdef(p.oid) AS definition
           FROM pg_constraint p JOIN pg_class c ON c.oid=p.conrelid
           JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema()
           AND c.relname IN ('notification_outbox','audit_log')
           ORDER BY c.relname,p.conname""",
    )
    functions = await conn.fetch(
        """SELECT p.proname,pg_get_functiondef(p.oid) AS definition FROM pg_proc p
           JOIN pg_namespace n ON n.oid=p.pronamespace
           WHERE n.nspname=current_schema() AND p.proname IN ('notification_outbox_guard','audit_log_reject_mutation')
           ORDER BY p.proname""",
    )
    return ([dict(row) for row in constraints], [dict(row) for row in functions])


async def cleanup(conn, schema):
    if conn.is_in_transaction():
        await conn.execute("ROLLBACK")
    await conn.execute('SET search_path TO public')
    await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await conn.close()


async def pending(conn, uid="user-a", number="BINANCE-123"):
    row = await payments.create_request(conn, uid)
    return await payments.submit_transaction(conn, uid, row.id, number)


async def approve(conn, row, role="OWNER"):
    async with conn.transaction():
        return await admin.decision(conn, AuditActor("owner",role,str(uuid.uuid4())), row.id, "approved")


def scenario(action):
    async def run():
        conn, schema, before = await setup()
        try:
            await action(conn, before)
        finally:
            await cleanup(conn, schema)
    asyncio.run(run())


def test_real_original_risk_audit_and_stale_writer_guard():
    from app import v25_execution as live
    from app.audit_log import write_audit
    from app.original_live_risk import audit_snapshot

    async def check(conn, before):
        await conn.execute((MIGRATIONS / "20261014_001_original_live_risk_audit.sql").read_text(encoding="utf-8"))
        await conn.execute("""CREATE TABLE application_state_snapshots (
            state_key TEXT PRIMARY KEY, updated_at TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL
        )""")
        previous = {"risk_percent": 0.5, "daily_loss_percent": 5}
        choice = {**previous, "risk_percent": 10, "daily_loss_percent": 25,
                  "high_risk_confirmed": True, "warning_balance_usdt": 1000, "risk_revision": 1}
        payload = {"persistence_revision": 1, "original_v2_live": choice}
        assert await conn.fetchrow(live.V25_RISK_SNAPSHOT_UPSERT_SQL, live.V25_SNAPSHOT_KEY, json.dumps(payload), 1) is None
        with pytest.raises(RuntimeError, match="rollback probe"):
            async with conn.transaction():
                await conn.fetchrow(live.V25_RISK_SNAPSHOT_UPSERT_SQL, live.V25_SNAPSHOT_KEY, json.dumps(payload), 0)
                await write_audit(conn, AuditActor("owner", "OWNER", str(uuid.uuid4())),
                                  "original_live.risk_changed", "USER", "owner",
                                  audit_snapshot(previous), audit_snapshot(choice))
                raise RuntimeError("rollback probe")
        assert await conn.fetchval("SELECT COUNT(*) FROM application_state_snapshots") == 0
        assert await conn.fetchval("SELECT COUNT(*) FROM audit_log WHERE action='original_live.risk_changed'") == 0
        async with conn.transaction():
            await conn.fetchrow(live.V25_RISK_SNAPSHOT_UPSERT_SQL, live.V25_SNAPSHOT_KEY, json.dumps(payload), 0)
            await write_audit(conn, AuditActor("owner", "OWNER", str(uuid.uuid4())),
                              "original_live.risk_changed", "USER", "owner",
                              audit_snapshot(previous), audit_snapshot(choice))
        stale = {"persistence_revision": 2, "original_v2_live": previous}
        assert await conn.fetchrow(live.V25_SNAPSHOT_UPSERT_SQL, live.V25_SNAPSHOT_KEY, json.dumps(stale)) is None
        assert await conn.fetchval("SELECT payload->'original_v2_live'->>'risk_percent' FROM application_state_snapshots") == "10"
        assert await conn.fetchval("SELECT COUNT(*) FROM audit_log WHERE action='original_live.risk_changed'") == 1
        payload["persistence_revision"] = 3
        assert await conn.fetchrow(live.V25_SNAPSHOT_UPSERT_SQL, live.V25_SNAPSHOT_KEY, json.dumps(payload)) is not None
        payload["persistence_revision"] = 4
        assert await conn.fetchrow(live.V25_RISK_SNAPSHOT_UPSERT_SQL, live.V25_SNAPSHOT_KEY, json.dumps(payload), 0) is None
        payload["original_v2_live"]["risk_revision"] = 2
        assert await conn.fetchrow(live.V25_RISK_SNAPSHOT_UPSERT_SQL, live.V25_SNAPSHOT_KEY, json.dumps(payload), 1) is not None
    scenario(check)


def test_real_free_trial_schema_guards_and_transaction_rollback(monkeypatch):
    monkeypatch.setenv("FREE_TRIAL_ENABLED", "true")

    async def check(conn, before):
        await conn.execute((MIGRATIONS / "20261011_004_member_free_trials.sql").read_text(encoding="utf-8"))
        user = {"id": "user-a", "auth_version": 1}
        with pytest.raises(RuntimeError, match="rollback probe"):
            async with conn.transaction():
                await free_trial.start(conn, user)
                raise RuntimeError("rollback probe")
        assert await conn.fetchval("SELECT COUNT(*) FROM member_free_trials") == 0
        first = await free_trial.start(conn, user)
        assert first["ends_at"] - first["started_at"] == timedelta(days=7)
        repeated = await free_trial.start(conn, user)
        assert repeated["started_at"] == first["started_at"] and repeated["ends_at"] == first["ends_at"]
        assert await conn.fetchval("SELECT COUNT(*) FROM subscriptions") == 0
        assert await conn.fetchval("SELECT COUNT(*) FROM binance_manual_periods") == 0
        with pytest.raises(asyncpg.UniqueViolationError):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO member_free_trials(user_hash,user_id) VALUES ($1,'user-a')",
                    free_trial.account_hash("user-a"),
                )
        for query in (
            "UPDATE member_free_trials SET ends_at=ends_at+INTERVAL '1 day'",
            "DELETE FROM member_free_trials",
        ):
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                async with conn.transaction():
                    await conn.execute(query)
        await conn.execute(
            "UPDATE commercial_auth_users SET security=jsonb_set(security,'{email_verified}','false') WHERE user_id='user-b'",
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO member_free_trials(user_hash,user_id) VALUES ($1,'user-b')",
                    free_trial.account_hash("user-b"),
                )
        await conn.execute(
            "INSERT INTO commercial_erased_users(user_hash) VALUES ($1)", free_trial.account_hash("user-a"),
        )
        row = await conn.fetchrow("SELECT * FROM member_free_trials")
        assert row["user_id"] == "ERASED" and row["revoked_at"] is not None
        assert row["ends_at"] == first["ends_at"]
        assert await free_trial.current(conn, "user-a") is None
        triggers = await conn.fetch(
            """SELECT tgenabled FROM pg_trigger
               WHERE tgname IN ('member_free_trial_immutable','member_free_trial_tombstone')
                 AND tgrelid IN ('member_free_trials'::regclass,'commercial_erased_users'::regclass)""",
        )
        assert len(triggers) == 2 and all(row["tgenabled"] == b"A" for row in triggers)

    scenario(check)


def test_real_schema_unique_transaction_fingerprint_and_amount_constraints():
    async def check(conn, before):
        row = await pending(conn)
        other = await payments.create_request(conn,"user-b")
        with pytest.raises(payments.HTTPException) as failure:
            await payments.submit_transaction(conn,"user-b",other.id,"BINANCE-123")
        assert failure.value.status_code == 409
        assert await conn.fetchval("SELECT status FROM binance_manual_payment_requests WHERE id=$1",other.id) == "created"
        async with conn.transaction():
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute("INSERT INTO binance_manual_payment_requests(id,user_id,reference_code,amount) VALUES ($1,'mod','KT-ABC234',1)",
                                       uuid.uuid4().hex)
        triggers = await conn.fetch(
            """SELECT tgname,tgenabled FROM pg_trigger WHERE tgrelid IN (
               'binance_manual_payment_requests'::regclass,'binance_manual_periods'::regclass,
               'binance_notification_reads'::regclass) AND NOT tgisinternal""",
        )
        assert len(triggers) == 8 and all(t["tgenabled"] == b"A" for t in triggers)
        assert await conn.fetchval("SELECT expires_at-created_at FROM binance_manual_payment_requests WHERE id=$1",row.id) == timedelta(hours=48)
    scenario(check)


def test_real_concurrent_transaction_number_only_one_request_succeeds():
    async def check(conn, before):
        first = await payments.create_request(conn, "user-a")
        second = await payments.create_request(conn, "user-b")
        peer = await asyncpg.connect(local_dsn())
        try:
            schema = await conn.fetchval("SELECT current_schema()")
            await peer.execute(f'SET search_path TO "{schema}"')
            results = await asyncio.gather(
                payments.submit_transaction(conn, "user-a", first.id, "CONCURRENT-123"),
                payments.submit_transaction(peer, "user-b", second.id, "CONCURRENT-123"),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, payments.HTTPException)]
            assert len(errors) == 1 and errors[0].status_code == 409
            assert sum(isinstance(result, BaseException) for result in results) == 1
            assert await conn.fetchval("SELECT COUNT(*) FROM binance_manual_payment_requests WHERE status='pending'") == 1
            assert await conn.fetchval("SELECT COUNT(*) FROM binance_manual_payment_requests WHERE status='created'") == 1
        finally:
            await peer.close()
    scenario(check)


def test_real_concurrent_double_approval_creates_one_period_and_audit():
    async def check(conn, before):
        row = await pending(conn)
        peer = await asyncpg.connect(local_dsn())
        try:
            schema = await conn.fetchval("SELECT current_schema()")
            await peer.execute(f'SET search_path TO "{schema}"')
            first, second = await asyncio.gather(approve(conn, row), approve(peer, row))
            assert first == second
            assert await conn.fetchval("SELECT COUNT(*) FROM binance_manual_periods") == 1
            assert await conn.fetchval("SELECT COUNT(*) FROM audit_log") == 1
        finally:
            await peer.close()
    scenario(check)


def test_real_double_approval_is_idempotent_and_moderator_cannot_grant():
    async def check(conn, before):
        row = await pending(conn)
        with pytest.raises(payments.HTTPException) as failure:
            await approve(conn,row,"MODERATOR")
        assert failure.value.status_code == 403
        first = await approve(conn,row)
        second = await approve(conn,row)
        assert first == second
        assert await conn.fetchval("SELECT COUNT(*) FROM binance_manual_periods") == 1
        assert await conn.fetchval("SELECT COUNT(*) FROM audit_log") == 1
        assert await conn.fetchval("SELECT period_end-period_start FROM binance_manual_periods") == timedelta(days=30)
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            async with conn.transaction():
                await conn.execute("UPDATE binance_manual_periods SET period_end=period_end+INTERVAL '1 day'")
    scenario(check)


def test_real_deferred_trigger_rolls_back_missing_audit_or_missing_period():
    async def check(conn, before):
        row = await pending(conn)
        for include_period in (False,True):
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                async with conn.transaction():
                    await conn.execute("""UPDATE binance_manual_payment_requests SET status='approved',decided_by='owner',
                        decided_at=clock_timestamp(),version=version+1 WHERE id=$1""",row.id)
                    if include_period:
                        await conn.execute("""INSERT INTO binance_manual_periods(payment_request_id,user_id,period_start,period_end)
                            VALUES ($1,'user-a',now(),now()+INTERVAL '720 hours')""",row.id)
            assert await conn.fetchval("SELECT status FROM binance_manual_payment_requests WHERE id=$1",row.id) == "pending"
            assert await conn.fetchval("SELECT COUNT(*) FROM binance_manual_periods") == 0
        with pytest.raises(RuntimeError):
            async with conn.transaction():
                await admin.decision(conn,AuditActor("owner","OWNER",str(uuid.uuid4())),row.id,"approved")
                raise RuntimeError("Injected failure before commit")
        assert await conn.fetchval("SELECT COUNT(*) FROM audit_log") == 0
        await approve(conn,row)
    scenario(check)


def test_real_notifications_contract_receipt_dedupe_and_erasure():
    async def check(conn, before):
        row = await pending(conn)
        await approve(conn,row)
        async with conn.transaction():
            await notifications.reconcile(conn)
            await notifications.reconcile(conn)
        assert await conn.fetchval("SELECT COUNT(*) FROM notification_outbox") == 2
        lease = await conn.fetchrow("SELECT * FROM notification_outbox WHERE kind='payment.binance.bell'")
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute("INSERT INTO binance_notification_reads(outbox_id) VALUES ($1)",lease["id"])
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("""INSERT INTO notification_outbox(id,kind,recipient_user_id,payload,dedupe_key)
                VALUES ($1,'payment.binance.email','user-a','{"payment_id":"invalid","event":"approved"}','invalid')""",uuid.uuid4().hex)
        await conn.execute("UPDATE notification_outbox SET status='sending',attempts=attempts+1 WHERE id=$1",lease["id"])
        await conn.execute("UPDATE notification_outbox SET status='sent',sent_at=clock_timestamp() WHERE id=$1",lease["id"])
        await conn.execute("INSERT INTO binance_notification_reads(outbox_id) VALUES ($1)",lease["id"])
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute("DELETE FROM binance_notification_reads")
        await conn.execute("""INSERT INTO commercial_erased_users(user_hash)
            VALUES (encode(sha256(convert_to('user-a','UTF8')),'hex'))""")
        assert await conn.fetchval("SELECT user_id FROM binance_manual_periods") == "ERASED"
        assert await conn.fetchval("SELECT revoked_at IS NOT NULL FROM binance_manual_periods") is True
        assert await conn.fetchval("SELECT transaction_number FROM binance_manual_payment_requests WHERE id=$1",row.id) is None
        assert await conn.fetchval("SELECT transaction_fingerprint IS NOT NULL FROM binance_manual_payment_requests WHERE id=$1",row.id) is True
    scenario(check)


def test_real_empty_schema_rollback_restores_exact_prior_contract_and_reapply():
    async def check(conn, before):
        await conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        assert await schema_contract(conn) == before
        assert await conn.fetchval("SELECT to_regclass('binance_manual_payment_requests')") is None
        assert await conn.fetchval("SELECT to_regclass('binance_manual_periods')") is None
        assert await conn.fetchval("SELECT to_regclass('binance_notification_reads')") is None
        for file in FEATURE:
            await conn.execute(file.read_text(encoding="utf-8"))
        row = await pending(conn)
        await approve(conn,row)
    scenario(check)


def test_real_populated_rollback_refuses_without_losing_data_and_flags_close_access(monkeypatch):
    from app.member_entitlements import resolve
    async def check(conn, before):
        row = await pending(conn);await approve(conn,row)
        stored = await conn.fetchrow("SELECT * FROM binance_manual_periods")
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        await conn.execute("ROLLBACK")
        assert await conn.fetchrow("SELECT * FROM binance_manual_periods") == stored
        assert await conn.fetchval("SELECT COUNT(*) FROM audit_log") == 1
        monkeypatch.setenv("BINANCE_MANUAL_ACCESS_ENABLED","false")
        assert not (await resolve(conn,{"subscriptions":[]},"user-a"))["master_trade_access"]
        monkeypatch.setenv("BINANCE_MANUAL_ACCESS_ENABLED","true")
        assert (await resolve(conn,{"subscriptions":[]},"user-a"))["master_trade_access"]
    scenario(check)
