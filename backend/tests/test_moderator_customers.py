"""Offline moderator customer reads through production guards and middleware."""
import copy
from datetime import datetime, timezone

import pytest

from app.moderator_customers import router
from test_moderator_role import CanonicalPool, headers, setup


class CustomerReadPool(CanonicalPool):
    def __init__(self, users):
        super().__init__(users)
        self.persisted_users = {}
        self.subscriptions = {}
        self.erased = set()

    def visible(self, actor, query=None):
        return [
            uid for uid in sorted(self.users)
            if uid != actor and uid not in self.erased
            and self.users[uid]["security"].get("role") == "CUSTOMER"
            and (query is None or uid == query
                 or (self.users[uid]["security"].get("email") or "").lower() == query.lower())
        ]

    def profile(self, uid):
        security = self.users[uid]["security"]
        return {
            "user_id": uid, "email": security.get("email"), "role": security.get("role"),
            "active": security.get("active") is True,
            "created_at": self.persisted_users.get(uid, {}).get("created_at"),
            "email_verified": security.get("email_verified") is True,
            "mfa_enabled": self.settings.get(uid, {}).get("two_factor_enabled") is True,
        }

    async def fetchrow(self, sql, *args):
        if "FOR SHARE OF u" in sql:
            self.check(sql)
            assert "NOT EXISTS" in sql and "commercial_erased_users" in sql
            return self.profile(args[1]) if args[1] in self.visible(args[0]) else None
        if "FROM subscriptions" in sql:
            self.check(sql)
            return copy.deepcopy(self.subscriptions.get(args[0]))
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        if "COUNT(*) FROM commercial_auth_users u" in sql:
            self.check(sql)
            return len(self.visible(*args))
        return await super().fetchval(sql, *args)

    async def fetch(self, sql, *args):
        if "ORDER BY u.user_id LIMIT" in sql:
            self.check(sql)
            ids = self.visible(args[0], args[1])
            return [self.profile(uid) for uid in ids[args[3]:args[3] + args[2]]]
        return await super().fetch(sql, *args)


@pytest.fixture
def reads(setup):
    client, _ = setup
    users = client.app.state.v22_commercial["state"]["users"]
    pool = CustomerReadPool(users)
    pool.settings["moderator"] = {"two_factor_enabled": True}
    pool.permissions["moderator"] = {
        permission: {} for permission in ("customers.view", "subscriptions.view", "payments.view")
    }
    pool.persisted_users["customer"] = {"created_at": datetime(2026, 10, 1, tzinfo=timezone.utc)}
    pool.subscriptions["customer"] = {
        "plan": "MASTER_MODE", "subscription_status": "PAST_DUE",
        "current_period_end": datetime(2026, 11, 1, tzinfo=timezone.utc),
        "cancel_at_period_end": True, "last_payment_status": "FAILED",
        "last_payment_at": datetime(2026, 10, 8, tzinfo=timezone.utc),
    }
    client.app.state.db_pool = pool
    client.app.include_router(router)
    yield client, pool


PATHS = [
    ("/api/mod/customers", "customers.view"),
    ("/api/mod/customers/customer", "customers.view"),
    ("/api/mod/customers/customer/subscription", "subscriptions.view"),
    ("/api/mod/customers/customer/payments", "payments.view"),
]


@pytest.mark.parametrize("path,permission", PATHS)
@pytest.mark.parametrize("actor,role,status", [
    ("moderator", "MODERATOR", 200), ("owner", "OWNER", 200), ("customer", "CUSTOMER", 403),
])
def test_required_roles_and_permissions(reads, path, permission, actor, role, status):
    client, _ = reads
    assert client.get(path, headers=headers(actor, role)).status_code == status


@pytest.mark.parametrize("path,permission", PATHS)
def test_missing_permission_and_disabled_mfa_fail_closed(reads, path, permission):
    client, pool = reads
    h = headers("moderator", "MODERATOR")
    del pool.permissions["moderator"][permission]
    assert client.get(path, headers=h).status_code == 403
    pool.permissions["moderator"][permission] = {}
    pool.settings["moderator"]["two_factor_enabled"] = False
    response = client.get(path, headers=h)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "mfa_required"


@pytest.mark.parametrize("target", ["owner", "moderator", "missing", "erased-customer"])
@pytest.mark.parametrize("suffix", ["", "/subscription", "/payments"])
def test_privileged_missing_and_erased_targets_are_indistinguishable(reads, target, suffix):
    client, pool = reads
    pool.users["erased-customer"] = copy.deepcopy(pool.users["customer"])
    pool.erased.add("erased-customer")
    response = client.get(f"/api/mod/customers/{target}{suffix}", headers=headers("owner", "OWNER"))
    assert response.status_code == 404
    assert response.json() == {"detail": "Customer not found"}


@pytest.mark.parametrize("query,found", [
    ("CUSTOMER@EXAMPLE.TEST", True), ("customer", True), ("cust", False),
    ("customer@", False), ("example.test", False), ("%example.test", False),
    ("customer@other.test", False), ("' OR TRUE --", False),
])
def test_search_is_full_email_equality_or_exact_user_id(reads, query, found):
    client, _ = reads
    response = client.get("/api/mod/customers", headers=headers("moderator", "MODERATOR"),
                          params={"search": query})
    assert response.status_code == 200
    assert response.json()["total"] == int(found)
    assert len(response.json()["items"]) == int(found)
    if found:
        assert response.json()["items"][0]["user_id"] == "customer"


def test_page_filters_and_maximum_50(reads):
    client, pool = reads
    for index in range(70):
        pool.users[f"member-{index:03d}"] = copy.deepcopy(pool.users["customer"])
    pool.users["erased-customer"] = copy.deepcopy(pool.users["customer"])
    pool.erased.add("erased-customer")
    h = headers("moderator", "MODERATOR")
    result = client.get("/api/mod/customers", headers=h, params={"limit": 50, "offset": 10})
    assert result.status_code == 200
    assert result.json()["total"] == 71
    assert len(result.json()["items"]) == 50
    assert all(item["role"] == "CUSTOMER" and item["user_id"] not in {"owner", "moderator", "erased-customer"}
               for item in result.json()["items"])
    for params in ({"limit": 51}, {"limit": 0}, {"offset": -1}):
        assert client.get("/api/mod/customers", headers=h, params=params).status_code == 422


def test_profile_uses_canonical_security_and_only_persisted_snapshot_registration_date(reads):
    client, pool = reads
    user = next(user for user in client.app.state.v22_commercial["state"]["users"] if user["id"] == "customer")
    user.update(role="OWNER", email="worker@example.test", created_at="2000-01-01T00:00:00Z")
    response = client.get("/api/mod/customers/customer", headers=headers("moderator", "MODERATOR"))
    assert response.status_code == 200
    data = response.json()
    assert data["role"] == "CUSTOMER"
    assert data["email_masked"] == "c***@example.test"
    assert data["created_at"].startswith("2026-10-01")
    assert "worker@example.test" not in response.text
    pool.users["customer"]["security"]["role"] = "OWNER"
    assert client.get("/api/mod/customers/customer", headers=headers("moderator", "MODERATOR")).status_code == 404


def test_subscription_and_payments_only_expose_recorded_values(reads):
    client, pool = reads
    h = headers("moderator", "MODERATOR")
    response = client.get("/api/mod/customers/customer/subscription", headers=h)
    assert response.json() == {
        "user_id": "customer", "plan": "MASTER_MODE", "subscription_status": "PAST_DUE",
        "current_period_end": "2026-11-01T00:00:00Z", "cancel_at_period_end": True,
    }
    response = client.get("/api/mod/customers/customer/payments", headers=h)
    assert response.json() == {
        "user_id": "customer", "payment_status": "FAILED", "last_failed_payment_at": "2026-10-08T00:00:00Z",
    }
    pool.subscriptions["customer"]["last_payment_status"] = "PAID"
    assert client.get("/api/mod/customers/customer/payments", headers=h).json()["last_failed_payment_at"] is None
    pool.subscriptions.clear()
    assert client.get("/api/mod/customers/customer/payments", headers=h).json() == {
        "user_id": "customer", "payment_status": "veri yok", "last_failed_payment_at": None,
    }
    assert client.get("/api/mod/customers/customer/subscription", headers=h).json() == {
        "user_id": "customer", "plan": None, "subscription_status": None,
        "current_period_end": None, "cancel_at_period_end": None,
    }
