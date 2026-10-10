import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from app import customer_event_writer as writer
from test_moderator_support import SupportPool


class EventsPool(SupportPool):
    def __init__(self, users):
        super().__init__(users)
        self.events = []
        self.now = datetime.now(timezone.utc)
        self.system_events = []

    @asynccontextmanager
    async def transaction(self):
        old = copy.deepcopy(self.events)
        try:
            async with super().transaction():
                yield
        except BaseException:
            self.events = old
            raise

    async def execute(self, sql, *args):
        if "INSERT INTO customer_events" in sql:
            self.check(sql)
            if args[1] not in self.users or args[1] in self.erased:
                return "INSERT 0 0"
            row = next((r for r in self.events if r["user_id"] == args[1] and
                        (r["kind"], r["code"], r["feature"]) == args[2:5] and r["last_at"] >= self.now - timedelta(minutes=10)), None)
            kind, code, feature, status, ref = args[2:]
            if row is None and sum(r["user_id"] == args[1] and r["first_at"] >= self.now - timedelta(hours=1) for r in self.events) >= 49:
                row = next((r for r in self.events if r["user_id"] == args[1] and r["code"] == "event_limit" and r["first_at"] >= self.now - timedelta(hours=1)), None)
                kind, code, feature, status, ref = "api.error", "event_limit", "events", None, None
            if row:
                row.update(count=row["count"] + 1, last_at=self.now)
                return "INSERT 0 0"
            self.events.append({"id": args[0], "user_id": args[1], "kind": kind, "code": code, "feature": feature,
                                "http_status": status, "request_id": ref, "count": 1, "first_at": self.now, "last_at": self.now})
            return "INSERT 0 1"
        if "DELETE FROM customer_events" in sql:
            self.check(sql)
            old = [r for r in self.events if r["last_at"] < self.now - timedelta(days=90)][:500]
            self.events = [r for r in self.events if r not in old]
            return f"DELETE {len(old)}"
        if "INSERT INTO commercial_erased_users" in sql:
            from app.account_erasure import user_hash
            self.events = [r for r in self.events if user_hash(r["user_id"]) != args[0]]
        return await super().execute(sql, *args)


def environment():
    pool = EventsPool([{"id": "customer", "role": "CUSTOMER", "active": True, "email_verified": True, "auth_version": 1}])
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))
    request = Request({"type": "http", "app": app})
    return request, pool


async def record(request, kind="auth.login_failed", code="invalid_credentials", feature="auth", uid="customer"):
    writer.record_event(request, uid, kind, code, feature, 401)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.gather(*getattr(request.app.state, "customer_event_tasks", ()))


def test_allowlists_reject_unknown_kind_code_feature_without_data_or_private_logging(caplog):
    request, pool = environment()
    for args in [("unknown", "invalid_credentials", "auth"), ("auth.login_failed", "private@example.test", "auth"), ("auth.login_failed", "invalid_credentials", "body-token")]:
        asyncio.run(record(request, *args))
    assert pool.events == [] and pool.queries == []
    assert "recording failed (ValueError)" in caplog.text
    assert "private@example.test" not in caplog.text and "body-token" not in caplog.text


def test_repeats_merge_within_ten_minutes_and_later_repeat_starts_new_row():
    request, pool = environment()
    for _ in range(5):
        asyncio.run(record(request))
    assert len(pool.events) == 1 and pool.events[0]["count"] == 5
    pool.now += timedelta(minutes=11)
    asyncio.run(record(request))
    assert len(pool.events) == 2


def test_two_parallel_writers_aggregate_once_per_event():
    request, pool = environment()
    async def proof():
        await asyncio.gather(*(record(request) for _ in range(10)))
    asyncio.run(proof())
    assert len(pool.events) == 1 and pool.events[0]["count"] == 10


def test_hourly_limit_reserves_one_counter_and_never_adds_a_fifty_first_row():
    request, pool = environment()
    pairs = [(kind, code, feature) for kind, code in writer.EVENT_CODES.items() for feature in writer.FEATURES]
    for args in pairs:
        asyncio.run(record(request, *args))
    assert len(pool.events) == 50
    overflow = next(r for r in pool.events if r["code"] == "event_limit")
    assert overflow["count"] == 11 and overflow["request_id"] is None and overflow["http_status"] is None


def test_absent_user_has_no_row_and_payload_has_no_free_text():
    request, pool = environment()
    asyncio.run(record(request, uid="missing"))
    assert pool.events == []
    asyncio.run(record(request))
    assert set(pool.events[0]) == {"id", "user_id", "kind", "code", "feature", "http_status", "request_id", "count", "first_at", "last_at"}
    assert not any(value in str(pool.events) for value in ("@", "Bearer ", "192.168.", "secret", "message"))


def test_database_failure_is_swallowed_with_type_only_warning(caplog):
    request, pool = environment()
    pool.fail = True
    asyncio.run(record(request))
    assert pool.events == [] and "Customer event write failed (RuntimeError)" in caplog.text
    assert "private" not in caplog.text


def test_retention_and_real_erasure_remove_events():
    request, pool = environment()
    asyncio.run(record(request))
    pool.now += timedelta(days=91)
    asyncio.run(writer.cleanup_events(request.app))
    assert pool.events == []
    asyncio.run(record(request))
    from app import account_erasure
    with patch.object(account_erasure, "table_exists", AsyncMock(return_value=False)):
        asyncio.run(account_erasure.erase_database(request.app, {"id": "customer"}))
    assert pool.events == []


def test_recording_does_not_wait_for_a_slow_storage_task():
    request, _ = environment()
    async def proof():
        entered = asyncio.Event()
        release = asyncio.Event()
        async def blocked(*_):
            entered.set()
            await release.wait()
        with patch.object(writer, "write_event", blocked):
            writer.record_event(request, "customer", "auth.login_failed", "invalid_credentials", "auth")
            assert not entered.is_set()
            await asyncio.sleep(0); await asyncio.sleep(0)
            assert entered.is_set()
            release.set()
            await writer.shutdown_events(request.app)
    asyncio.run(proof())
