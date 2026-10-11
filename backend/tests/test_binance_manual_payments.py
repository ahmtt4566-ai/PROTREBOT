import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from app import binance_manual_payments as payments

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
MIGRATION = Path(__file__).parents[1] / "migrations" / "20261011_001_binance_manual_payments.sql"


class Duplicate(payments.UniqueViolationError):
    def __init__(self, constraint_name):
        self.constraint_name = constraint_name


class Connection:
    def __init__(self):
        self.rows = {}
        self.now = NOW
        self.users = {"user-a", "user-b"}
        self.subscriptions = []
        self.fail = False
        self.expiry_lock_skip = set()
        self._lock = asyncio.Lock()
        self._owner = None

    @asynccontextmanager
    async def transaction(self):
        task = asyncio.current_task()
        outer = self._owner is not task
        if outer:
            await self._lock.acquire()
            self._owner = task
        before = copy.deepcopy(self.rows)
        try:
            yield
        except BaseException:
            self.rows = before
            raise
        finally:
            if outer:
                self._owner = None
                self._lock.release()

    def check(self):
        assert self._owner is asyncio.current_task(), "All payment reads/writes need a transaction"
        if self.fail:
            raise OSError("private financial details")

    async def execute(self, sql, *args):
        self.check()
        assert "pg_advisory_xact_lock" in sql
        assert len(args) == 1
        return "SELECT 1"

    async def fetchval(self, sql, *args):
        self.check()
        if "FROM commercial_auth_users" in sql:
            assert "commercial_erased_users" in sql and "email_verified" in sql
            assert "FOR SHARE" in sql
            return args[0] in self.users
        if "FROM subscriptions" in sql:
            assert "stripe_subscription_id IS NOT NULL" in sql
            assert "plan = 'MASTER_MODE'" in sql and "status = 'ACTIVE'" in sql
            assert "current_period_end > clock_timestamp()" in sql
            return any(row["user_id"] == args[0] and row.get("stripe_subscription_id")
                       and row["plan"] == "MASTER_MODE" and row["status"] == "ACTIVE"
                       and row["current_period_end"] > self.now for row in self.subscriptions)
        if "SELECT $1::timestamptz" in sql:
            return args[0] <= self.now
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        self.check()
        assert "status = 'created'" in sql and "LIMIT 100 FOR UPDATE SKIP LOCKED" in sql
        assert "erased_at IS NULL" in sql
        return copy.deepcopy([row for row in self.rows.values()
                              if row["status"] == "created" and row["expires_at"] <= self.now
                              and row["erased_at"] is None
                              and row["id"] not in self.expiry_lock_skip
                              and (args[0] is None or row["user_id"] == args[0])][:100])

    async def fetchrow(self, sql, *args):
        self.check()
        if "INSERT INTO binance_manual_payment_requests" in sql:
            rid, uid, reference = args
            if any(row["reference_code"] == reference for row in self.rows.values()):
                raise Duplicate("binance_manual_reference_unique")
            assert not any(row["user_id"] == uid and row["status"] in ("created", "pending")
                           for row in self.rows.values())
            row = {
                "id": rid, "user_id": uid, "reference_code": reference, "amount": Decimal("119.90"),
                "currency": "USDT", "method": "binance_manual", "status": "created",
                "transaction_number": None, "transaction_fingerprint": None,
                "created_at": self.now, "expires_at": self.now + timedelta(hours=48),
                "submitted_at": None, "decided_by": None, "decided_at": None,
                "decision_note": None, "version": 1, "erased_at": None,
            }
            self.rows[rid] = row
            return copy.deepcopy(row)
        if "SELECT * FROM binance_manual_payment_requests WHERE user_id" in sql:
            if "ORDER BY created_at DESC,id DESC LIMIT 1" in sql:
                rows = [row for row in self.rows.values() if row["user_id"] == args[0] and row["erased_at"] is None]
                return copy.deepcopy(max(rows, key=lambda row: (row["created_at"], row["id"]))) if rows else None
            assert "status IN ('created','pending') FOR UPDATE" in sql
            return next((copy.deepcopy(row) for row in self.rows.values()
                         if row["user_id"] == args[0] and row["status"] in ("created", "pending")), None)
        if "SELECT * FROM binance_manual_payment_requests WHERE id" in sql:
            assert "user_id = $2" in sql and "erased_at IS NULL FOR UPDATE" in sql
            row = self.rows.get(args[0])
            return copy.deepcopy(row) if row and row["user_id"] == args[1] and row["erased_at"] is None else None
        if "UPDATE binance_manual_payment_requests" in sql:
            rid, version, status, number, fingerprint = args
            row = self.rows[rid]
            if row["version"] != version or row["status"] != "created":
                return None
            if status == "pending" and row["expires_at"] <= self.now:
                return None
            if status == "expired" and row["expires_at"] > self.now:
                return None
            if fingerprint and any(r["transaction_fingerprint"] == fingerprint for r in self.rows.values()):
                raise Duplicate("binance_manual_fingerprint_unique")
            row.update(status=status, transaction_number=number, transaction_fingerprint=fingerprint,
                       submitted_at=self.now if status == "pending" else None, version=version + 1)
            return copy.deepcopy(row)
        raise AssertionError(sql)


def run(coroutine):
    return asyncio.run(coroutine)


def test_new_request_has_exact_amount_currency_method_reference_and_48_hours():
    conn = Connection()
    item = run(payments.create_request(conn, "user-a"))
    assert item.amount == Decimal("119.90")
    assert item.currency == "USDT" and item.method == "binance_manual"
    assert item.status == "created" and item.transaction_number is None
    assert item.expires_at - item.created_at == timedelta(hours=48)
    assert item.reference_code.startswith("KT-") and len(item.reference_code) == 9
    assert all(char in payments.REFERENCE_ALPHABET for char in item.reference_code[3:])


def test_repeated_and_concurrent_creation_return_one_request():
    conn = Connection()

    async def create():
        rows = await asyncio.gather(*(payments.create_request(conn, "user-a") for _ in range(5)))
        assert len({row.id for row in rows}) == 1
    run(create())
    assert len(conn.rows) == 1


@pytest.mark.parametrize("status,plan,days,stripe_id,blocked", [
    ("ACTIVE", "MASTER_MODE", 10, "sub_paid", True),
    ("TRIALING", "TRIAL", 7, "sub_trial", False),
    ("ACTIVE", "MASTER_MODE", -1, "sub_expired", False),
    ("CANCELLED", "MASTER_MODE", 10, "sub_cancelled", False),
    ("PAST_DUE", "MASTER_MODE", 10, "sub_due", False),
    ("ACTIVE", "MASTER_MODE", 10, None, False),
])
def test_only_unexpired_paid_active_stripe_blocks_creation(status, plan, days, stripe_id, blocked):
    conn = Connection()
    conn.subscriptions = [{"user_id": "user-a", "status": status, "plan": plan,
                           "stripe_subscription_id": stripe_id, "current_period_end": NOW + timedelta(days=days)}]
    before = copy.deepcopy(conn.subscriptions)
    if blocked:
        with pytest.raises(payments.HTTPException) as failure:
            run(payments.create_request(conn, "user-a"))
        assert failure.value.status_code == 409 and conn.rows == {}
    else:
        assert run(payments.create_request(conn, "user-a")).status == "created"
    assert conn.subscriptions == before


def test_unverified_inactive_or_erased_user_cannot_create():
    conn = Connection()
    with pytest.raises(payments.HTTPException) as failure:
        run(payments.create_request(conn, "missing"))
    assert failure.value.status_code == 403 and conn.rows == {}


def test_submit_is_pending_idempotent_and_does_not_grant_access():
    conn = Connection()

    async def submit():
        item = await payments.create_request(conn, "user-a")
        submitted = await payments.submit_transaction(conn, "user-a", item.id, " 123-Binance_456 ")
        repeated = await payments.submit_transaction(conn, "user-a", item.id, "123-Binance_456")
        assert submitted == repeated
        assert submitted.status == "pending" and submitted.version == 2
        assert submitted.submitted_at == NOW
        assert submitted.decided_by is None and submitted.decided_at is None
    run(submit())
    assert conn.subscriptions == []


@pytest.mark.parametrize("number", ["", " ", "123/456", "ö123", "a" * 129, "123\n456"])
def test_invalid_transaction_is_rejected_explicitly(number):
    with pytest.raises(payments.HTTPException) as failure:
        payments.normalize_transaction(number)
    assert failure.value.status_code == 422


def test_other_users_request_is_not_found():
    conn = Connection()

    async def submit():
        item = await payments.create_request(conn, "user-a")
        with pytest.raises(payments.HTTPException) as failure:
            await payments.submit_transaction(conn, "user-b", item.id, "number")
        assert failure.value.status_code == 404
        assert conn.rows[item.id]["status"] == "created"
    run(submit())


def test_concurrent_duplicate_transaction_is_accepted_by_exactly_one_user():
    conn = Connection()

    async def submit():
        a = await payments.create_request(conn, "user-a")
        b = await payments.create_request(conn, "user-b")
        results = await asyncio.gather(
            payments.submit_transaction(conn, "user-a", a.id, "shared-number"),
            payments.submit_transaction(conn, "user-b", b.id, "shared-number"), return_exceptions=True,
        )
        assert sum(isinstance(result, payments.PaymentRequest) for result in results) == 1
        errors = [result for result in results if isinstance(result, payments.HTTPException)]
        assert len(errors) == 1 and errors[0].status_code == 409
    run(submit())
    assert sorted(row["status"] for row in conn.rows.values()) == ["created", "pending"]


def test_transaction_remains_reserved_after_rejection_or_erasure():
    conn = Connection()

    async def submit():
        a = await payments.create_request(conn, "user-a")
        await payments.submit_transaction(conn, "user-a", a.id, "reserved")
        # Simulate a future audited decision, then privacy erasure: fingerprint survives.
        conn.rows[a.id].update(status="rejected", user_id="ERASED", erased_at=NOW, transaction_number=None)
        b = await payments.create_request(conn, "user-b")
        with pytest.raises(payments.HTTPException) as failure:
            await payments.submit_transaction(conn, "user-b", b.id, "reserved")
        assert failure.value.status_code == 409
    run(submit())


@pytest.mark.parametrize("elapsed,expected", [(47, "pending"), (48, "expired"), (49, "expired")])
def test_exact_48_hour_boundary_and_expiry_commit_before_error(elapsed, expected):
    conn = Connection()

    async def submit():
        item = await payments.create_request(conn, "user-a")
        conn.now = NOW + timedelta(hours=elapsed)
        if expected == "expired":
            with pytest.raises(payments.HTTPException) as failure:
                await payments.submit_transaction(conn, "user-a", item.id, "late")
            assert failure.value.status_code == 410
        else:
            await payments.submit_transaction(conn, "user-a", item.id, "on-time")
        assert conn.rows[item.id]["status"] == expected
    run(submit())


def test_expiry_only_changes_unsubmitted_requests_and_allows_new_reference():
    conn = Connection()

    async def expire():
        a = await payments.create_request(conn, "user-a")
        b = await payments.create_request(conn, "user-b")
        await payments.submit_transaction(conn, "user-b", b.id, "on-time")
        conn.now += timedelta(hours=48)
        assert await payments.expire_due(conn) == 1
        assert await payments.expire_due(conn) == 0
        new = await payments.create_request(conn, "user-a")
        assert new.id != a.id and new.reference_code != a.reference_code
        assert conn.rows[b.id]["status"] == "pending"
    run(expire())


def test_paid_stripe_activated_after_request_blocks_submission():
    conn = Connection()

    async def submit():
        item = await payments.create_request(conn, "user-a")
        conn.subscriptions = [{"user_id": "user-a", "status": "ACTIVE", "plan": "MASTER_MODE",
                               "stripe_subscription_id": "sub", "current_period_end": NOW + timedelta(days=30)}]
        with pytest.raises(payments.HTTPException) as failure:
            await payments.submit_transaction(conn, "user-a", item.id, "number")
        assert failure.value.status_code == 409 and conn.rows[item.id]["status"] == "created"
    run(submit())


def test_expiry_sweep_skips_erased_requests_without_aborting_other_users():
    conn = Connection()

    async def expire():
        a = await payments.create_request(conn, "user-a")
        b = await payments.create_request(conn, "user-b")
        conn.rows[a.id].update(user_id="ERASED", erased_at=NOW)
        conn.now += timedelta(hours=48)
        assert await payments.expire_due(conn) == 1
        assert conn.rows[a.id]["status"] == "created"
        assert conn.rows[b.id]["status"] == "expired"
    run(expire())


def test_reference_collision_retries_in_savepoint():
    conn = Connection()

    async def create():
        with patch.object(payments, "new_reference", side_effect=["KT-ABC234", "KT-ABC234", "KT-DEF567"]):
            await payments.create_request(conn, "user-a")
            b = await payments.create_request(conn, "user-b")
        assert b.reference_code == "KT-DEF567"
    run(create())


def test_exhausted_reference_collisions_surface_storage_error(caplog):
    conn = Connection()

    async def create():
        with patch.object(payments, "new_reference", return_value="KT-ABC234"):
            await payments.create_request(conn, "user-a")
            with pytest.raises(payments.HTTPException) as failure:
                await payments.create_request(conn, "user-b")
        assert failure.value.status_code == 503
    run(create())
    assert len(conn.rows) == 1 and "reference collision" in caplog.text


def test_storage_error_is_explicit_and_does_not_leak_details(caplog):
    conn = Connection()
    conn.fail = True
    with pytest.raises(payments.HTTPException) as failure:
        run(payments.create_request(conn, "user-a"))
    assert failure.value.status_code == 503 and conn.rows == {}
    assert "storage failed type=OSError" in caplog.text
    assert "private financial details" not in caplog.text


@pytest.mark.parametrize("trial_days,manual_days,start_days", [
    (7, None, 7), (None, 20, 20), (7, 20, 20), (-1, -2, 0), (None, None, 0),
])
def test_30_day_period_preserves_remaining_trial_and_manual_days(trial_days, manual_days, start_days):
    start, end = payments.payment_period(
        NOW, trial_end=NOW + timedelta(days=trial_days) if trial_days is not None else None,
        manual_end=NOW + timedelta(days=manual_days) if manual_days is not None else None,
    )
    assert start == NOW + timedelta(days=start_days)
    assert end - start == timedelta(days=30)


def test_period_rejects_naive_dates():
    with pytest.raises(ValueError, match="timezone-aware"):
        payments.payment_period(NOW, trial_end=NOW.replace(tzinfo=None))


def test_migration_is_additive_unique_immutable_and_unwired():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "amount = 119.90" in sql and "currency = 'USDT'" in sql
    for name in ("binance_manual_reference_unique", "binance_manual_transaction_unique",
                 "binance_manual_fingerprint_unique", "binance_manual_one_open_user"):
        assert name in sql
    assert "INTERVAL '48 hours'" in sql
    assert "OLD.status <> 'created'" in sql and "NEW.version <> OLD.version + 1" in sql
    assert "NEW.status = 'pending'" in sql and "NEW.status = 'expired'" in sql
    assert "Manual payment transition is not enabled" in sql
    assert "BEFORE DELETE OR TRUNCATE" in sql
    assert "transaction_fingerprint" not in sql.split("pg_trigger_depth() > 1", 1)[1].split("RETURN NEW;", 1)[0]
    assert "ALTER TABLE subscriptions" not in sql and "INSERT INTO subscriptions" not in sql
    assert "application_state_snapshots" not in sql
    assert sql.strip().endswith("COMMIT;")
    main = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert MIGRATION.name not in main
    assert "app.include_router(binance_manual_router)" in main
