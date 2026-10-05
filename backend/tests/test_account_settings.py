"""Offline request regressions using a real, shared SQLite account store."""
import asyncio
import copy
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from contextlib import closing
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import account_settings as account
from app import account_store as store
from app import v22_commercial as auth
from app import google_oauth as google
from app.commercial_core import default_commercial_state, hash_password, issue_token, verify_token

SECRET = b"offline-account-security-fixture-secret"
PASSWORD = "Offline-account-password-123!"


@pytest.fixture
def setup():
    # Scratch artifacts stay in the project, never in OS temporary directories.
    path = Path(__file__).parent / (".account-test-" + uuid.uuid4().hex + ".sqlite3")
    customer = {"id": "customer", "email": "member@example.test", "display_name": "Member",
                "password": hash_password(PASSWORD), "role": "CUSTOMER", "active": True,
                "email_verified": True, "auth_version": 1, "created_at": auth.now_iso()}
    owner = {**copy.deepcopy(customer), "id": "owner", "email": "owner@example.test", "role": "OWNER"}
    other = {**copy.deepcopy(customer), "id": "other", "email": "other@example.test"}
    users = [customer, owner, other]
    clients = []
    def runtime():
        application = FastAPI()
        application.include_router(auth.router)
        application.include_router(account.router)
        state = default_commercial_state()
        state["users"] = copy.deepcopy(users)
        state["owner_user_id"] = "owner"
        state["profiles"] = [{"user_id": "customer", "preferences": {"unrelated": "retained"}}]
        application.state.v22_commercial = {"state": state, "secret": SECRET, "lock": asyncio.Lock(),
                                             "storage_lock": asyncio.Lock(), "auth_baseline": {}}
        application.state.db_pool = None
        application.state.account_settings_path = path
        @application.middleware("http")
        async def canonical(request, call_next):
            try:
                if request.url.path.startswith(("/api/v22/account", "/api/v22/admin", "/api/v22/customers", "/api/v22/profile")):
                    request.state.member = await auth.authenticated_user_async(request)
                return await call_next(request)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        client = TestClient(application, base_url="https://account.example.test")
        clients.append(client)
        return client
    with patch.object(auth, "DURABLE_AUTH_REQUIRED", False), patch.object(auth, "save_state"), \
            patch.object(auth, "persist_v22_commercial", AsyncMock(return_value=True)), \
            patch.object(auth, "restore_demo_state_for_user", AsyncMock()), \
            patch.object(auth, "restore_v21_state_for_user", AsyncMock()), \
            patch.object(auth, "schedule_log_event"), \
            patch.object(auth, "gmail_configured", return_value=True), \
            patch.object(auth, "send_auth_email") as mail:
        auth.LOGIN_ATTEMPTS.clear()
        yield SimpleNamespace(runtime=runtime, users=users, path=path, mail=mail)
    for client in clients:
        client.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def headers(user="customer", version=1, **extra):
    return {"Authorization": "Bearer " + issue_token(user, "OWNER" if user == "owner" else "CUSTOMER", SECRET,
                                                    token_version=version), **extra}


def get_doc(setup, user="customer"):
    with closing(sqlite3.connect(setup.path)) as db, db:
        return json.loads(db.execute("SELECT payload FROM commercial_account_settings WHERE user_id=?", (user,)).fetchone()[0])


def alter_doc(setup, mutate, user="customer"):
    with closing(sqlite3.connect(setup.path)) as db, db:
        doc = json.loads(db.execute("SELECT payload FROM commercial_account_settings WHERE user_id=?", (user,)).fetchone()[0])
        mutate(doc)
        db.execute("UPDATE commercial_account_settings SET payload=? WHERE user_id=?", (json.dumps(doc), user))


def enroll(setup, client, h):
    response = client.post("/api/v22/account/2fa/setup", headers=h, json={"current_password": PASSWORD})
    assert response.status_code == 200
    secret = response.json()["secret"]
    response = client.post("/api/v22/account/2fa/enable", headers=h, json={"code": pyotp.TOTP(secret).now()})
    assert response.status_code == 200
    assert response.json()["reauthenticate"] is True
    return secret, response.json()["recovery_codes"]


def test_overview_preferences_profile_persist_and_are_secret_free(setup):
    client = setup.runtime()
    h = headers()
    first = client.get("/api/v22/account/overview", headers=h).json()
    assert first["security"]["active_sessions"] == 1
    assert first["sessions"][0]["current"] is True
    assert first["user"]["last_login"] is None
    assert first["subscription"]["plan"] == "FREE"
    assert not first["subscription"]["is_premium"]
    assert set(first["user"]["auth_methods"]) == {"password"}
    assert first["pending_email"] is None
    assert client.patch("/api/v22/account/preferences", headers=h, json={"trading_mode": "AUTO", "risk_per_trade": 0.5, "symbols": ["BTCUSDT"]}).status_code == 200
    assert client.patch("/api/v22/account/profile", headers=h, json={"display_name": "Renamed"}).status_code == 200
    separate = setup.runtime()
    result = separate.get("/api/v22/account/overview", headers=h)
    assert result.status_code == 200
    dto = result.json()
    assert dto["user"]["display_name"] == "Renamed"
    assert dto["preferences"]["trading_mode"] == "AUTO"
    assert dto["preferences"]["risk_per_trade"] == 0.5
    assert client.app.state.v22_commercial["state"]["profiles"][0]["preferences"]["unrelated"] == "retained"
    assert client.app.state.v22_commercial["state"]["profiles"][0]["preferences"]["risk_per_trade"] == 0.5
    legacy_profile = separate.get("/api/v22/profile", headers=h)
    assert legacy_profile.status_code == 200
    assert legacy_profile.json()["profile"]["preferences"] == {"unrelated": "retained", "trading_mode": "AUTO", "risk_per_trade": 0.5, "symbols": ["BTCUSDT"]}
    assert not any(key in result.text for key in ("digest", "totp_seed", "code_hash", "auth_overlay"))
    assert "password" not in dto["user"]
    assert not hasattr(client.app.state, "v25_execution")
    for invalid in ({"risk_per_trade": 1.01}, {"risk_per_trade": 0.05}, {"risk_per_trade": True}, {"exchange": "OTHER"},
                    {"symbols": ["bad"]}, {"unknown": True}, {"timeframe": None}, {"trading_mode": "LIVE"}):
        assert client.patch("/api/v22/account/preferences", headers=h, json=invalid).status_code == 422
    response = client.patch("/api/v22/account/preferences", headers=h, json={"symbols": ["SUSDT", "FUSDT", "CUSDT"]})
    assert response.status_code == 200 and response.json()["symbols"] == ["SUSDT", "FUSDT", "CUSDT"]


def test_session_revoke_cookie_native_and_other_workers(setup):
    client, separate = setup.runtime(), setup.runtime()
    current, other, unobserved = headers(), headers(), headers()
    assert client.get("/api/v22/account/overview", headers=current).status_code == 200
    other_dto = client.get("/api/v22/account/overview", headers=other).json()
    other_id = next(row["id"] for row in other_dto["sessions"] if row["current"])
    assert client.post("/api/v22/account/sessions/revoke", headers=current, json={"session_id": other_id}).status_code == 200
    assert separate.get("/api/v22/account/overview", headers=other).status_code == 401
    cookie = other["Authorization"].removeprefix("Bearer ")
    separate.cookies.set(auth.SESSION_COOKIE_NAME, cookie)
    assert separate.get("/api/v22/account/overview").status_code == 401
    separate.cookies.clear()
    assert client.post("/api/v22/account/sessions/revoke-others", headers=current, json={}).status_code == 200
    assert separate.get("/api/v22/account/overview", headers=unobserved).status_code == 401
    assert separate.get("/api/v22/account/overview", headers=current).status_code == 200
    assert client.post("/api/v22/account/sessions/revoke", headers=headers("other"), json={"session_id": other_id}).status_code == 404
    # Fresh issuance after revoke-others works even during the same second.
    login = separate.post("/api/v22/auth/login", json={"email": setup.users[0]["email"], "password": PASSWORD})
    assert login.status_code == 200
    assert separate.get("/api/v22/account/overview", headers={"Authorization": "Bearer " + login.json()["token"]}).status_code == 200


def test_totp_enrollment_login_challenge_bad_codes_replay_and_redaction(setup):
    client = setup.runtime()
    h = headers()
    client.get("/api/v22/account/overview", headers=h)
    secret, recovery = enroll(setup, client, h)
    doc = get_doc(setup)
    assert secret not in json.dumps(doc)
    assert not any(code in json.dumps(doc) for code in recovery)
    assert "pending_totp" not in doc
    assert client.get("/api/v22/account/overview", headers=h).status_code == 401
    login = client.post("/api/v22/auth/login", json={"email": setup.users[0]["email"], "password": PASSWORD, "browser_session": True})
    assert login.status_code == 200
    assert login.json()["mfa_required"] is True
    assert "set-cookie" not in login.headers and "token" not in login.json()
    challenge = login.json()["challenge_id"]
    assert client.post("/api/v22/auth/2fa/login", json={"challenge_id": challenge, "code": "000000"}).status_code == 401
    # Enrollment's OTP is one-use; recovery is independently one-use.
    assert client.post("/api/v22/auth/2fa/login", json={"challenge_id": challenge, "code": pyotp.TOTP(secret).now()}).status_code == 401
    success = client.post("/api/v22/auth/2fa/login", json={"challenge_id": challenge, "code": recovery[0]})
    assert success.status_code == 200
    assert success.json()["token"].startswith(auth.BROWSER_SESSION_MARKER_PREFIX)
    assert success.json()["remember"] is False
    assert "set-cookie" in success.headers
    assert client.post("/api/v22/auth/2fa/login", json={"challenge_id": challenge, "code": recovery[1]}).status_code == 401
    dto = client.get("/api/v22/account/overview").json()
    assert dto["security"]["two_factor_enabled"] is True
    assert dto["user"]["last_login"] is not None
    assert secret not in json.dumps(dto) and not any(code in json.dumps(dto) for code in recovery)
    assert client.post("/api/v22/account/password", json={"current_password": PASSWORD, "new_password": PASSWORD+"x", "confirm_password": PASSWORD+"x"}).status_code == 401
    disable = client.post("/api/v22/account/2fa/disable", json={"current_password": PASSWORD, "totp_code": recovery[1]})
    assert disable.status_code == 200
    assert not get_doc(setup)["two_factor_enabled"]


def test_expired_enrollment_challenge_attempt_limit(setup):
    client, h = setup.runtime(), headers()
    client.get("/api/v22/account/overview", headers=h)
    response = client.post("/api/v22/account/2fa/setup", headers=h, json={"current_password": PASSWORD})
    secret = response.json()["secret"]
    alter_doc(setup, lambda doc: doc.update(pending_totp_expires=0))
    assert client.post("/api/v22/account/2fa/enable", headers=h, json={"code": pyotp.TOTP(secret).now()}).status_code == 401
    secret, recovery = enroll(setup, client, h)
    login = client.post("/api/v22/auth/login", json={"email": setup.users[0]["email"], "password": PASSWORD}).json()
    challenge = login["challenge_id"]
    alter_doc(setup, lambda doc: doc["challenges"][account.digest(challenge)].update(expires=0))
    assert client.post("/api/v22/auth/2fa/login", json={"challenge_id": challenge, "code": recovery[0]}).status_code == 401
    auth.LOGIN_ATTEMPTS.clear()
    challenge = client.post("/api/v22/auth/login", json={"email": setup.users[0]["email"], "password": PASSWORD}).json()["challenge_id"]
    for _ in range(5):
        assert client.post("/api/v22/auth/2fa/login", json={"challenge_id": challenge, "code": "000000"}).status_code == 401
    assert client.post("/api/v22/auth/2fa/login", json={"challenge_id": challenge, "code": recovery[0]}).status_code == 401


def test_google_only_verified_email_otp_password_establishment_and_failures(setup):
    setup.users[0].update(password={}, auth_provider="google")
    client, h = setup.runtime(), headers()
    client.get("/api/v22/account/overview", headers=h)
    setup.mail.side_effect = RuntimeError("provider offline with private recipient")
    failure = client.post("/api/v22/account/reauth/email", headers=h, json={})
    assert failure.status_code == 503 and "private recipient" not in failure.text
    assert not get_doc(setup)["challenges"]
    setup.mail.side_effect = None
    challenge = client.post("/api/v22/account/reauth/email", headers=h, json={}).json()["challenge_id"]
    code = setup.mail.call_args.kwargs["action_label"].split(": ")[1]
    assert code not in json.dumps(get_doc(setup))
    data = {"new_password": PASSWORD, "confirm_password": PASSWORD, "challenge_id": challenge, "email_code": "wrong"}
    assert client.post("/api/v22/account/password", headers=h, json=data).status_code == 401
    data["email_code"] = code
    assert client.post("/api/v22/account/password", headers=headers("other"), json=data).status_code == 401
    response = client.post("/api/v22/account/password", headers=h, json=data)
    assert response.status_code == 200 and response.json()["reauthenticate"]
    assert setup.runtime().get("/api/v22/account/overview", headers=h).status_code == 401
    new_headers = headers(version=2)
    assert setup.runtime().post("/api/v22/account/password", headers=new_headers, json=data).status_code == 401
    login = setup.runtime().post("/api/v22/auth/login", json={"email": setup.users[0]["email"], "password": PASSWORD})
    assert login.status_code == 200 and "token" in login.json()


def test_pending_email_confirm_binding_single_use_duplicates_and_notification_failure(setup):
    client, h = setup.runtime(), headers()
    client.get("/api/v22/account/overview", headers=h)
    pending = client.post("/api/v22/account/email/request", headers=h, json={"new_email": "new@example.test", "current_password": PASSWORD})
    assert pending.status_code == 200
    assert client.get("/api/v22/account/overview", headers=h).json()["user"]["email"] == setup.users[0]["email"]
    token = parse_qs(urlsplit(setup.mail.call_args.kwargs["action_url"]).query)["email_token"][0]
    assert token not in json.dumps(get_doc(setup))
    assert client.post("/api/v22/account/email/confirm", headers=headers("other"), json={"token": token}).status_code == 400
    setup.mail.side_effect = RuntimeError("notification failed")
    success = client.post("/api/v22/account/email/confirm", headers=h, json={"token": token})
    assert success.status_code == 200 and success.json()["reauthenticate"]
    separate = setup.runtime()
    assert separate.get("/api/v22/account/overview", headers=h).status_code == 401
    new = headers(version=2)
    assert separate.get("/api/v22/account/overview", headers=new).json()["user"]["email"] == "new@example.test"
    assert separate.post("/api/v22/auth/login", json={"email": setup.users[0]["email"], "password": PASSWORD}).status_code == 401
    assert separate.post("/api/v22/auth/login", json={"email": "new@example.test", "password": PASSWORD}).status_code == 200
    assert separate.post("/api/v22/account/email/confirm", headers=new, json={"token": token}).status_code == 400
    setup.mail.side_effect = None
    assert separate.post("/api/v22/account/email/request", headers=new, json={"new_email": setup.users[2]["email"], "current_password": PASSWORD}).status_code == 200
    duplicate = parse_qs(urlsplit(setup.mail.call_args.kwargs["action_url"]).query)["email_token"][0]
    assert separate.post("/api/v22/account/email/confirm", headers=new, json={"token": duplicate}).status_code == 409
    assert separate.post("/api/v22/account/email/cancel", headers=new, json={}).status_code == 200
    assert separate.post("/api/v22/account/email/resend", headers=new, json={}).status_code == 409


def test_soft_closure_retention_blockers_login_rejected_and_admin_reopen(setup):
    client, h = setup.runtime(), headers()
    client.get("/api/v22/account/overview", headers=h)
    request = {"confirmation": "HESABI KAPAT", "current_password": PASSWORD}
    assert client.post("/api/v22/account/close", headers=headers("owner"), json=request).status_code == 409
    client.app.state._v21_demo_user_state = {"customer": {"snapshot": {"positions": [{"symbol": "BTCUSDT"}]}}}
    assert client.post("/api/v22/account/close", headers=h, json=request).status_code == 409
    client.app.state._v21_demo_user_state = {}
    assert client.post("/api/v22/account/close", headers=h, json={**request, "confirmation": "wrong"}).status_code == 422
    assert client.post("/api/v22/account/close", headers=h, json=request).status_code == 200
    assert len(client.app.state.v22_commercial["state"]["users"]) == 3
    separate = setup.runtime()
    assert separate.get("/api/v22/account/overview", headers=h).status_code == 401
    assert separate.post("/api/v22/auth/login", json={"email": setup.users[0]["email"], "password": PASSWORD}).status_code == 401
    admin = separate.get("/api/v22/admin/accounts/customer", headers=headers("owner"))
    assert admin.status_code == 200 and admin.json()["user"]["active"] is False
    listing = separate.get("/api/v22/admin/accounts?status=inactive", headers=headers("owner")).json()
    assert listing["total"] == 1 and listing["users"][0]["closed_at"]
    # Existing owner status control reopens without resurrecting old versions.
    reopened = separate.post("/api/v22/customers/customer/status", headers=headers("owner"),
                             json={"active": True, "reason": "Offline reopen"})
    assert reopened.status_code == 200
    assert setup.runtime().get("/api/v22/account/overview", headers=h).status_code == 401
    fresh = setup.runtime()
    login = fresh.post("/api/v22/auth/login", json={"email": setup.users[0]["email"], "password": PASSWORD})
    assert login.status_code == 200 and "token" in login.json()
    token = login.json()["token"]
    assert verify_token(token, SECRET)["ver"] == 3
    assert fresh.get("/api/v22/account/overview", headers={"Authorization": "Bearer "+token}).status_code == 200
    reopened_dto = fresh.get("/api/v22/admin/accounts/customer", headers=headers("owner")).json()
    assert reopened_dto["user"]["active"] is True
    assert any(row["kind"] == "ACCOUNT_CLOSED" for row in reopened_dto["activity"])


def test_owner_only_admin_redaction_reset_delivery_and_filters(setup):
    client = setup.runtime()
    client.get("/api/v22/account/overview", headers=headers())
    for endpoint in ("/api/v22/admin/accounts", "/api/v22/admin/accounts/customer"):
        assert client.get(endpoint, headers=headers()).status_code == 403
    assert client.post("/api/v22/admin/accounts/customer/password-reset", headers=headers(), json={}).status_code == 403
    owner = headers("owner")
    result = client.get("/api/v22/admin/accounts?premium=free&verified=verified&search=member@example.test&page_size=1", headers=owner)
    assert result.status_code == 200 and result.json()["total"] == 1
    dto = client.get("/api/v22/admin/accounts/customer", headers=owner).json()
    assert not any(row["current"] for row in dto["sessions"])
    assert dto["user"]["role"] == "CUSTOMER"
    assert client.get("/api/v22/admin/accounts?premium=invalid", headers=owner).status_code == 422
    assert not client.get("/api/v22/admin/accounts/owner", headers=owner).json()["subscription"]["is_premium"]
    setup.mail.side_effect = RuntimeError("smtp failure secret-not-returned")
    response = client.post("/api/v22/admin/accounts/customer/password-reset", headers=owner, json={})
    assert response.status_code == 503 and "secret-not-returned" not in response.text
    setup.mail.side_effect = None
    response = client.post("/api/v22/admin/accounts/customer/password-reset", headers=owner, json={})
    assert response.json() == {"ok": True}
    assert "token" not in response.text


def test_shared_reset_token_expiry_replay_and_totp_enforcement(setup):
    client = setup.runtime()
    h = headers()
    client.get("/api/v22/account/overview", headers=h)
    secret, recovery = enroll(setup, client, h)
    auth.LOGIN_ATTEMPTS.clear()
    forgot = client.post("/api/v22/auth/forgot-password", json={"email": setup.users[0]["email"]})
    assert forgot.status_code == 200
    token = parse_qs(urlsplit(setup.mail.call_args.kwargs["action_url"]).query)["token"][0]
    with closing(sqlite3.connect(setup.path)) as db, db:
        row = db.execute("SELECT token_hash,used FROM commercial_account_tokens").fetchone()
        assert token != row[0] and row[1] == 0
    separate = setup.runtime()
    data = {"token": token, "password": PASSWORD+"x", "confirm_password": PASSWORD+"x"}
    assert separate.post("/api/v22/auth/reset-password", json=data).status_code == 401
    assert separate.post("/api/v22/auth/reset-password", json={**data, "totp_code": "000000"}).status_code == 401
    assert separate.post("/api/v22/auth/reset-password", json={**data, "totp_code": recovery[0]}).status_code == 200
    assert client.post("/api/v22/auth/reset-password", json={**data, "totp_code": recovery[1]}).status_code == 400
    with closing(sqlite3.connect(setup.path)) as db, db:
        assert db.execute("SELECT used FROM commercial_account_tokens").fetchone()[0] == 1
    # Expiry checked both by signed token verification and canonical token row.
    auth.LOGIN_ATTEMPTS.clear()
    client.post("/api/v22/auth/forgot-password", json={"email": setup.users[0]["email"]})
    expired = parse_qs(urlsplit(setup.mail.call_args.kwargs["action_url"]).query)["token"][0]
    with closing(sqlite3.connect(setup.path)) as db, db:
        db.execute("UPDATE commercial_account_tokens SET expires=0 WHERE token_hash=?", (account.digest(expired),))
    assert separate.post("/api/v22/auth/reset-password", json={**data, "token": expired, "totp_code": recovery[2]}).status_code == 400


def test_persistence_outage_is_not_success_or_partial_preferences_write(setup):
    client, h = setup.runtime(), headers()
    client.get("/api/v22/account/overview", headers=h)
    before = get_doc(setup)
    with patch.object(store, "connection", side_effect=sqlite3.OperationalError("private storage path")):
        response = client.patch("/api/v22/account/preferences", headers=h, json={"trading_mode": "AUTO"})
        assert response.status_code == 503 and "private storage path" not in response.text
    assert get_doc(setup) == before
    # Failed setup commit returns no seed and does not enable MFA.
    with patch.object(account, "cipher", side_effect=RuntimeError("encrypted-private-value")):
        response = client.post("/api/v22/account/2fa/setup", headers=h, json={"current_password": PASSWORD})
        assert response.status_code == 503 and "encrypted-private-value" not in response.text
    assert not get_doc(setup)["two_factor_enabled"]


def test_google_session_helper_never_issues_cookie_before_mfa(setup):
    client, h = setup.runtime(), headers()
    client.get("/api/v22/account/overview", headers=h)
    enroll(setup, client, h)
    from fastapi import Response
    request = SimpleNamespace(app=client.app, state=SimpleNamespace(), headers={}, cookies={}, client=SimpleNamespace(host="127.0.0.1"))
    user = client.app.state.v22_commercial["state"]["users"][0]
    response = Response()
    with pytest.raises(HTTPException) as caught:
        asyncio.run(google.establish_session(request, response, user, False))
    assert caught.value.status_code == 403
    assert "set-cookie" not in response.headers


def test_email_reauth_expiry_attempt_limits_and_pending_email_expiry(setup):
    setup.users[0].update(password={}, auth_provider="google")
    client, h = setup.runtime(), headers()
    client.get("/api/v22/account/overview", headers=h)
    challenge = client.post("/api/v22/account/reauth/email", headers=h, json={}).json()["challenge_id"]
    code = setup.mail.call_args.kwargs["action_label"].split(": ")[1]
    alter_doc(setup, lambda doc: doc["challenges"][account.digest(challenge)].update(expires=0))
    payload = {"challenge_id": challenge, "email_code": code}
    assert client.post("/api/v22/account/2fa/setup", headers=h, json=payload).status_code == 401
    challenge = client.post("/api/v22/account/reauth/email", headers=h, json={}).json()["challenge_id"]
    code = setup.mail.call_args.kwargs["action_label"].split(": ")[1]
    for _ in range(5):
        assert client.post("/api/v22/account/2fa/setup", headers=h, json={"challenge_id": challenge, "email_code": "wrong"}).status_code == 401
    assert client.post("/api/v22/account/2fa/setup", headers=h, json={"challenge_id": challenge, "email_code": code}).status_code == 401
    auth.LOGIN_ATTEMPTS.clear()
    challenge = client.post("/api/v22/account/reauth/email", headers=h, json={}).json()["challenge_id"]
    code = setup.mail.call_args.kwargs["action_label"].split(": ")[1]
    assert client.post("/api/v22/account/email/request", headers=h, json={"challenge_id": challenge, "email_code": code, "new_email": "pending@example.test"}).status_code == 200
    token = parse_qs(urlsplit(setup.mail.call_args.kwargs["action_url"]).query)["email_token"][0]
    alter_doc(setup, lambda doc: doc["challenges"][account.digest(token)].update(expires=0))
    assert client.post("/api/v22/account/email/confirm", headers=h, json={"token": token}).status_code == 400
    assert client.post("/api/v22/account/email/request", headers=h, json={"challenge_id": challenge, "email_code": code, "new_email": "another@example.test"}).status_code == 401
    # Provider absence is explicit, with no pretend-delivered challenge.
    auth.LOGIN_ATTEMPTS.clear()
    with patch.object(auth, "gmail_configured", return_value=False):
        assert client.post("/api/v22/account/reauth/email", headers=h, json={}).status_code == 503


def test_verification_resend_real_delivery_cross_worker_single_use(setup):
    setup.users[0]["email_verified"] = False
    client, h = setup.runtime(), headers()
    assert client.get("/api/v22/account/overview", headers=h).status_code == 403
    response = client.post("/api/v22/account/verification/resend", headers=h, json={})
    assert response.status_code == 200
    token = parse_qs(urlsplit(setup.mail.call_args.kwargs["action_url"]).query)["token"][0]
    separate = setup.runtime()
    assert separate.post("/api/v22/auth/verify-email", json={"token": token}).status_code == 200
    assert client.post("/api/v22/auth/verify-email", json={"token": token}).status_code == 400
    # Already verified does not generate a fake mail or fake verification row.
    sent = setup.mail.call_count
    assert separate.post("/api/v22/account/verification/resend", headers=h, json={}).status_code == 200
    assert setup.mail.call_count == sent


def test_google_callback_mfa_handoff_has_no_session_cookie(setup):
    client, h = setup.runtime(), headers()
    client.get("/api/v22/account/overview", headers=h)
    enroll(setup, client, h)
    user = client.app.state.v22_commercial["state"]["users"][0]
    request = SimpleNamespace(app=client.app, state=SimpleNamespace(google_callback={"state": "opaque", "code": "opaque", "error": ""}),
                              headers={}, cookies={}, client=SimpleNamespace(host="127.0.0.1"))
    attempt = {"origin": "https://kaistrade.com", "remember": True}
    with patch.object(google, "read_attempt", AsyncMock(return_value=attempt)), \
            patch.object(google, "exchange_identity", AsyncMock(return_value={})), \
            patch.object(google, "resolve_identity", AsyncMock(return_value=user)), \
            patch.object(google, "clear_flow_cookie"):
        response = asyncio.run(google.callback(request))
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://kaistrade.com/login?mfa_challenge=")
    assert response.headers["location"].endswith("&google_remember=1")
    assert "set-cookie" not in response.headers


def test_mid_commit_failure_rolls_back_preferences_and_pending_seed(setup):
    client, h = setup.runtime(), headers()
    client.get("/api/v22/account/overview", headers=h)
    original = store.connection

    class FailingConnection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, key):
            return getattr(self.db, key)

        def execute(self, query, args=()):
            if query.startswith("INSERT OR REPLACE"):
                doc = json.loads(args[1])
                if doc.get("preferences", {}).get("trading_mode") == "AUTO" or doc.get("pending_totp") or doc.get("display_name") == "NotPersisted":
                    raise sqlite3.OperationalError("private commit failure")
            return self.db.execute(query, args)

    with patch.object(store, "connection", side_effect=lambda request: FailingConnection(original(request))):
        response = client.patch("/api/v22/account/preferences", headers=h, json={"trading_mode": "AUTO"})
        assert response.status_code == 503 and "private commit failure" not in response.text
        response = client.post("/api/v22/account/2fa/setup", headers=h, json={"current_password": PASSWORD})
        assert response.status_code == 503 and "secret" not in response.text
        assert client.patch("/api/v22/account/profile", headers=h, json={"display_name": "NotPersisted"}).status_code == 503
    doc = get_doc(setup)
    assert doc["preferences"] == {}
    assert "pending_totp" not in doc and not doc["two_factor_enabled"]
    assert client.get("/api/v22/account/overview", headers=h).json()["user"]["display_name"] == "Member"


def test_closure_checks_other_worker_exchange_context_and_fails_closed(setup):
    client = setup.runtime()
    request = SimpleNamespace(app=client.app)
    pool = SimpleNamespace(fetch=AsyncMock(return_value=[{"active": True, "account_summary": {}}]))
    client.app.state.db_pool = pool
    assert asyncio.run(account.close_blocker(request, setup.users[0]))
    pool.fetch.assert_awaited_with("SELECT active, account_summary FROM protrebot_exchange_session_vault WHERE user_id = $1", "customer")
    pool.fetch.return_value = [{"active": False, "account_summary": {"positions": [{"symbol": "BTCUSDT"}]}}]
    assert asyncio.run(account.close_blocker(request, setup.users[0]))
    pool.fetch.side_effect = RuntimeError("private-vault-error")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(account.close_blocker(request, setup.users[0]))
    assert caught.value.status_code == 503 and "private-vault-error" not in caught.value.detail


def test_pre_migration_reset_link_is_atomically_claimed_across_workers(setup):
    client = setup.runtime()
    state = client.app.state.v22_commercial["state"]
    token = auth.issue_one_time_token(state, state["users"][0], SECRET, kind="PASSWORD_RESET")
    separate = setup.runtime()
    separate.app.state.v22_commercial["state"]["auth_tokens"] = copy.deepcopy(state["auth_tokens"])
    payload = {"token": token, "password": PASSWORD+"x", "confirm_password": PASSWORD+"x"}
    assert client.post("/api/v22/auth/reset-password", json=payload).status_code == 200
    assert separate.post("/api/v22/auth/reset-password", json=payload).status_code == 400
    with closing(sqlite3.connect(setup.path)) as db:
        assert db.execute("SELECT used FROM commercial_account_tokens WHERE token_hash=?", (account.digest(token),)).fetchone()[0] == 1


def test_admin_deactivation_refuses_protected_positions_and_security_rotation_retains_them(setup):
    client = setup.runtime()
    h = headers()
    client.get("/api/v22/account/overview", headers=h)
    protected = {"snapshot": {"positions": [{"symbol": "BTCUSDT", "position_amt": 0.01}],
                               "open_algo_orders": [{"symbol": "BTCUSDT", "order_id": 123}]},
                 "plans": {"protected-plan": {"status": "OPEN", "stop_order_id": 123}}}
    client.app.state._v21_demo_user_state = {"customer": copy.deepcopy(protected)}
    response = client.post("/api/v22/customers/customer/status", headers=headers("owner"),
                           json={"active": False, "reason": "Offline suspend"})
    assert response.status_code == 409
    assert client.app.state.v22_commercial["state"]["users"][0]["active"] is True
    assert client.app.state._v21_demo_user_state["customer"] == protected
    response = client.post("/api/v22/account/password", headers=h,
                           json={"current_password": PASSWORD, "new_password": PASSWORD+"x", "confirm_password": PASSWORD+"x"})
    assert response.status_code == 200
    assert client.app.state._v21_demo_user_state["customer"] == protected
    new = headers(version=2)
    setup_response = client.post("/api/v22/account/2fa/setup", headers=new, json={"current_password": PASSWORD+"x"})
    secret = setup_response.json()["secret"]
    enabled = client.post("/api/v22/account/2fa/enable", headers=new, json={"code": pyotp.TOTP(secret).now()})
    assert enabled.status_code == 200
    assert client.app.state._v21_demo_user_state["customer"] == protected
    disabled = client.post("/api/v22/account/2fa/disable", headers=headers(version=3),
                           json={"current_password": PASSWORD+"x", "totp_code": enabled.json()["recovery_codes"][0]})
    assert disabled.status_code == 200
    assert client.app.state._v21_demo_user_state["customer"] == protected
    client.app.state._v21_demo_user_state = {}
    assert client.post("/api/v22/customers/customer/status", headers=headers("owner"),
                       json={"active": False, "reason": "Offline safe suspend"}).status_code == 200


def test_legacy_owner_actions_never_fake_mail_or_storage_success(setup):
    client, owner = setup.runtime(), headers("owner")
    setup.mail.side_effect = RuntimeError("private provider failure")
    response = client.post("/api/v22/admin/users/customer/password-reset", headers=owner, json={})
    assert response.status_code == 503 and "private provider failure" not in response.text
    setup.mail.side_effect = None
    with patch.object(auth, "gmail_configured", return_value=False):
        assert client.post("/api/v22/admin/users/customer/password-reset", headers=owner, json={}).status_code == 503
    with patch.object(auth, "save_state", side_effect=OSError("private snapshot path")):
        response = client.post("/api/v22/customers/customer/status", headers=owner,
                               json={"active": False, "reason": "Offline safe suspend"})
        assert response.status_code == 503 and "private snapshot path" not in response.text
        response = client.post("/api/v22/admin/users/other/sessions/revoke", headers=owner, json={})
        assert response.status_code == 503 and "private snapshot path" not in response.text
    assert len(client.app.state.v22_commercial["state"]["users"]) == 3


@patch.object(auth, "BOOTSTRAP_OWNER_EMAIL", "owner@example.test")
def test_owner_email_password_changes_preserve_id_authority_without_email_privilege_bypass(setup):
    client, owner = setup.runtime(), headers("owner")
    client.get("/api/v22/account/overview", headers=owner)
    response = client.post("/api/v22/account/email/request", headers=owner,
                           json={"new_email": "renamed-owner@example.test", "current_password": PASSWORD})
    assert response.status_code == 200
    token = parse_qs(urlsplit(setup.mail.call_args.kwargs["action_url"]).query)["email_token"][0]
    assert client.post("/api/v22/account/email/confirm", headers=owner, json={"token": token}).status_code == 200
    assert client.app.state.v22_commercial["state"]["owner_user_id"] == "owner"
    fresh = setup.runtime()
    assert fresh.get("/api/v22/admin/accounts", headers=owner).status_code == 401
    login = fresh.post("/api/v22/auth/login", json={"email": "renamed-owner@example.test", "password": PASSWORD})
    assert login.status_code == 200 and login.json()["user"]["id"] == "owner"
    assert login.json()["user"]["role"] == "OWNER"
    authenticated = {"Authorization": "Bearer " + login.json()["token"]}
    assert fresh.get("/api/v22/admin/accounts", headers=authenticated).status_code == 200
    changed = fresh.post("/api/v22/account/password", headers=authenticated,
                         json={"current_password": PASSWORD, "new_password": PASSWORD+"x", "confirm_password": PASSWORD+"x"})
    assert changed.status_code == 200 and changed.json()["reauthenticate"]
    assert fresh.get("/api/v22/admin/accounts", headers=authenticated).status_code == 401
    newest = setup.runtime()
    login = newest.post("/api/v22/auth/login", json={"email": "renamed-owner@example.test", "password": PASSWORD+"x"})
    assert login.status_code == 200 and login.json()["user"]["role"] == "OWNER"
    assert newest.app.state.v22_commercial["state"]["owner_user_id"] == "owner"
    assert newest.get("/api/v22/admin/accounts", headers={"Authorization": "Bearer "+login.json()["token"]}).status_code == 200
    # A non-owner cannot acquire ownership merely by adopting the configured email.
    with patch.object(auth, "BOOTSTRAP_OWNER_EMAIL", "other@example.test"):
        ordinary = newest.post("/api/v22/auth/login", json={"email": "other@example.test", "password": PASSWORD})
    assert ordinary.status_code == 200 and ordinary.json()["user"]["role"] == "CUSTOMER"
    assert newest.get("/api/v22/admin/accounts", headers={"Authorization": "Bearer "+ordinary.json()["token"]}).status_code == 403
