"""HTTP regressions through the production membership middleware, without services."""
import copy
import re

import pytest
from fastapi import Depends

from app import main
from app.moderator_access import PERMISSIONS, require_permission
from test_moderator_role import headers, setup


@pytest.mark.parametrize("user_id,role,mfa,status", [
    ("customer", "CUSTOMER", True, 403),
    ("moderator", "MODERATOR", False, 403),
    ("moderator", "MODERATOR", True, 200),
    ("owner", "OWNER", False, 200),
])
def test_me_returns_only_canonical_role_and_own_permissions(setup, user_id, role, mfa, status):
    client, pool = setup
    pool.settings[user_id] = {"two_factor_enabled": mfa}
    pool.permissions["moderator"] = {"events.view": {}}
    pool.permissions["customer"] = {"customers.view": {}}
    result = client.get("/api/mod/me", headers=headers(user_id, role))
    assert result.status_code == status
    if status == 200:
        assert result.json() == {
            "role": role, "permissions": list(PERMISSIONS) if role == "OWNER" else ["events.view"],
        }
    elif role == "MODERATOR":
        assert result.json()["detail"]["code"] == "mfa_required"


def test_moderator_without_grants_can_inspect_own_empty_permissions(setup):
    client, pool = setup
    pool.settings["moderator"] = {"two_factor_enabled": True}
    assert client.get("/api/mod/me", headers=headers("moderator", "MODERATOR")).json() == {
        "role": "MODERATOR", "permissions": [],
    }


@pytest.mark.parametrize("permission", PERMISSIONS)
def test_permission_is_read_again_on_every_request(setup, permission):
    client, pool = setup
    pool.settings["moderator"] = {"two_factor_enabled": True}

    @client.app.get("/api/mod/offline-check")
    async def checked(identity=Depends(require_permission(permission))):
        return {"allowed": True}

    h = headers("moderator", "MODERATOR")
    assert client.get("/api/mod/offline-check", headers=h).status_code == 403
    pool.permissions["moderator"] = {permission: {}}
    assert client.get("/api/mod/offline-check", headers=h).status_code == 200
    pool.permissions["moderator"].clear()
    assert client.get("/api/mod/offline-check", headers=h).status_code == 403
    assert client.get("/api/mod/offline-check", headers=headers("owner", "OWNER")).status_code == 200
    assert "owner" not in pool.permissions


def test_role_mfa_and_session_revocation_are_not_cached_or_token_claim_authorized(setup):
    client, pool = setup
    pool.settings["moderator"] = {"two_factor_enabled": True}
    h = headers("moderator", "OWNER")
    assert client.get("/api/mod/me", headers=h).json()["role"] == "MODERATOR"
    pool.settings["moderator"]["two_factor_enabled"] = False
    assert client.get("/api/mod/me", headers=h).json()["detail"]["code"] == "mfa_required"
    pool.settings["moderator"]["two_factor_enabled"] = True
    pool.users["moderator"]["security"]["role"] = "CUSTOMER"
    assert client.get("/api/mod/me", headers=h).status_code == 403
    pool.users["moderator"]["security"]["role"] = "MODERATOR"
    pool.users["moderator"]["auth_version"] += 1
    assert client.get("/api/mod/me", headers=h).status_code == 401


def test_missing_canonical_storage_cannot_fall_back_to_snapshot(setup):
    client, _ = setup
    client.app.state.db_pool = None
    assert client.get("/api/mod/me", headers=headers("owner", "OWNER")).status_code == 503


def test_canonical_permission_failure_is_explicit_and_logs_no_private_content(setup, caplog):
    client, pool = setup
    original = pool.fetchrow

    async def unavailable(sql, *args):
        if "AS mfa_enabled" in sql:
            raise RuntimeError("sensitive@example.test private-token")
        return await original(sql, *args)

    pool.fetchrow = unavailable
    response = client.get("/api/mod/me", headers=headers("moderator", "MODERATOR"))
    assert response.status_code == 503
    assert "Moderator permission read failed (RuntimeError)" in caplog.text
    assert "sensitive@example.test" not in caplog.text
    assert "private-token" not in caplog.text


def test_unknown_dependency_permission_fails_at_definition():
    with pytest.raises(ValueError):
        require_permission("customers.delete")


OWNER_ENDPOINTS = [
    ("GET", "/api/v22/admin/overview", None),
    ("GET", "/api/v22/admin/users/customer/trading-accounts", None),
    ("POST", "/api/v22/admin/users/customer/trading-accounts",
     {"provider": "BINANCE", "environment": "DEMO", "account_reference": "offline"}),
    ("DELETE", "/api/v22/admin/users/customer/trading-accounts/offline", None),
    ("PATCH", "/api/v22/admin/users/customer/role", {"role": "MODERATOR"}),
    ("POST", "/api/v22/admin/users/customer/password-reset", None),
    ("POST", "/api/v22/admin/users/customer/sessions/revoke", None),
    ("DELETE", "/api/v22/admin/users/customer",
     {"email": "customer@example.test", "confirmation": "DELETE USER"}),
    ("GET", "/api/v22/admin/accounts", None),
    ("GET", "/api/v22/admin/accounts/customer", None),
    ("POST", "/api/v22/admin/accounts/customer/password-reset", None),
    ("GET", "/api/v22/admin/errors", None),
    ("GET", "/api/v22/admin/errors/summary", None),
    ("POST", "/api/v22/admin/errors/test", None),
    ("GET", "/api/v22/admin/errors/1", None),
    ("PATCH", "/api/v22/admin/errors/1", {"status": "ACKNOWLEDGED"}),
    ("GET", "/api/v22/admin/system-health", None),
    ("POST", "/api/v22/admin/system-health/check", None),
    ("GET", "/api/v22/admin/maintenance", None),
    ("POST", "/api/v22/admin/maintenance", {"mode": "OFF"}),
    ("POST", "/api/v22/customers",
     {"display_name": "Offline", "email": "offline@example.test", "password": "Offline-password-123!"}),
    ("POST", "/api/v22/customers/customer/status", {"active": True}),
    ("POST", "/api/v22/subscriptions/activate-demo",
     {"user_id": "customer", "plan": "TRIAL", "days": 7}),
    ("POST", "/api/v22/licenses/offline/revoke", {"confirmation": "L\u0130SANS \u0130PTAL", "reason": "offline"}),
    ("PUT", "/api/v22/plans/TRIAL", {"monthly_usd": 0, "agents": 1, "bots": 1}),
    ("PUT", "/api/v22/release-evidence/offline", {"status": "RECORDED", "note": "offline"}),
    ("GET", "/api/v22/commerce/overview", None),
    ("PUT", "/api/v22/commerce/settings", {"brand_name": "Offline"}),
    ("POST", "/api/v22/commerce/leads", {"name": "Offline", "email": "offline@example.test"}),
    ("PUT", "/api/v22/commerce/leads/offline/status", {"status": "NEW"}),
    ("PUT", "/api/v22/commerce/support/offline", {"status": "OPEN"}),
    ("GET", "/api/exchange-connections/time-status", None),
]


@pytest.mark.parametrize("method,path,payload", OWNER_ENDPOINTS, ids=[
    f"{method} {path}" for method, path, _ in OWNER_ENDPOINTS
])
def test_moderator_cannot_enter_any_existing_owner_endpoint(setup, method, path, payload):
    client, pool = setup
    pool.settings["moderator"] = {"two_factor_enabled": True}
    pool.permissions["moderator"] = dict.fromkeys(PERMISSIONS, {})
    before = copy.deepcopy(pool.users)
    response = client.request(method, path, headers=headers("moderator", "MODERATOR"), json=payload)
    assert response.status_code == 403, response.text
    assert pool.users == before
    assert all(sql.startswith("SELECT auth_version, security FROM commercial_auth_users")
               for sql in pool.queries)


def test_owner_inventory_covers_every_registered_admin_route():
    covered = set()
    paths = main.app.openapi()["paths"]
    admin_routes = {path: operations for path, operations in paths.items()
                    if path.startswith("/api/v22/admin/")}
    for method, path, _ in OWNER_ENDPOINTS:
        for template, operations in admin_routes.items():
            pattern = re.sub(r"\{[^}]+\}", "[^/]+", template)
            if re.fullmatch(pattern, path) and method.lower() in operations:
                covered.add((template, method))
    expected = {(path, method.upper()) for path, operations in admin_routes.items()
                for method in operations if method in {"get", "post", "put", "patch", "delete"}}
    assert covered == expected
    assert "/api/mod/me" in paths
