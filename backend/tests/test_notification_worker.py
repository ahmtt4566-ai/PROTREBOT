import asyncio
import copy
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app import notification_worker as worker
from app.email_service import EmailDeliveryError
from test_notification_outbox import OutboxPool, notifications
from test_approval_requests import create
from test_moderator_role import setup
schedule_sweep = worker.schedule_sweep


class WorkerPool(OutboxPool):
    async def execute(self, sql, *args):
        if "UPDATE notification_outbox SET status = CASE" in sql:
            self.check(sql)
            for row in self.outbox.values():
                if row["status"] == "sending" and row["next_attempt_at"] <= self.now:
                    row["status"] = "dead" if row["attempts"] >= 8 else "pending"
                    row["last_error_code"] = "attempts_exhausted" if row["attempts"] >= 8 else "lease_expired"
            return "UPDATE 1"
        if "UPDATE notification_outbox SET status = $3" in sql:
            self.check(sql)
            row = self.outbox[args[0]]
            if row["status"] != "sending" or row["attempts"] != args[1]:
                return "UPDATE 0"
            row.update(status=args[2], last_error_code=args[3], next_attempt_at=self.now + timedelta(seconds=args[4]),
                       sent_at=self.now if args[2] == "sent" else None)
            return "UPDATE 1"
        return await super().execute(sql, *args)

    async def fetchrow(self, sql, *args):
        if "WITH due AS" in sql:
            self.check(sql)
            candidates = [r for r in self.outbox.values() if r["status"] in ("pending", "failed")
                          and r["attempts"] < 8 and r["next_attempt_at"] <= self.now]
            if not candidates:
                return None
            row = candidates[0]
            row.update(status="sending", attempts=row["attempts"] + 1, next_attempt_at=self.now + timedelta(seconds=args[0]))
            if row["kind"] == "approval.pending_digest" and "count" not in row["payload"]:
                row["payload"]["count"] = sum(r["status"] == "pending" for r in self.approvals.values())
            return copy.deepcopy(row)
        if "SELECT * FROM notification_outbox WHERE id" in sql:
            self.check(sql)
            row = self.outbox.get(args[0])
            return copy.deepcopy(row) if row and row["status"] == "sending" and row["attempts"] == args[1] else None
        if "SELECT security->>'email' AS email" in sql:
            self.check(sql)
            row = self.users.get(args[0])
            value = row["security"] if row else {}
            if args[0] in self.erased or not value.get("active") or not value.get("email_verified") or value.get("role") not in ("OWNER", "MODERATOR"):
                return None
            return {"email": value["email"]}
        return await super().fetchrow(sql, *args)


@pytest.fixture
def worker_notifications(notifications):
    client, previous = notifications
    pool = WorkerPool(client.app.state.v22_commercial["state"]["users"])
    pool.settings = previous.settings
    pool.permissions = previous.permissions
    client.app.state.db_pool = pool
    yield client, pool


def run(client, **options):
    asyncio.run(worker.process_due(client.app, **options))


def test_two_workers_do_not_send_twice_and_sent_rows_are_not_mutated(worker_notifications):
    client, pool = worker_notifications
    create(client)
    async def concurrent():
        await asyncio.gather(worker.process_due(client.app), worker.process_due(client.app))
    with patch.object(worker, "send_notification") as send:
        asyncio.run(concurrent())
        assert send.call_count == 1
        before = copy.deepcopy(pool.outbox)
        run(client)
        assert send.call_count == 1 and pool.outbox == before
    assert all(r["status"] == "sent" for r in pool.outbox.values())
    assert any("FOR UPDATE SKIP LOCKED" in sql for sql in pool.queries)


def test_failures_advance_attempts_follow_backoff_and_eighth_failure_is_dead(worker_notifications, caplog):
    client, pool = worker_notifications
    create(client)
    row = next(iter(pool.outbox.values()))
    with patch.object(worker, "send_notification", side_effect=EmailDeliveryError(
        "private@example.test secret-token", provider="resend", code="provider_timeout",
    )) as send:
        for attempt in range(1, 9):
            now = pool.now
            run(client)
            assert row["attempts"] == attempt
            assert row["next_attempt_at"] == now + timedelta(seconds=worker.BACKOFF_SECONDS[min(attempt - 1, 4)])
            assert row["status"] == ("dead" if attempt == 8 else "failed")
            assert row["last_error_code"] == "provider_timeout"
            pool.now = row["next_attempt_at"]
        run(client)
    assert send.call_count == 8
    assert "private@example.test" not in caplog.text and "secret-token" not in str(pool.outbox)


def test_provider_unavailable_is_retryable_never_sent(worker_notifications):
    client, pool = worker_notifications
    create(client)
    with patch.object(worker, "send_notification", side_effect=EmailDeliveryError(
        "disabled", provider="resend", code="provider_unavailable",
    )):
        run(client)
    row = next(iter(pool.outbox.values()))
    assert row["status"] == "failed" and row["sent_at"] is None and row["last_error_code"] == "provider_unavailable"


def test_crashed_sending_is_recovered_and_fenced_lease_cannot_resend(worker_notifications):
    client, pool = worker_notifications
    create(client)
    row = next(iter(pool.outbox.values()))
    old_lease = None
    async def crash():
        nonlocal old_lease
        async with pool.acquire() as conn, conn.transaction():
            old_lease = await worker.claim(conn)
    asyncio.run(crash())
    assert row["status"] == "sending"
    pool.now += timedelta(seconds=worker.LEASE_SECONDS + 1)
    with patch.object(worker, "send_notification") as send:
        run(client)
        async def old_sender():
            async with pool.acquire() as conn, conn.transaction():
                await worker.deliver(conn, old_lease)
        asyncio.run(old_sender())
        assert send.call_count == 1
    assert row["status"] == "sent" and row["attempts"] == 2


def test_erased_recipient_is_scrubbed_by_real_erasure_and_never_sent(worker_notifications):
    client, pool = worker_notifications
    row = create(client)
    # Decision goes to requester, not the OWNER digest.
    from app.notification_outbox import enqueue_decision
    async def decision():
        async with pool.transaction():
            await enqueue_decision(pool, {"id": row["id"], "requester_user_id": "moderator", "status": "rejected"})
    asyncio.run(decision())
    from app import account_erasure
    with patch.object(account_erasure, "table_exists", AsyncMock(return_value=False)):
        asyncio.run(account_erasure.erase_database(client.app, {"id": "moderator", "email": "moderator@example.test"}))
    erased = next(r for r in pool.outbox.values() if r["kind"] == "approval.decision")
    assert erased["status"] == "dead" and erased["payload"] == {} and erased["recipient_user_id"] == "ERASED"
    with patch.object(worker, "send_notification") as send:
        run(client)
    assert all(call.kwargs["to_email"] != "moderator@example.test" for call in send.call_args_list)


def test_sweep_is_bounded_to_ten_and_runtime_scheduling_is_coalesced(worker_notifications):
    client, pool = worker_notifications
    create(client)
    template = next(iter(pool.outbox.values()))
    for i in range(15):
        pool.outbox[f"row-{i}"] = {**copy.deepcopy(template), "id": f"row-{i}", "dedupe_key": f"dedupe-{i}"}
    with patch.object(worker, "send_notification") as send:
        run(client, limit=100)
        assert send.call_count == 10
    async def triggers():
        with patch.object(worker, "process_due", AsyncMock()) as process:
            schedule_sweep(client.app); schedule_sweep(client.app)
            await client.app.state.notification_task
            schedule_sweep(client.app)
            assert process.await_count == 1
    asyncio.run(triggers())


def test_digest_count_is_frozen_on_first_attempt_for_provider_idempotency(worker_notifications):
    client, pool = worker_notifications
    create(client)
    row = next(iter(pool.outbox.values()))
    with patch.object(worker, "send_notification", side_effect=EmailDeliveryError("timeout", provider="resend", code="provider_timeout")):
        run(client)
    assert row["payload"]["count"] == 1
    pool.approvals["another"] = {**next(iter(pool.approvals.values())), "id": "another"}
    pool.now = row["next_attempt_at"]
    with patch.object(worker, "send_notification") as send:
        run(client)
    assert send.call_args.kwargs["payload"]["count"] == 1


def test_missing_recipient_or_exhausted_crash_never_sends(worker_notifications):
    client, pool = worker_notifications
    create(client)
    row = next(iter(pool.outbox.values()))
    row.update(status="sending", attempts=8, next_attempt_at=pool.now - timedelta(seconds=1))
    with patch.object(worker, "send_notification") as send:
        run(client)
        assert send.call_count == 0
    assert row["status"] == "dead" and row["last_error_code"] == "attempts_exhausted"


def test_provider_crash_and_database_failure_cannot_escape_background_worker(worker_notifications, caplog):
    client, pool = worker_notifications
    create(client)
    with patch.object(worker, "send_notification", side_effect=RuntimeError("private@example.test")):
        run(client)
    assert next(iter(pool.outbox.values()))["last_error_code"] == "delivery_error"
    pool.fail = True
    run(client)
    assert "Notification sweep failed (RuntimeError)" in caplog.text
    assert "private@example.test" not in caplog.text
