"""Exact response-key contracts, recursive secret scans and no fabricated values."""
import copy
import re
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.moderator_customers import CustomerSummary, CustomerSubscription, CustomerPayments, CustomerPage
from test_moderator_customers import reads
from test_moderator_role import headers, setup

PROFILE_FIELDS = {
    "user_id", "email_masked", "role", "active", "created_at", "email_verified", "mfa_enabled",
}
SUBSCRIPTION_FIELDS = {"user_id", "plan", "subscription_status", "current_period_end", "cancel_at_period_end"}
PAYMENT_FIELDS = {"user_id", "payment_status", "last_failed_payment_at"}
PRIVATE_VALUES = {
    "email": "customer@example.test", "password": "private-password", "hash": "private-hash",
    "token": "private-token", "secret": "private-secret", "api_key": "private-key",
    "session": "private-session", "binance_connection": "private-exchange",
    "stripe_customer_id": "private-customer-reference", "stripe_subscription_id": "private-subscription-reference",
    "raw_provider": "private-provider-payload", "ip": "203.0.113.123", "name": "Private Customer Name",
    "preferences": "private-preferences", "positions": "private-trades",
}


def assert_safe(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in {"email_masked", "email_verified"}:
                assert not re.search(r"token|secret|api|key|password|hash|session|binance|stripe", key, re.I)
            assert key not in {"email", "name", "display_name", "ip", "preferences", "positions", "connections"}
            assert_safe(item)
    elif isinstance(value, list):
        for item in value:
            assert_safe(item)
    elif isinstance(value, str):
        assert not any(private in value for private in PRIVATE_VALUES.values())


@pytest.mark.parametrize("path,expected", [
    ("/api/mod/customers/customer", PROFILE_FIELDS),
    ("/api/mod/customers/customer/subscription", SUBSCRIPTION_FIELDS),
    ("/api/mod/customers/customer/payments", PAYMENT_FIELDS),
])
def test_exact_response_keys_exclude_private_fields_even_in_hostile_database_rows(reads, path, expected):
    client, pool = reads
    pool.users["customer"]["security"].update(PRIVATE_VALUES)
    pool.persisted_users["customer"].update(PRIVATE_VALUES)
    pool.settings["customer"] = {**PRIVATE_VALUES, "two_factor_enabled": True,
                                 "sessions": ["private-session"], "preferences": {"secret": "private-secret"}}
    pool.subscriptions["customer"].update(PRIVATE_VALUES)
    profile = pool.profile

    def hostile_profile(uid):
        return {**profile(uid), **{key: value for key, value in PRIVATE_VALUES.items() if key != "email"}}

    pool.profile = hostile_profile
    response = client.get(path, headers=headers("moderator", "MODERATOR"))
    assert response.status_code == 200
    assert set(response.json()) == expected
    assert_safe(response.json())
    assert not any(private in response.text for private in PRIVATE_VALUES.values())


def test_list_envelope_and_each_item_are_exact_allowlisted_shapes(reads):
    client, _ = reads
    response = client.get("/api/mod/customers", headers=headers("moderator", "MODERATOR"))
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"items", "total", "limit", "offset"}
    assert len(data["items"]) == 1
    assert set(data["items"][0]) == PROFILE_FIELDS
    assert_safe(data)
    assert data["items"][0]["email_masked"] == "c***@example.test"


@pytest.mark.parametrize("model,expected", [
    (CustomerSummary, PROFILE_FIELDS), (CustomerSubscription, SUBSCRIPTION_FIELDS),
    (CustomerPayments, PAYMENT_FIELDS), (CustomerPage, {"items", "total", "limit", "offset"}),
])
def test_response_model_schema_has_only_approved_keys_and_forbids_extras(model, expected):
    assert set(model.model_fields) == expected
    assert model.model_config["extra"] == "forbid"


@pytest.mark.parametrize("field,value", [
    ("user_id", "customer@example.test"), ("email_masked", "customer@example.test"),
    ("role", "OWNER"), ("active", "true"), ("mfa_enabled", "yes"),
])
def test_profile_dto_rejects_invalid_or_unmasked_allowed_values(field, value):
    payload = {
        "user_id": "customer", "email_masked": "c***@example.test", "role": "CUSTOMER",
        "active": True, "created_at": None, "email_verified": True, "mfa_enabled": False,
    }
    payload[field] = value
    with pytest.raises(ValidationError):
        CustomerSummary.model_validate(payload)


def test_no_registration_date_or_payment_history_is_fabricated(reads):
    client, pool = reads
    pool.persisted_users.clear()
    pool.subscriptions["customer"]["last_payment_status"] = "PAID"
    h = headers("moderator", "MODERATOR")
    assert client.get("/api/mod/customers/customer", headers=h).json()["created_at"] is None
    assert client.get("/api/mod/customers/customer/payments", headers=h).json()["last_failed_payment_at"] is None
    pool.subscriptions["customer"]["last_payment_status"] = None
    assert client.get("/api/mod/customers/customer/payments", headers=h).json() == {
        "user_id": "customer", "payment_status": "veri yok", "last_failed_payment_at": None,
    }


@pytest.mark.parametrize("field,value", [
    ("plan", "private-token"), ("subscription_status", "private-provider-payload"),
    ("last_payment_status", "private-key"),
])
def test_invalid_persisted_status_values_fail_closed_instead_of_being_reflected(reads, field, value):
    client, pool = reads
    pool.subscriptions["customer"][field] = value
    suffix = "payments" if field == "last_payment_status" else "subscription"
    response = client.get(f"/api/mod/customers/customer/{suffix}", headers=headers("moderator", "MODERATOR"))
    assert response.status_code == 503
    assert value not in response.text


@pytest.mark.parametrize("actor,role", [("owner", "OWNER"), ("moderator", "MODERATOR")])
@pytest.mark.parametrize("suffix", ["", "/subscription", "/payments"])
def test_caller_cannot_view_itself(reads, actor, role, suffix):
    client, _ = reads
    assert client.get(f"/api/mod/customers/{actor}{suffix}", headers=headers(actor, role)).status_code == 404


def test_reads_never_change_customer_stores_or_call_stripe(reads):
    from app import v22_commercial as auth
    client, pool = reads
    before = copy.deepcopy((pool.users, pool.settings, pool.subscriptions, pool.persisted_users))
    with patch.object(auth.stripe, "Subscription") as subscription, \
            patch.object(auth.stripe, "Customer") as customer, \
            patch.object(auth.stripe, "Webhook") as webhook:
        h = headers("moderator", "MODERATOR")
        for path in ("/api/mod/customers", "/api/mod/customers/customer",
                     "/api/mod/customers/customer/subscription", "/api/mod/customers/customer/payments"):
            assert client.get(path, headers=h).status_code == 200
        subscription.assert_not_called()
        customer.assert_not_called()
        webhook.assert_not_called()
    assert (pool.users, pool.settings, pool.subscriptions, pool.persisted_users) == before
    source_queries = [sql for sql in pool.queries if "ORDER BY u.user_id" in sql or "FROM subscriptions" in sql]
    assert source_queries
    assert not any("SELECT *" in sql or "stripe_" in sql or "password" in sql for sql in source_queries)


def test_router_contains_only_get_operations():
    from app.moderator_customers import router
    assert len(router.routes) == 4
    assert all(route.methods == {"GET"} for route in router.routes)
