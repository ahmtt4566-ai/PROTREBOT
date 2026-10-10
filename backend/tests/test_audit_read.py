"""OWNER-only bounded audit listing using real request validation and SQL filters."""
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from test_moderator_role import headers, setup

START = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)


def seed(pool, count=130):
    pool.audits = [{
        "id": index + 1, "created_at": START + timedelta(minutes=index),
        "actor_user_id": "owner" if index % 2 == 0 else "other-owner",
        "actor_role": "OWNER", "target_id": "customer" if index % 3 == 0 else "moderator",
        "target_type": "USER", "action": "ROLE_CHANGED" if index % 2 == 0 else "PERMISSION_GRANTED",
        "request_id": str(uuid.uuid4()), "approval_request_id": None, "reason": None,
        "before": json.dumps({"role": "CUSTOMER"}), "after": json.dumps({"role": "MODERATOR"}),
        "ip_masked": "192.0.2.0/24",
    } for index in range(count)]


@pytest.mark.parametrize("user_id,role,status", [
    ("owner", "OWNER", 200), ("moderator", "MODERATOR", 403), ("customer", "CUSTOMER", 403),
])
def test_only_owner_can_read_audit(setup, user_id, role, status):
    client, pool = setup
    seed(pool, 1)
    pool.settings["moderator"] = {"two_factor_enabled": True}
    pool.permissions["moderator"] = {"events.view": {}}
    result = client.get("/api/v22/admin/audit", headers=headers(user_id, role))
    assert result.status_code == status
    if status != 200:
        assert not any("FROM audit_log" in sql for sql in pool.queries)


def test_owner_pagination_has_exact_shape_and_limit_with_stable_order(setup):
    client, pool = setup
    seed(pool)
    before = copy.deepcopy(pool.audits)
    result = client.get("/api/v22/admin/audit", headers=headers("owner", "OWNER"))
    assert result.status_code == 200
    data = result.json()
    assert set(data) == {"items", "total", "limit", "offset"}
    assert (data["total"], data["limit"], data["offset"], len(data["items"])) == (130, 50, 0, 50)
    assert [item["id"] for item in data["items"]] == list(range(130, 80, -1))
    assert data["items"][0]["before"] == {"role": "CUSTOMER"}
    result = client.get("/api/v22/admin/audit", headers=headers("owner", "OWNER"),
                        params={"limit": 100, "offset": 30})
    assert result.status_code == 200
    assert [item["id"] for item in result.json()["items"]] == list(range(100, 0, -1))
    assert pool.audits == before


@pytest.mark.parametrize("params,expected", [
    ({"actor": "owner"}, [9, 7, 5, 3, 1]),
    ({"target": "customer"}, [10, 7, 4, 1]),
    ({"action": "PERMISSION_GRANTED"}, [10, 8, 6, 4, 2]),
    ({"created_from": "2026-10-10T10:02:00Z", "created_to": "2026-10-10T10:05:00Z"}, [6, 5, 4, 3]),
    ({"actor": "owner", "target": "customer", "action": "ROLE_CHANGED",
      "created_from": "2026-10-10T12:01:00+02:00", "created_to": "2026-10-10T12:09:00+02:00"}, [7]),
    ({"actor": "unknown-id"}, []),
])
def test_each_filter_and_combined_filters_are_applied_to_count_and_items(setup, params, expected):
    client, pool = setup
    seed(pool, 10)
    response = client.get("/api/v22/admin/audit", headers=headers("owner", "OWNER"), params=params)
    assert response.status_code == 200
    data = response.json()
    assert [item["id"] for item in data["items"]] == expected
    assert data["total"] == len(expected)
    assert all("WHERE" in sql for sql in pool.queries if "FROM audit_log" in sql)


@pytest.mark.parametrize("params", [
    {"limit": 101}, {"limit": 0}, {"offset": -1}, {"action": "LOGIN"},
    {"actor": "private@example.test"}, {"target": "' OR TRUE --"},
    {"created_from": "not-a-date"}, {"created_from": "2026-10-10T10:00:00"},
    {"created_from": "2026-10-10T11:00:00Z", "created_to": "2026-10-10T10:00:00Z"},
])
def test_invalid_filters_and_out_of_bounds_pagination_never_query_audit(setup, params):
    client, pool = setup
    response = client.get("/api/v22/admin/audit", headers=headers("owner", "OWNER"), params=params)
    assert response.status_code == 422
    assert not any("FROM audit_log" in sql for sql in pool.queries)


def test_no_audit_storage_is_not_a_success_shaped_empty_result(setup, caplog):
    client, pool = setup
    fetchval = pool.fetchval

    async def unavailable(sql, *args):
        if "FROM audit_log" in sql:
            raise RuntimeError("private@example.test private-token")
        return await fetchval(sql, *args)

    pool.fetchval = unavailable
    response = client.get("/api/v22/admin/audit", headers=headers("owner", "OWNER"))
    assert response.status_code == 503
    assert "Audit read failed (RuntimeError)" in caplog.text
    assert "private@example.test" not in caplog.text and "private-token" not in caplog.text


def test_database_ids_are_not_trusted_from_forged_owner_token_or_snapshot(setup):
    client, pool = setup
    user = next(user for user in client.app.state.v22_commercial["state"]["users"]
                if user["id"] == "moderator")
    user["role"] = "OWNER"
    assert pool.users["moderator"]["security"]["role"] == "MODERATOR"
    assert client.get("/api/v22/admin/audit", headers=headers("moderator", "OWNER")).status_code == 403
    assert not any("FROM audit_log" in sql for sql in pool.queries)


def test_get_returns_actual_mutation_evidence_as_json_objects(setup):
    client, pool = setup
    h = headers("owner", "OWNER")
    assert client.patch("/api/v22/admin/users/customer/role", headers=h,
                        json={"role": "MODERATOR"}).status_code == 200
    result = client.get("/api/v22/admin/audit", headers=h)
    assert result.status_code == 200
    assert result.json()["total"] == 1
    item = result.json()["items"][0]
    assert item["action"] == "ROLE_CHANGED"
    assert item["before"] == {"role": "CUSTOMER"}
    assert item["after"] == {"role": "MODERATOR"}
    assert not any(key in item for key in ("email", "name", "token", "api_key"))
