"""No database, migration, HTTP, email or model calls: transaction-level fakes only."""
import asyncio
import copy
import json
import unittest
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
from app.chart_ai_store import (
    LEASE_DURATION,
    MONTHLY_BUDGET_MICROUSD,
    POLICY_VERSION,
    PRIVACY_NOTICE,
    REQUEST_RESERVATION_MICROUSD,
    RESULT_RETENTION,
    ChartAiQuotaError,
    ChartAiStorageError,
    ChartAiStore,
    QuotaAccess,
    quota_period,
    user_hash,
)


@dataclass
class State:
    periods: dict = field(default_factory=dict)
    budgets: dict = field(default_factory=dict)
    requests: dict = field(default_factory=dict)
    acknowledgements: dict = field(default_factory=dict)
    outbox: dict = field(default_factory=dict)


class FakePool:
    def __init__(self):
        self.now = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)
        self.state = State()
        self.lock = asyncio.Lock()
        self.queries = []
        self.fail_on = None
        self.commit_fail = False
        self.owners = {
            "owner": {"role": "OWNER", "active": True, "verified": True, "erased": False},
        }

    @asynccontextmanager
    async def acquire(self, *, timeout):
        assert timeout == 5
        yield FakeConnection(self)

    def seed_budget(self, spent):
        month = self.now.astimezone(timezone.utc).date().replace(day=1)
        self.state.budgets[month] = {
            "month_start": month, "spent_microusd": spent, "reserved_microusd": 0, "warning_enqueued": False,
        }
        return month


class FakeConnection:
    def __init__(self, pool):
        self.pool = pool
        self.depth = 0
        self.account_hash = None
        self.locked_budgets = set()

    @asynccontextmanager
    async def transaction(self):
        outer = self.depth == 0
        if outer:
            await self.pool.lock.acquire()
        self.depth += 1
        before = copy.deepcopy(self.pool.state)
        try:
            yield
            if outer and self.pool.commit_fail:
                raise asyncpg.ConnectionDoesNotExistError("private connection failure")
        except BaseException:
            self.pool.state = before
            raise
        finally:
            self.depth -= 1
            if outer:
                self.pool.lock.release()

    def check(self, query, args):
        assert self.depth > 0
        query = " ".join(query.split())
        self.pool.queries.append((query, args))
        assert not query.startswith(("CREATE ", "ALTER ", "DROP "))
        assert all(name not in query for name in ("assistant_", "analyst_", "binance_", "member_free_trials"))
        if self.pool.fail_on and self.pool.fail_on in query:
            raise asyncpg.UndefinedTableError("private@example.test secret-provider-detail")
        return query

    def account(self, value):
        assert self.account_hash == value, "Every per-user mutation requires its transaction lock"

    @staticmethod
    def check_quota(row):
        assert 0 <= row["reserved"] <= 1 and row["used"] >= 0
        if row["quota_limit"] is not None:
            assert row["used"] + row["reserved"] <= row["quota_limit"]

    async def execute(self, query, *args):
        query = self.check(query, args)
        state = self.pool.state
        if query.startswith("SELECT set_config"):
            return "SELECT 1"
        if query.startswith("SELECT pg_advisory_xact_lock"):
            self.account_hash = args[0]
            return "SELECT 1"
        if query.startswith("INSERT INTO chart_ai_acknowledgements"):
            self.account(args[0])
            state.acknowledgements.setdefault(tuple(args), self.pool.now)
        elif query.startswith("INSERT INTO chart_ai_quota_periods"):
            self.account(args[0])
            state.periods.setdefault(tuple(args[:2]), {
                "user_hash": args[0], "period_key": args[1], "access_type": args[2],
                "period_start": args[3], "period_end": args[4], "quota_limit": args[5], "used": 0, "reserved": 0,
            })
        elif query.startswith("INSERT INTO chart_ai_monthly_budget"):
            state.budgets.setdefault(args[0], {
                "month_start": args[0], "spent_microusd": 0, "reserved_microusd": 0, "warning_enqueued": False,
            })
        elif query.startswith("UPDATE chart_ai_monthly_budget"):
            assert args[0] in self.locked_budgets
            row = state.budgets[args[0]]
            if "warning_enqueued=TRUE" in query:
                row["warning_enqueued"] = True
            elif "spent_microusd=spent_microusd+$3" in query:
                row["reserved_microusd"] -= args[1]
                row["spent_microusd"] += args[2]
            else:
                assert "reserved_microusd=reserved_microusd+$2" in query
                row["reserved_microusd"] += args[1]
            assert row["reserved_microusd"] >= 0 and row["spent_microusd"] >= 0
            assert row["spent_microusd"] + row["reserved_microusd"] <= MONTHLY_BUDGET_MICROUSD
        elif query.startswith("UPDATE chart_ai_quota_periods"):
            self.account(args[0])
            row = state.periods[tuple(args[:2])]
            if "reserved=reserved+1" in query:
                row["reserved"] += 1
            else:
                assert "reserved=reserved-1,used=used+$3" in query
                row["reserved"] -= 1
                row["used"] += args[2]
            self.check_quota(row)
        elif query.startswith("INSERT INTO chart_ai_requests"):
            self.account(args[0])
            assert not any(r["user_hash"] == args[0] and r["status"] == "processing" for r in state.requests.values())
            assert tuple(args[:2]) not in state.requests
            state.requests[tuple(args[:2])] = {
                "user_hash": args[0], "request_key": args[1], "content_hash": args[2], "period_key": args[3],
                "month_start": args[4], "status": "processing", "lease_token": args[5],
                "lease_expires_at": args[6], "created_at": args[7], "budget_state": "reserved",
                "provider_started_at": None, "completed_at": None, "result_expires_at": None,
                "result_text": None, "error_code": None, "cost_microusd": None,
                "generation_attempts": 0, "accounted_microusd": 0,
            }
        elif query.startswith("UPDATE chart_ai_requests"):
            self.account(args[0])
            row = state.requests[tuple(args[:2])]
            if "provider_started_at=$3" in query:
                row["provider_started_at"] = args[2]
                row["generation_attempts"] = 1
            elif "generation_attempts=2" in query:
                row.update(generation_attempts=2, accounted_microusd=args[2])
            elif "status=$3" in query:
                row.update(status=args[2], completed_at=args[3], result_expires_at=args[4], result_text=args[5],
                           error_code=args[6], budget_state=args[7], cost_microusd=args[8],
                           accounted_microusd=args[9])
            elif "status='expired'" in query:
                row.update(status="expired", result_text=None)
            elif "budget_state='settled'" in query:
                row.update(budget_state="settled", cost_microusd=args[2], accounted_microusd=args[2])
            else:
                raise AssertionError(query)
            assert 0 <= row["generation_attempts"] <= 2
            assert 0 <= row["accounted_microusd"] <= REQUEST_RESERVATION_MICROUSD
            assert row["cost_microusd"] is None or row["cost_microusd"] == row["accounted_microusd"]
        elif query.startswith("INSERT INTO notification_outbox"):
            assert self.depth == 2, "Enqueue must be isolated in a savepoint"
            state.outbox.setdefault(args[3], {
                "id": args[0], "kind": "chart_ai.budget_warning", "recipient_user_id": args[1],
                "payload": json.loads(args[2]), "dedupe_key": args[3],
            })
        else:
            raise AssertionError(query)
        return "UPDATE 1" if query.startswith("UPDATE ") else "INSERT 0 1"

    async def fetchval(self, query, *args):
        query = self.check(query, args)
        if query == "SELECT clock_timestamp()":
            return self.pool.now
        if query.startswith("SELECT accepted_at FROM chart_ai_acknowledgements"):
            return self.pool.state.acknowledgements.get(tuple(args))
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        query = self.check(query, args)
        if query.startswith("SELECT * FROM chart_ai_requests"):
            self.account(args[0])
            assert "FOR UPDATE" in query
            if "request_key=$2" in query:
                row = self.pool.state.requests.get(tuple(args))
            else:
                assert "status='processing'" in query
                row = next((r for r in self.pool.state.requests.values()
                            if r["user_hash"] == args[0] and r["status"] == "processing"), None)
        elif query.startswith("SELECT * FROM chart_ai_quota_periods"):
            self.account(args[0])
            assert "FOR UPDATE" in query
            row = self.pool.state.periods.get(tuple(args))
        elif query.startswith("SELECT * FROM chart_ai_monthly_budget"):
            if "FOR UPDATE" in query:
                self.locked_budgets.add(args[0])
            row = self.pool.state.budgets.get(args[0])
        else:
            raise AssertionError(query)
        return copy.deepcopy(row)

    async def fetch(self, query, *args):
        query = self.check(query, args)
        if query.startswith("SELECT user_id FROM commercial_auth_users"):
            assert "email_verified" in query and "commercial_erased_users" in query
            return [{"user_id": uid} for uid, owner in self.pool.owners.items()
                    if owner["role"] == "OWNER" and owner["active"] and owner["verified"] and not owner["erased"]]
        if query.startswith("SELECT user_hash FROM chart_ai_requests"):
            rows = [r for r in self.pool.state.requests.values()
                    if r["status"] == "processing" and r["lease_expires_at"] <= args[0]]
            rows.sort(key=lambda r: (r["lease_expires_at"], r["user_hash"]))
            return [{"user_hash": r["user_hash"]} for r in rows[:args[1]]]
        if query.startswith("WITH expired AS"):
            assert "FOR UPDATE SKIP LOCKED" in query
            rows = [r for r in self.pool.state.requests.values()
                    if r["status"] == "succeeded" and r["result_expires_at"] <= args[0]]
            rows.sort(key=lambda r: (r["result_expires_at"], r["user_hash"], r["request_key"]))
            for row in rows[:args[1]]:
                row.update(status="expired", result_text=None)
            return [{"request_key": r["request_key"]} for r in rows[:args[1]]]
        raise AssertionError(query)


class ChartAiStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = FakePool()
        self.store = ChartAiStore(self.pool)
        self.access = QuotaAccess("subscriber")
        self.digest = "a" * 64
        self.month = self.pool.now.date().replace(day=1)
        await self.store.acknowledge("user", POLICY_VERSION)

    async def reserve(self, *, user="user", key=None, access=None, digest=None):
        return await self.store.reserve(user, access or self.access, key or uuid4(), digest or self.digest)

    async def complete(self, *, user="user", key=None, access=None, cost=1000, text="Validated analysis"):
        reservation = await self.reserve(user=user, key=key, access=access)
        await self.store.start_call(user, reservation.request.request_key, reservation.lease_token)
        return await self.store.succeed(user, reservation.request.request_key, reservation.lease_token, text, cost)

    def stored(self, key, user="user"):
        return self.pool.state.requests[(user_hash(user), key)]

    async def test_trial_total_three_and_reconstructed_store_does_not_reset(self):
        access = QuotaAccess("trial", self.pool.now - timedelta(days=1), self.pool.now + timedelta(days=6))
        for _ in range(3):
            await self.complete(access=access)
        self.store = ChartAiStore(self.pool)
        snapshot = await self.store.usage("user", access)
        self.assertEqual((snapshot.used, snapshot.reserved, snapshot.remaining), (3, 0, 0))
        with self.assertRaises(ChartAiQuotaError) as error:
            await self.reserve(access=access)
        self.assertEqual((error.exception.code, error.exception.status_code), ("quota_exhausted", 429))
        self.assertEqual(len(self.pool.state.requests), 3)

    async def test_trial_does_not_reset_on_monday_and_expiration_is_inclusive(self):
        self.pool.now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        start = self.pool.now - timedelta(days=1)
        access = QuotaAccess("trial", start, start + timedelta(days=7))
        for _ in range(3):
            await self.complete(access=access)
        self.pool.now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        self.assertEqual((await self.store.usage("user", access)).remaining, 0)
        self.pool.now = access.trial_end
        with self.assertRaises(ChartAiQuotaError) as error:
            await self.reserve(access=access)
        self.assertEqual(error.exception.code, "access_denied")

    async def test_changed_trial_identity_cannot_create_three_new_credits(self):
        start = self.pool.now - timedelta(days=1)
        await self.complete(access=QuotaAccess("trial", start, start + timedelta(days=7)))
        shifted = QuotaAccess("trial", start + timedelta(hours=1), start + timedelta(days=7, hours=1))
        with self.assertRaises(ChartAiStorageError):
            await self.reserve(access=shifted)
        self.assertEqual(len(self.pool.state.periods), 1)

    async def test_subscriber_twenty_and_twenty_first_is_rejected(self):
        for _ in range(20):
            await self.complete()
        with self.assertRaises(ChartAiQuotaError) as error:
            await self.reserve()
        self.assertEqual(error.exception.code, "quota_exhausted")
        self.assertEqual((await self.store.usage("user", self.access)).remaining, 0)

    async def test_utc_monday_boundary_and_original_reservation_week(self):
        self.pool.now = datetime(2026, 10, 18, 23, 59, 59, tzinfo=timezone.utc)
        reservation = await self.reserve()
        key = reservation.request.request_key
        await self.store.start_call("user", key, reservation.lease_token)
        self.pool.now += timedelta(seconds=1)
        snapshot = await self.store.usage("user", self.access)
        self.assertEqual((snapshot.period.start, snapshot.remaining),
                         (datetime(2026, 10, 19, tzinfo=timezone.utc), 20))
        await self.store.succeed("user", key, reservation.lease_token, "Result", 1000)
        old_period = self.pool.state.periods[(user_hash("user"), "week:2026-10-12")]
        self.assertEqual((old_period["used"], old_period["reserved"]), (1, 0))
        self.assertEqual((await self.store.usage("user", self.access)).remaining, 20)

    async def test_month_rollover_charges_original_budget(self):
        self.pool.now = datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)
        reservation = await self.reserve()
        await self.store.start_call("user", reservation.request.request_key, reservation.lease_token)
        self.pool.now += timedelta(seconds=1)
        await self.store.succeed("user", reservation.request.request_key, reservation.lease_token, "Result", 1000)
        await self.reserve()
        self.assertEqual(self.pool.state.budgets[date(2026, 10, 1)]["spent_microusd"], 1000)
        self.assertEqual(self.pool.state.budgets[date(2026, 11, 1)]["reserved_microusd"], 20_000)

    async def test_owner_is_unlimited_but_not_exempt_from_budget(self):
        owner = QuotaAccess("owner")
        for _ in range(25):
            await self.complete(access=owner)
        snapshot = await self.store.usage("user", owner)
        self.assertIsNone(snapshot.remaining)
        self.assertIsNone(snapshot.period.limit)
        self.pool.seed_budget(MONTHLY_BUDGET_MICROUSD)
        with self.assertRaises(ChartAiQuotaError) as error:
            await self.reserve(access=owner)
        self.assertEqual(error.exception.code, "budget_unavailable")
        self.assertEqual(str(error.exception), "Şu an analiz yapılamıyor, daha sonra tekrar dene")
        self.assertEqual((await self.store.usage("user", owner)).used, 25)

    async def test_upgrade_and_return_to_trial_preserve_trial_history(self):
        trial = QuotaAccess("trial", self.pool.now - timedelta(days=1), self.pool.now + timedelta(days=6))
        await self.complete(access=trial)
        await self.complete(access=self.access)
        self.assertEqual((await self.store.usage("user", trial)).remaining, 2)
        self.assertEqual((await self.store.usage("user", self.access)).remaining, 19)

    async def test_concurrent_replays_get_only_one_private_lease(self):
        key = uuid4()
        responses = await asyncio.gather(*(self.reserve(key=key) for _ in range(20)))
        self.assertEqual(sum(r.created for r in responses), 1)
        self.assertEqual(sum(r.lease_token is not None for r in responses), 1)
        self.assertEqual(len(self.pool.state.requests), 1)
        self.assertEqual(self.pool.state.budgets[self.month]["reserved_microusd"], 20_000)
        self.assertEqual((await self.store.usage("user", self.access)).reserved, 1)

    async def test_parallel_different_keys_cannot_oversubscribe_last_credit(self):
        for _ in range(19):
            await self.complete()
        responses = await asyncio.gather(*(self.reserve() for _ in range(10)), return_exceptions=True)
        self.assertEqual(sum(not isinstance(r, Exception) for r in responses), 1)
        self.assertTrue(all(r.code == "analysis_in_progress" for r in responses if isinstance(r, ChartAiQuotaError)))
        self.assertEqual((await self.store.usage("user", self.access)).remaining, 0)

    async def test_global_budget_reservations_are_atomic_across_users(self):
        self.pool.seed_budget(MONTHLY_BUDGET_MICROUSD - REQUEST_RESERVATION_MICROUSD)
        for index in range(10):
            await self.store.acknowledge(f"user-{index}", POLICY_VERSION)
        responses = await asyncio.gather(*(self.reserve(user=f"user-{i}") for i in range(10)), return_exceptions=True)
        self.assertEqual(sum(not isinstance(r, Exception) for r in responses), 1)
        self.assertTrue(all(r.code == "budget_unavailable" for r in responses if isinstance(r, ChartAiQuotaError)))
        self.assertEqual(self.pool.state.budgets[self.month]["reserved_microusd"], 20_000)

    async def test_conflicting_payload_is_rejected_without_changing_reservation(self):
        reservation = await self.reserve()
        with self.assertRaises(ChartAiQuotaError) as error:
            await self.reserve(key=reservation.request.request_key, digest="b" * 64)
        self.assertEqual(error.exception.code, "idempotency_conflict")
        self.assertEqual((await self.store.usage("user", self.access)).reserved, 1)

    async def test_same_key_is_scoped_to_user_and_read_cannot_leak_result(self):
        key = uuid4()
        await self.complete(key=key, text="User one result")
        await self.store.acknowledge("other", POLICY_VERSION)
        await self.complete(user="other", key=key, text="User two result")
        self.assertEqual((await self.store.request("user", key)).result_text, "User one result")
        self.assertEqual((await self.store.request("other", key)).result_text, "User two result")
        with self.assertRaises(ChartAiQuotaError) as error:
            await self.store.request("stranger", key)
        self.assertEqual(error.exception.status_code, 404)

    async def test_success_survives_lost_response_without_second_charge_or_call(self):
        key = uuid4()
        await self.complete(key=key)
        restored = ChartAiStore(self.pool)
        read = await restored.request("user", key)
        replay = await restored.reserve("user", self.access, key, self.digest)
        self.assertTrue(read.quota_consumed)
        self.assertEqual(read.result_text, "Validated analysis")
        self.assertFalse(replay.created)
        self.assertIsNone(replay.lease_token)
        self.assertEqual((await restored.usage("user", self.access)).used, 1)
        self.assertEqual(self.pool.state.budgets[self.month]["spent_microusd"], 1000)

    async def test_duplicate_finalization_and_late_failure_cannot_charge_or_refund_twice(self):
        reservation = await self.reserve()
        key, token = reservation.request.request_key, reservation.lease_token
        await self.store.start_call("user", key, token)
        await self.store.succeed("user", key, token, "First result", 1000)
        await self.store.succeed("user", key, token, "Second result", 2000)
        result = await self.store.fail("user", key, token, "connection_lost", 0)
        self.assertEqual(result.result_text, "First result")
        self.assertTrue(result.quota_consumed)
        self.assertEqual((await self.store.usage("user", self.access)).used, 1)
        self.assertEqual(self.pool.state.budgets[self.month]["spent_microusd"], 1000)

    async def test_error_refunds_quota_but_keeps_actual_provider_cost(self):
        reservation = await self.reserve()
        key, token = reservation.request.request_key, reservation.lease_token
        await self.store.start_call("user", key, token)
        failed = await self.store.fail("user", key, token, "invalid_output", 12_000)
        self.assertFalse(failed.quota_consumed)
        self.assertEqual((await self.store.usage("user", self.access)).remaining, 20)
        self.assertEqual(self.pool.state.budgets[self.month]["spent_microusd"], 12_000)
        self.assertEqual(self.pool.state.budgets[self.month]["reserved_microusd"], 0)
        await self.store.fail("user", key, token, "invalid_output", 12_000)
        self.assertFalse((await self.reserve(key=key)).created)
        self.assertEqual(self.pool.state.budgets[self.month]["spent_microusd"], 12_000)

    async def test_timeout_releases_quota_but_holds_uncertain_cost_until_reconciled(self):
        reservation = await self.reserve()
        key, token = reservation.request.request_key, reservation.lease_token
        await self.store.start_call("user", key, token)
        await self.store.fail("user", key, token, "ai_timeout")
        self.assertEqual((await self.store.usage("user", self.access)).remaining, 20)
        self.assertEqual(self.pool.state.budgets[self.month]["reserved_microusd"], 20_000)
        await self.store.settle_held_cost("user", key, 15_000)
        await self.store.settle_held_cost("user", key, 15_000)
        self.assertEqual(self.pool.state.budgets[self.month]["reserved_microusd"], 0)
        self.assertEqual(self.pool.state.budgets[self.month]["spent_microusd"], 15_000)
        with self.assertRaises(ChartAiQuotaError):
            await self.store.settle_held_cost("user", key, 14_000)

    async def test_failure_before_provider_call_releases_budget_and_quota(self):
        reservation = await self.reserve()
        await self.store.fail("user", reservation.request.request_key, reservation.lease_token, "invalid_image")
        self.assertEqual((await self.store.usage("user", self.access)).remaining, 20)
        self.assertEqual(self.pool.state.budgets[self.month]["reserved_microusd"], 0)
        self.assertEqual(self.pool.state.budgets[self.month]["spent_microusd"], 0)

    async def test_stale_unstarted_lease_is_recovered_once_and_late_worker_cannot_finalize(self):
        reservation = await self.reserve()
        self.pool.now += LEASE_DURATION
        self.assertEqual(await self.store.recover_expired(), 1)
        self.assertEqual(await self.store.recover_expired(), 0)
        late = await self.store.succeed("user", reservation.request.request_key, reservation.lease_token, "Late", 1000)
        self.assertEqual(late.status, "failed")
        self.assertEqual(late.error_code, "lease_expired")
        self.assertEqual((await self.store.usage("user", self.access)).remaining, 20)
        self.assertEqual(self.pool.state.budgets[self.month]["reserved_microusd"], 0)

    async def test_stale_started_lease_refunds_quota_without_restarting_provider(self):
        reservation = await self.reserve()
        await self.store.start_call("user", reservation.request.request_key, reservation.lease_token)
        self.pool.now += LEASE_DURATION
        self.assertEqual(await self.store.recover_expired(), 1)
        self.assertEqual((await self.store.request("user", reservation.request.request_key)).status, "failed")
        self.assertEqual(self.pool.state.budgets[self.month]["reserved_microusd"], 20_000)
        new = await self.reserve()
        self.assertTrue(new.created)
        self.assertNotEqual(new.lease_token, reservation.lease_token)
        self.assertEqual(self.pool.state.budgets[self.month]["reserved_microusd"], 40_000)
        self.assertFalse((await self.reserve(key=reservation.request.request_key)).created)

    async def test_request_read_also_recovers_expired_lease(self):
        reservation = await self.reserve()
        self.pool.now += LEASE_DURATION
        self.assertEqual((await self.store.request("user", reservation.request.request_key)).status, "failed")
        self.assertEqual((await self.store.usage("user", self.access)).reserved, 0)

    async def test_twenty_four_hour_expiry_clears_text_and_never_recharges_key(self):
        key = uuid4()
        result = await self.complete(key=key)
        self.pool.now = result.result_expires_at - timedelta(microseconds=1)
        self.assertEqual((await self.store.request("user", key)).status, "succeeded")
        self.pool.now += timedelta(microseconds=1)
        expired = await self.store.request("user", key)
        self.assertEqual(expired.status, "expired")
        self.assertIsNone(expired.result_text)
        self.assertTrue(expired.quota_consumed)
        self.assertFalse((await self.reserve(key=key)).created)
        self.assertEqual(self.stored(key)["result_text"], None)
        self.assertEqual((await self.store.usage("user", self.access)).used, 1)

    async def test_result_cleanup_is_bounded_and_idempotent(self):
        for _ in range(3):
            await self.complete()
        self.pool.now += RESULT_RETENTION
        self.assertEqual(await self.store.purge_results(limit=2), 2)
        self.assertEqual(await self.store.purge_results(limit=2), 1)
        self.assertEqual(await self.store.purge_results(limit=2), 0)
        self.assertTrue(all(r["result_text"] is None for r in self.pool.state.requests.values()))
        self.assertEqual((await self.store.usage("user", self.access)).used, 3)

    async def test_wrong_lease_and_second_start_do_not_change_financial_state(self):
        reservation = await self.reserve()
        key, token = reservation.request.request_key, reservation.lease_token
        with self.assertRaises(ChartAiQuotaError):
            await self.store.start_call("user", key, uuid4())
        await self.store.start_call("user", key, token)
        with self.assertRaises(ChartAiQuotaError) as error:
            await self.store.start_call("user", key, token)
        self.assertEqual(error.exception.code, "call_already_started")
        self.assertEqual(self.pool.state.budgets[self.month]["reserved_microusd"], 20_000)

    async def test_initial_consent_is_required_versioned_and_persistent(self):
        with self.assertRaises(ChartAiQuotaError) as error:
            await self.reserve(user="unconfirmed")
        self.assertEqual(error.exception.code, "acknowledgement_required")
        accepted = await self.store.acknowledge("unconfirmed", POLICY_VERSION)
        self.pool.now += timedelta(seconds=1)
        self.assertEqual(await self.store.acknowledge("unconfirmed", POLICY_VERSION), accepted)
        self.assertEqual(await self.store.acknowledgement("unconfirmed"), accepted)
        self.assertIsNone(await self.store.acknowledgement("stranger"))
        with self.assertRaises(ChartAiQuotaError):
            await self.store.acknowledge("unconfirmed", "old-version")

    async def test_budget_warning_once_per_month_and_only_verified_active_owners(self):
        self.pool.seed_budget(19_990_000)
        self.pool.owners.update({
            "owner2": dict(self.pool.owners["owner"]),
            "inactive": {**self.pool.owners["owner"], "active": False},
            "unverified": {**self.pool.owners["owner"], "verified": False},
            "erased": {**self.pool.owners["owner"], "erased": True},
            "customer": {**self.pool.owners["owner"], "role": "CUSTOMER"},
        })
        await self.complete(cost=10_000)
        await self.complete(cost=1000)
        await self.store.queue_budget_warning(self.month)
        self.assertEqual(len(self.pool.state.outbox), 2)
        self.assertEqual({r["recipient_user_id"] for r in self.pool.state.outbox.values()}, {"owner", "owner2"})
        self.assertTrue(self.pool.state.budgets[self.month]["warning_enqueued"])
        self.assertTrue(all(r["payload"] == {"month": "2026-10", "spent_microusd": 20_000_000,
                                           "budget_microusd": 25_000_000}
                            for r in self.pool.state.outbox.values()))

    async def test_budget_warning_does_not_trigger_on_reservations_alone(self):
        self.pool.seed_budget(19_990_000)
        await self.reserve()
        await self.store.queue_budget_warning(self.month)
        self.assertFalse(self.pool.state.budgets[self.month]["warning_enqueued"])
        self.assertEqual(self.pool.state.outbox, {})

    async def test_warning_savepoint_failure_does_not_undo_success_and_can_be_retried(self):
        self.pool.seed_budget(19_990_000)
        self.pool.fail_on = "INSERT INTO notification_outbox"
        with self.assertLogs("app.chart_ai_store", level="WARNING") as logs:
            result = await self.complete(cost=10_000)
        self.assertTrue(result.quota_consumed)
        self.assertEqual((await self.store.usage("user", self.access)).used, 1)
        self.assertEqual(self.pool.state.budgets[self.month]["spent_microusd"], 20_000_000)
        self.assertFalse(self.pool.state.budgets[self.month]["warning_enqueued"])
        self.assertNotIn("private@example.test", " ".join(logs.output))
        self.pool.fail_on = None
        await self.store.queue_budget_warning(self.month)
        await self.store.queue_budget_warning(self.month)
        self.assertEqual(len(self.pool.state.outbox), 1)

    async def test_missing_owner_is_reported_and_does_not_mark_warning_as_sent(self):
        self.pool.seed_budget(19_990_000)
        self.pool.owners.clear()
        with self.assertLogs("app.chart_ai_store", level="WARNING") as logs:
            await self.complete(cost=10_000)
        self.assertIn("owner_unavailable", " ".join(logs.output))
        self.assertFalse(self.pool.state.budgets[self.month]["warning_enqueued"])

    async def test_missing_schema_and_pool_fail_closed_without_logging_private_error(self):
        with self.assertLogs("app.chart_ai_store", level="WARNING") as logs:
            with self.assertRaises(ChartAiStorageError):
                await ChartAiStore(None).usage("user", self.access)
            self.pool.fail_on = "INSERT INTO chart_ai_quota_periods"
            with self.assertRaises(ChartAiStorageError):
                await self.store.usage("user", self.access)
        self.assertNotIn("secret-provider-detail", " ".join(logs.output))
        self.assertNotIn("private@example.test", " ".join(logs.output))
        self.assertEqual(self.pool.state.requests, {})

    async def test_finalization_storage_failure_rolls_back_budget_and_quota(self):
        reservation = await self.reserve()
        key, token = reservation.request.request_key, reservation.lease_token
        await self.store.start_call("user", key, token)
        self.pool.fail_on = "UPDATE chart_ai_requests SET status=$3"
        with self.assertLogs("app.chart_ai_store", level="WARNING"), self.assertRaises(ChartAiStorageError):
            await self.store.succeed("user", key, token, "Result", 1000)
        self.pool.fail_on = None
        self.assertEqual(self.stored(key)["status"], "processing")
        self.assertEqual((await self.store.usage("user", self.access)).used, 0)
        self.assertEqual(self.pool.state.budgets[self.month]["spent_microusd"], 0)
        self.assertEqual(self.pool.state.budgets[self.month]["reserved_microusd"], 20_000)

    async def test_commit_failure_rolls_back_reservation_and_is_not_success_shaped(self):
        self.pool.commit_fail = True
        with self.assertLogs("app.chart_ai_store", level="WARNING"), self.assertRaises(ChartAiStorageError):
            await self.reserve()
        self.pool.commit_fail = False
        self.assertEqual(self.pool.state.requests, {})
        self.assertEqual(self.pool.state.budgets, {})

    async def test_invalid_cost_empty_result_and_call_not_started_are_explicit(self):
        reservation = await self.reserve()
        key, token = reservation.request.request_key, reservation.lease_token
        for cost in (-1, 20_001, 0.01, True):
            with self.subTest(cost=cost), self.assertRaises(ValueError):
                await self.store.succeed("user", key, token, "Result", cost)
        for text in ("", " ", "x" * 32_001):
            with self.subTest(text_length=len(text)), self.assertRaises(ValueError):
                await self.store.succeed("user", key, token, text, 1000)
        with self.assertRaises(ChartAiQuotaError):
            await self.store.succeed("user", key, token, "Result", 1000)
        with self.assertRaises(ChartAiQuotaError):
            await self.store.fail("user", key, token, "invalid_image", 1000)
        self.assertEqual((await self.store.usage("user", self.access)).reserved, 1)
        self.assertEqual(self.pool.state.budgets[self.month]["spent_microusd"], 0)

    async def test_cleanup_and_recovery_reject_invalid_batches(self):
        for limit in (0, 1001, True):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    await self.store.purge_results(limit=limit)
                with self.assertRaises(ValueError):
                    await self.store.recover_expired(limit=limit)

    def test_period_uses_utc_not_local_clock_and_rejects_invalid_trial(self):
        local = datetime(2026, 10, 19, 2, 59, 59, tzinfo=timezone(timedelta(hours=3)))
        self.assertEqual(quota_period(self.access, local).key, "week:2026-10-12")
        self.assertEqual(quota_period(self.access, local + timedelta(seconds=1)).key, "week:2026-10-19")
        with self.assertRaises(ValueError):
            quota_period(self.access, self.pool.now.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            quota_period(QuotaAccess("trial", self.pool.now, self.pool.now + timedelta(days=8)), self.pool.now)
        with self.assertRaises(ChartAiQuotaError):
            quota_period(QuotaAccess("trial", self.pool.now + timedelta(days=1),
                                     self.pool.now + timedelta(days=8)), self.pool.now)

    def test_privacy_notice_is_exact_and_does_not_claim_provider_nonretention(self):
        self.assertEqual(PRIVACY_NOTICE,
                         "Resmin sitemizde saklanmaz; analiz için yapay zeka sağlayıcımıza gönderilir.")
