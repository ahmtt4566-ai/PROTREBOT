"""Offline canonical-role and session-revocation regressions."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import account_settings
from app import v22_commercial as auth
from app.commercial_core import default_commercial_state, issue_token

SECRET = b"offline-moderator-test-secret"


class CanonicalPool:
    def __init__(self, users):
        self.users = {
            user["id"]: {"auth_version": user["auth_version"], "security": auth.auth_security(user)}
            for user in users
        }
        self.permissions = {}
        self.settings = {}
        self.queries = []
        self.fail = False

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        previous = copy.deepcopy((self.users, self.permissions))
        try:
            yield
        except BaseException:
            self.users, self.permissions = previous
            raise

    def check(self, sql):
        self.queries.append(sql)
        if self.fail:
            raise RuntimeError("offline database failure with private content")

    async def fetchrow(self, sql, *args):
        self.check(sql)
        row = self.users.get(args[0])
        if "AS mfa_enabled" in sql:
            if row is None:
                return None
            return {
                **copy.deepcopy(row),
                "mfa_enabled": self.settings.get(args[0], {}).get("two_factor_enabled") is True,
                "permissions": list(self.permissions.get(args[0], {})),
            }
        if "moderator_permissions" in sql:
            from app.moderator_access import PERMISSIONS
            permission = args[1]
            if permission not in PERMISSIONS:
                raise ValueError("offline CHECK violation")
            grants = self.permissions.setdefault(args[0], {})
            if "INSERT INTO" in sql:
                if permission in grants:
                    return None
                grants[permission] = {
                    "user_id": args[0], "permission": permission,
                    "granted_by": args[2], "granted_at": datetime.now(timezone.utc),
                }
                return copy.deepcopy(grants[permission])
            if "DELETE FROM" in sql:
                return grants.pop(permission, None)
            return copy.deepcopy(grants.get(permission))
        if "UPDATE commercial_auth_users" in sql:
            if not row or (args[2] is not None and args[2] != row["auth_version"]):
                return None
            row["security"].update(json.loads(args[1]))
            row["auth_version"] += 1
        return copy.deepcopy(row)

    async def fetch(self, sql, *args):
        self.check(sql)
        assert "ORDER BY user_id FOR UPDATE" in sql
        return [{"user_id": user_id, **copy.deepcopy(self.users[user_id])}
                for user_id in sorted(set(args[0])) if user_id in self.users]


def headers(user_id, role, version=1):
    return {"Authorization": "Bearer " + issue_token(user_id, role, SECRET, token_version=version)}


@pytest.fixture
def setup():
    from app import main, v24_commerce, exchange_connections
    from app.moderator_access import router as moderator_router
    users = [
        {"id": uid, "email": f"{uid}@example.test", "display_name": uid, "role": role,
         "active": True, "email_verified": True, "auth_version": 1}
        for uid, role in (("owner", "OWNER"), ("moderator", "MODERATOR"), ("customer", "CUSTOMER"))
    ]
    application = FastAPI()
    application.include_router(auth.router)
    application.include_router(account_settings.router)
    application.include_router(v24_commerce.router)
    application.include_router(exchange_connections.router)
    application.include_router(moderator_router)
    for route in main.app.routes:
        if getattr(route, "path", "").startswith("/api/v22/admin/"):
            if not any(getattr(existing, "path", None) == route.path
                       and getattr(existing, "methods", None) == route.methods
                       for existing in application.routes):
                application.router.routes.append(route)
    state = default_commercial_state()
    state.update(users=copy.deepcopy(users), owner_user_id="owner")
    application.state.v22_commercial = {
        "state": state, "secret": SECRET, "lock": asyncio.Lock(), "storage_lock": asyncio.Lock(),
        "auth_baseline": {},
    }
    pool = CanonicalPool(users)
    application.state.db_pool = pool

    application.middleware("http")(main.owner_preview_gate)

    with patch.object(account_settings, "track_session", AsyncMock()), \
            patch.object(auth, "save_state"), \
            patch.object(auth, "persist_v22_commercial", AsyncMock(return_value=True)), \
            patch.object(auth, "schedule_log_event"), \
            patch.object(main, "WEB_REQUIRE_AUTH", False), \
            patch.object(main, "hydrate_authenticated_user_state", AsyncMock()):
        with TestClient(application, base_url="https://moderator.example.test") as client:
            yield client, pool


def test_role_change_revokes_old_token_and_preserves_snapshot_shape(setup):
    client, pool = setup
    keys = set(client.app.state.v22_commercial["state"])
    result = client.patch("/api/v22/admin/users/customer/role",
                          headers=headers("owner", "OWNER"), json={"role": "MODERATOR"})
    assert result.status_code == 200
    assert pool.users["customer"]["security"]["role"] == "MODERATOR"
    assert pool.users["customer"]["auth_version"] == 2
    assert set(client.app.state.v22_commercial["state"]) == keys
    assert client.get("/api/v22/operations", headers=headers("customer", "CUSTOMER")).status_code == 401
    assert any("($3::bigint IS NULL OR auth_version = $3)" in sql for sql in pool.queries)


@pytest.mark.parametrize("actor,role,target,new_role,status", [
    ("moderator", "MODERATOR", "moderator", "CUSTOMER", 403),
    ("owner", "OWNER", "owner", "CUSTOMER", 403),
    ("owner", "OWNER", "moderator", "OWNER", 409),
    ("owner", "OWNER", "customer", "OWNER", 409),
    ("owner", "OWNER", "customer", "ADMIN", 422),
    ("customer", "CUSTOMER", "moderator", "CUSTOMER", 403),
])
def test_role_changes_fail_closed(setup, actor, role, target, new_role, status):
    client, pool = setup
    before = copy.deepcopy(pool.users)
    response = client.patch(f"/api/v22/admin/users/{target}/role",
                            headers=headers(actor, role), json={"role": new_role})
    assert response.status_code == status
    assert pool.users == before


def test_bootstrap_owner_id_cannot_be_demoted_even_with_stale_role(setup):
    client, pool = setup
    client.app.state.v22_commercial["state"]["owner_user_id"] = "moderator"
    result = client.patch("/api/v22/admin/users/moderator/role",
                          headers=headers("owner", "OWNER"), json={"role": "CUSTOMER"})
    assert result.status_code == 409
    assert pool.users["moderator"]["security"]["role"] == "MODERATOR"
