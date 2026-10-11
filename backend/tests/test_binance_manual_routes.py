import copy
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from app import binance_manual_routes as routes
from app.subscription_core import entitlement_snapshot
from test_binance_manual_payments import NOW, Connection
from test_moderator_role import headers
from test_moderator_role import setup as canonical_setup

setup = canonical_setup


@pytest.fixture
def api(setup, monkeypatch):
    client, pool = setup
    conn = Connection()
    conn.users = {"customer", "moderator", "owner"}

    @asynccontextmanager
    async def acquire():
        yield conn
    pool.acquire = acquire
    client.app.include_router(routes.router)
    monkeypatch.setenv("BINANCE_MANUAL_PAY_ID", "987654321")
    return client, conn


H = headers("customer", "CUSTOMER")
BASE = "/api/v22/payments/binance-manual"


def test_configuration_comes_from_server_and_disabled_method_has_no_pay_id(api, monkeypatch):
    client, _ = api
    response = client.get(BASE + "/config", headers=H)
    assert response.status_code == 200
    assert response.json() == {"enabled": True, "amount": "119.90", "currency": "USDT", "pay_id": "987654321"}
    assert response.headers["cache-control"] == "no-store"
    monkeypatch.delenv("BINANCE_MANUAL_PAY_ID")
    assert client.get(BASE + "/config", headers=H).json() == {"enabled": False}
    assert client.post(BASE + "/requests", headers=H).status_code == 503


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/config", None), ("GET", "/requests/current", None), ("POST", "/requests", None),
    ("POST", "/requests/" + "a" * 32 + "/submit", {"transaction_number": "123"}),
])
def test_every_payment_endpoint_requires_authentication(api, method, path, body):
    client, conn = api
    response = client.request(method, BASE + path, json=body)
    assert response.status_code == 401 and conn.rows == {}


def test_create_submit_and_reload_are_user_scoped_and_never_grant_access(api):
    client, conn = api
    state = client.app.state.v22_commercial["state"]
    before = copy.deepcopy(state["subscriptions"])
    item = client.post(BASE + "/requests", headers=H)
    assert item.status_code == 200, item.text
    payload = item.json()
    assert payload["amount"] == "119.90" and payload["status"] == "created"
    assert set(payload) == {"id", "reference_code", "amount", "currency", "method", "status", "transaction_number", "expires_at"}
    assert client.get(BASE + "/requests/current", headers=headers("moderator", "MODERATOR")).json() == {"request": None, "period": None}
    path = BASE + "/requests/" + payload["id"] + "/submit"
    assert client.post(path, headers=headers("moderator", "MODERATOR"), json={"transaction_number": "123"}).status_code == 404
    submitted = client.post(path, headers=H, json={"transaction_number": "123"})
    assert submitted.status_code == 200 and submitted.json()["status"] == "pending"
    assert client.get(BASE + "/requests/current", headers=H).json()["request"] == submitted.json()
    assert client.post(path, headers=H, json={"transaction_number": "123", "amount": "1"}).status_code == 422
    assert state["subscriptions"] == before
    assert not entitlement_snapshot(state, "customer")["master_trade_access"]
    assert len(conn.rows) == 1


def test_current_read_expires_late_unsubmitted_request(api):
    client, conn = api
    assert client.post(BASE + "/requests", headers=H).status_code == 200
    conn.now += timedelta(hours=48)
    response = client.get(BASE + "/requests/current", headers=H)
    assert response.status_code == 200 and response.json()["request"]["status"] == "expired"


def test_missing_schema_or_database_surfaces_error_without_access(api):
    client, conn = api
    conn.fail = True
    response = client.post(BASE + "/requests", headers=H)
    assert response.status_code == 503
    assert "private financial" not in response.text and conn.rows == {}


def test_paid_stripe_blocks_but_trial_is_allowed_and_unchanged(api):
    client, conn = api
    conn.subscriptions = [{"user_id": "customer", "plan": "MASTER_MODE", "status": "ACTIVE",
                           "stripe_subscription_id": "sub_paid", "current_period_end": NOW + timedelta(days=30)}]
    assert client.post(BASE + "/requests", headers=H).status_code == 409
    conn.subscriptions[0].update(plan="TRIAL", status="TRIALING")
    before = copy.deepcopy(conn.subscriptions)
    assert client.post(BASE + "/requests", headers=H).status_code == 200
    assert conn.subscriptions == before


def test_router_has_no_approval_rejection_or_webhook_entry_points():
    assert {route.path for route in routes.router.routes} == {
        BASE + "/config", BASE + "/requests/current", BASE + "/requests", BASE + "/requests/{request_id}/submit",
    }
