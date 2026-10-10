"""Role and permission mutations share their connection/transaction with audit."""
import copy
import json
import uuid

import pytest

from test_moderator_role import headers, setup


def assert_audit(pool, action, target, before, after, request_id):
    row = pool.audits[-1]
    assert row["action"] == action
    assert row["actor_user_id"] == "owner"
    assert row["actor_role"] == "OWNER"
    assert row["target_id"] == target
    assert row["request_id"] == request_id
    assert json.loads(row["before"]) == before
    assert json.loads(row["after"]) == after
    assert row["approval_request_id"] is None
    assert row["reason"] is None
    assert "created_at" in row
    assert "owner@example.test" not in str(row)
    assert "Bearer" not in str(row) and "private-token" not in str(row)
    assert len(pool.audits) == 1


@pytest.mark.parametrize("initial_role,new_role", [
    ("CUSTOMER", "MODERATOR"), ("MODERATOR", "CUSTOMER"),
])
def test_role_change_records_canonical_old_and_new_role(setup, initial_role, new_role):
    client, pool = setup
    pool.users["customer"]["security"]["role"] = initial_role
    rid = str(uuid.uuid4())
    result = client.patch("/api/v22/admin/users/customer/role",
                          headers={**headers("owner", "OWNER"), "x-request-id": rid},
                          json={"role": new_role, "email": "private@example.test", "token": "private-token"})
    assert result.status_code == 200
    assert pool.users["customer"]["auth_version"] == 2
    assert_audit(pool, "ROLE_CHANGED", "customer", {"role": initial_role}, {"role": new_role}, rid)
    assert pool.audits[0]["target_type"] == "USER"
    assert "private@example.test" not in str(pool.audits)


@pytest.mark.parametrize("method,initial,action,final", [
    ("POST", False, "PERMISSION_GRANTED", True),
    ("POST", True, "PERMISSION_GRANTED", True),
    ("DELETE", True, "PERMISSION_REVOKED", False),
    ("DELETE", False, "PERMISSION_REVOKED", False),
])
def test_permission_operations_and_idempotent_retries_record_exact_state(setup, method, initial, action, final):
    client, pool = setup
    permission = "events.view"
    if initial:
        pool.permissions["moderator"] = {permission: {
            "user_id": "moderator", "permission": permission, "granted_by": "owner", "granted_at": "offline",
        }}
    rid = str(uuid.uuid4())
    path = "/api/v22/admin/users/moderator/permissions"
    if method == "DELETE":
        path += "/" + permission
    result = client.request(method, path, headers={**headers("owner", "OWNER"), "x-request-id": rid},
                            json={"permission": permission} if method == "POST" else None)
    assert result.status_code == 200
    assert_audit(pool, action, "moderator",
                 {"permission": permission, "granted": initial},
                 {"permission": permission, "granted": final}, rid)
    assert pool.audits[0]["target_type"] == "MODERATOR_PERMISSION"


@pytest.mark.parametrize("failure", ["audit", "commit"])
@pytest.mark.parametrize("operation", ["role", "grant", "revoke"])
def test_failed_audit_or_commit_rolls_back_entire_mutation_and_local_role(setup, failure, operation):
    client, pool = setup
    if operation == "revoke":
        pool.permissions["moderator"] = {"events.view": {"permission": "events.view"}}
    assert client.get("/api/v22/session", headers=headers("owner", "OWNER")).status_code == 200
    state = client.app.state.v22_commercial
    before = copy.deepcopy((pool.users, pool.permissions, pool.settings, pool.audits))
    local_before = copy.deepcopy((state["state"], state["auth_baseline"]))
    pool.audit_fail = failure == "audit"
    pool.commit_fail = failure == "commit"
    if operation == "role":
        result = client.patch("/api/v22/admin/users/customer/role",
                              headers=headers("owner", "OWNER"), json={"role": "MODERATOR"})
    elif operation == "grant":
        result = client.post("/api/v22/admin/users/moderator/permissions",
                             headers=headers("owner", "OWNER"), json={"permission": "events.view"})
    else:
        result = client.delete("/api/v22/admin/users/moderator/permissions/events.view",
                               headers=headers("owner", "OWNER"))
    assert result.status_code == 503
    assert (pool.users, pool.permissions, pool.settings, pool.audits) == before
    assert state["state"] == local_before[0]
    # Request authentication may refresh the OWNER baseline, never the failed target.
    assert state["auth_baseline"].get("customer") == local_before[1].get("customer")
    assert pool.transaction_depth == 0
    if operation == "role":
        pool.audit_fail = pool.commit_fail = False
        assert client.get("/api/v22/session", headers=headers("customer", "CUSTOMER")).status_code == 200


@pytest.mark.parametrize("change,status", [
    ({"role": "MODERATOR"}, 403), ({"active": False}, 403), ({"auth_version": 2}, 401),
])
def test_role_actor_is_rechecked_under_transaction_lock(setup, change, status):
    client, pool = setup
    fetch = pool.fetch

    async def changed(sql, *args):
        if "auth_version" in change:
            pool.users["owner"]["auth_version"] = change["auth_version"]
        else:
            pool.users["owner"]["security"].update(change)
        return await fetch(sql, *args)

    pool.fetch = changed
    response = client.patch("/api/v22/admin/users/customer/role",
                            headers=headers("owner", "OWNER"), json={"role": "MODERATOR"})
    assert response.status_code == status
    assert pool.users["customer"]["security"]["role"] == "CUSTOMER"
    assert pool.users["customer"]["auth_version"] == 1
    assert pool.audits == []
