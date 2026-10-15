"""Canonical auth/entitlement middleware with offline quota and account stores."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from app import chart_ai_api, free_trial, main, member_entitlements
from app.chart_ai_models import NOTICE
from app.chart_ai_store import POLICY_VERSION, ChartAiStore, user_hash
from fastapi import Request
from test_binance_manual_entitlements import decide, pending
from test_chart_ai_store import FakePool
from test_free_trial import setup as canonical_setup
from test_free_trial import trial_api as canonical_trial_api
from test_moderator_role import headers

setup = canonical_setup
trial_api = canonical_trial_api
USAGE = "/api/chart-ai/usage"
ACK = "/api/chart-ai/acknowledgement"
BODY = {"policy_version": POLICY_VERSION, "accepted": True}


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    monkeypatch.setenv("CHART_AI_ENABLED", "true")
    monkeypatch.setenv("FREE_TRIAL_ENABLED", "true")
    monkeypatch.setenv("BINANCE_MANUAL_ACCESS_ENABLED", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def chart_api(trial_api, monkeypatch):
    client, authority = trial_api
    authority.now = datetime.now(timezone.utc)
    ledger_pool = FakePool()
    ledger_pool.now = authority.now
    ledger = ChartAiStore(ledger_pool)
    client.app.include_router(chart_ai_api.router)
    monkeypatch.setattr(chart_ai_api, "store", lambda _request: ledger)
    return client, authority, ledger_pool


def start_trial(authority):
    asyncio.run(free_trial.start(authority, {"id": "customer", "auth_version": 1}))


def subscribe(client, *, status="ACTIVE", expired=False, grace=None, plan="MASTER_MODE"):
    now = datetime.now(timezone.utc)
    row = {"user_id": "customer", "plan": plan, "status": status,
           "current_period_end": (now + timedelta(days=-1 if expired else 30)).isoformat()}
    if grace is not None:
        row["grace_until"] = (now + timedelta(days=grace)).isoformat()
    client.app.state.v22_commercial["state"]["subscriptions"] = [row]


@pytest.mark.parametrize("value", [None, "", "false", "0"])
def test_feature_is_default_off_and_never_reads_chart_storage(chart_api, monkeypatch, value):
    client, _, pool = chart_api
    if value is None:
        monkeypatch.delenv("CHART_AI_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CHART_AI_ENABLED", value)
    for method, path in (("GET", USAGE), ("POST", ACK)):
        response = client.request(method, path, headers=headers("owner", "OWNER"), json=BODY if method == "POST" else None)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "feature_disabled"
    assert pool.queries == []
    main.hydrate_authenticated_user_state.assert_not_awaited()


def test_invalid_feature_setting_is_explicit_and_not_logged_raw(chart_api, monkeypatch, caplog):
    client, _, pool = chart_api
    monkeypatch.setenv("CHART_AI_ENABLED", "private-invalid-setting")
    response = client.get(USAGE, headers=headers("owner", "OWNER"))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "configuration_invalid"
    assert "private-invalid-setting" not in caplog.text
    assert pool.queries == []


def test_active_trial_has_three_separate_rights_and_exact_warning(chart_api):
    client, authority, pool = chart_api
    start_trial(authority)
    response = client.get(USAGE, headers=headers("customer", "CUSTOMER"))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["eligible"] and payload["access_type"] == "trial"
    assert payload["quota"] == {"limit": 3, "used": 0, "reserved": 0, "remaining": 3, "unlimited": False}
    assert payload["trial_expires_at"] == payload["period_end"] and payload["resets_at"] is None
    assert payload["acknowledgement"] == {
        "policy_version": POLICY_VERSION, "accepted_at": None, "required": True, "text": NOTICE,
    }
    assert "Resmin sitemizde saklanmaz; analiz için yapay zeka sağlayıcımıza gönderilir." in NOTICE
    assert "Bu analiz yatırım tavsiyesi değildir." in NOTICE
    assert payload["analysis_enabled"] is False and payload["availability"]["available"] is False
    assert response.headers["cache-control"] == "no-store"
    assert pool.state.requests == {} and pool.state.budgets == {}
    main.hydrate_authenticated_user_state.assert_not_awaited()


def test_acknowledgement_is_persistent_idempotent_user_scoped_and_does_not_spend(chart_api):
    client, authority, pool = chart_api
    start_trial(authority)
    auth = headers("customer", "CUSTOMER")
    first = client.post(ACK, headers=auth, json=BODY)
    assert first.status_code == 200, first.text
    assert first.json()["quota_consumed"] is False
    assert first.json()["acknowledgement"]["required"] is False
    assert first.json()["acknowledgement"]["text"] == NOTICE
    pool.now += timedelta(seconds=1)
    again = client.post(ACK, headers=auth, json=BODY)
    assert again.json() == first.json()
    usage = client.get(USAGE, headers=auth).json()
    assert usage["acknowledgement"] == first.json()["acknowledgement"]
    assert usage["quota"]["remaining"] == 3
    other = client.get(USAGE, headers=headers("owner", "OWNER")).json()
    assert other["acknowledgement"]["accepted_at"] is None
    assert len(pool.state.acknowledgements) == 1
    assert (user_hash("customer"), POLICY_VERSION) in pool.state.acknowledgements
    assert pool.state.requests == {} and pool.state.budgets == {}


@pytest.mark.parametrize("change,status", [
    ({"accepted": False}, 422),
    ({"accepted": "true"}, 422),
    ({"accepted": 1}, 422),
    ({"policy_version": "old-version"}, 403),
    ({"policy_version": "x" * 65}, 422),
    ({"user_id": "owner"}, 422),
    ({"isPremium": True}, 422),
    ({"role": "OWNER"}, 422),
])
def test_invalid_or_forged_acknowledgement_never_writes(chart_api, change, status):
    client, authority, pool = chart_api
    start_trial(authority)
    response = client.post(ACK, headers=headers("customer", "CUSTOMER"), json={**BODY, **change})
    assert response.status_code == status, response.text
    assert pool.state.acknowledgements == {} and pool.state.requests == {}


def test_paid_master_mode_uses_twenty_rights_and_utc_monday_period(chart_api):
    client, _, pool = chart_api
    subscribe(client)
    payload = client.get(USAGE, headers=headers("customer", "CUSTOMER")).json()
    assert payload["access_type"] == "subscriber"
    assert payload["quota"]["remaining"] == payload["quota"]["limit"] == 20
    start = datetime.fromisoformat(payload["period_start"].replace("Z", "+00:00"))
    assert start.weekday() == 0 and start.hour == start.minute == start.second == 0
    assert start.utcoffset() == timedelta(0)
    assert payload["resets_at"] == payload["period_end"] and payload["trial_expires_at"] is None
    assert payload["server_now"] == pool.now.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("grace,status", [(1, 200), (-1, 403)])
def test_paid_past_due_uses_existing_canonical_grace_rules(chart_api, grace, status):
    client, _, pool = chart_api
    subscribe(client, status="PAST_DUE", expired=True, grace=grace)
    response = client.get(USAGE, headers=headers("customer", "CUSTOMER"))
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()["quota"]["remaining"] == 20
    else:
        assert pool.queries == []


def test_owner_is_unlimited_without_subscription_and_actual_role_is_authoritative(chart_api):
    client, _, pool = chart_api
    response = client.get(USAGE, headers=headers("owner", "OWNER"))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["access_type"] == "owner" and payload["quota"]["unlimited"]
    assert payload["quota"]["remaining"] is None and payload["quota"]["limit"] is None
    assert payload["period_start"] is None and payload["period_end"] is None
    assert not pool.state.requests


def test_claimed_owner_role_and_query_fields_cannot_promote_trial(chart_api):
    client, authority, _ = chart_api
    start_trial(authority)
    response = client.get(USAGE + "?user_id=owner&isPremium=true&plan=MASTER_MODE",
                          headers=headers("customer", "OWNER"))
    assert response.status_code == 200, response.text
    assert response.json()["access_type"] == "trial" and response.json()["quota"]["limit"] == 3


@pytest.mark.parametrize("status", ["ACTIVE", "CANCELLED", "EXPIRED", "UNPAID"])
def test_expired_or_inactive_subscription_cannot_read_or_acknowledge(chart_api, status):
    client, _, pool = chart_api
    subscribe(client, status=status, expired=True)
    for method, path in (("GET", USAGE), ("POST", ACK)):
        response = client.request(method, path, headers=headers("customer", "CUSTOMER"),
                                  json=BODY if method == "POST" else None)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "access_denied"
    assert pool.queries == []


def test_expired_trial_cannot_read_or_acknowledge(chart_api):
    client, authority, pool = chart_api
    start_trial(authority)
    authority.now += timedelta(days=7)
    for method, path in (("GET", USAGE), ("POST", ACK)):
        response = client.request(method, path, headers=headers("customer", "CUSTOMER"),
                                  json=BODY if method == "POST" else None)
        assert response.status_code == 403, response.text
    assert pool.queries == []


@pytest.mark.parametrize("missing_schema", [False, True])
def test_unverifiable_trial_entitlement_fails_closed(chart_api, missing_schema):
    client, authority, pool = chart_api
    authority.trial_failure = not missing_schema
    authority.trial_schema_missing = missing_schema
    for method, path in (("GET", USAGE), ("POST", ACK)):
        response = client.request(method, path, headers=headers("customer", "CUSTOMER"),
                                  json=BODY if method == "POST" else None)
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "entitlement_unavailable"
    assert pool.queries == []


@pytest.mark.parametrize("change", [
    {"user_id": "another-user"}, {"auth_version": 2}, {"error": True},
])
def test_owner_cannot_bypass_bad_entitlement_proof(chart_api, monkeypatch, change):
    client, _, pool = chart_api
    original_prepare = member_entitlements.prepare

    async def prepare(request, user):
        proof = replace(await original_prepare(request, user), **change)
        request.state.member_entitlement_proof = proof
        return proof

    monkeypatch.setattr(member_entitlements, "prepare", prepare)
    response = client.get(USAGE, headers=headers("owner", "OWNER"))
    assert response.status_code == 503, response.text
    assert pool.queries == []


def test_missing_proof_is_not_replaced_with_cached_premium(chart_api, monkeypatch):
    client, _, pool = chart_api
    original_prepare = member_entitlements.prepare

    async def prepare(request, user):
        proof = await original_prepare(request, user)
        request.state.member_entitlement_proof = None
        return proof

    monkeypatch.setattr(member_entitlements, "prepare", prepare)
    response = client.get(USAGE, headers=headers("owner", "OWNER"))
    assert response.status_code == 503
    assert pool.queries == []


def test_authentication_verification_and_revocation_remain_required(chart_api):
    client, authority, pool = chart_api
    assert client.get(USAGE).status_code == 401
    authority.canonical_users["customer"]["security"]["email_verified"] = False
    assert client.get(USAGE, headers=headers("customer", "CUSTOMER")).status_code == 403
    authority.canonical_users["customer"]["security"]["email_verified"] = True
    authority.canonical_users["customer"]["auth_version"] = 2
    assert client.get(USAGE, headers=headers("customer", "CUSTOMER")).status_code == 401
    assert pool.queries == []


def test_missing_quota_schema_is_explicit_without_fabricated_balance_or_raw_logs(chart_api, caplog):
    client, _, pool = chart_api
    pool.fail_on = "INSERT INTO chart_ai_quota_periods"
    response = client.get(USAGE, headers=headers("owner", "OWNER"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert "quota" not in response.json()
    assert "private@example.test" not in caplog.text
    assert "secret-provider-detail" not in caplog.text


def test_general_assistant_lock_is_unchanged_while_chart_metadata_is_allowed(chart_api):
    client, authority, _ = chart_api
    start_trial(authority)
    calls = []

    async def chat():
        calls.append(True)
        return {"reply": "must not reach"}

    client.app.add_api_route("/api/assistant/chat", chat, methods=["POST"])
    assert client.get(USAGE, headers=headers("customer", "CUSTOMER")).status_code == 200
    denied = client.post("/api/assistant/chat", headers=headers("customer", "CUSTOMER"), json={})
    assert denied.status_code == 403
    assert denied.json()["code"] == "ASSISTANT_MASTER_MODE_REQUIRED" and not calls


def test_chart_metadata_never_hydrates_credentials_or_drains_trading_tasks(chart_api):
    client, authority, _ = chart_api
    start_trial(authority)
    pending = {"customer": [object()]}
    client.app.state._binance_demo_persistence_tasks = pending
    assert client.get(USAGE, headers=headers("customer", "CUSTOMER")).status_code == 200
    assert client.post(ACK, headers=headers("customer", "CUSTOMER"), json=BODY).status_code == 200
    assert "customer" in pending and len(pending["customer"]) == 1
    main.hydrate_authenticated_user_state.assert_not_awaited()


def test_nonchart_paths_preserve_existing_hydration(chart_api):
    client, _, _ = chart_api

    async def handler():
        return {"status": "fixture"}

    client.app.add_api_route("/api/v22/chart-test-existing", handler, methods=["GET"])
    assert client.get("/api/v22/chart-test-existing", headers=headers("owner", "OWNER")).status_code == 200
    main.hydrate_authenticated_user_state.assert_awaited_once()


def test_chart_endpoints_are_registered_and_not_public(chart_api):
    client, _, _ = chart_api
    paths = main.app.openapi()["paths"]
    assert {path for path in paths if path.startswith("/api/chart-ai/")} == {
        USAGE, ACK, "/api/chart-ai/analyze", "/api/chart-ai/requests/{request_key}",
    }
    assert USAGE not in main.MEMBER_PUBLIC_PATHS and ACK not in main.MEMBER_PUBLIC_PATHS
    assert client.post("/api/chart-ai/analyze", headers=headers("owner", "OWNER"), json={}).status_code == 422
    assert client.get("/api/chart-ai/requests/not-a-key", headers=headers("owner", "OWNER")).status_code == 422


def test_store_factory_uses_only_existing_server_pool():
    from fastapi import FastAPI
    application = FastAPI()
    pool = FakePool()
    application.state.db_pool = pool
    request = Request({"type": "http", "app": application})
    assert chart_ai_api.store(request).pool is pool


def test_canonical_approved_manual_master_mode_is_subscriber(chart_api):
    client, authority, _ = chart_api

    async def approve():
        row = await pending(authority, uid="customer")
        await decide(authority, row)

    asyncio.run(approve())
    response = client.get(USAGE, headers=headers("customer", "CUSTOMER"))
    assert response.status_code == 200, response.text
    assert response.json()["access_type"] == "subscriber"
    assert response.json()["quota"]["remaining"] == 20


def test_unsubscribed_account_cannot_use_premium_hint_as_entitlement(chart_api, monkeypatch):
    client, _, pool = chart_api
    monkeypatch.setattr(member_entitlements, "access", lambda _subscription, _user: {"isPremium": True})
    response = client.get(USAGE, headers=headers("customer", "CUSTOMER"))
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "access_denied" and pool.queries == []


@pytest.mark.parametrize("change", [{"trial_start": None}, {"trial_end": "not-a-date"},
                                   {"trial_end": "2030-01-01T00:00:00Z"}])
def test_malformed_trial_period_fails_closed(chart_api, monkeypatch, change):
    client, authority, pool = chart_api
    start_trial(authority)
    original_prepare = member_entitlements.prepare

    async def prepare(request, user):
        proof = await original_prepare(request, user)
        proof = replace(proof, subscription={**proof.subscription, **change})
        request.state.member_entitlement_proof = proof
        return proof

    monkeypatch.setattr(member_entitlements, "prepare", prepare)
    response = client.get(USAGE, headers=headers("customer", "CUSTOMER"))
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "entitlement_invalid" and pool.queries == []


@pytest.mark.parametrize("path", [USAGE, ACK])
def test_missing_chart_store_pool_is_explicit(chart_api, monkeypatch, path):
    client, _, _ = chart_api
    monkeypatch.setattr(chart_ai_api, "store", lambda _request: ChartAiStore(None))
    response = (client.get(path, headers=headers("owner", "OWNER")) if path == USAGE
                else client.post(path, headers=headers("owner", "OWNER"), json=BODY))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert response.json()["quota_consumed"] is False


def test_missing_acknowledgement_schema_never_claims_acceptance(chart_api):
    client, _, pool = chart_api
    pool.fail_on = "INSERT INTO chart_ai_acknowledgements"
    response = client.post(ACK, headers=headers("owner", "OWNER"), json=BODY)
    assert response.status_code == 503 and response.json()["error"]["code"] == "storage_unavailable"
    assert pool.state.acknowledgements == {}


def test_untrusted_browser_origin_cannot_write_acknowledgement(chart_api):
    client, _, pool = chart_api
    response = client.post(ACK, headers={**headers("owner", "OWNER"), "X-Requested-With": "XMLHttpRequest",
                                      "Origin": "https://untrusted.example.test"}, json=BODY)
    assert response.status_code == 403 and pool.queries == []


def test_usage_reports_existing_used_and_reserved_counts(chart_api):
    client, authority, pool = chart_api
    start_trial(authority)
    auth = headers("customer", "CUSTOMER")
    assert client.get(USAGE, headers=auth).status_code == 200
    period = next(iter(pool.state.periods.values()))
    period.update(used=1, reserved=1)
    payload = client.get(USAGE, headers=auth).json()
    assert payload["quota"] == {"limit": 3, "used": 1, "reserved": 1, "remaining": 1, "unlimited": False}
