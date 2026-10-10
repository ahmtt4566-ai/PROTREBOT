"""Offline canonical-role and session-revocation regressions."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
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
        if "UPDATE commercial_auth_users" in sql:
            if not row or (args[2] is not None and args[2] != row["auth_version"]):
                return None
            row["security"].update(json.loads(args[1]))
            row["auth_version"] += 1
        return copy.deepcopy(row)


def headers(user_id, role, version=1):
    return {"Authorization": "Bearer " + issue_token(user_id, role, SECRET, token_version=version)}


@pytest.fixture
def setup():
    users = [
        {"id": uid, "email": f"{uid}@example.test", "display_name": uid, "role": role,
         "active": True, "email_verified": True, "auth_version": 1}
        for uid, role in (("owner", "OWNER"), ("moderator", "MODERATOR"), ("customer", "CUSTOMER"))
    ]
    application = FastAPI()
    application.include_router(auth.router)
    state = default_commercial_state()
    state.update(users=copy.deepcopy(users), owner_user_id="owner")
    application.state.v22_commercial = {
        "state": state, "secret": SECRET, "lock": asyncio.Lock(), "storage_lock": asyncio.Lock(),
        "auth_baseline": {},
    }
    pool = CanonicalPool(users)
    application.state.db_pool = pool

    @application.middleware("http")
    async def canonical(request, call_next):
        try:
            request.state.member = await auth.authenticated_user_async(request)
            return await call_next(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    with patch.object(account_settings, "track_session", AsyncMock()), \
            patch.object(auth, "save_state"), \
            patch.object(auth, "persist_v22_commercial", AsyncMock(return_value=True)):
        with TestClient(application) as client:
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
