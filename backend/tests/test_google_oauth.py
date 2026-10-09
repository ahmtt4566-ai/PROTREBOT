"""Offline Google OAuth fixtures; no real provider credentials or requests."""
import asyncio
import base64
import copy
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import google_oauth as google
from app import v22_commercial as auth
from app import main
from app.commercial_core import default_commercial_state, hash_password, verify_token

ENV = {"GOOGLE_CLIENT_ID": "offline-google-client", "GOOGLE_CLIENT_SECRET": "offline-google-client-secret"}
SECRET = b"offline-commercial-session-secret-for-oauth-tests"
HEADERS = {"Origin": "https://kaistrade.com", "X-Requested-With": "XMLHttpRequest"}
IDENTITY = {"issuer": google.ISSUER, "subject": "offline-google-subject",
            "email": "google@example.test", "display_name": "Google Fixture"}
PERSIST = auth.persist_v22_commercial


class Pool:
    def __init__(self, state):
        self.snapshot = copy.deepcopy(state)
        self.attempts = {}
        self.identities = {}
        self.security = {}
        self.account_settings = {}
        self.auth_failures = {}
        self.lock = asyncio.Lock()
        self.queries = []
        for user in state["users"]:
            self.security[user["id"]] = {"auth_version": user.get("auth_version", 1), "security": auth.auth_security(user)}

    @asynccontextmanager
    async def acquire(self):
        async with self.lock:
            yield self

    @asynccontextmanager
    async def transaction(self):
        backup = copy.deepcopy((self.snapshot, self.attempts, self.identities, self.security, self.account_settings, self.auth_failures))
        try:
            yield
        except BaseException:
            self.snapshot, self.attempts, self.identities, self.security, self.account_settings, self.auth_failures = backup
            raise

    async def execute(self, query, *args):
        self.queries.append(query)
        if "INSERT INTO commercial_auth_failures" in query:
            self.auth_failures[args[0]] = json.loads(args[1])
            return "OK"
        if "INSERT INTO commercial_account_settings" in query:
            self.account_settings[args[0]] = json.loads(args[1])
            return "OK"
        if "commercial_account_tokens" in query:
            return "OK"
        if query.startswith("DELETE FROM commercial_google_attempts"):
            self.attempts = {key: value for key, value in self.attempts.items() if value["expires_at"] > datetime.now(timezone.utc)}
        elif "INSERT INTO commercial_google_attempts" in query:
            self.attempts[args[0]] = dict(binding_hash=args[1], purpose=args[2], payload=args[3],
                                         expires_at=args[4], consumed_at=None)
        elif "INSERT INTO commercial_auth_users" in query:
            if args[0] not in self.security:
                self.security[args[0]] = {"auth_version": args[1], "security": json.loads(args[2])}
        elif "INSERT INTO commercial_google_identities" in query:
            key = args[:2]
            assert key not in self.identities
            assert not any(row["user_id"] == args[2] for row in self.identities.values())
            self.identities[key] = {"user_id": args[2], "user_payload": json.loads(args[3])}
        elif "INSERT INTO application_state_snapshots" in query:
            self.snapshot = json.loads(args[1])
        elif "pg_advisory_xact_lock" not in query and "CREATE TABLE" not in query:
            raise AssertionError("Unexpected fixture SQL")
        return "OK"

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "commercial_auth_failures" in query:
            row = self.auth_failures.get(args[0])
            return {"payload": copy.deepcopy(row)} if row else None
        if "commercial_account_settings" in query:
            doc = self.account_settings.get(args[0])
            return {"payload": copy.deepcopy(doc)} if doc else None
        if "commercial_account_tokens" in query:
            return None
        if "commercial_google_attempts" in query:
            row = self.attempts.get(args[0])
            if not row or row["binding_hash"] != args[1] or row["purpose"] != args[2] or row["consumed_at"] or row["expires_at"] <= datetime.now(timezone.utc):
                return None
            if query.lstrip().startswith("UPDATE"):
                row["consumed_at"] = datetime.now(timezone.utc)
            return {"payload": row["payload"]}
        if "commercial_auth_limits" in query:
            return {"attempts": 1}
        if "SELECT user_id FROM commercial_google_identities" in query:
            return copy.deepcopy(self.identities.get(args[:2]))
        if "SELECT identity.user_payload" in query:
            found = next((row for row in self.identities.values() if
                          (row["user_id"] == args[0] if "identity.user_id = $1" in query else row["user_payload"]["email"] == args[0])), None)
            if found is None or found["user_id"] not in self.security:
                return None
            return copy.deepcopy({**found, **self.security[found["user_id"]]})
        if "SELECT payload FROM application_state_snapshots" in query:
            return {"payload": copy.deepcopy(self.snapshot)}
        if "UPDATE commercial_auth_users" in query:
            row = self.security.get(args[0])
            if row is None:
                return None
            row["auth_version"] += 1 if "auth_version = auth_version +" in query else 0
            row["security"].update(json.loads(args[1]))
            return copy.deepcopy(row)
        if "SELECT auth_version, security FROM commercial_auth_users" in query:
            return copy.deepcopy(self.security.get(args[0]))
        raise AssertionError("Unexpected fixture SQL")

    async def fetch(self, query, *args):
        assert "commercial_google_identities" in query
        return [copy.deepcopy({**row, **self.security[row["user_id"]]})
                for row in self.identities.values() if row["user_id"] in self.security]


@pytest.fixture
def setup():
    state = default_commercial_state()
    pool = Pool(state)
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(google.router)
    app.middleware("http")(main.owner_preview_gate)
    app.state.db_pool = pool
    app.state.google_oauth_schema_ready = True
    app.state.v22_commercial = {"state": state, "secret": SECRET, "lock": asyncio.Lock(),
                                "storage_lock": asyncio.Lock(), "auth_baseline": {},
                                "storage_status": "POSTGRESQL_KALICI"}
    client = TestClient(app, base_url="https://kaistrade.com")
    async def persist(application):
        pool.snapshot = copy.deepcopy(application.state.v22_commercial["state"])
        return True
    with patch.dict("os.environ", ENV), patch.object(auth, "restore_demo_state_for_user", AsyncMock()), \
            patch.object(auth, "restore_v21_state_for_user", AsyncMock()), \
            patch.object(auth, "save_state"), patch.object(auth, "persist_v22_commercial", side_effect=persist), \
            patch("app.exchange_connections.clear_session_vault_for_request", AsyncMock()):
        yield client, pool, app
    client.close()


def begin(client, remember=False, origin="https://kaistrade.com"):
    response = client.post("/api/v22/auth/google/start", json={"remember": remember},
                           headers={**HEADERS, "Origin": origin})
    assert response.status_code == 200
    return parse_qs(urlsplit(response.json()["authorization_url"]).query)


def bound_user(pool, app, role="CUSTOMER"):
    user = {"id": "existing-google-member", "email": IDENTITY["email"], "display_name": "Original name",
            "role": role, "active": True, "email_verified": True, "auth_version": 4,
            "password": {}, "created_at": auth.now_iso()}
    app.state.v22_commercial["state"]["users"].append(copy.deepcopy(user))
    pool.snapshot["users"].append(copy.deepcopy(user))
    pool.security[user["id"]] = {"auth_version": 4, "security": auth.auth_security(user)}
    pool.identities[(google.ISSUER, IDENTITY["subject"])] = {"user_id": user["id"], "user_payload": copy.deepcopy(user)}
    return user


def callback(client, state):
    return client.get("/api/v22/auth/google/callback", params={"state": state, "code": "offline-code"}, follow_redirects=False)


def test_start_creates_random_state_pkce_nonce_and_secure_binding(setup):
    client, pool, app = setup
    parameters = begin(client, remember=True)
    assert parameters["client_id"] == [ENV["GOOGLE_CLIENT_ID"]]
    assert parameters["scope"] == ["openid email profile"]
    assert parameters["redirect_uri"] == ["https://kaistrade.com" + google.CALLBACK_PATH]
    assert parameters["code_challenge_method"] == ["S256"]
    assert len(parameters["state"][0]) >= 43 and len(parameters["nonce"][0]) >= 43
    row = pool.attempts[google.digest(parameters["state"][0])]
    request = SimpleNamespace(app=app)
    data = json.loads(google.cipher(request).decrypt(row["payload"].encode()))
    challenge = base64.urlsafe_b64encode(__import__("hashlib").sha256(data["verifier"].encode()).digest()).decode().rstrip("=")
    assert parameters["code_challenge"] == [challenge]
    assert data["nonce_hash"] == google.digest(parameters["nonce"][0])
    assert data["verifier"] not in row["payload"]
    assert 290 < (row["expires_at"] - datetime.now(timezone.utc)).total_seconds() <= 300
    cookie = next(cookie for cookie in client.cookies.jar if cookie.name == google.BINDING_COOKIE)
    assert cookie.secure and cookie.path == google.COOKIE_PATH and cookie.has_nonstandard_attr("HttpOnly")
    assert ENV["GOOGLE_CLIENT_SECRET"] not in str(parameters)
    other = begin(client)
    assert other["state"] != parameters["state"]


@pytest.mark.parametrize("origin", ["https://evil.example", "http://kaistrade.com", "https://kaistrade.com.evil.example"])
def test_start_rejects_untrusted_origin(setup, origin):
    client, _, _ = setup
    response = client.post("/api/v22/auth/google/start", json={}, headers={**HEADERS, "Origin": origin})
    assert response.status_code == 403


def test_start_requires_browser_csrf_header_and_configured_storage(setup):
    client, _, app = setup
    assert client.post("/api/v22/auth/google/start", json={}, headers={"Origin": HEADERS["Origin"]}).status_code == 403
    app.state.db_pool = None
    assert client.post("/api/v22/auth/google/start", json={}, headers=HEADERS).status_code == 503
    with patch.dict("os.environ", {"GOOGLE_CLIENT_SECRET": ""}):
        assert client.post("/api/v22/auth/google/start", json={}, headers=HEADERS).status_code == 503


@pytest.mark.parametrize("remember", [False, True])
@pytest.mark.parametrize("role", ["CUSTOMER", "OWNER"])
def test_bound_identity_uses_existing_user_role_version_cookie_and_session(setup, role, remember):
    client, pool, app = setup
    user = bound_user(pool, app, role)
    parameters = begin(client, remember=remember)
    with patch.object(google, "exchange_identity", AsyncMock(return_value=IDENTITY)):
        response = callback(client, parameters["state"][0])
    assert "google_login=success" in response.headers["location"]
    assert "google_remember=" + ("1" if remember else "0") in response.headers["location"]
    cookie = client.cookies.get("protrebot_session")
    claims = verify_token(cookie, SECRET, expected_kind="USER")
    assert claims["sub"] == user["id"] and claims["role"] == role and claims["ver"] == 4
    assert claims["exp"] - claims["iat"] == (auth.REMEMBER_SESSION_SECONDS if remember else auth.STANDARD_SESSION_SECONDS)
    assert "HttpOnly" in response.headers["set-cookie"] and "Secure" in response.headers["set-cookie"]
    session = client.get("/api/v22/session")
    assert session.status_code == 200 and session.json()["user"]["id"] == user["id"]
    assert len(pool.identities) == 1 and len(pool.snapshot["users"]) == 1
    assert "offline-code" not in response.headers["location"]
    logout = client.post("/api/v22/auth/logout", headers=HEADERS)
    assert logout.status_code == 200 and not client.cookies.get("protrebot_session")
    assert pool.security[user["id"]]["auth_version"] == 5
    assert client.get("/api/v22/session", headers={"Authorization": "Bearer " + cookie}).status_code == 401


@pytest.mark.parametrize("case", ["invalid", "expired", "binding", "replay"])
def test_state_failures_prevent_exchange(setup, case):
    client, pool, app = setup
    bound_user(pool, app)
    state = begin(client)["state"][0]
    if case == "invalid":
        state = "invalid-state"
    elif case == "expired":
        pool.attempts[google.digest(state)]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif case == "binding":
        client.cookies.clear()
    with patch.object(google, "exchange_identity", AsyncMock(return_value=IDENTITY)) as exchange:
        if case == "replay":
            assert "success" in callback(client, state).headers["location"]
            exchange.reset_mock()
        response = callback(client, state)
        assert "google_login=invalid_attempt" in response.headers["location"]
        exchange.assert_not_called()
        assert "protrebot_session" not in response.headers.get("set-cookie", "")


def test_existing_email_is_not_automatically_merged(setup):
    client, pool, app = setup
    user = bound_user(pool, app)
    pool.identities.clear()
    state = begin(client)["state"][0]
    with patch.object(google, "exchange_identity", AsyncMock(return_value=IDENTITY)):
        response = callback(client, state)
    assert "account_link_required" in response.headers["location"]
    assert not pool.identities and not client.cookies.get("protrebot_session")
    assert pool.snapshot["users"][0]["id"] == user["id"]


def test_new_user_requires_terms_then_becomes_customer_free_without_password(setup):
    client, pool, app = setup
    state = begin(client)["state"][0]
    with patch.object(google, "exchange_identity", AsyncMock(return_value=IDENTITY)):
        response = callback(client, state)
    assert "google_login=consent" in response.headers["location"] and not pool.snapshot["users"]
    pending = client.get("/api/v22/auth/google/pending")
    assert pending.json() == {"email": IDENTITY["email"], "display_name": IDENTITY["display_name"]}
    assert pending.headers["cache-control"] == "no-store"
    assert client.post("/api/v22/auth/google/complete", json={"terms_accepted": False}, headers=HEADERS).status_code == 422
    result = client.post("/api/v22/auth/google/complete", json={"terms_accepted": True}, headers=HEADERS)
    assert result.status_code == 200
    user = pool.snapshot["users"][0]
    assert user["role"] == "CUSTOMER" and user["email_verified"] is True and user["password"] == {}
    assert pool.snapshot["subscriptions"][0]["plan"] == "FREE" and pool.snapshot["acceptances"][0]["terms_accepted"]
    assert result.json()["token"].startswith("cookie-session:")
    assert client.get("/api/v22/session").status_code == 200
    assert client.post("/api/v22/auth/google/complete", json={"terms_accepted": True}, headers=HEADERS).status_code == 400
    assert len(pool.snapshot["users"]) == 1


def test_concurrent_registration_between_consent_and_complete_does_not_merge(setup):
    client, pool, app = setup
    state = begin(client)["state"][0]
    with patch.object(google, "exchange_identity", AsyncMock(return_value=IDENTITY)):
        callback(client, state)
    user = bound_user(pool, app)
    pool.identities.clear()
    response = client.post("/api/v22/auth/google/complete", json={"terms_accepted": True}, headers=HEADERS)
    assert response.status_code == 409 and not client.cookies.get("protrebot_session")
    assert not pool.identities and pool.snapshot["users"][0]["id"] == user["id"]


def test_legacy_domain_returns_to_exact_legacy_callback_origin(setup):
    client, pool, app = setup
    bound_user(pool, app)
    client.base_url = "https://frontend-nu-two-18.vercel.app"
    state = begin(client, origin=str(client.base_url).rstrip("/"))
    assert state["redirect_uri"] == ["https://frontend-nu-two-18.vercel.app" + google.CALLBACK_PATH]
    with patch.object(google, "exchange_identity", AsyncMock(return_value=IDENTITY)):
        response = callback(client, state["state"][0])
    assert response.headers["location"].startswith("https://frontend-nu-two-18.vercel.app/?google_login=success")


def claims():
    return {"iss": google.ISSUER, "aud": ENV["GOOGLE_CLIENT_ID"], "sub": IDENTITY["subject"],
            "email": IDENTITY["email"], "email_verified": True, "nonce": "offline-nonce",
            "iat": int(time.time()), "exp": int(time.time()) + 300, "name": IDENTITY["display_name"]}


@pytest.mark.parametrize("change", [
    {"iss": "https://evil.example"}, {"aud": "other-client"}, {"azp": "other-client"},
    {"nonce": "other-nonce"}, {"email_verified": False}, {"email_verified": "true"},
    {"exp": int(time.time()) - 1}, {"exp": float("inf")}, {"sub": ""}, {"email": ""},
])
def test_invalid_oidc_claims_are_rejected(change):
    value = {**claims(), **change}
    with patch.object(google.id_token, "verify_oauth2_token", return_value=value):
        with pytest.raises(google.OAuthFailure, match="invalid_identity"):
            google.validate_identity("offline-token", ENV["GOOGLE_CLIENT_ID"], google.digest("offline-nonce"))


def test_signature_verification_failure_is_not_accepted():
    with patch.object(google.id_token, "verify_oauth2_token", side_effect=ValueError("offline-invalid-signature")):
        with pytest.raises(google.OAuthFailure, match="invalid_identity"):
            google.validate_identity("offline-token", ENV["GOOGLE_CLIENT_ID"], google.digest("offline-nonce"))


def test_valid_oidc_identity_is_verified_with_expected_client_audience():
    with patch.object(google.id_token, "verify_oauth2_token", return_value=claims()) as verify:
        assert google.validate_identity("offline-token", ENV["GOOGLE_CLIENT_ID"], google.digest("offline-nonce")) == IDENTITY
        assert verify.call_args.kwargs["audience"] == ENV["GOOGLE_CLIENT_ID"]


def test_password_login_is_unchanged_and_uses_existing_user_session(setup):
    client, pool, app = setup
    user = bound_user(pool, app)
    user["password"] = hash_password("OfflinePassword!123")
    pool.security[user["id"]]["security"]["password"] = user["password"]
    app.state.v22_commercial["state"]["users"][0]["password"] = user["password"]
    response = client.post("/api/v22/auth/login", json={"email": user["email"], "password": "OfflinePassword!123", "remember": False}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["token"] == "cookie-session:" + user["id"]
    assert verify_token(client.cookies.get("protrebot_session"), SECRET)["sub"] == user["id"]


def test_callback_query_is_removed_from_access_log_scope_and_errors_are_safe(setup, caplog):
    client, _, _ = setup
    response = callback(client, "offline-invalid-state")
    assert "offline-invalid-state" not in response.headers["location"]
    assert "offline-invalid-state" not in caplog.text and "offline-code" not in caplog.text
    request = SimpleNamespace(query_params=__import__("starlette.datastructures", fromlist=["QueryParams"]).QueryParams("code=offline-code&state=offline-state"),
                              state=SimpleNamespace(), scope={"query_string": b"code=offline-code&state=offline-state"})
    google.callback_query(request)
    assert request.scope["query_string"] == b"" and request.state.google_callback["code"] == "offline-code"


def test_real_rsa_signature_and_google_oidc_verification_offline():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from google.auth import crypt, jwt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "offline-google-fixture")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1)).sign(key, hashes.SHA256()))
    certs = json.dumps({"offline-key": cert.public_bytes(serialization.Encoding.PEM).decode()}).encode()
    signer = crypt.RSASigner.from_string(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), key_id="offline-key")
    token = jwt.encode(signer, claims()).decode()
    def certificates(url, **kwargs):
        assert url == "https://www.googleapis.com/oauth2/v1/certs" and kwargs["timeout"] == 10
        return SimpleNamespace(status=200, data=certs)
    with patch.object(google, "GoogleRequest", return_value=certificates):
        assert google.validate_identity(token, ENV["GOOGLE_CLIENT_ID"], google.digest("offline-nonce")) == IDENTITY
        pieces = token.split(".")
        pieces[2] = ("A" if pieces[2][0] != "A" else "B") + pieces[2][1:]
        with pytest.raises(google.OAuthFailure):
            google.validate_identity(".".join(pieces), ENV["GOOGLE_CLIENT_ID"], google.digest("offline-nonce"))


def test_new_google_account_survives_stale_worker_snapshot_write(setup):
    client, pool, app = setup
    state = begin(client)["state"][0]
    with patch.object(google, "exchange_identity", AsyncMock(return_value=IDENTITY)):
        callback(client, state)
    assert client.post("/api/v22/auth/google/complete", json={"terms_accepted": True}, headers=HEADERS).status_code == 200
    app.state.v22_commercial["state"] = default_commercial_state()
    asyncio.run(google.merge_registered_users(app, pool))
    restored = app.state.v22_commercial["state"]
    assert len(restored["users"]) == len(restored["profiles"]) == len(restored["subscriptions"]) == len(restored["acceptances"]) == 1
    assert "_registration" not in restored["users"][0]
    with patch.object(auth, "apply_erasure_tombstones", AsyncMock()), \
            patch.object(auth, "persist_auth_security", AsyncMock()), \
            patch.object(auth, "refresh_state_auth_security", AsyncMock()):
        assert asyncio.run(PERSIST(app)) is True
    assert pool.snapshot["users"][0]["id"] == restored["users"][0]["id"]
    assert pool.snapshot["subscriptions"][0]["plan"] == "FREE"


def test_exchange_uses_fixed_google_endpoint_verifier_and_backend_only_secret(setup):
    _, _, app = setup
    attempt = {"origin": HEADERS["Origin"], "verifier": "offline-verifier",
               "nonce_hash": google.digest("offline-nonce")}
    with patch.object(google.httpx, "AsyncClient") as factory, \
            patch.object(google, "validate_identity", return_value=IDENTITY) as validate:
        transport = factory.return_value.__aenter__.return_value
        transport.post = AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {"id_token": "offline-id-token"}))
        assert asyncio.run(google.exchange_identity(SimpleNamespace(app=app), "offline-code", attempt)) == IDENTITY
        factory.assert_called_once_with(timeout=15, follow_redirects=False, trust_env=False)
        transport.post.assert_awaited_once_with(google.TOKEN_URL, data={
            "grant_type": "authorization_code", "code": "offline-code",
            "client_id": ENV["GOOGLE_CLIENT_ID"], "client_secret": ENV["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": "https://kaistrade.com" + google.CALLBACK_PATH,
            "code_verifier": "offline-verifier",
        })
        validate.assert_called_once_with("offline-id-token", ENV["GOOGLE_CLIENT_ID"], attempt["nonce_hash"])


def test_provider_exchange_failure_never_exposes_sensitive_values(setup, caplog):
    _, _, app = setup
    with patch.object(google.httpx, "AsyncClient") as factory:
        transport = factory.return_value.__aenter__.return_value
        transport.post = AsyncMock(side_effect=google.httpx.ConnectError(ENV["GOOGLE_CLIENT_SECRET"]))
        with pytest.raises(google.OAuthFailure, match="^provider_error$") as error:
            asyncio.run(google.exchange_identity(SimpleNamespace(app=app), "offline-code", {
                "origin": HEADERS["Origin"], "verifier": "offline-verifier",
            }))
        assert ENV["GOOGLE_CLIENT_SECRET"] not in str(error.value)
        assert ENV["GOOGLE_CLIENT_SECRET"] not in caplog.text


def test_configured_commercial_initialization_creates_google_schema_in_dependency_order():
    pool = SimpleNamespace(execute=AsyncMock())
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool, v22_commercial={}))
    with patch.dict("os.environ", ENV), patch.object(auth, "ensure_erasure_schema", AsyncMock()):
        asyncio.run(auth.ensure_commercial_schema(app))
    queries = [call.args[0] for call in pool.execute.await_args_list]
    assert queries[-1] == google.SCHEMA
    assert "CREATE TABLE IF NOT EXISTS commercial_auth_users" in queries[1]
    assert app.state.google_oauth_schema_ready is True


def test_password_registration_and_google_callback_do_not_exhaust_two_connection_pool(setup):
    _, base, app = setup

    class Connection:
        def __init__(self, pool):
            self.pool = pool
            self.held = []

        @asynccontextmanager
        async def transaction(self):
            try:
                yield
            finally:
                for lock in self.held:
                    lock.release()

        async def execute(self, query, *args):
            if "pg_advisory_xact_lock" in query:
                lock = self.pool.advisory.setdefault(args[0], asyncio.Lock())
                await lock.acquire()
                self.held.append(lock)
                if self.pool.pause_first:
                    self.pool.pause_first = False
                    await asyncio.sleep(0.02)
            return await base.execute(query, *args)

        async def fetchrow(self, query, *args):
            return await base.fetchrow(query, *args)

    class SharedPool:
        def __init__(self):
            self.slots = asyncio.Semaphore(2)
            self.advisory = {}
            self.pause_first = True

        @asynccontextmanager
        async def acquire(self):
            async with self.slots:
                yield Connection(self)

        async def fetchrow(self, query, *args):
            async with self.acquire() as connection:
                return await connection.fetchrow(query, *args)

    async def run():
        app.state.db_pool = SharedPool()
        request = SimpleNamespace(app=app)
        async def registration():
            async with google.registration_guard(request, IDENTITY["email"]):
                assert await auth.enforce_auth_limit(
                    SimpleNamespace(app=app, client=SimpleNamespace(host="offline-client")), "register", IDENTITY["email"],
                ) is None
        await asyncio.wait_for(asyncio.gather(
            registration(), google.resolve_identity(request, IDENTITY, create=False),
        ), timeout=1)
    asyncio.run(run())


def test_password_registration_still_uses_gmail_verification_with_google_configured(setup):
    client, pool, app = setup
    with patch.object(auth, "gmail_configured", return_value=True), \
            patch.object(auth, "send_auth_email") as send, \
            patch.object(auth, "apply_erasure_tombstones", AsyncMock()):
        response = client.post("/api/v22/auth/register", headers=HEADERS, json={
            "email": "password-member@example.test", "display_name": "Password Member",
            "password": "OfflinePassword!123", "confirm_password": "OfflinePassword!123", "terms_accepted": True,
        })
    assert response.status_code == 200 and response.json()["email_verification_required"] is True
    assert len(pool.snapshot["users"]) == 1 and pool.snapshot["users"][0]["email_verified"] is False
    assert pool.snapshot["users"][0]["password"] and not pool.identities
    assert not client.cookies.get("protrebot_session")
    send.assert_called_once()
