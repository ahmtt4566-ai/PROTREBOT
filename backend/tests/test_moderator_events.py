import copy
import json
from datetime import datetime, timezone

import pytest

from app.moderator_events import router, safe_item
from app.moderator_access import PERMISSIONS
from test_customer_event_writer import EventsPool
from test_moderator_role import headers as role_headers, setup

PATH = "/api/mod/customers/customer/events"


def headers(uid):
    return role_headers(uid, {"owner": "OWNER", "moderator": "MODERATOR", "customer": "CUSTOMER"}[uid])


class ReadEventsPool(EventsPool):
    def rows(self, uid, kind, start, end):
        rows = [{**r, "source": "customer_event", "severity": "WARNING", "resolved": None}
                for r in self.events if r["user_id"] == uid] + copy.deepcopy(self.system_events)
        return [r for r in rows if (kind is None or r["kind"] == kind) and
                (start is None or r["last_at"] >= start) and (end is None or r["last_at"] <= end)]

    async def fetchval(self, sql, *args):
        if "FROM customer_events" in sql:
            self.check(sql)
            return len(self.rows(*args))
        return await super().fetchval(sql, *args)

    async def fetch(self, sql, *args):
        if "FROM customer_events" in sql:
            self.check(sql)
            return self.rows(*args[:4])[args[5]:args[5] + args[4]]
        return await super().fetch(sql, *args)


@pytest.fixture
def events(setup):
    client, _ = setup
    pool = ReadEventsPool(client.app.state.v22_commercial["state"]["users"])
    pool.settings["moderator"] = {"two_factor_enabled": True}
    pool.permissions["moderator"] = {"events.view": {}, "customers.view": {}}
    stamp = datetime.now(timezone.utc)
    pool.events.append({"id": "event", "user_id": "customer", "kind": "auth.login_failed", "code": "invalid_credentials",
                        "feature": "auth", "http_status": 401, "request_id": "12345678-1234-1234-1234-123456789abc",
                        "count": 5, "first_at": stamp, "last_at": stamp})
    client.app.state.db_pool = pool
    client.app.include_router(router)
    yield client, pool


def test_read_has_exact_safe_fields_and_audit_contains_no_customer_data(events):
    client, pool = events
    response = client.get(PATH, headers=headers("moderator"))
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert set(item) == {"kind", "code", "feature", "http_status", "count", "first_at", "last_at", "request_ref", "source", "severity", "resolved"}
    assert item["count"] == 5 and item["request_ref"] == "12345678"
    assert pool.audits[-1]["action"] == "customer.events.viewed"
    assert json.loads(pool.audits[-1]["before"]) == json.loads(pool.audits[-1]["after"]) == {}
    for word in ("email", "ip_address", "token", "stack", "context", "message", "secret", "@"):
        assert word not in response.text.lower() and word not in json.dumps(pool.audits[-1], default=str).lower()


def test_system_projection_is_fixed_enums_and_does_not_select_sensitive_fields(events):
    client, pool = events
    pool.system_events = [{**pool.events[0], "kind": "api.error", "code": "server_error", "feature": "api",
                           "request_id": None, "source": "system_error", "http_status": None,
                           "count": 1, "severity": "ERROR", "resolved": True,
                           "context": {"email": "secret@example.test"}, "message": "Bearer token", "stack": "private"}]
    response = client.get(PATH, headers=headers("moderator"))
    assert response.status_code == 200 and response.json()["items"][1]["count"] == 1
    assert "secret@example.test" not in response.text and "Bearer" not in response.text
    sql = next(q for q in pool.queries if "FROM error_events" in q)
    projection = sql.split("UNION ALL")[1].split("FROM error_events")[0]
    assert all(word not in projection for word in ("context", "stack", "message", "notes", "occurrences"))


@pytest.mark.parametrize("uid", ["owner", "moderator", "missing"])
def test_staff_self_and_absent_target_hidden(events, uid):
    client, _ = events
    assert client.get(f"/api/mod/customers/{uid}/events", headers=headers("moderator")).status_code == 404


def test_deleted_target_hidden(events):
    client, pool = events
    pool.erased.add("customer")
    assert client.get(PATH, headers=headers("moderator")).status_code == 404


def test_permission_mfa_customer_guards(events):
    client, pool = events
    assert client.get(PATH, headers=headers("customer")).status_code == 403
    pool.permissions["moderator"] = {}
    assert client.get(PATH, headers=headers("moderator")).status_code == 403
    pool.permissions["moderator"] = {"events.view": {}}
    pool.settings["moderator"]["two_factor_enabled"] = False
    response = client.get(PATH, headers=headers("moderator"))
    assert response.status_code == 403 and response.json()["detail"]["code"] == "mfa_required"


def test_owner_has_all_permissions_reads_and_audits_as_owner(events):
    client, pool = events
    me = client.get("/api/mod/me", headers=headers("owner"))
    assert me.status_code == 200 and set(me.json()["permissions"]) == set(PERMISSIONS)
    response = client.get(PATH, headers=headers("owner"))
    assert response.status_code == 200 and pool.audits[-1]["actor_role"] == "OWNER"
    assert client.get("/api/mod/customers/owner/events", headers=headers("owner")).status_code == 404
    assert client.get("/api/mod/customers/moderator/events", headers=headers("owner")).status_code == 404


def test_audit_storage_failure_denies_all_data(events):
    client, pool = events
    pool.audit_fail = True
    response = client.get(PATH, headers=headers("moderator"))
    assert response.status_code == 503 and "items" not in response.json()
    assert pool.audits == []


def test_pagination_filters_and_shared_rate_limit(events):
    client, _ = events
    assert client.get(PATH + "?limit=51", headers=headers("moderator")).status_code == 422
    assert client.get(PATH + "?start=2026-01-01", headers=headers("moderator")).status_code == 422
    assert client.get(PATH + "?kind=unknown", headers=headers("moderator")).status_code == 422
    response = client.get(PATH + "?kind=auth.mfa_failed", headers=headers("moderator"))
    assert response.json()["total"] == 0
    for _ in range(29):
        assert client.get(PATH, headers=headers("moderator")).status_code == 200
    assert client.get(PATH, headers=headers("moderator")).status_code == 429


def test_invalid_stored_code_fails_closed_not_free_text(events):
    client, pool = events
    pool.events[0]["code"] = "secret@example.test"
    response = client.get(PATH, headers=headers("moderator"))
    assert response.status_code == 503 and "secret@example.test" not in response.text
