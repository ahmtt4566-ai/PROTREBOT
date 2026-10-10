"""Offline production guards/transactions; no database or service requests."""
import asyncio
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app import account_erasure
from app.moderator_support import mask_support_text, router
from test_moderator_customers import CustomerReadPool
from test_moderator_role import headers, setup


class SupportPool(CustomerReadPool):
    def __init__(self, users):
        super().__init__(users)
        self.cases = {}
        self.notes = []
        self.tickets = [{"id": "legacy-1", "user_id": "customer", "subject": "Bağlantı sorusu",
                         "message": "Müşteri mesajı", "priority": "HIGH", "status": "OPEN",
                         "created_at": "2026-10-10T10:00:00+00:00"}]
        self.fresh = False
        self.lock = threading.RLock()
        self.summary_fail = False

    def check(self, sql):
        if "application_state_snapshots" in sql:
            assert sql.strip().startswith("SELECT"), "Snapshot write attempted"
        super().check(sql)

    @asynccontextmanager
    async def acquire(self):
        with self.lock:
            yield self

    @asynccontextmanager
    async def transaction(self):
        previous = copy.deepcopy((self.cases, self.notes, self.fresh, self.erased))
        try:
            async with super().transaction():
                yield
        except BaseException:
            self.cases, self.notes, self.fresh, self.erased = previous
            raise

    def visible_cases(self, actor, status=None, assignment=None, priority=None):
        return [row for row in self.cases.values()
                if row["user_id"] in self.visible(actor)
                and (not status or row["case_status"] == status or status == "OPEN" and row["case_status"] == "NEW")
                and (not assignment or assignment == "mine" and row["assignee_user_id"] == actor
                     or assignment == "unassigned" and row["assignee_user_id"] is None)
                and (not priority or row["priority"] == priority)]

    async def fetchrow(self, sql, *args):
        if "support_sync_state" in sql:
            self.check(sql)
            return {"fresh": self.fresh}
        if "SELECT payload FROM application_state_snapshots" in sql:
            self.check(sql)
            return {"payload": {"support_tickets": copy.deepcopy(self.tickets)}}
        if "SELECT user_id FROM commercial_auth_users" in sql:
            self.check(sql)
            return {"user_id": args[0]} if args[0] in self.visible("nobody") else None
        if "SELECT c.*" in sql:
            self.check(sql)
            rows = self.visible_cases(args[0])
            return copy.deepcopy(next((row for row in rows if row["id"] == args[1]), None))
        if "COUNT(*) FILTER" in sql:
            self.check(sql)
            if self.summary_fail:
                raise RuntimeError("private@example.test counts")
            active = [r for r in self.visible_cases(args[0]) if r["case_status"] in ("NEW", "OPEN", "WAITING")]
            return {"open_cases": len(active), "unassigned": sum(r["assignee_user_id"] is None for r in active),
                    "assigned_to_me": sum(r["assignee_user_id"] == args[0] for r in active)}
        if "UPDATE support_cases" in sql:
            self.check(sql)
            row = next(row for row in self.cases.values() if row["id"] == args[0])
            if "assignee_user_id IS NULL" in sql:
                if row["assignee_user_id"] is not None:
                    return None
                row["assignee_user_id"] = args[1]
            else:
                if row["assignee_user_id"] != args[1]:
                    return None
                if "case_status = $3" in sql:
                    if row["version"] != args[3]:
                        return None
                    row["case_status"] = args[2]
                elif "assignee_user_id = NULL" in sql:
                    row["assignee_user_id"] = None
            row["version"] += 1
            return copy.deepcopy(row)
        return await super().fetchrow(sql, *args)

    async def execute(self, sql, *args):
        if "INSERT INTO support_cases" in sql:
            self.check(sql)
            row = self.cases.get(args[1])
            if row:
                assert row["user_id"] == args[2]
                for key, value in zip(("subject", "message", "priority", "legacy_status", "legacy_response_note"), args[3:8]):
                    row[key] = value
            else:
                self.cases[args[1]] = dict(zip(
                    ("id", "legacy_ticket_id", "user_id", "subject", "message", "priority", "legacy_status", "legacy_response_note", "created_at_legacy"), args
                )) | {"case_status": "NEW", "assignee_user_id": None, "version": 1}
            return "INSERT 0 1"
        if "INSERT INTO support_sync_state" in sql:
            self.check(sql)
            return "INSERT 0 1"
        if "UPDATE support_sync_state" in sql:
            self.check(sql)
            self.fresh = True
            return "UPDATE 1"
        if "INSERT INTO support_notes" in sql:
            self.check(sql)
            self.notes.append({"id": args[0], "case_id": args[1], "author_user_id": args[2],
                               "body": args[3], "created_at": datetime.now(timezone.utc)})
            return "INSERT 0 1"
        if "INSERT INTO commercial_erased_users" in sql:
            self.check(sql)
            # Offline model of the migration trigger, invoked by real erase_database.
            erased = next(uid for uid in self.users if account_erasure.user_hash(uid) == args[0])
            self.erased.add(erased)
            ids = {row["id"] for row in self.cases.values() if row["user_id"] == erased}
            for row in self.cases.values():
                if row["id"] in ids:
                    row.update(subject="", message="", legacy_response_note="")
            for note in self.notes:
                if note["case_id"] in ids or note["author_user_id"] == erased:
                    note["body"] = ""
            return "INSERT 0 1"
        if "DELETE FROM commercial_auth_users" in sql:
            self.check(sql)
            self.users.pop(args[0])
            return "DELETE 1"
        return await super().execute(sql, *args)

    async def fetch(self, sql, *args):
        if "FROM support_notes" in sql:
            self.check(sql)
            return [{k: v for k, v in note.items() if k != "case_id"} for note in self.notes if note["case_id"] == args[0]]
        if "SELECT c.*" in sql:
            self.check(sql)
            return copy.deepcopy(self.visible_cases(*args[:4])[args[5]:args[5] + args[4]])
        return await super().fetch(sql, *args)

    async def fetchval(self, sql, *args):
        if "FROM support_cases" in sql:
            self.check(sql)
            return len(self.visible_cases(*args))
        return await super().fetchval(sql, *args)


@pytest.fixture
def support(setup):
    client, _ = setup
    pool = SupportPool(client.app.state.v22_commercial["state"]["users"])
    pool.settings["moderator"] = {"two_factor_enabled": True}
    pool.permissions["moderator"] = {"support.view": {}, "support.manage": {}}
    pool.users["moderator2"] = copy.deepcopy(pool.users["moderator"])
    pool.settings["moderator2"] = {"two_factor_enabled": True}
    pool.permissions["moderator2"] = {"support.view": {}, "support.manage": {}}
    client.app.state.db_pool = pool
    client.app.include_router(router)
    yield client, pool


H = headers("moderator", "MODERATOR")


def load_case(client):
    response = client.get("/api/mod/support/cases", headers=H)
    assert response.status_code == 200, response.text
    return response.json()["items"][0]["id"]


ROUTES = [
    ("GET", "/cases", None, "support.view"),
    ("GET", "/summary", None, "support.view"),
    ("GET", "/cases/case-id", None, "support.view"),
    ("POST", "/cases/case-id/take", None, "support.manage"),
    ("POST", "/cases/case-id/release", None, "support.manage"),
    ("POST", "/cases/case-id/status", {"status": "OPEN", "expected_version": 1}, "support.manage"),
    ("POST", "/cases/case-id/notes", {"body": "Not"}, "support.manage"),
]


@pytest.mark.parametrize("method,path,body,permission", ROUTES)
def test_support_requires_permission_mfa_and_customer_is_denied(support, method, path, body, permission):
    client, pool = support
    url = "/api/mod/support" + path
    del pool.permissions["moderator"][permission]
    assert client.request(method, url, headers=H, json=body).status_code == 403
    pool.permissions["moderator"][permission] = {}
    pool.settings["moderator"]["two_factor_enabled"] = False
    response = client.request(method, url, headers=H, json=body)
    assert response.status_code == 403 and response.json()["detail"]["code"] == "mfa_required"
    assert client.request(method, url, headers=headers("customer", "CUSTOMER"), json=body).status_code == 403


def test_list_triggers_cached_sync_filters_page_limit_and_never_writes_snapshot(support):
    client, pool = support
    with patch("app.v24_commerce.save_state", side_effect=AssertionError("Snapshot write")):
        id_ = load_case(client)
        assert load_case(client) == id_
        assert len(pool.cases) == 1
        assert sum("SELECT payload FROM application_state_snapshots" in sql for sql in pool.queries) == 1
        assert client.get("/api/mod/support/cases?limit=51", headers=H).status_code == 422
        assert client.get("/api/mod/support/cases?priority=LOW", headers=H).json()["total"] == 0
        assert client.get("/api/mod/support/cases?assignment=unassigned", headers=H).json()["total"] == 1
        assert client.get("/api/mod/support/cases?assignment=mine", headers=H).json()["total"] == 0
        assert client.get("/api/mod/support/cases?status=OPEN", headers=H).json()["total"] == 1


def test_all_support_reads_and_writes_fail_test_if_snapshot_writer_is_called(support):
    client, pool = support
    forbidden = AssertionError("Support must never write legacy snapshots")
    with patch("app.v24_commerce.save_state", side_effect=forbidden), \
         patch("app.v22_commercial.save_state", side_effect=forbidden), \
         patch("app.v22_commercial.persist_v22_commercial", AsyncMock(side_effect=forbidden)):
        path = f"/api/mod/support/cases/{load_case(client)}"
        assert client.get("/api/mod/support/summary", headers=H).status_code == 200
        assert client.get(path, headers=H).status_code == 200
        assert client.post(path + "/take", headers=H).status_code == 200
        assert client.post(path + "/status", headers=H, json={"status": "OPEN", "expected_version": 2}).status_code == 200
        assert client.post(path + "/notes", headers=H, json={"body": "Ekip notu"}).status_code == 200
        assert client.post(path + "/release", headers=H).status_code == 200
    assert all(sql.strip().startswith("SELECT") for sql in pool.queries if "application_state_snapshots" in sql)


def test_owner_works_and_audit_contains_only_ids(support):
    client, pool = support
    id_ = load_case(client)
    owner = headers("owner", "OWNER")
    assert client.get(f"/api/mod/support/cases/{id_}", headers=owner).status_code == 200
    assert client.post(f"/api/mod/support/cases/{id_}/take", headers=owner).status_code == 200
    assert client.post(f"/api/mod/support/cases/{id_}/notes", headers=owner, json={"body": "private@example.test secret-note"}).status_code == 200
    assert [row["action"] for row in pool.audits] == ["support.case.viewed", "support.case.taken", "support.note.added"]
    for row in pool.audits:
        assert row["target_type"] == "SUPPORT_CASE" and row["target_id"] == id_
        assert json.loads(row["before"]) == json.loads(row["after"]) == {}
    assert "private@example.test" not in str(pool.audits) and "secret-note" not in str(pool.audits)


def test_two_moderators_take_atomically_only_one_succeeds(support):
    client, pool = support
    id_ = load_case(client)
    def take(actor):
        return client.post(f"/api/mod/support/cases/{id_}/take", headers=headers(actor, "MODERATOR")).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(take, ["moderator", "moderator2"]))
    assert sorted(results) == [200, 409]
    assert pool.cases["legacy-1"]["version"] == 2
    assert len(pool.audits) == 1


def test_assignee_and_version_guards_and_all_write_audits(support):
    client, pool = support
    path = f"/api/mod/support/cases/{load_case(client)}"
    for suffix, body in (("/notes", {"body": "Not"}), ("/status", {"status": "OPEN", "expected_version": 1}), ("/release", None)):
        assert client.post(path + suffix, headers=H, json=body).status_code == 409
    assert client.post(path + "/take", headers=H).status_code == 200
    other = headers("moderator2", "MODERATOR")
    for suffix, body in (("/notes", {"body": "Not"}), ("/status", {"status": "OPEN", "expected_version": 2}), ("/release", None)):
        assert client.post(path + suffix, headers=other, json=body).status_code == 409
    assert client.post(path + "/status", headers=H, json={"status": "WAITING", "expected_version": 1}).status_code == 409
    assert client.post(path + "/status", headers=H, json={"status": "WAITING", "expected_version": 2}).status_code == 200
    assert client.post(path + "/notes", headers=H, json={"body": "<img src=x onerror=alert(1)>"}).status_code == 200
    assert client.get(path, headers=H).json()["notes"][0]["body"] == "<img src=x onerror=alert(1)>"
    assert client.post(path + "/release", headers=H).status_code == 200
    assert [r["action"] for r in pool.audits] == ["support.case.taken", "support.case.status_changed", "support.note.added", "support.case.viewed", "support.case.released"]


@pytest.mark.parametrize("body", ["", "   ", "a" * 2001])
def test_blank_or_overlong_note_is_rejected(support, body):
    client, _ = support
    id_ = load_case(client)
    assert client.post(f"/api/mod/support/cases/{id_}/notes", headers=H, json={"body": body}).status_code == 422


def test_note_exactly_2000_characters_is_accepted(support):
    client, pool = support
    path = f"/api/mod/support/cases/{load_case(client)}"
    assert client.post(path + "/take", headers=H).status_code == 200
    body = "Bir ekip notu. " * 133 + "x" * 5
    assert len(body) == 2000
    result = client.post(path + "/notes", headers=H, json={"body": body})
    assert result.status_code == 200
    assert pool.notes[0]["body"] == body

@pytest.mark.parametrize("suffix,body", [
    ("/take", None), ("/release", None), ("/status", {"status": "OPEN", "expected_version": 2}),
    ("/notes", {"body": "Private note"}), ("", None),
])
def test_audit_failure_rolls_back_writes_and_detail_returns_no_data(support, suffix, body, caplog):
    client, pool = support
    path = f"/api/mod/support/cases/{load_case(client)}"
    if suffix not in ("", "/take"):
        assert client.post(path + "/take", headers=H).status_code == 200
    before = copy.deepcopy((pool.cases, pool.notes, pool.audits))
    pool.audit_fail = True
    result = client.request("POST" if suffix else "GET", path + suffix, headers=H, json=body)
    assert result.status_code == 503
    assert (pool.cases, pool.notes, pool.audits) == before
    assert "message" not in result.json() and "notes" not in result.json()
    assert "private@example.test" not in caplog.text and "secret-token" not in caplog.text


@pytest.mark.parametrize("value", [
    "sk_live_aBcDef123456", "pk_test_ZyX987", "Bearer aBcDeF0123456789", "4242424242424242",
    "4242 4242 4242 4242", "1234567890123", "1234567890123456789",
    "aBcD1234EfGh5678IjKl9012MnOp3456", "api_key=private", "secret: confidential",
])
def test_secret_patterns_are_redacted(value):
    masked = mask_support_text("Bilgi " + value + " son")
    assert "[GİZLİ]" in masked and value not in masked


def test_detail_masks_subject_message_legacy_note_and_notes_before_response(support):
    client, pool = support
    id_ = load_case(client)
    row = pool.cases["legacy-1"]
    secret = "sk_live_aBcDef123456"
    row.update(subject=secret, message=secret, legacy_response_note=secret)
    assert client.post(f"/api/mod/support/cases/{id_}/take", headers=H).status_code == 200
    assert client.post(f"/api/mod/support/cases/{id_}/notes", headers=H, json={"body": secret}).status_code == 200
    result = client.get(f"/api/mod/support/cases/{id_}", headers=H)
    assert result.status_code == 200 and secret not in result.text
    assert result.json()["message"] == result.json()["notes"][0]["body"] == "[GİZLİ]"
    assert set(result.json()["customer"]) == {"user_id", "email_masked", "role", "active", "created_at", "email_verified", "mfa_enabled"}


@pytest.mark.parametrize("target", ["owner", "moderator", "moderator2", "erased"])
def test_staff_self_and_erased_cases_are_hidden(support, target):
    client, pool = support
    id_ = load_case(client)
    pool.cases["legacy-1"]["user_id"] = "customer" if target == "erased" else target
    if target == "erased":
        pool.erased.add("customer")
    assert client.get(f"/api/mod/support/cases/{id_}", headers=H).status_code == 404
    assert client.post(f"/api/mod/support/cases/{id_}/take", headers=H).status_code == 404


def test_summary_real_counts_null_on_failure_and_shared_customer_rate_limit(support):
    client, pool = support
    id_ = load_case(client)
    assert client.get("/api/mod/support/summary", headers=H).json() == {"open_cases": 1, "unassigned": 1, "assigned_to_me": 0}
    client.post(f"/api/mod/support/cases/{id_}/take", headers=H)
    assert client.get("/api/mod/support/summary", headers=H).json()["assigned_to_me"] == 1
    pool.summary_fail = True
    assert client.get("/api/mod/support/summary", headers=H).json() == {"open_cases": None, "unassigned": None, "assigned_to_me": None}
    pool.summary_fail = False
    from app.moderator_customers import router as customers_router
    client.app.include_router(customers_router)
    pool.permissions["moderator"]["customers.view"] = {}
    for _ in range(25):
        assert client.get("/api/mod/customers", headers=H).status_code == 200
    result = client.get("/api/mod/support/summary", headers=H)
    assert result.status_code == 429 and result.headers["Retry-After"] == "60"


def test_actor_is_rechecked_in_transaction_with_verified_email(support):
    client, pool = support
    load_case(client)
    sql = next(sql for sql in pool.queries if "AS allowed" in sql)
    assert "AS email_verified" in sql
    original = pool.fetchrow
    async def revoked(sql, *args):
        if "AS allowed" in sql:
            pool.users["moderator"]["security"]["role"] = "CUSTOMER"
        return await original(sql, *args)
    pool.fetchrow = revoked
    response = client.get("/api/mod/support/summary", headers=H)
    assert response.status_code == 403


def test_transaction_commit_failure_does_not_report_success(support):
    client, pool = support
    path = f"/api/mod/support/cases/{load_case(client)}"
    before = copy.deepcopy((pool.cases, pool.notes, pool.audits))
    pool.commit_fail = True
    assert client.post(path + "/take", headers=H).status_code == 503
    assert (pool.cases, pool.notes, pool.audits) == before


@pytest.mark.parametrize("erased", ["customer", "moderator"])
def test_real_erasure_flow_invokes_tombstone_scrub_model(support, erased):
    client, pool = support
    id_ = load_case(client)
    client.post(f"/api/mod/support/cases/{id_}/take", headers=H)
    client.post(f"/api/mod/support/cases/{id_}/notes", headers=H, json={"body": "Kişisel ekip notu"})
    user = {"id": erased, "email": erased + "@example.test"}
    with patch.object(account_erasure, "table_exists", AsyncMock(return_value=False)):
        asyncio.run(account_erasure.erase_database(client.app, user))
    assert pool.notes[0]["body"] == ""
    if erased == "customer":
        assert pool.cases["legacy-1"]["message"] == pool.cases["legacy-1"]["subject"] == pool.cases["legacy-1"]["legacy_response_note"] == ""
    else:
        assert pool.cases["legacy-1"]["message"] == "Müşteri mesajı"
    assert all(sql.strip().startswith("SELECT") for sql in pool.queries if "application_state_snapshots" in sql)
