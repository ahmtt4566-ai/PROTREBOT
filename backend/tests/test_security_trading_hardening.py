import asyncio
import copy
import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import exchange_connections as vault
from app import v25_execution as live


def request_for(state, *, user="alice", token="alice-session", role="OWNER", method="POST"):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            v25_execution=state, db_pool=SimpleNamespace(execute=AsyncMock()),
            http=None,
            exchange_vault={"ready": True},
            v22_commercial={"state": {}},
        )),
        state=SimpleNamespace(member={"id": user, "role": role, "auth_version": 1}),
        headers={"x-protrebot-session": token} if token else {},
        method=method,
    )


def owned_state():
    state = live.initial_state()
    request = request_for(state)
    state["live_session_authorization"].update(
        user_id="alice", session_id=vault.session_id(request), auth_version=1,
        auth_expires_at_epoch=time.time() + 3600,
    )
    return state


MUTATIONS = [
    "v25_connect", "v25_policy", "v25_policy_ack", "v25_web_consent",
    "v25_revoke_web_consent", "v25_order_test", "v25_risk_preview",
    "v25_arm", "v25_disarm", "v25_recovery_check", "v25_order",
    "v25_auto_start", "v25_auto_stop", "v25_close", "v25_emergency",
    "v25_emergency_clear", "v25_adopt_external_position_preview", "v25_adopt_external_position",
]


@pytest.mark.parametrize("role", ["OWNER", "CUSTOMER"])
@pytest.mark.parametrize("route", MUTATIONS)
def test_other_member_cannot_mutate_shared_controller(route, role):
    import inspect

    # Keep the inventory aligned with actual handlers, including external adoption.
    handler = getattr(live, route)
    state = owned_state()
    before = copy.deepcopy(state)
    request = request_for(state, user="bob", token="bob-session", role=role)
    args = [request] + [None for name in inspect.signature(handler).parameters if name != "request"]
    with patch.object(live, "authenticated_user", return_value=request.state.member), \
         patch.object(live, "subscription_for_user", return_value={"master_trade_access": True}):
        with pytest.raises(HTTPException) as error:
            asyncio.run(handler(*args))
    assert error.value.status_code == 403
    assert state == before


@pytest.mark.parametrize("route", ["v25_status", "v25_history", "v25_mtf_history", "v25_market_candles"])
def test_sensitive_reads_deny_other_session(route):
    request = request_for(owned_state(), user="alice", token="rotated-session", method="GET")
    with pytest.raises(HTTPException) as error:
        asyncio.run(getattr(live, route)(request))
    assert error.value.status_code == 403


def test_idle_claim_is_reserved_before_connect_await():
    state = live.initial_state()
    first = request_for(state)
    live.execution_owner(first)
    live.execution_owner(first)
    with pytest.raises(HTTPException):
        live.execution_owner(request_for(state, user="bob", token="bob"))
    assert state["live_session_authorization"]["user_id"] == "alice"
    assert state["live_session_authorization"]["auth_version"] == 1
    assert live.sanitized_state(state)["live_session_authorization"]["auth_version"] == 1


def test_missing_session_and_unattributed_recovery_fail_closed():
    with pytest.raises(HTTPException) as error:
        live.execution_owner(request_for(live.initial_state(), token=""))
    assert error.value.status_code == 401
    state = live.initial_state()
    state["plans"]["recovered"] = {"symbol": "BTCUSDT"}
    with pytest.raises(HTTPException) as error:
        live.execution_owner(request_for(state))
    assert error.value.status_code == 403


def test_owner_account_proof_is_checked_and_safety_stop_needs_no_key():
    state = owned_state()
    state["live_session_authorization"].update(fingerprint="SHA256:abc", trading_account_id="BINANCE:LIVE:1")
    request = request_for(state)
    with patch.object(live, "live_credentials_status", return_value=("key", "secret", "SHA256:abc")), \
         patch.object(live, "session_account_identity", return_value="BINANCE:LIVE:2"):
        with pytest.raises(HTTPException):
            live.execution_owner(request)
    state["auto"]["enabled"] = True
    with patch.object(live, "live_credentials_status", return_value=("", "", None)), \
         patch.object(live, "persist_state"), patch.object(live, "public_status", return_value={}):
        asyncio.run(live.v25_auto_stop(request))
    assert state["auto"]["enabled"] is False


def test_revocation_retains_controller_ownership():
    state = owned_state()
    authorization = dict(state["live_session_authorization"])
    with patch.object(live, "persist_state"), patch.object(live, "public_status", return_value={}):
        asyncio.run(live.v25_revoke_web_consent(
            request_for(state), live.Confirmation(confirmation="24 SAATLİK CONSENTİ KALDIR"),
        ))
    assert state["live_session_authorization"] == authorization


def test_direct_helpers_and_exchange_runtime_cannot_bypass_ownership():
    state = owned_state()
    state["auto"]["enabled"] = True
    request = request_for(state, user="bob", token="bob")
    before = copy.deepcopy(state)
    for call in (
        lambda: live.public_status(request.app, request),
        lambda: asyncio.run(live.connect_read_only_for_request(request.app, request, actor="alice")),
        lambda: asyncio.run(live.execute_live_order(request.app, None, source="MANUAL", request=request)),
    ):
        with pytest.raises(HTTPException):
            call()
    vault._lock_runtime(request.app, "LIVE", request)
    assert state == before
    vault._lock_runtime(request.app, "LIVE", request_for(state))
    assert state["auto"]["enabled"] is False


def binance_check(payload, *, status=200, mode="LIVE"):
    paths = []
    check_credentials = vault.test_binance_credentials

    def response(request):
        paths.append((request.url.host, request.url.path))
        if request.url.path == "/sapi/v1/account/apiRestrictions":
            assert request.headers["X-MBX-APIKEY"] == "api-key-safe"
            assert "signature=" in str(request.url)
            return httpx.Response(status, json=payload)
        if request.url.path == "/fapi/v3/account":
            return httpx.Response(200, json={"positions": [], "totalWalletBalance": "100"})
        if request.url.path == "/fapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": False})
        raise AssertionError(request.url)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
            with patch.object(vault, "_server_time_offset", new=AsyncMock(return_value=0)):
                return await check_credentials(http, mode, "api-key-safe", "secret-safe")

    return run, paths


@pytest.mark.parametrize("payload,status", [
    ({"enableWithdrawals": True}, 200), ({}, 200),
    ({"enableWithdrawals": "false"}, 200), ({"enableWithdrawals": 0}, 200),
    ({"enableWithdrawals": None}, 200), ([], 200), ({"msg": "denied"}, 403),
])
def test_live_withdrawal_enabled_or_unproven_is_rejected(payload, status):
    run, paths = binance_check(payload, status=status)
    with pytest.raises(vault.VaultError):
        asyncio.run(run())
    assert paths == [("api.binance.com", "/sapi/v1/account/apiRestrictions")]


def test_live_restrictions_checked_at_spot_host_before_futures_account():
    run, paths = binance_check({"enableWithdrawals": False})
    account = asyncio.run(run())
    assert vault.withdrawal_permissions_safe("LIVE", account)
    assert paths == [
        ("api.binance.com", "/sapi/v1/account/apiRestrictions"),
        ("fapi.binance.com", "/fapi/v3/account"),
        ("fapi.binance.com", "/fapi/v1/positionSide/dual"),
    ]


def test_testnet_explicitly_reports_no_real_permission_validation():
    run, paths = binance_check({}, mode="TESTNET")
    account = asyncio.run(run())
    assert account["api_permissions"] == {
        "withdrawal_check": "NOT_APPLICABLE_TESTNET", "enableWithdrawals": None,
    }
    assert all(host == "demo-fapi.binance.com" for host, _ in paths)


def test_legacy_live_credentials_need_reactivation_in_request_and_background_paths():
    state = owned_state()
    request = request_for(state)
    sid = vault.session_id(request)
    key = (sid, "LIVE")
    fingerprint = vault.key_fingerprint("api-key-safe")
    with patch.dict(vault._SESSION_CACHE, {key: ("api-key-safe", "secret-safe")}, clear=True), \
         patch.dict(vault._SESSION_META, {key: {"active": True, "user_id": "alice", "account": {}}}, clear=True), \
         patch.object(vault, "ensure_exchange_vault", new=AsyncMock(return_value=True)), \
         patch.object(vault, "ensure_schema", new=AsyncMock()):
        row = {
            "mode": "LIVE", "active": True, "fingerprint": fingerprint, "account_summary": {},
        }
        request.app.state.db_pool.fetchrow = AsyncMock(side_effect=lambda query, *args: (
            {"auth_version": 1, "security": {"active": True, "email_verified": True}}
            if "commercial_auth_users" in query else row
        ))
        assert vault.session_credentials(request, "LIVE") == ("", "")
        assert vault.session_credentials(request, "LIVE", active_only=False) == ("api-key-safe", "secret-safe")
        assert asyncio.run(vault.session_credentials_for_identity(
            request.app, sid, "alice", "LIVE", fingerprint, force_refresh=True, auth_version=1,
            auth_expires_at_epoch=time.time() + 3600,
        )) == ("", "")


def test_activation_revalidates_saved_live_key_and_fails_closed():
    state = owned_state()
    state["auto"]["enabled"] = True
    request = request_for(state)
    key = (vault.session_id(request), "LIVE")
    with patch.dict(vault._SESSION_CACHE, {key: ("api-key-safe", "secret-safe")}, clear=True), \
         patch.dict(vault._SESSION_META, {key: {"active": True}}, clear=True), \
         patch.object(vault, "_require_ready", new=AsyncMock(return_value=request.app.state.db_pool)), \
         patch.object(vault, "ensure_session_cache", new=AsyncMock()), \
         patch.object(vault, "test_binance_credentials", new=AsyncMock(side_effect=vault.VaultError("withdrawal denied"))):
        with pytest.raises(HTTPException):
            asyncio.run(vault.exchange_connection_activate(
                request, vault.ConnectionActionRequest(
                    mode="LIVE", confirmation="CANLI SALT OKUNUR BAĞLANTIYI AÇ",
                ),
            ))
        assert vault._SESSION_META[key]["active"] is False
        assert state["auto"]["enabled"] is False
        assert "active = FALSE" in request.app.state.db_pool.execute.await_args.args[0]


def test_verified_db_credentials_remain_usable_by_background_owner():
    request = request_for(owned_state())
    fingerprint = vault.key_fingerprint("api-key-safe")
    row = {
        "mode": "LIVE", "active": True, "fingerprint": fingerprint,
        "encrypted_payload": b"ciphertext",
        "account_summary": json.dumps({
            "api_permissions": {"withdrawal_check": "VERIFIED", "enableWithdrawals": False},
        }),
    }
    request.app.state.db_pool.fetchrow = AsyncMock(side_effect=lambda query, *args: (
        {"auth_version": 1, "security": {"active": True, "email_verified": True}}
        if "commercial_auth_users" in query else row
    ))
    with patch.object(vault, "ensure_exchange_vault", new=AsyncMock(return_value=True)), \
         patch.object(vault, "ensure_schema", new=AsyncMock()), \
         patch.object(vault, "decrypt_credentials", return_value=("api-key-safe", "secret-safe")):
        assert asyncio.run(vault.session_credentials_for_identity(
            request.app, vault.session_id(request), "alice", "LIVE", fingerprint, force_refresh=True, auth_version=1,
            auth_expires_at_epoch=time.time() + 3600,
        )) == ("api-key-safe", "secret-safe")


@pytest.mark.parametrize("headers", [
    {"x-protrebot-session": "cookie-session:alice"},
    {"authorization": "Bearer cookie-session:alice"},
    {"x-protrebot-session": "COOKIE-SESSION:alice"},
    {"authorization": "Bearer COOKIE-SESSION:alice"},
    {"authorization": "Bearer "},
    {},
])
def test_cookie_session_binding_uses_private_cookie_not_public_marker(headers):
    request = request_for(owned_state())
    request.headers = headers
    request.cookies = {"protrebot_session": "signed-private-session"}
    assert vault.session_id(request) == hashlib.sha256(b"signed-private-session").hexdigest()


@pytest.mark.parametrize("headers", [
    {"x-protrebot-session": "cookie-session:alice"},
    {"authorization": "Bearer cookie-session:alice"},
    {"x-protrebot-session": "COOKIE-SESSION:alice"},
    {"authorization": "Bearer COOKIE-SESSION:alice"},
    {"authorization": "Bearer "},
    {},
])
def test_browser_marker_alone_proves_no_exchange_session(headers):
    request = request_for(owned_state())
    request.headers = headers
    request.cookies = {}
    assert vault.session_id(request) == ""


@pytest.mark.parametrize("headers,identity", [
    ({"x-protrebot-session": "native-session"}, "native-session"),
    ({"authorization": "Bearer native-session"}, "Bearer native-session"),
])
def test_native_exchange_session_binding_preserves_existing_identity(headers, identity):
    request = request_for(owned_state())
    request.headers = headers
    request.cookies = {"protrebot_session": "unrelated-cookie"}
    assert vault.session_id(request) == hashlib.sha256(identity.encode()).hexdigest()


@pytest.mark.parametrize("controller_owner", ["alice", "bob"])
def test_account_erasure_cache_hook_only_clears_target_user_and_locks_own_controller(controller_owner):
    state = owned_state()
    state["live_session_authorization"]["user_id"] = controller_owner
    state["auto"]["enabled"] = True
    state["live_auto_trade"] = True
    state["real_trading_locked"] = False
    state["plans"]["retained-financial-plan"] = {"realized_pnl": 12}
    request = request_for(state)
    before = copy.deepcopy(state)
    credentials = {
        ("alice-live", "LIVE"): ("alice-key", "secret"),
        ("alice-demo", "TESTNET"): ("alice-demo-key", "secret"),
        ("bob-live", "LIVE"): ("bob-key", "secret"),
        ("orphan-known-session", "LIVE"): ("orphan-key", "secret"),
    }
    metadata = {
        ("alice-live", "LIVE"): {"user_id": "alice"},
        ("alice-demo", "TESTNET"): {"user_id": "alice"},
        ("bob-live", "LIVE"): {"user_id": "bob"},
    }
    with patch.dict(vault._SESSION_CACHE, credentials, clear=True), \
         patch.dict(vault._SESSION_META, metadata, clear=True):
        vault.clear_session_vault_for_user_cache(
            request.app, "alice", session_ids=("orphan-known-session", "bob-live"),
        )
        assert vault._SESSION_CACHE == {("bob-live", "LIVE"): ("bob-key", "secret")}
        assert vault._SESSION_META == {("bob-live", "LIVE"): {"user_id": "bob"}}
    if controller_owner == "alice":
        assert state["auto"]["enabled"] is False
        assert state["live_auto_trade"] is False
        assert state["real_trading_locked"] is True
        assert state["plans"] == before["plans"]
    else:
        assert state == before


class SharedAuthDatabase:
    def __init__(self):
        self.version = 1
        self.active = True
        self.deleted = False
        self.unavailable = False
        self.reads = 0

    async def fetchrow(self, query, *args):
        assert "commercial_auth_users" in query
        self.reads += 1
        if self.unavailable:
            raise RuntimeError("shared database unavailable")
        if self.deleted:
            return None
        return {"auth_version": self.version, "security": {
            "active": self.active, "email_verified": True, "role": "OWNER",
        }}


@pytest.mark.parametrize("revocation", ["logout", "erasure", "inactive", "unavailable", "missing_version"])
def test_other_worker_cached_live_credentials_cannot_survive_shared_revocation(revocation):
    database = SharedAuthDatabase()
    worker_a = request_for(owned_state())
    worker_b = request_for(owned_state())
    for worker in (worker_a, worker_b):
        worker.state.member["active"] = True
        worker.app.state.db_pool = database
        worker.app.state.v25_execution["auto"]["enabled"] = True
        worker.app.state.v25_execution["live_auto_trade"] = True
        worker.app.state.v25_execution["real_trading_locked"] = False
    sid = vault.session_id(worker_b)
    fingerprint = vault.key_fingerprint("api-key-safe")
    key = (sid, "LIVE")
    metadata = {"user_id": "alice", "active": True, "account": {
        "api_permissions": {"withdrawal_check": "VERIFIED", "enableWithdrawals": False},
    }}
    with patch.dict(vault._SESSION_CACHE, {key: ("api-key-safe", "secret-safe")}, clear=True), \
         patch.dict(vault._SESSION_META, {key: metadata}, clear=True):
        assert asyncio.run(vault.session_credentials_for_identity(
            worker_a.app, sid, "alice", "LIVE", fingerprint, auth_version=1,
            auth_expires_at_epoch=time.time() + 3600,
        )) == ("api-key-safe", "secret-safe")
        if revocation == "logout":
            database.version = 2
        elif revocation == "erasure":
            database.deleted = True
        elif revocation == "inactive":
            database.active = False
        elif revocation == "unavailable":
            database.unavailable = True
        version = None if revocation == "missing_version" else 1
        assert asyncio.run(vault.session_credentials_for_identity(
            worker_b.app, sid, "alice", "LIVE", fingerprint, auth_version=version,
            auth_expires_at_epoch=time.time() + 3600,
        )) == ("", "")
        assert key not in vault._SESSION_CACHE
    assert worker_b.state.member["auth_version"] == 1
    assert worker_b.state.member["active"] is True
    assert worker_b.app.state.v25_execution["real_trading_locked"] is True
    assert worker_b.app.state.v25_execution["auto"]["enabled"] is False


@pytest.mark.parametrize("method,path", [
    ("GET", "/fapi/v3/account"), ("POST", "/fapi/v1/order"),
    ("POST", "/fapi/v1/algoOrder"), ("DELETE", "/fapi/v1/algoOrder"),
    ("POST", "/fapi/v1/listenKey"),
])
def test_retained_background_client_rechecks_shared_authority_before_private_use(method, path):
    database = SharedAuthDatabase()
    database.version = 2
    state = owned_state()
    state["auto"]["enabled"] = True
    request = request_for(state)
    request.app.state.db_pool = database
    request.app.state.http = AsyncMock()
    client = live.client_for(request.app, credentials=("api-key-safe", "secret-safe"))
    with pytest.raises(live.LiveExchangeError) as error:
        if path.endswith("listenKey"):
            asyncio.run(client.api_key_request(method, path))
        else:
            asyncio.run(client.signed(method, path))
    assert error.value.http_status == 423
    request.app.state.http.request.assert_not_awaited()
    assert database.reads >= 1
    assert state["auto"]["enabled"] is False
    assert state["real_trading_locked"] is True


def test_signed_user_expiry_caps_background_grant_without_adopting_db_version():
    from app.commercial_core import issue_token, verify_token

    state = live.initial_state()
    request = request_for(state)
    secret = b"offline-test-signing-key"
    token = issue_token("alice", "OWNER", secret, ttl_seconds=120, token_version=1)
    expected_expiry = verify_token(token, secret, expected_kind="USER")["exp"]
    request.app.state.v22_commercial["secret"] = secret
    request.headers = {"x-protrebot-session": token}
    with patch.object(live, "live_auto_start_gate", return_value=(True, "")), \
         patch.object(live, "live_credentials_status", return_value=("api-key-safe", "secret-safe", "SHA256:abc")), \
         patch.object(live, "persist_state"), patch.object(live, "public_status", return_value={}):
        asyncio.run(live.v25_auto_start(request, live.Confirmation(confirmation="CANLI OTOMATİK")))
    assert state["live_session_authorization"]["auth_version"] == 1
    assert state["live_session_authorization"]["auth_expires_at_epoch"] == expected_expiry
    assert state["auto"]["session_until"] == expected_expiry
    assert state["auto_authorization"]["expires_at_epoch"] == expected_expiry
    assert state["auto_authorization"]["auth_expires_at_epoch"] == expected_expiry
    assert live.sanitized_state(state)["live_session_authorization"]["auth_expires_at_epoch"] == expected_expiry


def test_expiry_capture_prefers_original_authoritative_user_token():
    from app.commercial_core import issue_token, verify_token

    request = request_for(owned_state())
    secret = b"offline-test-signing-key"
    token = issue_token("alice", "OWNER", secret, ttl_seconds=120, token_version=1)
    request.app.state.v22_commercial["secret"] = secret
    request.state.v22_authoritative_token = token
    request.headers = {"x-protrebot-session": "cookie-session:alice"}
    request.cookies = {}
    assert live.authenticated_live_expiry(request) == verify_token(token, secret, expected_kind="USER")["exp"]


@pytest.mark.parametrize("expires", [None, 0, float("nan"), float("inf")])
def test_background_user_token_expiry_missing_or_invalid_is_not_refreshed(expires):
    database = SharedAuthDatabase()
    state = owned_state()
    state["live_session_authorization"]["auth_expires_at_epoch"] = expires
    request = request_for(state)
    request.app.state.db_pool = database
    assert asyncio.run(live.background_authorization_active(request.app, state)) is False
    assert database.reads == 0
    assert state["real_trading_locked"] is True


def test_erasure_metadata_sync_failure_blocks_canonical_version_even_if_user_row_active():
    database = SharedAuthDatabase()
    request = request_for(owned_state())
    request.app.state.db_pool = database
    request.app.state.v22_commercial["auth_storage_sync_failed"] = True
    assert asyncio.run(live.background_authorization_active(
        request.app, request.app.state.v25_execution,
    )) is False
    assert database.reads == 0
    assert request.app.state.v25_execution["real_trading_locked"] is True


@pytest.mark.parametrize("valid", [False, True])
def test_save_only_writes_validated_permissions(valid):
    request = request_for(live.initial_state())
    body = vault.SaveCredentialsRequest(
        mode="LIVE", api_key="api-key-safe", secret_key="secret-safe",
        confirmation="CANLI KASAYA KAYDET",
    )
    run, _ = binance_check({"enableWithdrawals": not valid})

    async def check(*args):
        return await run()

    with patch.object(vault, "_require_ready", new=AsyncMock(return_value=request.app.state.db_pool)), \
         patch.object(vault, "ensure_schema", new=AsyncMock()), \
         patch.object(vault, "test_binance_credentials", side_effect=check), \
         patch.object(vault, "encrypt_credentials", return_value=b"ciphertext"), \
         patch.dict(vault._SESSION_CACHE, clear=True), patch.dict(vault._SESSION_META, clear=True):
        if valid:
            asyncio.run(vault.exchange_connection_save(request, body))
            args = request.app.state.db_pool.execute.await_args.args
            assert json.loads(args[-1])["api_permissions"]["withdrawal_check"] == "VERIFIED"
            assert vault.session_credentials(request, "LIVE") == ("api-key-safe", "secret-safe")
        else:
            with pytest.raises(HTTPException):
                asyncio.run(vault.exchange_connection_save(request, body))
            request.app.state.db_pool.execute.assert_not_awaited()
