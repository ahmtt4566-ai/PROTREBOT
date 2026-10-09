"""Offline verification lifecycle tests with the real shared SQLite transaction."""
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_account_settings as fixtures
from app import account_store as store
from app import email_service
from app import email_verification as verification
from app import v22_commercial as auth
from app.browser_security import REGISTRATION_PENDING_COOKIE, validate_browser_request
from app.commercial_core import verify_token
from fastapi import HTTPException
from test_account_settings import PASSWORD, SECRET, alter_doc, get_doc
from test_security_auth_hardening import SharedStore, make_request, make_user

setup = fixtures.setup


@pytest.fixture(autouse=True)
def feature(monkeypatch):
    monkeypatch.setenv("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED", "true")


def request(client):
    return SimpleNamespace(app=client.app, cookies={}, headers={}, state=SimpleNamespace())


def signup(client, email="new@example.test"):
    result = client.post("/api/v22/auth/register", json={
        "display_name": "Yeni Hesap", "email": email, "password": PASSWORD,
        "confirm_password": PASSWORD, "terms_accepted": True})
    assert result.status_code == 200, result.text
    return result


def sent_token(setup):
    return setup.mail.call_args.kwargs["action_url"].split("token=", 1)[1]


def fresh_link(setup):
    client = setup.runtime()
    signup(client)
    return client, sent_token(setup)


def user_id(client):
    return next(user["id"] for user in client.app.state.v22_commercial["state"]["users"]
                if user["email"] == "new@example.test")


def confirm(client, token):
    return client.post("/api/v22/auth/verify-email", json={"token": token})


def test_secure_pending_cookie_and_opaque_hash_only_30_minute_link(setup):
    client, token = fresh_link(setup)
    assert token.startswith(verification.PREFIX)
    import base64
    assert len(base64.urlsafe_b64decode(token.removeprefix(verification.PREFIX) + "=")) == 32
    doc = get_doc(setup, user_id(client))
    row = doc["email_verification_tokens"][verification.digest(token)]
    assert 1795 <= row["expires"] - time.time() <= 1800
    assert row["used_at"] is None
    assert token not in json.dumps(doc)
    pending = client.cookies.get(REGISTRATION_PENDING_COOKIE)
    assert pending not in json.dumps(doc)
    proof = verify_token(pending, SECRET, expected_kind="EMAIL_PENDING")
    assert proof["exp"] - proof["iat"] == 1800
    response = signup(setup.runtime(), "another@example.test")
    cookie = response.headers["set-cookie"]
    assert all(part in cookie for part in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=1800"))
    assert "protrebot_session=" not in cookie


def allow_next_send(setup, uid):
    alter_doc(setup, lambda doc: doc.update(verification_last_sent=time.time() - 61), uid)


def test_pending_email_correction_preserves_account_invalidates_old_links_and_requires_password(setup):
    client, old_link = fresh_link(setup)
    uid = user_id(client)
    old_cookie = client.cookies.get(REGISTRATION_PENDING_COOKIE)
    allow_next_send(setup, uid)
    body = {"new_email": "corrected@example.test", "current_password": "Wrong-password-123!"}
    denied = client.post("/api/v22/auth/registration/email", json=body)
    assert denied.status_code == 401
    assert get_doc(setup, uid)["auth_overlay"]["email"] == "new@example.test"
    body["current_password"] = PASSWORD
    changed = client.post("/api/v22/auth/registration/email", json=body)
    assert changed.status_code == 200, changed.text
    assert changed.json() == {"ok": True, "email": "corrected@example.test", "retry_after": 60}
    assert client.cookies.get(REGISTRATION_PENDING_COOKIE) != old_cookie
    status = client.get("/api/v22/auth/registration/status")
    assert status.json()["email"] == "corrected@example.test"
    assert not status.json()["verified"]
    assert confirm(client, old_link).status_code == 400
    new_link = sent_token(setup)
    assert setup.mail.call_args.kwargs["to_email"] == "corrected@example.test"
    assert confirm(client, new_link).status_code == 200
    assert get_doc(setup, uid)["auth_overlay"]["email_verified"]
    stale = setup.runtime()
    stale.cookies.set(REGISTRATION_PENDING_COOKIE, old_cookie)
    assert stale.get("/api/v22/auth/registration/status").status_code == 401


def test_pending_email_correction_rejects_duplicates_cooldown_and_missing_cookie(setup):
    client, link = fresh_link(setup)
    uid = user_id(client)
    body = {"new_email": "corrected@example.test", "current_password": PASSWORD}
    assert setup.runtime().post("/api/v22/auth/registration/email", json=body).status_code == 401
    assert client.post("/api/v22/auth/registration/email", json=body).status_code == 429
    allow_next_send(setup, uid)
    duplicate = client.post("/api/v22/auth/registration/email", json={**body, "new_email": setup.users[0]["email"]})
    assert duplicate.status_code == 409
    assert get_doc(setup, uid)["auth_overlay"]["email"] == "new@example.test"
    assert confirm(client, link).status_code == 200
    assert client.post("/api/v22/auth/registration/email", json=body).status_code == 401


def test_pending_email_correction_rolls_back_address_links_and_cookie_when_delivery_fails(setup):
    client, link = fresh_link(setup)
    uid = user_id(client)
    cookie = client.cookies.get(REGISTRATION_PENDING_COOKIE)
    allow_next_send(setup, uid)
    setup.mail.side_effect = email_service.EmailDeliveryError("Offline failure", provider="resend")
    failed = client.post("/api/v22/auth/registration/email",
                         json={"new_email": "corrected@example.test", "current_password": PASSWORD})
    assert failed.status_code == 503
    assert "set-cookie" not in failed.headers
    assert client.cookies.get(REGISTRATION_PENDING_COOKIE) == cookie
    assert get_doc(setup, uid)["auth_overlay"]["email"] == "new@example.test"
    assert confirm(client, link).status_code == 200


def test_email_correction_capability_and_wrong_password_do_not_reveal_duplicate_registration(setup):
    created, duplicate = setup.runtime(), setup.runtime()
    signup(created)
    signup(duplicate, setup.users[0]["email"])
    assert created.get("/api/v22/auth/registration/status").json()["can_change_email"] is True
    assert duplicate.get("/api/v22/auth/registration/status").json()["can_change_email"] is True
    body = {"new_email": "corrected@example.test", "current_password": "Wrong-password-123!"}
    real_result = created.post("/api/v22/auth/registration/email", json=body)
    synthetic_result = duplicate.post("/api/v22/auth/registration/email", json=body)
    assert real_result.status_code == synthetic_result.status_code == 401
    assert real_result.json() == synthetic_result.json()


@pytest.mark.parametrize("kind", ["owner", "version", "mfa", "inactive", "expired"])
def test_pending_email_correction_never_bypasses_account_security(setup, kind):
    client, _ = fresh_link(setup)
    uid = user_id(client)
    def change(doc):
        if kind == "owner":
            doc["auth_overlay"]["role"] = "OWNER"
        elif kind == "version":
            doc["auth_overlay"]["auth_version"] += 1
        elif kind == "mfa":
            doc["two_factor_enabled"] = True
        elif kind == "inactive":
            doc["auth_overlay"]["active"] = False
        else:
            for row in doc["registration_pending"].values():
                row["expires"] = time.time() - 1
    alter_doc(setup, change, uid)
    result = client.post("/api/v22/auth/registration/email",
                         json={"new_email": "corrected@example.test", "current_password": PASSWORD})
    assert result.status_code == 401
    assert get_doc(setup, uid)["auth_overlay"]["email"] == "new@example.test"


def test_email_correction_accepts_the_existing_full_password_length(setup):
    from app.commercial_core import hash_password
    client, _ = fresh_link(setup)
    uid = user_id(client)
    password = "Aa1!" + "x" * 252
    alter_doc(setup, lambda doc: doc["auth_overlay"].update(password=hash_password(password)), uid)
    allow_next_send(setup, uid)
    result = client.post("/api/v22/auth/registration/email",
                         json={"new_email": "corrected@example.test", "current_password": password})
    assert result.status_code == 200, result.text
    assert result.json()["email"] == "corrected@example.test"


def test_valid_confirmation_does_not_login_and_get_does_not_consume(setup):
    client, token = fresh_link(setup)
    assert client.get("/api/v22/auth/verify-email", params={"token": token}).status_code == 405
    result = confirm(client, token)
    assert result.status_code == 200
    assert result.json()["verified"] is True
    assert "set-cookie" not in result.headers
    assert client.cookies.get("protrebot_session") is None
    doc = get_doc(setup, user_id(client))
    assert doc["auth_overlay"]["email_verified_at"]
    assert doc["email_verification_tokens"][verification.digest(token)]["used_at"]
    repeated = confirm(client, token)
    assert repeated.status_code == 400
    assert repeated.json()["detail"]["code"] == "used"


@pytest.mark.parametrize("kind", ["expired", "invalid", "revoked", "version", "email"])
def test_bad_links_are_rejected_without_auth_changes(setup, kind):
    client, token = fresh_link(setup)
    uid = user_id(client)
    def mutate(doc):
        row = doc["email_verification_tokens"][verification.digest(token)]
        if kind == "expired":
            row["expires"] = time.time() - 1
        elif kind == "revoked":
            row["revoked_at"] = verification.iso()
        elif kind == "version":
            row["version"] += 1
        elif kind == "email":
            row["email"] = "different@example.test"
    alter_doc(setup, mutate, uid)
    result = confirm(client, token + "wrong" if kind == "invalid" else token)
    assert result.status_code == 400
    assert result.json()["detail"]["code"] == ("expired" if kind == "expired" else "invalid")
    assert get_doc(setup, uid)["auth_overlay"]["email_verified"] is False
    assert "set-cookie" not in result.headers


def test_confirmation_is_atomic_and_rolls_back_token_on_security_failure(setup):
    client, token = fresh_link(setup)
    with patch.object(auth, "update_auth_security", side_effect=HTTPException(503, "offline write failed")):
        assert confirm(client, token).status_code == 503
    row = get_doc(setup, user_id(client))["email_verification_tokens"][verification.digest(token)]
    assert row["used_at"] is None
    assert confirm(client, token).status_code == 200


def test_only_one_concurrent_confirmation_succeeds_across_workers(setup):
    first, token = fresh_link(setup)
    second = setup.runtime()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda client: confirm(client, token), [first, second]))
    assert sorted(result.status_code for result in results) == [200, 400]
    assert next(result for result in results if result.status_code == 400).json()["detail"]["code"] == "used"
    assert get_doc(setup, user_id(first))["auth_overlay"]["email_verified"] is True


def test_pending_status_survives_worker_restart_and_exchanges_exactly_once(setup):
    first, token = fresh_link(setup)
    cookie = first.cookies.get(REGISTRATION_PENDING_COOKIE)
    second = setup.runtime()
    second.cookies.set(REGISTRATION_PENDING_COOKIE, cookie, domain="account.example.test", path="/api")
    assert second.get("/api/v22/auth/registration/status").json()["verified"] is False
    assert confirm(first, token).status_code == 200
    assert second.get("/api/v22/auth/registration/status").json()["can_exchange"] is True
    exchanged = second.post("/api/v22/auth/registration/exchange")
    assert exchanged.status_code == 200
    assert exchanged.json()["requires_login"] is False
    assert exchanged.json()["token"].startswith("cookie-session:")
    assert second.cookies.get(REGISTRATION_PENDING_COOKIE) is None
    assert second.get("/api/v22/account/overview").status_code == 200
    second.cookies.set(REGISTRATION_PENDING_COOKIE, cookie, domain="account.example.test", path="/api")
    assert second.post("/api/v22/auth/registration/exchange").status_code == 401


def test_second_device_confirms_without_any_login_or_pending_cookie(setup):
    first, token = fresh_link(setup)
    other = setup.runtime()
    confirmed = confirm(other, token)
    assert confirmed.status_code == 200
    assert confirmed.json()["email"] == "new@example.test"
    assert "set-cookie" not in confirmed.headers
    assert other.post("/api/v22/auth/registration/exchange").status_code == 401
    assert other.get("/api/v22/account/overview").status_code == 401
    assert first.get("/api/v22/auth/registration/status").json()["verified"] is True


@pytest.mark.parametrize("reason", ["2fa", "password-rotation"])
def test_pending_conversion_never_bypasses_2fa_or_rotated_credentials(setup, reason):
    client, token = fresh_link(setup)
    assert confirm(client, token).status_code == 200
    uid = user_id(client)
    def change(doc):
        if reason == "2fa":
            doc["two_factor_enabled"] = True
        else:
            doc["auth_overlay"]["auth_version"] += 1
    alter_doc(setup, change, uid)
    exchanged = client.post("/api/v22/auth/registration/exchange")
    assert exchanged.status_code == 200
    assert exchanged.json() == {"requires_login": True, "email": "new@example.test"}
    assert client.cookies.get("protrebot_session") is None
    assert client.cookies.get(REGISTRATION_PENDING_COOKIE) is None


def test_pending_cookie_is_not_a_user_session_and_unverified_customer_stays_403(setup):
    client, _ = fresh_link(setup)
    assert client.get("/api/v22/account/overview").status_code == 401
    assert client.post("/api/v22/auth/registration/exchange").status_code == 403
    result = client.post("/api/v22/auth/login", json={"email": "new@example.test", "password": PASSWORD})
    assert result.status_code == 200
    assert result.json()["user"]["email_verified"] is False
    assert client.get("/api/v22/account/overview", headers={"Authorization": "Bearer " + result.json()["token"]}).status_code == 403
    assert client.post("/api/v22/subscription/checkout", json={"plan": "MASTER_MODE"},
                       headers={"Authorization": "Bearer " + result.json()["token"]}).status_code == 403


def test_pending_cookie_requires_same_origin_csrf_verification():
    req = SimpleNamespace(method="POST", cookies={REGISTRATION_PENDING_COOKIE: "offline"},
                          headers={}, url=SimpleNamespace(scheme="https", netloc="kaistrade.com"))
    with pytest.raises(HTTPException) as caught:
        validate_browser_request(req, ["https://kaistrade.com"])
    assert caught.value.status_code == 403
    req.headers = {"x-requested-with": "XMLHttpRequest", "origin": "https://evil.example"}
    with pytest.raises(HTTPException):
        validate_browser_request(req, ["https://kaistrade.com"])
    req.headers["origin"] = "https://kaistrade.com"
    validate_browser_request(req, ["https://kaistrade.com"])


def test_resend_paths_share_durable_cooldown_and_hourly_quota_and_invalidate_old_link(setup):
    client, old_token = fresh_link(setup)
    uid = user_id(client)
    assert client.post("/api/v22/auth/registration/resend", json={}).status_code == 429
    user_token = auth.issue_token(uid, "CUSTOMER", SECRET)
    h = {"Authorization": "Bearer " + user_token}
    assert client.post("/api/v22/account/verification/resend", headers=h).status_code == 429
    for index in range(4):
        alter_doc(setup, lambda doc: doc.update(verification_last_sent=time.time() - 61), uid)
        auth.LOGIN_ATTEMPTS.clear()
        path = "/api/v22/account/verification/resend" if index % 2 else "/api/v22/auth/registration/resend"
        assert client.post(path, headers=h if index % 2 else {}, json={}).status_code == 200
    assert confirm(client, old_token).status_code == 400
    new_token = sent_token(setup)
    alter_doc(setup, lambda doc: doc.update(verification_last_sent=time.time() - 61), uid)
    auth.LOGIN_ATTEMPTS.clear()
    restarted = setup.runtime()
    assert restarted.post("/api/v22/account/verification/resend", headers=h).status_code == 429
    assert confirm(client, new_token).status_code == 200
    assert setup.mail.call_count == 5


def test_duplicate_registration_pending_response_never_reveals_existing_verification(setup):
    new, duplicate = setup.runtime(), setup.runtime()
    first = signup(new)
    second = signup(duplicate, setup.users[0]["email"])
    assert set(first.json()) == set(second.json())
    assert first.json()["message"] == second.json()["message"]
    assert new.get("/api/v22/auth/registration/status").json()["verified"] is False
    assert duplicate.get("/api/v22/auth/registration/status").json()["verified"] is False
    assert duplicate.post("/api/v22/auth/registration/exchange").status_code == 401
    assert len(duplicate.app.state.v22_commercial["state"]["users"]) == 3


def test_legacy_24_hour_link_still_works_after_flag_is_enabled(setup):
    client = setup.runtime()
    user = client.app.state.v22_commercial["state"]["users"][0]
    user["email_verified"] = False
    token = auth.issue_one_time_token(client.app.state.v22_commercial["state"], user, SECRET, kind="EMAIL_VERIFY")
    assert auth.verify_token(token, SECRET)["exp"] - auth.verify_token(token, SECRET)["iat"] == 86400
    assert confirm(client, token).status_code == 200
    assert get_doc(setup)["auth_overlay"]["email_verified_at"]
    assert confirm(client, token).status_code == 400


def test_legacy_consumption_rolls_back_with_verification_write_failure(setup):
    client = setup.runtime()
    user = client.app.state.v22_commercial["state"]["users"][0]
    user["email_verified"] = False
    token = auth.issue_one_time_token(client.app.state.v22_commercial["state"], user, SECRET, kind="EMAIL_VERIFY")
    asyncio.run(store.save_action_token(request(client), token, user, "EMAIL_VERIFY"))
    with patch.object(auth, "update_auth_security", side_effect=HTTPException(503, "offline failure")):
        assert confirm(client, token).status_code == 503
    assert confirm(client, token).status_code == 200


def test_flag_off_exact_legacy_registration_mail_token_and_verification_contract(setup, monkeypatch):
    monkeypatch.delenv("PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED")
    client = setup.runtime()
    result = signup(client)
    assert set(result.json()) == {"user", "message", "email_verification_required", "verification_status_token"}
    assert "set-cookie" not in result.headers
    assert "email_verification_v2_enabled" not in client.get("/api/v22/public").json()
    token = sent_token(setup)
    payload = verify_token(token, SECRET, expected_kind="EMAIL_VERIFY")
    assert payload["exp"] - payload["iat"] == 86400
    assert "verification_v2" not in setup.mail.call_args.kwargs
    assert confirm(client, token).json() == {"ok": True, "message": "E-posta doğrulandı. Artık giriş yapabilirsiniz."}
    assert client.get("/api/v22/auth/registration/status").status_code == 404


def test_one_time_migration_never_verifies_accounts_created_afterwards(setup):
    client = setup.runtime()
    client.app.state.v22_commercial["state"]["users"][0]["email_verified"] = False
    assert verification.migrate_local_accounts(request(client)) == 3
    timestamp = get_doc(setup)["auth_overlay"]["email_verified_at"]
    signup(client)
    assert verification.migrate_local_accounts(request(client)) == 0
    assert get_doc(setup, user_id(client))["auth_overlay"]["email_verified"] is False
    assert get_doc(setup)["auth_overlay"]["email_verified_at"] == timestamp
    sql = (Path(__file__).parents[1] / "migrations" / "20261009_001_email_verification_v2.sql").read_text()
    assert "ON CONFLICT (name) DO NOTHING" in sql
    assert "FROM first_run" in sql
    assert "LOCK TABLE commercial_auth_users" in sql


def test_postgres_transaction_rolls_back_verification_token_and_security_together():
    user = make_user()
    user["email_verified"] = False
    class LookupStore(SharedStore):
        async def fetchrow(self, sql, *args):
            if "payload->'email_verification_tokens'" in sql:
                self.check(sql)
                return {"user_id": user["id"]}
            return await super().fetchrow(sql, *args)
    shared = LookupStore(user)
    req = make_request(user, shared)
    token = asyncio.run(verification.registration_link(req, user))
    with patch.object(auth, "update_auth_security", side_effect=HTTPException(503, "offline failure")), \
            pytest.raises(HTTPException):
        asyncio.run(verification.verify_link(req, token))
    assert shared.account_settings[user["id"]]["email_verification_tokens"][verification.digest(token)]["used_at"] is None
    assert shared.users[user["id"]]["security"]["email_verified"] is False
    assert asyncio.run(verification.verify_link(req, token))["verified"] is True
    assert shared.users[user["id"]]["security"]["email_verified_at"]


def test_only_verification_mail_uses_dark_table_template():
    text, html = email_service.verification_email("https://kaistrade.com/verify-email?token=offline&x=1")
    assert "30 dakika" in text and "tek kullanımlıktır" in text
    assert "#0B0E0C" in html and "<table" in html
    assert "offline&amp;x=1" in html
    assert "backdrop-filter" not in html and "blur(" not in html
    legacy = email_service.auth_email_html("Parolanı yenile", "Ada", "https://example.test/reset", "Yenile", "24 saat")
    assert "#f4f6f8" in legacy and "#0B0E0C" not in legacy


def test_unverified_customer_is_denied_at_real_middleware_before_exchange_billing_or_live(setup):
    from app import exchange_connections, main, v25_execution
    client = setup.runtime()
    client.app.include_router(exchange_connections.router)
    client.app.include_router(v25_execution.router)
    @client.app.middleware("http")
    async def gate(req, call_next):
        return await main.owner_preview_gate(req, call_next)
    signup(client)
    uid = user_id(client)
    h = {"Authorization": "Bearer " + auth.issue_token(uid, "CUSTOMER", SECRET),
         "Origin": "https://account.example.test", "X-Requested-With": "XMLHttpRequest"}
    paths = ("/api/exchange-connections/save", "/api/exchange-connections/activate",
             "/api/v22/subscription/checkout", "/api/v25/consent", "/api/v25/arm",
             "/api/v25/order", "/api/v25/auto/start")
    with patch.object(main, "WEB_REQUIRE_AUTH", True), \
            patch.object(main, "WEB_ACCESS_TOKEN", "offline-owner-access-key-long-enough"), \
            patch.object(main, "WEB_CORS_ORIGINS", ["https://account.example.test"]):
        for path in paths:
            response = client.post(path, json={}, headers=h)
            assert response.status_code == 403, (path, response.text)
            assert response.json()["detail"] == "E-posta doğrulaması gerekli"


def test_pending_exchange_does_not_replace_unrelated_cookie_even_with_selected_bearer(setup):
    client, token = fresh_link(setup)
    assert confirm(client, token).status_code == 200
    other = auth.issue_token("other", "CUSTOMER", SECRET)
    client.cookies.set("protrebot_session", other, domain="account.example.test", path="/api")
    selected = auth.issue_token(user_id(client), "CUSTOMER", SECRET)
    response = client.post("/api/v22/auth/registration/exchange",
                           headers={"Authorization": "Bearer " + selected}, json={})
    assert response.status_code == 409
    assert "set-cookie" not in response.headers
    assert client.cookies.get("protrebot_session") == other


def test_resend_delivery_failure_rolls_back_quota_and_old_link_invalidation(setup):
    client, token = fresh_link(setup)
    uid = user_id(client)
    alter_doc(setup, lambda doc: doc.update(verification_last_sent=time.time() - 61), uid)
    previous = get_doc(setup, uid)
    failure = email_service.EmailDeliveryError("offline delivery failure", provider="resend")
    with patch.object(auth, "send_auth_email", side_effect=failure):
        response = client.post("/api/v22/auth/registration/resend", json={})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert get_doc(setup, uid) == previous
    assert confirm(client, token).status_code == 200


def test_concurrent_pending_exchange_has_only_one_session_issuance(setup):
    first, token = fresh_link(setup)
    assert confirm(first, token).status_code == 200
    second = setup.runtime()
    second.cookies.set(REGISTRATION_PENDING_COOKIE, first.cookies.get(REGISTRATION_PENDING_COOKIE),
                       domain="account.example.test", path="/api")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda client: client.post("/api/v22/auth/registration/exchange", json={}), [first, second]))
    assert sorted(result.status_code for result in results) == [200, 401]
    assert len(get_doc(setup, user_id(first))["sessions"]) == 1


def test_local_migration_cli_reads_explicit_snapshot_and_is_idempotent(setup):
    from backend.tools.migrate_email_verification_v2 import run_local
    snapshot = setup.path.with_suffix(".json")
    snapshot.write_text(json.dumps({"users": setup.users}), encoding="utf-8")
    try:
        assert run_local(setup.path, snapshot) == 3
        assert run_local(setup.path, snapshot) == 0
        assert get_doc(setup)["auth_overlay"]["email_verified_at"]
    finally:
        snapshot.unlink()
