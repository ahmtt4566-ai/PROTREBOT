import copy
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.moderator_approvals import router
from test_moderator_role import headers, setup
from test_moderator_support import SupportPool


class ApprovalPool(SupportPool):
    def __init__(self, users):
        super().__init__(users)
        self.approvals = {}
        self.subscription_active = False
        self.canonical_changes = 0
        self.fail_action = None

    @asynccontextmanager
    async def transaction(self):
        previous = copy.deepcopy((self.approvals, self.canonical_changes))
        try:
            async with super().transaction():
                yield
        except BaseException:
            self.approvals, self.canonical_changes = previous
            raise

    async def execute(self, sql, *args):
        if "INSERT INTO commercial_auth_users" in sql:
            self.check(sql)
            assert args[0] in self.users, "Approval must not create a new canonical user"
            return "INSERT 0 0"
        if "INSERT INTO audit_log" in sql and args[2] == self.fail_action:
            raise RuntimeError("private@example.test secret-token")
        if "INSERT INTO commercial_erased_users" in sql:
            for row in self.approvals.values():
                from app.account_erasure import user_hash
                if args[0] in (user_hash(row["target_user_id"]), user_hash(row["requester_user_id"])):
                    row.update(reason="", decision_note="")
        return await super().execute(sql, *args)

    def filtered(self, sql, args):
        sql = sql.split("FROM approval_requests", 1)[-1]
        rows = list(self.approvals.values())
        if "requester_user_id = $1" in sql:
            rows = [r for r in rows if r["requester_user_id"] == args[0]]
        elif "target_user_id = $1" in sql:
            rows = [r for r in rows if r["target_user_id"] == args[0]]
        if "AND status = 'pending'" in sql or "WHERE status = 'pending'" in sql:
            rows = [r for r in rows if r["status"] == "pending"]
        if "($2::text IS NULL OR status = $2)" in sql and args[1] is not None:
            rows = [r for r in rows if r["status"] == args[1]]
        if "($1::text IS NULL OR status = $1)" in sql and args[0] is not None:
            rows = [r for r in rows if r["status"] == args[0]]
        return rows

    async def fetchrow(self, sql, *args):
        if "SELECT user_id FROM moderator_permissions" in sql:
            self.check(sql)
            return {"user_id": args[0]} if args[1] in self.permissions.get(args[0], {}) else None
        if "INSERT INTO approval_requests" in sql:
            self.check(sql)
            now = datetime.now(timezone.utc)
            row = dict(zip(("id", "action_type", "target_user_id", "requester_user_id", "target_snapshot", "reason"), args))
            row.update(requester_role="MODERATOR", payload={}, status="pending", decided_by=None, decided_at=None,
                       decision_note=None, executed_at=None, result_code=None, expires_at=now + timedelta(hours=72),
                       version=1, created_at=now)
            assert all(r["target_user_id"] != row["target_user_id"] or r["action_type"] != row["action_type"]
                       or r["status"] != "pending" for r in self.approvals.values())
            self.approvals[row["id"]] = row
            return copy.deepcopy(row)
        if "SELECT * FROM approval_requests WHERE id" in sql:
            self.check(sql)
            return copy.deepcopy(self.approvals.get(args[0]))
        if "UPDATE approval_requests SET" in sql:
            self.check(sql)
            row = self.approvals[args[0]]
            if row["version"] != args[5]:
                return None
            assert args[2] is None or args[2] != row["requester_user_id"], "DB CHECK self approval"
            row.update(status=args[1], decided_by=args[2], decision_note=args[3], result_code=args[4], version=row["version"] + 1)
            if row["decided_by"] and row["decided_at"] is None:
                row["decided_at"] = datetime.now(timezone.utc)
            if row["status"] == "executed":
                row["executed_at"] = datetime.now(timezone.utc)
            return copy.deepcopy(row)
        if "COUNT(*) FILTER" in sql and "FROM approval_requests" in sql:
            self.check(sql)
            rows = self.filtered(sql, args)
            today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            return {"pending": sum(r["status"] == "pending" for r in rows), "today": sum(r["created_at"] >= today for r in rows)}
        if sql.strip().startswith("SELECT user_id, auth_version, security"):
            self.check(sql)
            return {"user_id": args[0], **copy.deepcopy(self.users[args[0]])} if args[0] in self.users and args[0] not in self.erased else None
        if "AS requester_allowed" in sql:
            self.check(sql)
            row = self.users.get(args[0])
            return None if not row else {"role": row["security"]["role"], "active": row["security"]["active"],
                                        "allowed": "approvals.create" in self.permissions.get(args[0], {})}
        if "UPDATE commercial_auth_users" in sql:
            row = await super().fetchrow(sql, *args)
            if row is not None:
                self.canonical_changes += 1
            return row
        return await super().fetchrow(sql, *args)

    async def fetch(self, sql, *args):
        if "FROM protrebot_exchange_session_vault" in sql:
            self.check(sql)
            return []
        if "FROM approval_requests WHERE status = 'pending' AND expires_at" in sql:
            self.check(sql)
            now = datetime.now(timezone.utc)
            return copy.deepcopy([r for r in self.approvals.values() if r["status"] == "pending" and r["expires_at"] <= now
                                  and (args[0] or r["requester_user_id"] == args[1] or r["target_user_id"] == args[2])])
        if "SELECT * FROM approval_requests" in sql:
            self.check(sql)
            rows = sorted(self.filtered(sql, args), key=lambda r: r["created_at"], reverse=True)
            return copy.deepcopy(rows[args[-1]:args[-1] + args[-2]])
        return await super().fetch(sql, *args)

    async def fetchval(self, sql, *args):
        if "COUNT(*) FROM approval_requests" in sql:
            self.check(sql)
            return len(self.filtered(sql, args))
        if "SELECT $1::timestamptz" in sql:
            self.check(sql)
            return args[0] <= datetime.now(timezone.utc)
        if "EXISTS" in sql and "FROM subscriptions" in sql:
            self.check(sql)
            return self.subscription_active
        return await super().fetchval(sql, *args)


@pytest.fixture
def approvals(setup):
    client, _ = setup
    pool = ApprovalPool(client.app.state.v22_commercial["state"]["users"])
    pool.settings["moderator"] = {"two_factor_enabled": True}
    pool.permissions["moderator"] = {"approvals.create": {}}
    client.app.state.db_pool = pool
    client.app.include_router(router)
    yield client, pool


H = headers("moderator", "MODERATOR")
BODY = {"action_type": "account.deactivate", "target_user_id": "customer", "reason": "Hesap güvenliği için inceleme."}


def create(client, **changes):
    result = client.post("/api/mod/approvals", headers=H, json={**BODY, **changes})
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.parametrize("method,path,body", [
    ("POST", "", BODY), ("GET", "", None), ("POST", "/missing/cancel", None),
])
def test_moderator_routes_require_permission_mfa_and_reject_customer_owner(approvals, method, path, body):
    client, pool = approvals
    url = "/api/mod/approvals" + path
    for uid, role in (("customer", "CUSTOMER"), ("owner", "OWNER")):
        assert client.request(method, url, headers=headers(uid, role), json=body).status_code == 403
    pool.permissions["moderator"] = {}
    assert client.request(method, url, headers=H, json=body).status_code == 403
    pool.permissions["moderator"] = {"approvals.create": {}}
    pool.settings["moderator"]["two_factor_enabled"] = False
    result = client.request(method, url, headers=H, json=body)
    assert result.status_code == 403 and result.json()["detail"]["code"] == "mfa_required"


def test_creation_is_snapshot_only_no_target_change_and_own_cancel(approvals):
    client, pool = approvals
    before = copy.deepcopy(pool.users["customer"])
    row = create(client)
    assert pool.users["customer"] == before and pool.canonical_changes == 0
    assert row["target_snapshot"] == {"active": True, "role": "CUSTOMER", "auth_version": 1}
    assert (datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])).total_seconds() == 72 * 3600
    assert client.get("/api/mod/approvals", headers=H).json()["pending_count"] == 1
    assert client.post(f"/api/mod/approvals/{row['id']}/cancel", headers=H).json()["status"] == "cancelled"
    assert pool.users["customer"] == before
    assert [r["action"] for r in pool.audits] == ["approval.requested", "approval.cancelled"]
    assert "güvenliği" not in str(pool.audits)


@pytest.mark.parametrize("target", ["owner", "moderator", "missing"])
def test_staff_self_targets_are_hidden(approvals, target):
    client, _ = approvals
    assert client.post("/api/mod/approvals", headers=H, json={**BODY, "target_user_id": target}).status_code == 404


def test_duplicate_and_already_desired_state_never_increase_auth_version(approvals):
    client, pool = approvals
    create(client)
    assert client.post("/api/mod/approvals", headers=H, json=BODY).status_code == 409
    pool.users["customer"]["security"]["active"] = False
    assert client.post("/api/mod/approvals", headers=H, json=BODY).status_code == 409
    assert pool.users["customer"]["auth_version"] == 1 and pool.canonical_changes == 0


def test_lazy_expiry_is_audited_and_cancel_cannot_apply_expired_request(approvals):
    client, pool = approvals
    row = create(client)
    pool.approvals[row["id"]]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert client.post(f"/api/mod/approvals/{row['id']}/cancel", headers=H).status_code == 409
    assert pool.approvals[row["id"]]["status"] == "expired"
    assert pool.audits[-1]["action"] == "approval.expired"
    assert client.get("/api/mod/approvals", headers=H).json()["pending_count"] == 0


@pytest.mark.parametrize("limit", ["pending", "today"])
def test_pending_five_and_utc_daily_twenty_limits(approvals, limit):
    client, pool = approvals
    row = create(client)
    for i in range(4 if limit == "pending" else 19):
        copied = copy.deepcopy(pool.approvals[row["id"]])
        copied.update(id=f"other-{i}", target_user_id=f"other-user-{i}", status="pending" if limit == "pending" else "cancelled")
        pool.approvals[copied["id"]] = copied
    pool.users["another"] = copy.deepcopy(pool.users["customer"])
    result = client.post("/api/mod/approvals", headers=H, json={**BODY, "target_user_id": "another"})
    assert result.status_code == 429


def test_create_audit_failure_rolls_back_request_and_no_target_change(approvals):
    client, pool = approvals
    before = copy.deepcopy(pool.users)
    pool.audit_fail = True
    assert client.post("/api/mod/approvals", headers=H, json=BODY).status_code == 503
    assert pool.approvals == {} and pool.users == before


def test_other_moderators_requests_are_not_listed_or_cancellable_and_page_is_bounded(approvals):
    client, pool = approvals
    row = create(client)
    pool.users["other-mod"] = copy.deepcopy(pool.users["moderator"])
    pool.settings["other-mod"] = {"two_factor_enabled": True}
    pool.permissions["other-mod"] = {"approvals.create": {}}
    other = headers("other-mod", "MODERATOR")
    assert client.get("/api/mod/approvals", headers=other).json()["items"] == []
    assert client.post(f"/api/mod/approvals/{row['id']}/cancel", headers=other).status_code == 404
    assert client.get("/api/mod/approvals?limit=51", headers=H).status_code == 422
