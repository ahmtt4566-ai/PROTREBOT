"""Offline regressions for all eight authentication requirements."""
import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import test_account_settings as account_fixture
from app import account_settings as account
from app import account_store
from app import auth_failures as failures
from app import v22_commercial as auth
from app.commercial_core import verify_token
from app.password_policy import password_rules, validate_new_password
from app.web_security import validate_auth_security_configuration
from fastapi import HTTPException
from test_account_settings import PASSWORD, SECRET, alter_doc, get_doc, headers
from test_security_auth_hardening import SharedStore, make_request, make_user

POLICY_VECTORS = json.loads(Path(__file__).with_name("auth_password_vectors.json").read_text(encoding="utf-8"))
setup = account_fixture.setup


@pytest.mark.parametrize("vector", POLICY_VECTORS)
def test_shared_python_browser_password_vectors(vector):
    password = vector["password"] + vector.get("repeat", "") * vector.get("count", 0)
    assert all(password_rules(password).values()) == vector["valid"]


def request_for(client, host="192.0.2.1"):
    return SimpleNamespace(app=client.app, client=SimpleNamespace(host=host))


def assert_status(status, operation):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(operation)
    assert caught.value.status_code == status
    return caught.value


def test_login_account_failure_limit_survives_workers_and_success(setup):
    first, second = setup.runtime(), setup.runtime()
    email = setup.users[0]["email"]
    for index in range(5):
        response = (first if index % 2 else second).post(
            "/api/v22/auth/login", json={"email": email.upper(), "password": "Wrong-password-123!"})
        assert response.status_code == 401
        if index == 1:
            assert first.post("/api/v22/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    response = second.post("/api/v22/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 429
    assert 895 <= int(response.headers["retry-after"]) <= 900
    assert "set-cookie" not in response.headers


def test_login_ip_limit_counts_distinct_unknown_accounts_and_survives_workers(setup):
    first, second = setup.runtime(), setup.runtime()
    for index in range(20):
        request = request_for(first if index % 2 else second)
        asyncio.run(failures.login_failure_limits(request, f"unknown-{index}@example.test", failed=True))
    blocked = assert_status(429, failures.login_failure_limits(request_for(second), "another@example.test"))
    assert 895 <= int(blocked.headers["Retry-After"]) <= 900
    asyncio.run(failures.login_failure_limits(request_for(second, "192.0.2.2"), "another@example.test"))
    with closing(sqlite3.connect(setup.path)) as db:
        buckets = [row[0] for row in db.execute("SELECT bucket FROM commercial_auth_failures")]
    assert not any("@" in bucket or "192.0.2" in bucket for bucket in buckets)


def test_login_failure_updates_are_atomic_across_threads(setup):
    clients = [setup.runtime() for _ in range(5)]
    def fail(index):
        asyncio.run(failures.login_failure_limits(request_for(clients[index]), "same@example.test", failed=True))
    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(fail, range(5)))
    assert_status(429, failures.login_failure_limits(request_for(clients[0]), "same@example.test"))


def test_progressive_waits_and_24_hour_decay():
    state = failures.new_state()
    now = 100_000.0
    for wait in (900, 1800, 3600, 3600):
        for _ in range(5):
            failures.record_failure(state, failures.LOGIN_ACCOUNT, now)
        assert failures.retry_after(state, now) == wait
        now += wait
        assert failures.retry_after(state, now) == 0
    now += 86400
    for _ in range(5):
        failures.record_failure(state, failures.LOGIN_ACCOUNT, now)
    assert failures.retry_after(state, now) == 900


def test_wrong_attempts_outside_window_do_not_accumulate():
    state = failures.new_state()
    for index in range(30):
        failures.record_failure(state, failures.LOGIN_ACCOUNT, 100_000 + index * 901)
    assert state["level"] == 0
    assert len(state["failures"]) == 1


def test_postgres_failure_limits_shared_and_fail_closed():
    user = make_user()
    store = SharedStore(user)
    first, second = make_request(user, store), make_request(user, store)
    for _ in range(5):
        asyncio.run(failures.login_failure_limits(first, user["email"], failed=True))
    assert_status(429, failures.login_failure_limits(second, user["email"]))
    assert any("pg_advisory_xact_lock" in sql for sql in store.sql)
    store.fail = True
    assert_status(503, failures.login_failure_limits(second, user["email"]))


def test_mfa_total_limit_includes_recovery_and_survives_new_challenges(setup):
    client, other_worker = setup.runtime(), setup.runtime()
    current = headers()
    enrollment = client.post("/api/v22/account/2fa/setup", headers=current, json={"current_password": PASSWORD})
    import pyotp
    enabled = client.post("/api/v22/account/2fa/enable", headers=current,
                          json={"code": pyotp.TOTP(enrollment.json()["secret"]).now()})
    assert enabled.status_code == 200
    recovery = enabled.json()["recovery_codes"][0]
    login_body = {"email": setup.users[0]["email"], "password": PASSWORD}
    first = client.post("/api/v22/auth/login", json=login_body).json()["challenge_id"]
    req = request_for(client)
    async def early_failures():
        for code in ("BAD-RECOVERY", "not-a-totp") * 4:
            async with account_store.edit(req, "customer") as doc:
                assert account.check_limited_totp(req, doc, code) is False
    asyncio.run(early_failures())
    second = other_worker.post("/api/v22/auth/login", json=login_body).json()["challenge_id"]
    assert first != second
    for challenge in (first, second):
        bad = client.post("/api/v22/auth/2fa/login", json={"challenge_id": challenge, "code": "BAD-RECOVERY"})
        assert bad.status_code == 401
    assert get_doc(setup)["mfa_failures"]["level"] == 1
    locked = other_worker.post("/api/v22/auth/login", json=login_body)
    assert locked.status_code == 429
    assert 3590 <= int(locked.headers["retry-after"]) <= 3600
    locked_code = client.post("/api/v22/auth/2fa/login", json={"challenge_id": second, "code": recovery})
    assert locked_code.status_code == 429
    assert account.digest(recovery) in get_doc(setup)["recovery_hashes"]
    alter_doc(setup, lambda doc: doc["mfa_failures"].update(blocked_until=time.time() - 1))
    resumed = other_worker.post("/api/v22/auth/2fa/login", json={"challenge_id": second, "code": recovery})
    assert resumed.status_code == 200
    assert account.digest(recovery) not in get_doc(setup)["recovery_hashes"]


@pytest.mark.parametrize("password", [
    "abcdefghij", "ABCDEFGHIJ", "Abcdefghij!", "Abcdefghij1", "Ab1!short", "A1!" + "a" * 254,
])
def test_password_policy_rejects_each_missing_rule_without_echoing_password(password):
    with pytest.raises(HTTPException) as caught:
        validate_new_password(password)
    assert caught.value.status_code == 422
    assert password not in caught.value.detail


@pytest.mark.parametrize("password", ["Good-password-123!", "Aa1!" + "🙂" * 6, "Aa1 " + "a" * 6])
def test_password_policy_preserves_existing_symbol_semantics_and_unicode_length(password):
    assert all(password_rules(password).values())
    assert validate_new_password(password) == password


@pytest.mark.parametrize("path,body", [
    ("/api/v22/auth/register", {"display_name": "Member", "email": "new@example.test",
                              "password": "abcdefghij", "confirm_password": "abcdefghij", "terms_accepted": True}),
    ("/api/v22/auth/reset-password", {"token": "x" * 40, "password": "abcdefghij", "confirm_password": "abcdefghij"}),
    ("/api/v22/account/password", {"current_password": PASSWORD, "new_password": "abcdefghij", "confirm_password": "abcdefghij"}),
    ("/api/v22/auth/change-password", {"current_password": PASSWORD, "new_password": "abcdefghij"}),
])
def test_all_new_password_endpoints_use_the_same_policy(setup, path, body):
    client = setup.runtime()
    before = client.app.state.v22_commercial["state"]["users"][0]["password"]
    response = client.post(path, headers=headers(), json=body)
    assert response.status_code == 422
    assert "büyük harf" in response.json()["detail"]
    assert "abcdefghij" not in response.text
    assert client.app.state.v22_commercial["state"]["users"][0]["password"] == before
    assert not setup.mail.called


def test_registration_existing_email_has_same_public_envelope_and_unlinked_status(setup):
    client = setup.runtime()
    body = {"display_name": "Member", "password": PASSWORD, "confirm_password": PASSWORD, "terms_accepted": True}
    start = time.perf_counter()
    duplicate = client.post("/api/v22/auth/register", json={**body, "email": setup.users[0]["email"]})
    duplicate_elapsed = time.perf_counter() - start
    notice = setup.mail.call_args.kwargs
    start = time.perf_counter()
    new = client.post("/api/v22/auth/register", json={**body, "email": "new@example.test"})
    new_elapsed = time.perf_counter() - start
    assert duplicate.status_code == new.status_code == 200
    assert duplicate.json().keys() == new.json().keys()
    assert duplicate.json()["message"] == new.json()["message"]
    assert duplicate.json()["user"].keys() == new.json()["user"].keys()
    assert duplicate.json()["user"]["email_verified"] is new.json()["user"]["email_verified"] is False
    assert duplicate.json()["user"]["id"] != "customer"
    assert len(client.app.state.v22_commercial["state"]["users"]) == 4
    assert "zaten bir hesabın var" in notice["title"]
    assert "şifreni sıfırla" in notice["title"]
    assert notice["information_only"] is True
    assert notice["action_url"].endswith("/login")
    for result in (duplicate, new):
        status = client.get("/api/v22/auth/verification-status", params={"token": result.json()["verification_status_token"]})
        assert status.status_code == 200
        assert status.json() == {"verified": False}
    assert min(duplicate_elapsed, new_elapsed) >= auth.REGISTRATION_RESPONSE_SECONDS * 0.95
    assert abs(duplicate_elapsed - new_elapsed) < 0.3


def test_login_negative_cases_have_same_error_and_real_scrypt_work(setup):
    client = setup.runtime()
    inactive = client.app.state.v22_commercial["state"]["users"][2]
    inactive["active"] = False
    outcomes, durations = [], []
    with patch.object(auth, "verify_password", wraps=auth.verify_password) as verification:
        for email, password in (("missing@example.test", PASSWORD), (inactive["email"], PASSWORD),
                                (setup.users[0]["email"], "Wrong-password-123!")):
            start = time.perf_counter()
            result = client.post("/api/v22/auth/login", json={"email": email, "password": password})
            durations.append(time.perf_counter() - start)
            outcomes.append((result.status_code, result.json()))
            assert "set-cookie" not in result.headers
        assert verification.call_count == 3
        assert verification.call_args_list[0].args[1] is auth.DUMMY_PASSWORD_RECORD
    assert outcomes[0] == outcomes[1] == outcomes[2] == (401, {"detail": "E-posta veya parola hatalı"})
    assert min(durations) >= auth.AUTH_FAILURE_RESPONSE_SECONDS * 0.95
    assert max(durations) - min(durations) < 0.3


@pytest.mark.parametrize("operation", ["password", "legacy-password", "enable", "disable"])
@pytest.mark.parametrize("browser", [False, True])
def test_sensitive_changes_renew_current_session_only_and_preserve_expiry(setup, operation, browser):
    import pyotp
    client, worker = setup.runtime(), setup.runtime()
    current = headers()
    if operation == "disable":
        enrollment = client.post("/api/v22/account/2fa/setup", headers=current, json={"current_password": PASSWORD})
        enabled = client.post("/api/v22/account/2fa/enable", headers=current,
                              json={"code": pyotp.TOTP(enrollment.json()["secret"]).now()})
        assert enabled.status_code == 200
        current = {"Authorization": "Bearer " + enabled.json()["token"]}
        code = enabled.json()["recovery_codes"][0]
    original_token = current["Authorization"].removeprefix("Bearer ")
    original = verify_token(original_token, SECRET)
    stale = headers(version=original["ver"])
    unobserved = headers(version=original["ver"])
    foreign = headers("other")
    assert worker.get("/api/v22/account/overview", headers=stale).status_code == 200
    assert worker.get("/api/v22/account/overview", headers=foreign).status_code == 200
    if browser:
        client.cookies.set(auth.SESSION_COOKIE_NAME, original_token, domain="account.example.test", path="/api")
        current = {}
    if operation in {"password", "legacy-password"}:
        path = "/api/v22/account/password" if operation == "password" else "/api/v22/auth/change-password"
        response = client.post(path, headers=current, json={
            "current_password": PASSWORD, "new_password": PASSWORD + "new", "confirm_password": PASSWORD + "new"})
    elif operation == "enable":
        enrollment = client.post("/api/v22/account/2fa/setup", headers=current, json={"current_password": PASSWORD})
        response = client.post("/api/v22/account/2fa/enable", headers=current,
                               json={"code": pyotp.TOTP(enrollment.json()["secret"]).now()})
    else:
        response = client.post("/api/v22/account/2fa/disable", headers=current,
                               json={"current_password": PASSWORD, "totp_code": code})
    assert response.status_code == 200, response.text
    assert response.json()["reauthenticate"] is False
    if browser:
        assert response.json()["token"].startswith(auth.BROWSER_SESSION_MARKER_PREFIX)
        renewed_token = client.cookies.get(auth.SESSION_COOKIE_NAME)
        renewed_headers = {}
        assert "httponly" in response.headers["set-cookie"].lower()
    else:
        renewed_token = response.json()["token"]
        renewed_headers = {"Authorization": "Bearer " + renewed_token}
    renewed = verify_token(renewed_token, SECRET)
    assert (renewed["jti"], renewed["iat"], renewed["exp"]) == (original["jti"], original["iat"], original["exp"])
    assert renewed["ver"] == original["ver"] + 1
    assert client.get("/api/v22/account/overview", headers=renewed_headers).status_code == 200
    assert worker.get("/api/v22/account/overview", headers=stale).status_code == 401
    assert worker.get("/api/v22/account/overview", headers=unobserved).status_code == 401
    assert worker.get("/api/v22/account/overview", headers=foreign).status_code == 200
    assert client.app.state.v22_commercial["state"]["users"][2]["auth_version"] == 1
    assert_status(401, auth.validate_authoritative_session(client.app, "customer", original["ver"]))


def test_password_reset_revokes_all_target_sessions_but_no_other_account(setup):
    client, worker = setup.runtime(), setup.runtime()
    current, second, foreign = headers(), headers(), headers("other")
    for header in (current, second, foreign):
        assert worker.get("/api/v22/account/overview", headers=header).status_code == 200
    client.cookies.set(auth.SESSION_COOKIE_NAME, current["Authorization"].removeprefix("Bearer "))
    forgot = client.post("/api/v22/auth/forgot-password", json={"email": setup.users[0]["email"]})
    assert forgot.status_code == 200
    token = parse_qs(urlsplit(setup.mail.call_args.kwargs["action_url"]).query)["token"][0]
    result = client.post("/api/v22/auth/reset-password", json={
        "token": token, "password": PASSWORD + "new", "confirm_password": PASSWORD + "new"})
    assert result.status_code == 200
    assert "max-age=0" in result.headers["set-cookie"].lower()
    assert worker.get("/api/v22/account/overview", headers=current).status_code == 401
    assert worker.get("/api/v22/account/overview", headers=second).status_code == 401
    assert worker.get("/api/v22/account/overview", headers=foreign).status_code == 200


def test_password_change_never_replaces_an_unrelated_accounts_cookie(setup):
    client = setup.runtime()
    foreign = headers("other")
    foreign_token = foreign["Authorization"].removeprefix("Bearer ")
    client.cookies.set(auth.SESSION_COOKIE_NAME, foreign_token, domain="account.example.test", path="/api")
    response = client.post("/api/v22/account/password", headers=headers(), json={
        "current_password": PASSWORD, "new_password": PASSWORD + "new", "confirm_password": PASSWORD + "new"})
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert client.cookies.get(auth.SESSION_COOKIE_NAME) == foreign_token
    assert client.get("/api/v22/account/overview").json()["user"]["id"] == "other"
    assert client.get("/api/v22/account/overview", headers={"Authorization": "Bearer " + response.json()["token"]}).json()["user"]["id"] == "customer"


def test_unverified_login_stays_existing_verification_gate_not_private_access(setup):
    setup.users[0]["email_verified"] = False
    client = setup.runtime()
    login = client.post("/api/v22/auth/login", json={"email": setup.users[0]["email"], "password": PASSWORD})
    assert login.status_code == 200
    assert login.json()["user"]["email_verified"] is False
    token = {"Authorization": "Bearer " + login.json()["token"]}
    assert client.get("/api/v22/account/overview", headers=token).status_code == 403
    assert client.post("/api/v22/account/verification/resend", headers=token, json={}).status_code == 200


def test_unverified_owner_keeps_the_existing_owner_exception(setup):
    setup.users[1]["email_verified"] = False
    client = setup.runtime()
    login = client.post("/api/v22/auth/login", json={"email": setup.users[1]["email"], "password": PASSWORD})
    assert login.status_code == 200
    assert login.json()["user"]["email_verified"] is False
    token = {"Authorization": "Bearer " + login.json()["token"]}
    assert client.get("/api/v22/account/overview", headers=token).status_code == 200
    assert client.get("/api/v22/admin/accounts", headers=token).status_code == 200


@pytest.mark.parametrize("mode,host_key,host_value", [
    ("production", "", ""), ("development", "NODE_ENV", "production"),
    ("test", "VERCEL_ENV", "production"), ("test", "RENDER", "true"),
    ("development", "VERCEL", "1"), ("test", "DYNO", "web.1"), ("", "", ""),
])
def test_production_and_unknown_environment_refuse_exposed_tokens(monkeypatch, mode, host_key, host_value):
    for key in ("PROTREBOT_ENVIRONMENT", "ENVIRONMENT", "APP_ENV", "NODE_ENV", "VERCEL_ENV",
                "RAILWAY_ENVIRONMENT_NAME", "RENDER", "VERCEL", "DYNO"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PROTREBOT_EXPOSE_DEV_TOKENS", "true")
    monkeypatch.setenv("PROTREBOT_ENVIRONMENT", mode)
    if host_key:
        monkeypatch.setenv(host_key, host_value)
    with pytest.raises(RuntimeError, match="forbidden"):
        validate_auth_security_configuration()
    assert_status(503, asyncio.to_thread(auth.dev_tokens_exposed))


def test_production_startup_refuses_exposure_before_http_client_creation(monkeypatch):
    from app import main
    monkeypatch.setenv("PROTREBOT_ENVIRONMENT", "production")
    monkeypatch.setenv("PROTREBOT_EXPOSE_DEV_TOKENS", "true")
    async def start():
        async with main.lifespan(SimpleNamespace(state=SimpleNamespace())):
            pytest.fail("Unsafe startup was allowed")
    with patch.object(main, "build_http_client") as http, pytest.raises(RuntimeError, match="forbidden"):
        asyncio.run(start())
    http.assert_not_called()


@pytest.mark.parametrize("mode", ["development", "test"])
def test_explicit_local_development_can_still_expose_test_tokens(monkeypatch, mode):
    for key in ("ENVIRONMENT", "APP_ENV", "NODE_ENV", "VERCEL_ENV", "RAILWAY_ENVIRONMENT_NAME", "RENDER", "VERCEL", "DYNO"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PROTREBOT_EXPOSE_DEV_TOKENS", "true")
    monkeypatch.setenv("PROTREBOT_ENVIRONMENT", mode)
    validate_auth_security_configuration()
    assert auth.dev_tokens_exposed() is True


@pytest.mark.parametrize("path,body", [
    ("/api/v22/auth/forgot-password", {"email": "member@example.test"}),
    ("/api/v22/auth/register", {"email": "new@example.test", "display_name": "Member", "password": PASSWORD,
                              "confirm_password": PASSWORD, "terms_accepted": True}),
])
def test_production_request_time_guard_returns_no_token_and_sends_no_mail(setup, monkeypatch, path, body):
    monkeypatch.setenv("PROTREBOT_EXPOSE_DEV_TOKENS", "true")
    monkeypatch.setenv("PROTREBOT_ENVIRONMENT", "production")
    response = setup.runtime().post(path, json=body)
    assert response.status_code == 503
    assert "development_reset_token" not in response.text
    assert "development_verification_token" not in response.text
    setup.mail.assert_not_called()
