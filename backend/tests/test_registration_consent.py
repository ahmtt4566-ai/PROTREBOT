import asyncio
import copy
import hashlib
import json
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app import (
    account_settings,
    account_store,
    google_oauth as google,
    registration_consent as consent,
    v22_commercial as auth,
)
from test_google_oauth import HEADERS, IDENTITY, begin, callback, setup as setup
from test_google_oauth import PERSIST


@pytest.mark.parametrize("model", [auth.RegisterRequest, google.CompleteRequest])
@pytest.mark.parametrize("value", [1, "true", "false", None])
def test_registration_consents_require_real_booleans(model, value):
    body = {"terms_accepted": value}
    if model is auth.RegisterRequest:
        body.update(
            display_name="Member",
            email="member@example.test",
            password="OfflinePassword!123",
            confirm_password="OfflinePassword!123",
        )
    with pytest.raises(ValidationError):
        model(**body)
    body["terms_accepted"] = True
    body["marketing_accepted"] = value
    with pytest.raises(ValidationError):
        model(**body)


def test_document_hash_and_prior_versions_survive_marketing_changes():
    doc = {}
    records = consent.append_registration(doc, "member", False, "PASSWORD_REGISTRATION")
    previous = copy.deepcopy(records)
    assert records[0]["version"] == "2026-10-14.1"
    assert (
        records[0]["text_sha256"]
        == hashlib.sha256(
            "\n\n".join(consent.DOCUMENT["paragraphs"]).encode()
        ).hexdigest()
    )
    assert "24. KULLANICI ONAYI" in "\n\n".join(consent.DOCUMENT["paragraphs"])
    assert records[1]["accepted"] is False and records[1]["accepted_at"] is None
    consent.update_marketing(
        doc,
        "member",
        True,
        revision=doc["marketing_consent"]["id"],
        operation_id=uuid.uuid4().hex,
    )
    approved = copy.deepcopy(doc["marketing_consent"])
    withdrawal_revision, withdrawal_id = (
        doc["marketing_consent"]["id"],
        uuid.uuid4().hex,
    )
    consent.update_marketing(
        doc, "member", False, revision=withdrawal_revision, operation_id=withdrawal_id
    )
    assert doc["registration_consents"][:2] == previous
    assert doc["registration_consents"][2] == approved
    assert doc["marketing_consent"]["withdrawn_at"] is not None
    assert (
        datetime.fromisoformat(doc["marketing_consent"]["withdrawn_at"])
        .utcoffset()
        .total_seconds()
        == 0
    )
    assert doc["registration_consents"][0]["accepted"] is True
    consent.update_marketing(
        doc, "member", False, revision=withdrawal_revision, operation_id=withdrawal_id
    )
    assert len(doc["registration_consents"]) == 4


@pytest.mark.parametrize("marketing", [False, True])
def test_password_signup_persists_separate_consents_without_granting_live_access(
    setup, monkeypatch, marketing
):
    client, pool, app = setup
    monkeypatch.setattr(auth, "REGISTRATION_RESPONSE_SECONDS", 0)
    body = {
        "display_name": "Member",
        "email": "signup@example.test",
        "password": "OfflinePassword!123",
        "confirm_password": "OfflinePassword!123",
    }
    with (
        patch.object(auth, "gmail_configured", return_value=True),
        patch.object(auth, "send_auth_email") as mail,
        patch.object(auth, "apply_erasure_tombstones", AsyncMock()),
    ):
        rejected = client.post("/api/v22/auth/register", headers=HEADERS, json=body)
        assert (
            rejected.status_code == 422
            and rejected.json()["detail"] == consent.REQUIRED_MESSAGE
        )
        assert not pool.snapshot["users"] and not pool.account_settings
        mail.assert_not_called()
        result = client.post(
            "/api/v22/auth/register",
            headers=HEADERS,
            json={**body, "terms_accepted": True, "marketing_accepted": marketing},
        )
    assert result.status_code == 200
    uid = pool.snapshot["users"][0]["id"]
    doc = pool.account_settings[uid]
    assert [record["kind"] for record in doc["registration_consents"]] == [
        "TERMS",
        "MARKETING",
    ]
    assert doc["marketing_consent"]["accepted"] is marketing
    assert all(record["user_id"] == uid for record in doc["registration_consents"])
    assert not client.cookies.get("protrebot_session")
    assert pool.snapshot["users"][0]["email_verified"] is False
    assert "live_session_authorization" not in doc
    mail.assert_called_once()


@pytest.mark.parametrize("marketing", [False, True])
def test_google_signup_uses_same_authoritative_consent_document(setup, marketing):
    client, pool, app = setup
    state = begin(client)["state"][0]
    with patch.object(google, "exchange_identity", AsyncMock(return_value=IDENTITY)):
        assert "google_login=consent" in callback(client, state).headers["location"]
    assert not pool.account_settings and not pool.identities
    response = client.post(
        "/api/v22/auth/google/complete",
        headers=HEADERS,
        json={"terms_accepted": True, "marketing_accepted": marketing},
    )
    assert response.status_code == 200
    uid = pool.snapshot["users"][0]["id"]
    assert pool.account_settings[uid]["marketing_consent"]["accepted"] is marketing
    assert (
        pool.account_settings[uid]["registration_consents"][0]["version"]
        == consent.TERMS_VERSION
    )


def test_forged_dates_or_versions_are_rejected():
    with pytest.raises(ValidationError):
        google.CompleteRequest(
            terms_accepted=True, accepted_at="2000-01-01", version="forged"
        )
    with pytest.raises(ValidationError):
        consent.MarketingConsentUpdate(accepted=True, withdrawn_at="2000-01-01")


def test_password_consent_is_durable_before_new_account_snapshot(setup, monkeypatch):
    client, pool, app = setup
    monkeypatch.setattr(auth, "REGISTRATION_RESPONSE_SECONDS", 0)
    original_execute = pool.execute

    async def assert_consent_before_snapshot(query, *args):
        if "INSERT INTO application_state_snapshots" in query:
            for user in json.loads(args[1])["users"]:
                assert (
                    pool.account_settings[user["id"]]["registration_consents"][0][
                        "accepted"
                    ]
                    is True
                )
        return await original_execute(query, *args)

    with (
        patch.object(auth, "gmail_configured", return_value=True),
        patch.object(auth, "send_auth_email"),
        patch.object(auth, "apply_erasure_tombstones", AsyncMock()),
        patch.object(
            pool, "execute", side_effect=assert_consent_before_snapshot
        ) as writes,
    ):
        response = client.post(
            "/api/v22/auth/register",
            headers=HEADERS,
            json={
                "display_name": "Member",
                "email": "ordering@example.test",
                "password": "OfflinePassword!123",
                "confirm_password": "OfflinePassword!123",
                "terms_accepted": True,
            },
        )
    assert response.status_code == 200
    assert any(
        "INSERT INTO application_state_snapshots" in call.args[0]
        for call in writes.await_args_list
    )


def test_failed_durable_consent_write_cannot_complete_password_registration(
    setup, monkeypatch
):
    client, pool, app = setup
    monkeypatch.setattr(auth, "REGISTRATION_RESPONSE_SECONDS", 0)
    original_execute = pool.execute

    async def fail_consent(query, *args):
        if "INSERT INTO commercial_account_settings" in query:
            raise RuntimeError("offline consent store unavailable")
        return await original_execute(query, *args)

    with (
        patch.object(auth, "gmail_configured", return_value=True),
        patch.object(auth, "send_auth_email") as mail,
        patch.object(auth, "apply_erasure_tombstones", AsyncMock()),
        patch.object(pool, "execute", side_effect=fail_consent),
    ):
        response = client.post(
            "/api/v22/auth/register",
            headers=HEADERS,
            json={
                "display_name": "Member",
                "email": "fail@example.test",
                "password": "OfflinePassword!123",
                "confirm_password": "OfflinePassword!123",
                "terms_accepted": True,
            },
        )
    assert response.status_code == 503
    assert not pool.snapshot["users"] and not pool.account_settings
    mail.assert_not_called()


def test_authenticated_withdrawal_preserves_terms_and_survives_worker_reload(setup):
    client, pool, app = setup
    app.include_router(account_settings.router)
    state = begin(client)["state"][0]
    with patch.object(google, "exchange_identity", AsyncMock(return_value=IDENTITY)):
        callback(client, state)
    assert (
        client.post(
            "/api/v22/auth/google/complete",
            headers=HEADERS,
            json={"terms_accepted": True, "marketing_accepted": True},
        ).status_code
        == 200
    )
    uid = pool.snapshot["users"][0]["id"]
    terms = copy.deepcopy(pool.account_settings[uid]["registration_consents"][0])
    response = client.patch(
        "/api/v22/account/marketing-consent",
        headers=HEADERS,
        json={
            "accepted": False,
            "revision": pool.account_settings[uid]["marketing_consent"]["id"],
            "operation_id": uuid.uuid4().hex,
        },
    )
    assert response.status_code == 200
    assert response.json()["marketing_consent"]["accepted"] is False
    assert response.json()["marketing_consent"]["withdrawn_at"]
    doc = asyncio.run(account_store.read(SimpleNamespace(app=app), uid))
    assert (
        doc["registration_consents"][0] == terms
        and len(doc["registration_consents"]) == 3
    )
    assert client.get("/api/v22/session").status_code == 200
    client.cookies.clear()
    assert (
        client.patch(
            "/api/v22/account/marketing-consent",
            headers=HEADERS,
            json={"accepted": False},
        ).status_code
        == 401
    )


def test_initial_verification_write_failure_leaves_no_publishable_user(
    setup, monkeypatch
):
    client, pool, app = setup
    monkeypatch.setattr(auth, "REGISTRATION_RESPONSE_SECONDS", 0)
    monkeypatch.setenv("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED", "true")
    execute = pool.execute

    async def fail_initial_document(query, *args):
        if "INSERT INTO commercial_account_settings" in query:
            raise RuntimeError("offline first document failure")
        return await execute(query, *args)

    with (
        patch.object(google, "configured", return_value=False),
        patch.object(auth, "gmail_configured", return_value=True),
        patch.object(auth, "send_auth_email") as mail,
        patch.object(auth, "apply_erasure_tombstones", AsyncMock()),
        patch.object(pool, "execute", side_effect=fail_initial_document),
    ):
        response = client.post(
            "/api/v22/auth/register",
            headers=HEADERS,
            json={
                "display_name": "Member",
                "email": "atomic@example.test",
                "password": "OfflinePassword!123",
                "confirm_password": "OfflinePassword!123",
                "terms_accepted": True,
            },
        )
    assert response.status_code == 503
    assert not app.state.v22_commercial["state"]["users"]
    assert not pool.account_settings and not pool.security
    mail.assert_not_called()


def test_legacy_snapshot_failure_leaves_no_orphan_consent(setup, monkeypatch):
    client, pool, app = setup
    monkeypatch.setattr(auth, "REGISTRATION_RESPONSE_SECONDS", 0)
    monkeypatch.setattr(auth, "DURABLE_AUTH_REQUIRED", True)
    monkeypatch.setenv(
        "PROTREBOT_SESSION_SECRET", "offline-stable-consent-secret-long-enough"
    )
    monkeypatch.setenv("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED", "false")
    execute = pool.execute

    async def fail_snapshot(query, *args):
        if "INSERT INTO application_state_snapshots" in query:
            raise RuntimeError("offline snapshot failure")
        return await execute(query, *args)

    with (
        patch.object(google, "configured", return_value=False),
        patch.object(auth, "gmail_configured", return_value=True),
        patch.object(auth, "send_auth_email") as mail,
        patch.object(auth, "apply_erasure_tombstones", AsyncMock()),
        patch.object(auth, "persist_v22_commercial", side_effect=PERSIST),
        patch.object(pool, "execute", side_effect=fail_snapshot),
    ):
        response = client.post(
            "/api/v22/auth/register",
            headers=HEADERS,
            json={
                "display_name": "Member",
                "email": "legacy-atomic@example.test",
                "password": "OfflinePassword!123",
                "confirm_password": "OfflinePassword!123",
                "terms_accepted": True,
            },
        )
    assert response.status_code == 503
    assert not pool.account_settings and not pool.security
    assert not app.state.v22_commercial["state"]["users"]
    mail.assert_not_called()


def test_database_error_logs_never_contain_sensitive_exception_values(setup, caplog):
    _, pool, app = setup
    markers = [
        "OFFLINE_DSN_PASSWORD",
        "OFFLINE_PASSWORD",
        "OFFLINE_API_KEY",
        "OFFLINE_COOKIE",
        "private-review@example.test",
    ]
    failure = RuntimeError(
        "postgresql://review:OFFLINE_DSN_PASSWORD@offline.invalid/review "
        "password=OFFLINE_PASSWORD api_key=OFFLINE_API_KEY cookie=OFFLINE_COOKIE "
        "private-review@example.test"
    )
    with (
        patch.object(google, "configured", return_value=False),
        patch.object(auth, "apply_erasure_tombstones", AsyncMock()),
        patch.object(pool, "execute", AsyncMock(side_effect=failure)),
    ):
        assert asyncio.run(PERSIST(app)) is False
    assert all(marker not in caplog.text for marker in markers)
    assert "RuntimeError" in caplog.text and "storage_write_failed" in caplog.text
    assert "Traceback" not in caplog.text


def test_old_boolean_only_approval_cannot_reopen_withdrawn_permission(setup):
    client, pool, app = setup
    app.include_router(account_settings.router)
    state = begin(client)["state"][0]
    with patch.object(google, "exchange_identity", AsyncMock(return_value=IDENTITY)):
        callback(client, state)
    assert (
        client.post(
            "/api/v22/auth/google/complete",
            headers=HEADERS,
            json={"terms_accepted": True, "marketing_accepted": True},
        ).status_code
        == 200
    )
    uid = pool.snapshot["users"][0]["id"]
    assert (
        client.patch(
            "/api/v22/account/marketing-consent",
            headers=HEADERS,
            json={
                "accepted": False,
                "revision": pool.account_settings[uid]["marketing_consent"]["id"],
                "operation_id": uuid.uuid4().hex,
            },
        ).status_code
        == 200
    )
    stale = client.patch(
        "/api/v22/account/marketing-consent", headers=HEADERS, json={"accepted": True}
    )
    assert stale.status_code in (409, 422)
    assert pool.account_settings[uid]["marketing_consent"]["accepted"] is False
