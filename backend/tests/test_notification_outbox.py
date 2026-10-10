import copy
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from app import notification_outbox as outbox
from test_approval_requests import ApprovalPool, BODY, H, create
from test_moderator_role import headers, setup


class OutboxPool(ApprovalPool):
    def __init__(self, users):
        super().__init__(users)
        self.outbox = {}
        self.bucket = 100
        self.outbox_fail = False
        self.now = datetime.now(timezone.utc)
        self.delivery_events = []

    @asynccontextmanager
    async def transaction(self):
        before = copy.deepcopy(self.outbox)
        try:
            async with super().transaction():
                yield
        except BaseException:
            self.outbox = before
            raise

    async def execute(self, sql, *args):
        if "INSERT INTO notification_outbox" in sql:
            self.check(sql)
            assert self.is_in_transaction()
            if self.outbox_fail:
                raise RuntimeError("private@example.test reason token")
            if any(row["dedupe_key"] == args[3] for row in self.outbox.values()):
                return "INSERT 0 0"
            digest = "approval.pending_digest" in sql
            self.outbox[args[0]] = {
                "id": args[0], "kind": "approval.pending_digest" if digest else "approval.decision",
                "recipient_user_id": args[1], "payload": json.loads(args[2]), "dedupe_key": args[3],
                "status": "pending", "attempts": 0, "next_attempt_at": self.now,
                "last_error_code": None, "created_at": self.now, "sent_at": None,
            }
            self.delivery_events.append(("enqueued", self.transaction_depth))
            return "INSERT 0 1"
        if "INSERT INTO commercial_erased_users" in sql:
            from app.account_erasure import user_hash
            for row in self.outbox.values():
                if user_hash(row["recipient_user_id"]) == args[0]:
                    row.update(recipient_user_id="ERASED", payload={}, dedupe_key="erased:" + row["id"],
                               status="sent" if row["status"] == "sent" else "dead", last_error_code="recipient_erased")
        return await super().execute(sql, *args)

    async def fetch(self, sql, *args):
        if "SELECT user_id FROM commercial_auth_users WHERE security->>'role'" in sql:
            self.check(sql)
            return [{"user_id": uid} for uid, row in self.users.items()
                    if row["security"]["role"] == "OWNER" and row["security"]["active"] and uid not in self.erased]
        return await super().fetch(sql, *args)

    async def fetchval(self, sql, *args):
        if "floor(extract(epoch" in sql:
            self.check(sql)
            return self.bucket
        return await super().fetchval(sql, *args)


@pytest.fixture
def notifications(setup, monkeypatch):
    client, _ = setup
    pool = OutboxPool(client.app.state.v22_commercial["state"]["users"])
    pool.settings["moderator"] = {"two_factor_enabled": True}
    pool.permissions["moderator"] = {"approvals.create": {}}
    client.app.state.db_pool = pool
    from app.moderator_approvals import router
    client.app.include_router(router)
    yield client, pool


def test_create_enqueues_all_active_owners_inside_same_transaction_and_dedupes_five(notifications):
    client, pool = notifications
    pool.users["owner2"] = copy.deepcopy(pool.users["owner"])
    pool.users["inactive-owner"] = copy.deepcopy(pool.users["owner"])
    pool.users["inactive-owner"]["security"]["active"] = False
    for i in range(5):
        uid = f"target-{i}"
        pool.users[uid] = copy.deepcopy(pool.users["customer"])
        create(client, target_user_id=uid)
    assert len(pool.approvals) == 5 and len(pool.outbox) == 2
    assert {r["recipient_user_id"] for r in pool.outbox.values()} == {"owner", "owner2"}
    assert pool.delivery_events == [("enqueued", 2), ("enqueued", 2)]
    assert all(r["payload"] == {"bucket": 100} and "email" not in r for r in pool.outbox.values())
    assert "example.test" not in str(pool.outbox) and BODY["reason"] not in str(pool.outbox)


def test_enqueue_failure_does_not_rollback_request_and_log_has_no_private_text(notifications, caplog):
    client, pool = notifications
    pool.outbox_fail = True
    create(client)
    assert len(pool.approvals) == 1 and pool.outbox == {}
    assert pool.audits[0]["action"] == "approval.requested"
    assert "Notification enqueue failed (RuntimeError)" in caplog.text
    assert all(private not in caplog.text for private in ("private@example.test", "reason token", BODY["reason"]))


def test_outer_approval_rollback_also_rolls_back_outbox(notifications):
    client, pool = notifications
    pool.commit_fail = True
    assert client.post("/api/mod/approvals", headers=H, json=BODY).status_code == 503
    assert pool.outbox == {} and pool.approvals == {}


def test_decision_is_idempotently_enqueued_with_ids_and_numeric_result(notifications):
    client, pool = notifications
    row = create(client)
    response = client.post(f"/api/v22/admin/approvals/{row['id']}/reject", headers=headers("owner", "OWNER"),
                           json={"decision_note": "İnceleme sonrası red"})
    assert response.status_code == 200
    decision = [r for r in pool.outbox.values() if r["kind"] == "approval.decision"]
    assert len(decision) == 1
    assert decision[0]["payload"] == {"approval_id": row["id"], "result": 2}
    assert "İnceleme" not in str(decision) and "example.test" not in str(decision)
