"""Real PostgreSQL tests, restricted to the separately provisioned local test DB."""

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from fastapi import HTTPException, Response

from app import account_settings, account_store, campaign_routes, campaign_worker
from app import google_oauth, registration_consent as consent, v22_commercial as auth
from app.campaign_content import unsubscribe_token
from app.commercial_core import default_commercial_state

DSN = os.getenv("CONSENT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="Explicit isolated consent PostgreSQL DSN required"
)
SECRET = b"offline-consent-postgres-session-secret"

SCHEMA = """
CREATE TABLE commercial_auth_users(
 user_id TEXT PRIMARY KEY,auth_version BIGINT NOT NULL,security JSONB NOT NULL);
CREATE TABLE commercial_erased_users(user_hash TEXT PRIMARY KEY);
CREATE FUNCTION commercial_erasure_user_guard() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM commercial_erased_users
           WHERE user_hash=encode(sha256(convert_to(NEW.user_id,'UTF8')),'hex'))
 THEN RETURN NULL; END IF;
 RETURN NEW;
END $$;
CREATE TABLE application_state_snapshots(
 state_key TEXT PRIMARY KEY,updated_at TIMESTAMPTZ NOT NULL,payload JSONB NOT NULL);
CREATE TABLE commercial_auth_limits(
 bucket TEXT PRIMARY KEY,window_start TIMESTAMPTZ NOT NULL,attempts INTEGER NOT NULL);
CREATE TABLE email_preferences(
 user_id TEXT PRIMARY KEY,announcements_opt_out BOOLEAN NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL);
CREATE TABLE notification_outbox(
 id TEXT PRIMARY KEY,recipient_user_id TEXT NOT NULL,status TEXT NOT NULL,
 attempts INTEGER NOT NULL,payload JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,next_attempt_at TIMESTAMPTZ,
 last_error_code TEXT,sent_at TIMESTAMPTZ,dedupe_key TEXT NOT NULL);
CREATE TABLE campaign_recipients(
 outbox_id TEXT PRIMARY KEY,status TEXT NOT NULL,skip_reason TEXT);
"""


def local_dsn():
    parsed = urlsplit(DSN or "")
    if (
        parsed.scheme != "postgresql"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 55446
        or parsed.path != "/kaistrade_consent_test"
        or parsed.username != "consent_test_owner"
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(
            "Only the dedicated loopback consent test database is allowed"
        )
    return DSN


def application(pool):
    return SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            google_oauth_schema_ready=True,
            v22_commercial={
                "state": default_commercial_state(),
                "secret": SECRET,
                "lock": asyncio.Lock(),
                "storage_lock": asyncio.Lock(),
                "auth_baseline": {},
            },
        )
    )


def request(app, body=b"action=unsubscribe"):
    async def read_body():
        return body

    return SimpleNamespace(
        app=app, client=SimpleNamespace(host="127.0.0.1"), body=read_body
    )


@asynccontextmanager
async def database():
    connection = await asyncpg.connect(local_dsn())
    schema = "consent_test_" + uuid.uuid4().hex
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    await connection.execute(f'SET search_path TO "{schema}"')
    pool = None
    try:
        await connection.execute(SCHEMA)
        await connection.execute(account_store.SCHEMA)
        await connection.execute(google_oauth.SCHEMA)
        pool = await asyncpg.create_pool(
            local_dsn(), min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        yield pool, application(pool), connection
    finally:
        if pool is not None:
            await pool.close()
        await connection.execute("SET search_path TO public")
        await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await connection.close()


def payload(email="member@example.test", marketing=True):
    return auth.RegisterRequest(
        display_name="Member",
        email=email,
        password="OfflineConsentPassword!123",
        confirm_password="OfflineConsentPassword!123",
        terms_accepted=True,
        marketing_accepted=marketing,
    )


async def register(app, email="member@example.test"):
    return await auth.register_password_user(payload(email), request(app), Response())


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("PROTREBOT_SESSION_SECRET", SECRET.decode())
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "offline-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "offline-client-secret")
    monkeypatch.setattr(auth, "DURABLE_AUTH_REQUIRED", True)
    monkeypatch.setattr(auth, "gmail_configured", lambda: True)
    monkeypatch.setattr(auth, "send_auth_email", lambda **_: None)
    monkeypatch.setattr(auth, "record_event", lambda *_, **__: None)

    async def member(req, **_):
        return {"id": req.review_user_id}

    monkeypatch.setattr(account_settings, "member", member)


@pytest.mark.parametrize("v2", [False, True])
@pytest.mark.parametrize(
    "table",
    [
        "commercial_auth_users",
        "commercial_account_settings",
        "application_state_snapshots",
    ],
)
def test_registration_write_failure_rolls_back_every_record_and_memory(
    monkeypatch, table, v2
):
    monkeypatch.setenv("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED", str(v2).lower())

    async def run():
        async with database() as (pool, app, db):
            await db.execute("""CREATE FUNCTION reject_write() RETURNS TRIGGER
                                LANGUAGE plpgsql AS $$
                                BEGIN RAISE EXCEPTION 'offline injected failure'
                                USING ERRCODE='23514'; END $$""")
            await db.execute(
                f"CREATE TRIGGER reject_write BEFORE INSERT ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_write()"
            )
            with pytest.raises(HTTPException) as failure:
                await register(app)
            assert failure.value.status_code == 503
            for name in (
                "commercial_auth_users",
                "commercial_account_settings",
                "commercial_account_tokens",
            ):
                assert await db.fetchval(f"SELECT COUNT(*) FROM {name}") == 0
            assert not app.state.v22_commercial["state"]["users"]
            await db.execute(f"DROP TRIGGER reject_write ON {table}")
            assert await auth.persist_v22_commercial(app) is True
            assert await db.fetchval("SELECT COUNT(*) FROM commercial_auth_users") == 0

    asyncio.run(run())


def test_deferred_commit_failure_does_not_publish_registration(monkeypatch):
    monkeypatch.setenv("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED", "true")

    async def run():
        async with database() as (pool, app, db):
            await db.execute("""CREATE FUNCTION reject_commit() RETURNS TRIGGER
                                LANGUAGE plpgsql AS $$
                                BEGIN RAISE EXCEPTION 'offline deferred failure'
                                USING ERRCODE='23514'; END $$;
                                CREATE CONSTRAINT TRIGGER reject_commit
                                AFTER INSERT ON commercial_auth_users
                                DEFERRABLE INITIALLY DEFERRED
                                FOR EACH ROW EXECUTE FUNCTION reject_commit()""")
            with pytest.raises(HTTPException):
                await register(app)
            assert not app.state.v22_commercial["state"]["users"]
            for name in (
                "commercial_auth_users",
                "commercial_account_settings",
                "commercial_account_tokens",
            ):
                assert await db.fetchval(f"SELECT COUNT(*) FROM {name}") == 0

    asyncio.run(run())


@pytest.mark.parametrize("v2", [False, True])
def test_failed_verification_delivery_cleans_canonical_user_tokens_and_consents(
    monkeypatch, v2
):
    from app.email_service import EmailDeliveryError

    monkeypatch.setenv("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED", str(v2).lower())

    def failed_delivery(**_):
        raise EmailDeliveryError("offline delivery failure", provider="resend")

    monkeypatch.setattr(auth, "send_auth_email", failed_delivery)

    async def run():
        async with database() as (pool, app, db):
            with pytest.raises(HTTPException) as failure:
                await register(app)
            assert failure.value.status_code == 503
            for name in (
                "commercial_auth_users",
                "commercial_account_settings",
                "commercial_account_tokens",
            ):
                assert await db.fetchval(f"SELECT COUNT(*) FROM {name}") == 0
            assert not app.state.v22_commercial["state"]["users"]
            row = await db.fetchrow("SELECT payload FROM application_state_snapshots")
            assert not json.loads(row["payload"])["users"]

    asyncio.run(run())


@pytest.mark.parametrize("same_email", [False, True])
def test_concurrent_workers_register_without_duplicates_or_missing_consents(
    monkeypatch, same_email
):
    monkeypatch.setenv("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED", "true")

    async def run():
        async with database() as (pool, app, db):
            second = application(pool)
            await asyncio.wait_for(
                asyncio.gather(
                    register(app, "one@example.test"),
                    register(
                        second, "one@example.test" if same_email else "two@example.test"
                    ),
                ),
                timeout=10,
            )
            count = 1 if same_email else 2
            assert (
                await db.fetchval("SELECT COUNT(*) FROM commercial_auth_users") == count
            )
            rows = await db.fetch(
                """SELECT a.payload FROM commercial_account_settings a
                   JOIN commercial_auth_users u ON u.user_id=a.user_id"""
            )
            assert len(rows) == count
            assert (
                await db.fetchval(
                    """SELECT COUNT(*) FROM commercial_account_settings a
                   WHERE a.payload ? 'registration_consents'
                   AND NOT EXISTS(SELECT 1 FROM commercial_auth_users u
                                  WHERE u.user_id=a.user_id)"""
                )
                == 0
            )
            for row in rows:
                doc = json.loads(row["payload"])
                assert doc["registration_consents"][0]["accepted"] is True
                assert doc["email_verification_tokens"] and doc["registration_pending"]

    asyncio.run(run())


def test_password_and_google_creation_share_the_email_lock(monkeypatch):
    monkeypatch.setenv("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED", "true")

    async def run():
        async with database() as (pool, app, db):
            second = application(pool)
            identity = {
                "issuer": google_oauth.ISSUER,
                "subject": "offline-concurrent-subject",
                "email": "shared@example.test",
                "display_name": "Member",
            }

            async def google_registration():
                try:
                    return await google_oauth.resolve_identity(
                        request(second), identity, create=True
                    )
                except google_oauth.OAuthFailure as exc:
                    assert exc.reason == "account_link_required"
                    return None

            await asyncio.wait_for(
                asyncio.gather(
                    register(app, identity["email"]),
                    google_registration(),
                ),
                timeout=10,
            )
            assert await db.fetchval("SELECT COUNT(*) FROM commercial_auth_users") == 1
            assert (
                await db.fetchval(
                    """SELECT COUNT(*) FROM commercial_account_settings a
                   JOIN commercial_auth_users u ON u.user_id=a.user_id
                   WHERE a.payload->'registration_consents'->0->'accepted'
                         ='true'::jsonb"""
                )
                == 1
            )

    asyncio.run(run())


async def change(app, uid, accepted, revision, operation_id):
    req = request(app)
    req.review_user_id = uid
    return await account_settings.change_marketing_consent(
        consent.MarketingConsentUpdate(
            accepted=accepted, revision=revision, operation_id=operation_id
        ),
        req,
    )


@pytest.mark.parametrize("first", [False, True])
def test_concurrent_consent_changes_and_replay_cannot_undo_withdrawal(
    monkeypatch, first
):
    monkeypatch.setenv("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED", "true")

    async def run():
        async with database() as (pool, app, db):
            uid = (await register(app))["user"]["id"]
            original = await account_store.read(request(app), uid)
            revision = original["marketing_consent"]["id"]
            second = application(pool)

            async def attempt(application, accepted):
                try:
                    return await change(
                        application, uid, accepted, revision, uuid.uuid4().hex
                    )
                except HTTPException as exc:
                    return exc.status_code

            async with db.transaction():
                await db.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    "account-settings:" + uid,
                )
                one = asyncio.create_task(attempt(app, first))
                await asyncio.sleep(0.03)
                two = asyncio.create_task(attempt(second, not first))
                await asyncio.sleep(0.03)
                assert not one.done() and not two.done()
            results = await asyncio.wait_for(asyncio.gather(one, two), timeout=10)
            assert (
                sum(isinstance(result, dict) for result in results) == 1
                and 409 in results
            )
            doc = await account_store.read(request(app), uid)
            if doc["marketing_consent"]["accepted"]:
                await change(
                    app, uid, False, doc["marketing_consent"]["id"], uuid.uuid4().hex
                )
            withdrawn = await account_store.read(request(app), uid)
            with pytest.raises(HTTPException) as stale:
                await change(app, uid, True, revision, uuid.uuid4().hex)
            assert stale.value.status_code == 409
            assert (await account_store.read(request(app), uid))[
                "marketing_consent"
            ] == withdrawn["marketing_consent"]

    asyncio.run(run())


def test_identical_retry_is_idempotent_and_old_retry_cannot_reactivate(monkeypatch):
    monkeypatch.setenv("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED", "true")

    async def run():
        async with database() as (pool, app, db):
            uid = (await register(app))["user"]["id"]
            doc = await account_store.read(request(app), uid)
            revision, operation = doc["marketing_consent"]["id"], uuid.uuid4().hex
            result = await change(app, uid, True, revision, operation)
            assert await change(app, uid, True, revision, operation) == result
            doc = await account_store.read(request(app), uid)
            assert len(doc["registration_consents"]) == 3
            await change(
                app, uid, False, doc["marketing_consent"]["id"], uuid.uuid4().hex
            )
            with pytest.raises(HTTPException) as stale:
                await change(app, uid, True, revision, operation)
            assert stale.value.status_code == 409
            assert (await account_store.read(request(app), uid))["marketing_consent"][
                "accepted"
            ] is False

    asyncio.run(run())


def test_email_and_profile_share_history_and_old_subscribe_link_is_rejected(
    monkeypatch,
):
    monkeypatch.setenv("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED", "true")

    async def run():
        async with database() as (pool, app, db):
            uid = (await register(app))["user"]["id"]
            token = unsubscribe_token(SECRET, uid)
            await campaign_routes.preference_change(request(app), token)
            doc = await account_store.read(request(app), uid)
            assert doc["marketing_consent"]["source"] == "EMAIL_UNSUBSCRIBE"
            assert doc["marketing_consent"]["withdrawn_at"]
            await campaign_routes.preference_change(request(app), token)
            assert (await account_store.read(request(app), uid))[
                "registration_consents"
            ] == doc["registration_consents"]
            await change(
                app, uid, True, doc["marketing_consent"]["id"], uuid.uuid4().hex
            )
            assert (
                await db.fetchval(
                    "SELECT announcements_opt_out FROM email_preferences "
                    "WHERE user_id=$1",
                    uid,
                )
                is False
            )
            with pytest.raises(HTTPException) as old:
                await campaign_routes.preference_change(
                    request(app, b"action=subscribe"), token
                )
            assert old.value.status_code == 409

    asyncio.run(run())


def test_queued_delivery_waits_for_withdrawal_commit_then_sends_nothing(monkeypatch):
    monkeypatch.setenv("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED", "true")

    async def run():
        async with database() as (pool, app, db):
            uid = (await register(app))["user"]["id"]
            await db.execute(
                "UPDATE commercial_auth_users "
                "SET security=security || '{\"email_verified\":true}'::jsonb "
                "WHERE user_id=$1",
                uid,
            )
            stamp = datetime.now(timezone.utc)
            await db.execute(
                """INSERT INTO notification_outbox
                   (id,recipient_user_id,status,attempts,payload,created_at,dedupe_key)
                   VALUES('queued',$1,'sending',1,'{"campaign_id":"campaign"}',
                          $2,'offline-queued')""",
                uid,
                stamp,
            )
            await db.execute(
                "INSERT INTO campaign_recipients VALUES('queued','queued',NULL)"
            )
            lease = dict(
                await db.fetchrow("SELECT * FROM notification_outbox WHERE id='queued'")
            )

            async def deliver():
                async with pool.acquire() as conn, conn.transaction():
                    await campaign_worker.deliver_campaign(conn, lease, app)

            with (
                patch.object(
                    campaign_worker,
                    "campaign_row",
                    AsyncMock(return_value={"id": "campaign", "status": "sending"}),
                ),
                patch.object(campaign_worker, "refresh_counts", AsyncMock()),
                patch.object(campaign_worker, "send_campaign") as mail,
            ):
                async with account_store.edit(request(app), uid) as doc:
                    consent.update_marketing(
                        doc,
                        uid,
                        False,
                        revision=doc["marketing_consent"]["id"],
                        operation_id=uuid.uuid4().hex,
                    )
                    task = asyncio.create_task(deliver())
                    await asyncio.sleep(0.03)
                    assert not task.done()
                await asyncio.wait_for(task, timeout=10)
                mail.assert_not_called()
            assert (
                await db.fetchval(
                    "SELECT status FROM campaign_recipients WHERE outbox_id='queued'"
                )
                == "skipped"
            )

    asyncio.run(run())
