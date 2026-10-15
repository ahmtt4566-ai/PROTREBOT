"""Explicit local-only verification, with a hidden console password and disposable DB."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import ipaddress
import json
import os
import re
import sys
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.chart_ai_store import (
    LEASE_DURATION,
    POLICY_VERSION,
    RESULT_RETENTION,
    ChartAiQuotaError,
    ChartAiStore,
    QuotaAccess,
    user_hash,
)

HOST = "127.0.0.1"
PORT = 5432
DB_PREFIX = "chart_ai_stage3_"
MIGRATION = BACKEND / "migrations" / "20261015_001_chart_ai.sql"


class LocalVerificationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.public_message = message


def validate_target(host: str, port: int, database: str, *, administrative: bool = False) -> None:
    if host != HOST or not ipaddress.ip_address(host).is_loopback or port != PORT:
        raise ValueError("Only explicit 127.0.0.1:5432 is permitted")
    if administrative and database == "postgres":
        return
    if not re.fullmatch(DB_PREFIX + r"[a-f0-9]{32}", database):
        raise ValueError("Only a newly generated chart-ai test database is permitted")


def ignore_database_environment() -> None:
    for name in ("DATABASE_URL", "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER",
                 "PGPASSWORD", "PGPASSFILE", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"):
        os.environ.pop(name, None)


def console_password() -> str:
    if not sys.stdin.isatty():
        raise LocalVerificationError(
            "interactive_terminal_required", "Parola icin etkilesimli terminal gerekli; acik giris kullanilmayacak.",
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Yerel PostgreSQL postgres parolasi (gizli): ")
    except getpass.GetPassWarning:
        raise LocalVerificationError(
            "hidden_input_unavailable", "Terminal gizli parola girisini desteklemiyor; parola okunmadi.",
        ) from None
    if not password:
        raise LocalVerificationError("empty_password", "Bos parola kabul edilmez.")
    return password


async def verify_connection(conn: asyncpg.Connection, database: str) -> None:
    row = await conn.fetchrow(
        "SELECT host(inet_server_addr()) AS host,inet_server_port() AS port,current_database() AS database",
    )
    if row is None:
        raise LocalVerificationError("server_identity_unavailable", "Baglanti kuruldu, fakat sunucu kimligi okunamadi.")
    if row["host"] != HOST:
        raise LocalVerificationError("server_address_mismatch", "Baglanilan sunucunun IP adresi 127.0.0.1 degil.")
    if row["port"] != PORT:
        raise LocalVerificationError("server_port_mismatch", "Baglanilan sunucunun portu 5432 degil.")
    if row["database"] != database:
        raise LocalVerificationError("database_mismatch", "Baglanilan veritabani acikca belirlenen hedefle ayni degil.")
    if await conn.fetchval("SHOW listen_addresses") != "localhost":
        raise LocalVerificationError(
            "runtime_listen_mismatch", "Calisan sunucuda listen_addresses localhost degil; servis yeniden baslatilmali.",
        )


async def connect(password: str, database: str, *, administrative: bool = False) -> asyncpg.Connection:
    validate_target(HOST, PORT, database, administrative=administrative)
    conn = await asyncpg.connect(
        host=HOST, port=PORT, user="postgres", password=password, database=database,
        ssl=False, timeout=5, command_timeout=15,
        server_settings={"application_name": "chart-ai-isolated-stage3", "timezone": "UTC"},
    )
    try:
        await verify_connection(conn, database)
    except BaseException:
        await conn.close()
        raise
    return conn


def record_error(exc: Exception) -> dict[str, str]:
    result = {"error_type": type(exc).__name__}
    if isinstance(exc, LocalVerificationError):
        result.update(code=exc.code, message=exc.public_message)
    elif isinstance(exc, asyncpg.InvalidPasswordError):
        result.update(code="authentication_failed", message="PostgreSQL parolayi dogrulamadi; tekrar gizli giris yapin.")
    elif isinstance(exc, asyncpg.InvalidAuthorizationSpecificationError):
        result.update(code="authorization_rejected", message="PostgreSQL kimlik veya pg_hba.conf erisimini reddetti.")
    elif isinstance(exc, ConnectionRefusedError):
        result.update(code="connection_refused", message="127.0.0.1:5432 baglantiyi reddetti; servis ve portu kontrol edin.")
    elif isinstance(exc, TimeoutError):
        result.update(code="connection_timeout", message="Yerel PostgreSQL baglantisi veya islemi zaman asimina ugradi.")
    elif isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
        result.update(code="connection_closed", message="Yerel PostgreSQL baglantisi sunucu tarafindan kapatildi.")
    if isinstance(exc, asyncpg.PostgresError) and exc.sqlstate:
        result["sqlstate"] = exc.sqlstate
    return result


def print_failure(prefix: str, exc: Exception) -> None:
    error = record_error(exc)
    print(f"{prefix}: {error['error_type']} [{error.get('code', error.get('sqlstate', 'check_error'))}]"
          f" {error.get('message', 'Ayrintili hata turu rapora yazildi; gizli bilgiler gosterilmedi.')}", flush=True)


class Checks:
    def __init__(self, pool: asyncpg.Pool, db: asyncpg.Connection, report: dict[str, Any]):
        self.pool = pool
        self.db = db
        self.store = ChartAiStore(pool)
        self.report = report
        self.digest = "a" * 64
        self.subscriber = QuotaAccess("subscriber")

    async def clock(self, instant: datetime) -> None:
        await self.db.execute("UPDATE chart_ai_stage3_clock.instant SET value=$1", instant)

    async def user(self, *, owner: bool = False) -> str:
        uid = "stage3-" + uuid4().hex
        await self.db.execute(
            "INSERT INTO commercial_auth_users (user_id,auth_version,security) VALUES ($1,1,$2::jsonb)",
            uid, json.dumps({"role": "OWNER" if owner else "CUSTOMER", "active": True, "email_verified": True}),
        )
        await self.store.acknowledge(uid, POLICY_VERSION)
        return uid

    async def finish(self, uid: str, access: QuotaAccess | None = None, *, cost: int = 1000):
        reservation = await self.store.reserve(uid, access or self.subscriber, uuid4(), self.digest)
        assert reservation.created and reservation.lease_token is not None
        await self.store.start_call(uid, reservation.request.request_key, reservation.lease_token)
        return await self.store.succeed(
            uid, reservation.request.request_key, reservation.lease_token, "Local fixture analysis only", cost,
        )

    async def expect_error(self, expected: str, operation) -> None:
        try:
            await operation
        except ChartAiQuotaError as exc:
            assert exc.code == expected, (exc.code, expected)
        else:
            raise AssertionError(f"Expected explicit error: {expected}")

    async def double_request(self) -> None:
        uid, key = await self.user(), uuid4()
        stores = [ChartAiStore(self.pool) for _ in range(12)]
        responses = await asyncio.gather(
            *(store.reserve(uid, self.subscriber, key, self.digest) for store in stores),
        )
        created = [response for response in responses if response.created]
        assert len(created) == 1
        assert sum(response.lease_token is not None for response in responses) == 1
        token = created[0].lease_token
        assert token is not None
        await self.store.start_call(uid, key, token)
        await asyncio.gather(*(store.succeed(uid, key, token, "Local result", 1000) for store in stores))
        assert (await self.store.usage(uid, self.subscriber)).used == 1
        assert (await ChartAiStore(self.pool).request(uid, key)).result_text == "Local result"
        assert not (await self.store.reserve(uid, self.subscriber, key, self.digest)).created
        await self.expect_error(
            "idempotency_conflict", self.store.reserve(uid, self.subscriber, key, "b" * 64),
        )
        other = await self.user()
        await self.expect_error("request_not_found", self.store.request(other, key))

    async def quota_limits(self) -> None:
        now = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)
        trial = QuotaAccess("trial", now - timedelta(days=1), now + timedelta(days=6))
        for access, limit in ((trial, 3), (self.subscriber, 20)):
            uid = await self.user()
            for _ in range(limit):
                await self.finish(uid, access)
            snapshot = await self.store.usage(uid, access)
            assert snapshot.used == limit and snapshot.remaining == 0
            await self.expect_error(
                "quota_exhausted", self.store.reserve(uid, access, uuid4(), self.digest),
            )
        owner = await self.user(owner=True)
        for _ in range(25):
            await self.finish(owner, QuotaAccess("owner"))
        assert (await self.store.usage(owner, QuotaAccess("owner"))).remaining is None

    async def last_credit_race(self) -> None:
        uid = await self.user()
        trial = QuotaAccess("trial", datetime(2026, 10, 14, tzinfo=timezone.utc),
                            datetime(2026, 10, 21, tzinfo=timezone.utc))
        for _ in range(2):
            await self.finish(uid, trial)
        attempts = await asyncio.gather(
            *(self.store.reserve(uid, trial, uuid4(), self.digest) for _ in range(10)),
            return_exceptions=True,
        )
        successes = [item for item in attempts if not isinstance(item, Exception)]
        assert len(successes) == 1
        assert all(isinstance(item, ChartAiQuotaError) and item.code == "analysis_in_progress"
                   for item in attempts if isinstance(item, Exception))
        reservation = successes[0]
        await self.store.start_call(uid, reservation.request.request_key, reservation.lease_token)
        await self.store.succeed(uid, reservation.request.request_key, reservation.lease_token, "Last credit", 1000)
        assert (await self.store.usage(uid, trial)).remaining == 0

    async def failure_refund(self) -> None:
        uid = await self.user()
        failed = await self.store.reserve(uid, self.subscriber, uuid4(), self.digest)
        key, token = failed.request.request_key, failed.lease_token
        await self.store.start_call(uid, key, token)
        await self.store.fail(uid, key, token, "invalid_output", 12_000)
        assert (await self.store.usage(uid, self.subscriber)).remaining == 20
        assert not (await self.store.reserve(uid, self.subscriber, key, self.digest)).created
        row = await self.db.fetchrow(
            "SELECT status,cost_microusd FROM chart_ai_requests WHERE user_hash=$1 AND request_key=$2",
            user_hash(uid), key,
        )
        assert row["status"] == "failed" and row["cost_microusd"] == 12_000
        uncertain = await self.store.reserve(uid, self.subscriber, uuid4(), self.digest)
        key, token = uncertain.request.request_key, uncertain.lease_token
        await self.store.start_call(uid, key, token)
        await self.store.fail(uid, key, token, "ai_timeout")
        assert (await self.store.usage(uid, self.subscriber)).remaining == 20
        assert await self.db.fetchval(
            "SELECT budget_state FROM chart_ai_requests WHERE user_hash=$1 AND request_key=$2", user_hash(uid), key,
        ) == "held"
        await self.store.settle_held_cost(uid, key, 0)
        await self.store.settle_held_cost(uid, key, 0)

    async def monday(self) -> None:
        sunday = datetime(2026, 10, 18, 23, 59, 59, tzinfo=timezone.utc)
        await self.clock(sunday)
        uid = await self.user()
        pending = await self.store.reserve(uid, self.subscriber, uuid4(), self.digest)
        await self.store.start_call(uid, pending.request.request_key, pending.lease_token)
        trial = QuotaAccess("trial", sunday - timedelta(days=1), sunday + timedelta(days=6))
        trial_user = await self.user()
        for _ in range(3):
            await self.finish(trial_user, trial)
        await self.clock(sunday + timedelta(seconds=1))
        current = await self.store.usage(uid, self.subscriber)
        assert current.period.key == "week:2026-10-19" and current.remaining == 20
        await self.store.succeed(uid, pending.request.request_key, pending.lease_token, "Old week", 1000)
        assert await self.db.fetchval(
            "SELECT used FROM chart_ai_quota_periods WHERE user_hash=$1 AND period_key='week:2026-10-12'",
            user_hash(uid),
        ) == 1
        assert (await self.store.usage(uid, self.subscriber)).remaining == 20
        assert (await self.store.usage(trial_user, trial)).remaining == 0
        await self.clock(trial.trial_end)
        await self.expect_error("access_denied", self.store.reserve(trial_user, trial, uuid4(), self.digest))

    async def cleanup(self) -> None:
        await self.clock(datetime(2026, 11, 4, 12, tzinfo=timezone.utc))
        uid = await self.user()
        completed = await self.finish(uid)
        assert completed.result_expires_at == completed.completed_at + RESULT_RETENTION
        await self.clock(completed.result_expires_at - timedelta(microseconds=1))
        assert (await self.store.request(uid, completed.request_key)).status == "succeeded"
        await self.clock(completed.result_expires_at)
        await self.store.purge_results(limit=1000)
        expired = await self.store.request(uid, completed.request_key)
        assert expired.status == "expired" and expired.result_text is None and expired.quota_consumed
        assert (await self.store.usage(uid, self.subscriber)).used == 1
        assert not (await self.store.reserve(uid, self.subscriber, completed.request_key, self.digest)).created
        assert await self.store.purge_results(limit=1000) == 0

    async def stale_lease(self) -> None:
        await self.clock(datetime(2026, 12, 1, tzinfo=timezone.utc))
        uid = await self.user()
        pending = await self.store.reserve(uid, self.subscriber, uuid4(), self.digest)
        await self.clock(pending.request.created_at + LEASE_DURATION)
        assert await self.store.recover_expired() == 1
        assert await self.store.recover_expired() == 0
        late = await self.store.succeed(uid, pending.request.request_key, pending.lease_token, "Late", 1000)
        assert late.status == "failed" and not late.quota_consumed
        assert (await self.store.usage(uid, self.subscriber)).remaining == 20

    async def monthly_budget(self) -> None:
        await self.clock(datetime(2027, 1, 5, tzinfo=timezone.utc))
        month = date(2027, 1, 1)
        await self.db.execute(
            "INSERT INTO chart_ai_monthly_budget(month_start,spent_microusd) VALUES ($1,24980000)", month,
        )
        users = [await self.user() for _ in range(10)]
        attempts = await asyncio.gather(
            *(self.store.reserve(uid, self.subscriber, uuid4(), self.digest) for uid in users),
            return_exceptions=True,
        )
        successes = [(uid, item) for uid, item in zip(users, attempts, strict=True) if not isinstance(item, Exception)]
        assert len(successes) == 1
        assert all(isinstance(item, ChartAiQuotaError) and item.code == "budget_unavailable"
                   for item in attempts if isinstance(item, Exception))
        budget = await self.db.fetchrow("SELECT * FROM chart_ai_monthly_budget WHERE month_start=$1", month)
        assert budget["spent_microusd"] + budget["reserved_microusd"] == 25_000_000
        owner = await self.user(owner=True)
        await self.expect_error(
            "budget_unavailable", self.store.reserve(owner, QuotaAccess("owner"), uuid4(), self.digest),
        )
        uid, pending = successes[0]
        await self.store.fail(uid, pending.request.request_key, pending.lease_token, "invalid_image")
        assert (await self.store.usage(uid, self.subscriber)).used == 0

    async def budget_warning(self) -> None:
        await self.clock(datetime(2027, 2, 3, tzinfo=timezone.utc))
        month = date(2027, 2, 1)
        await self.db.execute(
            "INSERT INTO chart_ai_monthly_budget(month_start,spent_microusd) VALUES ($1,19990000)", month,
        )
        uid = await self.user()
        await self.finish(uid, cost=10_000)
        count = await self.db.fetchval(
            "SELECT count(*) FROM notification_outbox WHERE kind='chart_ai.budget_warning' AND payload->>'month'='2027-02'",
        )
        owner_count = await self.db.fetchval(
            "SELECT count(*) FROM commercial_auth_users WHERE security->>'role'='OWNER'",
        )
        assert count == owner_count and count > 0
        await self.finish(uid)
        await asyncio.gather(*(self.store.queue_budget_warning(month) for _ in range(8)))
        assert await self.db.fetchval(
            "SELECT count(*) FROM notification_outbox WHERE kind='chart_ai.budget_warning' AND payload->>'month'='2027-02'",
        ) == count
        assert await self.db.fetchval(
            "SELECT warning_enqueued FROM chart_ai_monthly_budget WHERE month_start=$1", month,
        ) is True

    async def legacy_notification_contract(self) -> None:
        owner = await self.db.fetchval(
            "SELECT user_id FROM commercial_auth_users WHERE security->>'role'='OWNER' LIMIT 1",
        )
        for kind, payload in (("approval.pending_digest", {"bucket": 1}),
                              ("approval.decision", {"approval_id": "local-fixture", "result": 2})):
            await self.db.execute(
                """INSERT INTO notification_outbox(id,kind,recipient_user_id,payload,dedupe_key)
                   VALUES ($1,$2,$3,$4::jsonb,$5)""",
                uuid4().hex, kind, owner, json.dumps(payload), uuid4().hex,
            )
        try:
            await self.db.execute(
                """INSERT INTO notification_outbox(id,kind,recipient_user_id,payload,dedupe_key)
                   VALUES ($1,'approval.decision',$2,'{"approval_id":"local","result":9}'::jsonb,$3)""",
                uuid4().hex, owner, uuid4().hex,
            )
        except asyncpg.CheckViolationError:
            pass
        else:
            raise AssertionError("Existing invalid approval payload must remain rejected")

    async def snapshot(self) -> str:
        parts = []
        for table in ("chart_ai_quota_periods", "chart_ai_requests", "chart_ai_acknowledgements",
                      "chart_ai_monthly_budget", "notification_outbox"):
            parts.append(await self.db.fetchval(
                f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb)::text FROM {table} t",
            ))
        rows = await self.db.fetch(
            """SELECT conrelid::regclass::text AS table_name,conname,pg_get_constraintdef(oid) AS definition
               FROM pg_constraint WHERE conrelid IN ('notification_outbox'::regclass,
                 'chart_ai_quota_periods'::regclass,'chart_ai_requests'::regclass,
                 'chart_ai_acknowledgements'::regclass,'chart_ai_monthly_budget'::regclass)
               ORDER BY table_name,conname""",
        )
        parts.append(json.dumps([dict(row) for row in rows]))
        indexes = await self.db.fetch(
            "SELECT indexname,indexdef FROM pg_indexes WHERE tablename LIKE 'chart_ai_%' ORDER BY indexname",
        )
        parts.append(json.dumps([dict(row) for row in indexes]))
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    async def repeat_migration(self) -> None:
        before = await self.snapshot()
        await self.db.execute(MIGRATION.read_text(encoding="utf-8"))
        await self.db.execute(MIGRATION.with_name("20261015_002_chart_ai_retry.sql").read_text(encoding="utf-8"))
        assert await self.snapshot() == before, "Second migration changed data or notification contracts"

    async def run(self) -> None:
        for name in ("double_request", "quota_limits", "last_credit_race", "failure_refund", "monday",
                     "cleanup", "stale_lease", "monthly_budget", "budget_warning",
                     "legacy_notification_contract", "repeat_migration"):
            try:
                await getattr(self, name)()
            except (asyncpg.PostgresError, asyncpg.InterfaceError, AssertionError, RuntimeError,
                    ValueError, TypeError, OSError) as exc:
                if self.db.is_in_transaction():
                    await self.db.execute("ROLLBACK")
                self.report["checks"].append({"name": name, "status": "failed", **record_error(exc)})
                print_failure(f"FAIL {name}", exc)
            else:
                self.report["checks"].append({"name": name, "status": "passed"})
                print(f"PASS {name}", flush=True)


async def run(password: str, report: dict[str, Any]) -> None:
    database = DB_PREFIX + uuid4().hex
    validate_target(HOST, PORT, database)
    report["target"] = {"host": HOST, "port": PORT, "database": database}
    admin = None
    db = None
    pool = None
    created = False
    try:
        report["stage"] = "local_admin_connection"
        admin = await connect(password, "postgres", administrative=True)
        report["local_identity"] = {
            "host": HOST, "port": PORT, "listen_addresses": "localhost",
            "inet_text": await admin.fetchval("SELECT inet_server_addr()::text"),
        }
        print(f"LOCAL VERIFIED: 127.0.0.1:5432, listen_addresses=localhost,"
              f" PostgreSQL inet text={report['local_identity']['inet_text']}", flush=True)
        report["server_version"] = await admin.fetchval("SHOW server_version")
        report["stage"] = "create_disposable_database"
        await admin.execute(f'CREATE DATABASE "{database}" TEMPLATE template0')
        created = True
        report["created"] = True
        print(f"Created temporary database: {HOST}:{PORT}/{database}", flush=True)
        db = await connect(password, database)
        for path in sorted((BACKEND / "migrations").glob("20*.sql")):
            report["stage"] = "migration:" + path.name
            await db.execute(path.read_text(encoding="utf-8"))
            report["migrations"].append(path.name)
            print("PASS migration " + path.name, flush=True)
        await db.execute(
            """CREATE SCHEMA chart_ai_stage3_clock;
               CREATE TABLE chart_ai_stage3_clock.instant(value TIMESTAMPTZ NOT NULL);
               INSERT INTO chart_ai_stage3_clock.instant VALUES ('2026-10-15T09:00:00Z');
               CREATE FUNCTION chart_ai_stage3_clock.clock_timestamp() RETURNS TIMESTAMPTZ LANGUAGE SQL STABLE
               AS 'SELECT value FROM chart_ai_stage3_clock.instant';""",
        )

        async def init(conn: asyncpg.Connection) -> None:
            await verify_connection(conn, database)

        pool = await asyncpg.create_pool(
            host=HOST, port=PORT, user="postgres", password=password, database=database,
            ssl=False, timeout=5, command_timeout=15, min_size=1, max_size=12, init=init,
            server_settings={"application_name": "chart-ai-isolated-stage3",
                             "timezone": "UTC", "search_path": "chart_ai_stage3_clock,public,pg_catalog"},
        )
        report["stage"] = "real_postgres_checks"
        await Checks(pool, db, report).run()
        report["stage"] = "checks_completed"
    except (asyncpg.PostgresError, asyncpg.InterfaceError, AssertionError, RuntimeError,
            ValueError, TypeError, OSError) as exc:
        report["error"] = record_error(exc)
        print_failure(f"STOP {report['stage']}", exc)
    finally:
        if pool is not None:
            try:
                await asyncio.wait_for(pool.close(), timeout=10)
            except (TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError):
                pool.terminate()
        if db is not None:
            await db.close()
        if created:
            validate_target(HOST, PORT, database)
            try:
                if admin is None or admin.is_closed():
                    admin = await connect(password, "postgres", administrative=True)
                await admin.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
                report["deleted"] = not await admin.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=$1)", database,
                )
                print(f"Temporary database deleted and absence verified: {report['deleted']}", flush=True)
            except (asyncpg.PostgresError, asyncpg.InterfaceError, RuntimeError, ValueError, OSError) as exc:
                report["cleanup_error"] = record_error(exc)
                print_failure(f"CLEANUP FAILED database={database}", exc)
        if admin is not None:
            await admin.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    validate_target(HOST, PORT, DB_PREFIX + "0" * 32)
    ignore_database_environment()
    report: dict[str, Any] = {"migrations": [], "checks": [], "created": False, "deleted": False}
    try:
        password = console_password()
        asyncio.run(run(password, report))
    except (RuntimeError, ValueError, OSError, KeyboardInterrupt) as exc:
        report["error"] = record_error(exc)
        print_failure("STOP", exc)
    finally:
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    success = (report["created"] and report["deleted"] and not report.get("error")
               and len(report["checks"]) == 11 and all(row["status"] == "passed" for row in report["checks"]))
    print(f"Local stage 3 success: {success}", flush=True)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
