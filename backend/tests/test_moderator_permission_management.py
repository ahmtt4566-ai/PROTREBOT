"""OWNER-only permission writes, canonical target checks and transaction failures."""
import copy
from datetime import datetime, timezone

import pytest

from app.moderator_access import PERMISSIONS
from test_moderator_role import headers, setup


@pytest.mark.parametrize("permission", PERMISSIONS)
def test_owner_grants_and_revokes_each_permission_with_durable_attribution(setup, permission):
    client, pool = setup
    h = headers("owner", "OWNER")
    path = "/api/v22/admin/users/moderator/permissions"
    before = datetime.now(timezone.utc)
    result = client.post(path, headers=h, json={"permission": permission})
    assert result.status_code == 200
    assert result.json()["user_id"] == "moderator"
    assert result.json()["permission"] == permission
    assert result.json()["granted_by"] == "owner"
    granted_at = datetime.fromisoformat(result.json()["granted_at"])
    assert before <= granted_at <= datetime.now(timezone.utc)
    assert pool.permissions["moderator"][permission]["granted_by"] == "owner"
    duplicate = client.post(path, headers=h, json={"permission": permission})
    assert duplicate.status_code == 200
    assert duplicate.json() == result.json()
    assert "owner" not in pool.permissions
    removed = client.delete(path + "/" + permission, headers=h)
    assert removed.status_code == 200
    assert removed.json() == {"user_id": "moderator", "permission": permission, "removed": True}
    assert permission not in pool.permissions["moderator"]
    assert client.delete(path + "/" + permission, headers=h).json()["removed"] is False
    locked = next(index for index, sql in enumerate(pool.queries) if "FOR UPDATE" in sql)
    inserted = next(index for index, sql in enumerate(pool.queries) if "INSERT INTO moderator_permissions" in sql)
    assert locked < inserted


@pytest.mark.parametrize("method", ["POST", "DELETE"])
@pytest.mark.parametrize("actor,role,target,status", [
    ("moderator", "MODERATOR", "moderator", 403),
    ("customer", "CUSTOMER", "moderator", 403),
    ("owner", "OWNER", "customer", 409),
    ("owner", "OWNER", "owner", 409),
    ("owner", "OWNER", "missing", 404),
])
def test_permission_write_restrictions(setup, method, actor, role, target, status):
    client, pool = setup
    before = copy.deepcopy(pool.permissions)
    path = f"/api/v22/admin/users/{target}/permissions"
    payload = {"permission": "events.view"} if method == "POST" else None
    if method == "DELETE":
        path += "/events.view"
    result = client.request(method, path, headers=headers(actor, role), json=payload)
    assert result.status_code == status
    assert pool.permissions == before


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_unknown_permission_cannot_reach_database_write(setup, method):
    client, pool = setup
    path = "/api/v22/admin/users/moderator/permissions"
    payload = {"permission": "customers.delete"} if method == "POST" else None
    if method == "DELETE":
        path += "/customers.delete"
    assert client.request(method, path, headers=headers("owner", "OWNER"), json=payload).status_code == 422
    assert pool.permissions == {}


def test_canonical_customer_cannot_receive_grants_from_stale_moderator_snapshot(setup):
    client, pool = setup
    pool.users["moderator"]["security"]["role"] = "CUSTOMER"
    assert client.post("/api/v22/admin/users/moderator/permissions",
                       headers=headers("owner", "OWNER"), json={"permission": "events.view"}).status_code == 409
    assert pool.permissions == {}


@pytest.mark.parametrize("change,status", [
    ({"role": "MODERATOR"}, 403),
    ({"active": False}, 403),
    ({"auth_version": 2}, 401),
])
def test_owner_is_rechecked_under_lock_before_permission_write(setup, change, status):
    client, pool = setup
    fetch = pool.fetch

    async def changed(sql, *args):
        if "auth_version" in change:
            pool.users["owner"]["auth_version"] = change["auth_version"]
        else:
            pool.users["owner"]["security"].update(change)
        return await fetch(sql, *args)

    pool.fetch = changed
    response = client.post("/api/v22/admin/users/moderator/permissions",
                           headers=headers("owner", "OWNER"), json={"permission": "events.view"})
    assert response.status_code == status
    assert pool.permissions == {}


def test_failed_commit_rolls_back_grant_and_logs_type_only(setup, caplog):
    from contextlib import asynccontextmanager
    client, pool = setup

    @asynccontextmanager
    async def failed_commit():
        before = copy.deepcopy(pool.permissions)
        try:
            yield
            raise RuntimeError("private@example.test secret-token")
        finally:
            pool.permissions = before

    pool.transaction = failed_commit
    response = client.post("/api/v22/admin/users/moderator/permissions",
                           headers=headers("owner", "OWNER"), json={"permission": "events.view"})
    assert response.status_code == 503
    assert pool.permissions == {}
    assert "Moderator permission update failed (RuntimeError)" in caplog.text
    assert "private@example.test" not in caplog.text and "secret-token" not in caplog.text


def test_grant_and_revoke_change_the_next_permission_request_without_new_token(setup):
    from fastapi import Depends
    from app.moderator_access import require_permission
    client, pool = setup
    pool.settings["moderator"] = {"two_factor_enabled": True}

    @client.app.get("/api/mod/offline-check")
    async def checked(identity=Depends(require_permission("events.view"))):
        return {"allowed": True}

    member = headers("moderator", "MODERATOR")
    owner = headers("owner", "OWNER")
    path = "/api/v22/admin/users/moderator/permissions"
    assert client.get("/api/mod/offline-check", headers=member).status_code == 403
    assert client.post(path, headers=owner, json={"permission": "events.view"}).status_code == 200
    assert client.get("/api/mod/offline-check", headers=member).status_code == 200
    assert client.delete(path + "/events.view", headers=owner).status_code == 200
    assert client.get("/api/mod/offline-check", headers=member).status_code == 403
    assert pool.users["moderator"]["auth_version"] == 1
