"""Isolated chart-analysis accounting; requires a manually installed schema."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

import asyncpg

logger = logging.getLogger(__name__)
REQUEST_RESERVATION_MICROUSD = 20_000
MONTHLY_BUDGET_MICROUSD = 25_000_000
WARNING_THRESHOLD_MICROUSD = 20_000_000
LEASE_DURATION = timedelta(seconds=120)
RESULT_RETENTION = timedelta(hours=24)
POLICY_VERSION = "chart-ai-v1"
PRIVACY_NOTICE = "Resmin sitemizde saklanmaz; analiz için yapay zeka sağlayıcımıza gönderilir."

AccessType = Literal["trial", "subscriber", "owner"]
RequestStatus = Literal["processing", "succeeded", "failed", "expired"]
Row = Mapping[str, Any]


class ChartAiConnection(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Any]: ...
    async def execute(self, query: str, *args: Any) -> str: ...
    async def fetchrow(self, query: str, *args: Any) -> Row | None: ...
    async def fetch(self, query: str, *args: Any) -> Sequence[Row]: ...
    async def fetchval(self, query: str, *args: Any) -> Any: ...


class ChartAiPool(Protocol):
    def acquire(self, *, timeout: float) -> AbstractAsyncContextManager[ChartAiConnection]: ...


class ChartAiStorageError(RuntimeError):
    pass


class ChartAiQuotaError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class QuotaAccess:
    """Trusted server entitlement, never a browser-supplied plan selection."""
    kind: AccessType
    trial_start: datetime | None = None
    trial_end: datetime | None = None


@dataclass(frozen=True)
class QuotaPeriod:
    key: str
    kind: AccessType
    start: datetime | None
    end: datetime | None
    limit: int | None


@dataclass(frozen=True)
class QuotaSnapshot:
    period: QuotaPeriod
    used: int
    reserved: int
    server_now: datetime

    @property
    def remaining(self) -> int | None:
        return None if self.period.limit is None else self.period.limit - self.used - self.reserved


@dataclass(frozen=True)
class ChartRequest:
    request_key: UUID
    status: RequestStatus
    created_at: datetime
    completed_at: datetime | None
    result_expires_at: datetime | None
    result_text: str | None
    error_code: str | None

    @property
    def quota_consumed(self) -> bool:
        return self.status in ("succeeded", "expired")


@dataclass(frozen=True)
class Reservation:
    request: ChartRequest
    created: bool
    lease_token: UUID | None


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware timestamp is required")
    return value.astimezone(timezone.utc)


def quota_period(access: QuotaAccess, now: datetime) -> QuotaPeriod:
    now = utc(now)
    if access.kind == "owner":
        return QuotaPeriod("owner", "owner", None, None, None)
    if access.kind == "subscriber":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return QuotaPeriod(f"week:{start.date().isoformat()}", "subscriber", start, start + timedelta(days=7), 20)
    if access.kind == "trial" and access.trial_start is not None and access.trial_end is not None:
        start, end = utc(access.trial_start), utc(access.trial_end)
        if end - start != timedelta(days=7):
            raise ValueError("The canonical seven-day trial period is required")
        if start <= now < end:
            return QuotaPeriod("trial", "trial", start, end, 3)
    raise ChartAiQuotaError("access_denied", "Grafik analizi erişiminiz bulunmuyor.", 403)


def user_hash(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 160:
        raise ValueError("A canonical user identifier is required")
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def validate_cost(value: int) -> None:
    if type(value) is not int or not 0 <= value <= REQUEST_RESERVATION_MICROUSD:
        raise ValueError("Cost must be integer micro-USD within the request reservation")


class ChartAiStore:
    def __init__(self, pool: ChartAiPool | None):
        self.pool = pool

    @asynccontextmanager
    async def _transaction(self, account_hash: str | None = None) -> AsyncGenerator[ChartAiConnection, None]:
        if self.pool is None:
            logger.warning("Chart AI storage unavailable code=storage_unavailable")
            raise ChartAiStorageError("Grafik analizi deposuna erişilemiyor.")
        try:
            async with self.pool.acquire(timeout=5) as conn, conn.transaction():
                await conn.execute("SELECT set_config('lock_timeout','5s',true)")
                if account_hash is not None:
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended('chart-ai:' || $1,0))", account_hash,
                    )
                yield conn
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError) as exc:
            logger.warning("Chart AI storage failed type=%s", type(exc).__name__)
            raise ChartAiStorageError("Grafik analizi deposu doğrulanamadı.") from None

    @staticmethod
    async def _now(conn: ChartAiConnection) -> datetime:
        value = await conn.fetchval("SELECT clock_timestamp()")
        if not isinstance(value, datetime):
            raise ChartAiStorageError("Grafik analizi sunucu zamanı doğrulanamadı.")
        return utc(value)

    @staticmethod
    def _request(row: Row) -> ChartRequest:
        status = row["status"]
        if status not in ("processing", "succeeded", "failed", "expired"):
            raise ChartAiStorageError("Grafik analizi istek durumu doğrulanamadı.")
        return ChartRequest(row["request_key"], status, row["created_at"], row["completed_at"],
                            row["result_expires_at"], row["result_text"], row["error_code"])

    @staticmethod
    async def _row(conn: ChartAiConnection, account_hash: str, key: UUID) -> Row | None:
        if not isinstance(key, UUID):
            raise TypeError("A UUID request key is required")
        return await conn.fetchrow(
            "SELECT * FROM chart_ai_requests WHERE user_hash=$1 AND request_key=$2 FOR UPDATE", account_hash, key,
        )

    @staticmethod
    async def _period(conn: ChartAiConnection, account_hash: str, period: QuotaPeriod) -> Row:
        await conn.execute(
            """INSERT INTO chart_ai_quota_periods
               (user_hash,period_key,access_type,period_start,period_end,quota_limit)
               VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (user_hash,period_key) DO NOTHING""",
            account_hash, period.key, period.kind, period.start, period.end, period.limit,
        )
        row = await conn.fetchrow(
            "SELECT * FROM chart_ai_quota_periods WHERE user_hash=$1 AND period_key=$2 FOR UPDATE",
            account_hash, period.key,
        )
        if row is None or (row["access_type"], row["period_start"], row["period_end"], row["quota_limit"]) != (
            period.kind, period.start, period.end, period.limit,
        ):
            raise ChartAiStorageError("Grafik analizi kota dönemi doğrulanamadı.")
        return row

    @staticmethod
    async def _budget(conn: ChartAiConnection, month: date) -> Row:
        await conn.execute(
            "INSERT INTO chart_ai_monthly_budget (month_start) VALUES ($1) ON CONFLICT (month_start) DO NOTHING",
            month,
        )
        row = await conn.fetchrow(
            "SELECT * FROM chart_ai_monthly_budget WHERE month_start=$1 FOR UPDATE", month,
        )
        if row is None:
            raise ChartAiStorageError("Grafik analizi bütçesi doğrulanamadı.")
        return row

    async def acknowledge(self, user_id: str, version: str) -> datetime:
        if version != POLICY_VERSION:
            raise ChartAiQuotaError("stale_acknowledgement", "Güncel kullanım uyarısını onaylayın.", 403)
        account_hash = user_hash(user_id)
        async with self._transaction(account_hash) as conn:
            await conn.execute(
                """INSERT INTO chart_ai_acknowledgements (user_hash,policy_version)
                   VALUES ($1,$2) ON CONFLICT (user_hash,policy_version) DO NOTHING""", account_hash, version,
            )
            value = await conn.fetchval(
                "SELECT accepted_at FROM chart_ai_acknowledgements WHERE user_hash=$1 AND policy_version=$2",
                account_hash, version,
            )
            if not isinstance(value, datetime):
                raise ChartAiStorageError("Grafik analizi kullanım onayı doğrulanamadı.")
            return utc(value)

    async def acknowledgement(self, user_id: str) -> datetime | None:
        async with self._transaction(user_hash(user_id)) as conn:
            value = await conn.fetchval(
                "SELECT accepted_at FROM chart_ai_acknowledgements WHERE user_hash=$1 AND policy_version=$2",
                user_hash(user_id), POLICY_VERSION,
            )
            if value is not None and not isinstance(value, datetime):
                raise ChartAiStorageError("Grafik analizi kullanım onayı doğrulanamadı.")
            return utc(value) if value is not None else None

    async def usage(self, user_id: str, access: QuotaAccess) -> QuotaSnapshot:
        account_hash = user_hash(user_id)
        async with self._transaction(account_hash) as conn:
            now = await self._now(conn)
            await self._recover_user(conn, account_hash, now)
            period = quota_period(access, now)
            row = await self._period(conn, account_hash, period)
            return QuotaSnapshot(period, row["used"], row["reserved"], now)

    async def reserve(self, user_id: str, access: QuotaAccess, key: UUID, content_hash: str) -> Reservation:
        if not isinstance(key, UUID) or not re.fullmatch(r"[a-f0-9]{64}", content_hash):
            raise ValueError("A UUID request key and SHA-256 content hash are required")
        account_hash = user_hash(user_id)
        async with self._transaction(account_hash) as conn:
            now = await self._now(conn)
            await self._recover_user(conn, account_hash, now)
            existing = await self._row(conn, account_hash, key)
            if existing is not None:
                if existing["content_hash"] != content_hash:
                    raise ChartAiQuotaError("idempotency_conflict", "İstek anahtarı farklı içerikle kullanılmış.", 409)
                existing = await self._expire_result(conn, existing, now)
                return Reservation(self._request(existing), False, None)
            period = quota_period(access, now)
            if await conn.fetchval(
                "SELECT accepted_at FROM chart_ai_acknowledgements WHERE user_hash=$1 AND policy_version=$2",
                account_hash, POLICY_VERSION,
            ) is None:
                raise ChartAiQuotaError("acknowledgement_required", "Kullanım uyarısını okuyup onaylayın.", 403)
            active = await conn.fetchrow(
                "SELECT * FROM chart_ai_requests WHERE user_hash=$1 AND status='processing' FOR UPDATE", account_hash,
            )
            if active is not None:
                raise ChartAiQuotaError("analysis_in_progress", "Devam eden analizinizin bitmesini bekleyin.", 409)
            quota = await self._period(conn, account_hash, period)
            if period.limit is not None and quota["used"] + quota["reserved"] >= period.limit:
                message = ("Deneme hakkın bitti, MASTER MODE ile haftada 20 analiz" if period.kind == "trial"
                           else "Haftalık hakkın doldu, Pazartesi yenilenir")
                raise ChartAiQuotaError("quota_exhausted", message, 429)
            month = now.date().replace(day=1)
            budget = await self._budget(conn, month)
            if budget["spent_microusd"] + budget["reserved_microusd"] + REQUEST_RESERVATION_MICROUSD > MONTHLY_BUDGET_MICROUSD:
                raise ChartAiQuotaError("budget_unavailable", "Şu an analiz yapılamıyor, daha sonra tekrar dene", 503)
            token = uuid4()
            await conn.execute(
                "UPDATE chart_ai_monthly_budget SET reserved_microusd=reserved_microusd+$2 WHERE month_start=$1",
                month, REQUEST_RESERVATION_MICROUSD,
            )
            await conn.execute(
                "UPDATE chart_ai_quota_periods SET reserved=reserved+1 WHERE user_hash=$1 AND period_key=$2",
                account_hash, period.key,
            )
            await conn.execute(
                """INSERT INTO chart_ai_requests
                   (user_hash,request_key,content_hash,period_key,month_start,status,lease_token,
                    lease_expires_at,created_at,budget_state)
                   VALUES ($1,$2,$3,$4,$5,'processing',$6,$7,$8,'reserved')""",
                account_hash, key, content_hash, period.key, month, token, now + LEASE_DURATION, now,
            )
            row = await self._row(conn, account_hash, key)
            if row is None:
                raise ChartAiStorageError("Grafik analizi rezervasyonu doğrulanamadı.")
            return Reservation(self._request(row), True, token)

    @staticmethod
    def _require_lease(row: Row | None, token: UUID) -> Row:
        if row is None:
            raise ChartAiQuotaError("request_not_found", "Analiz isteği bulunamadı.", 404)
        if row["lease_token"] != token:
            raise ChartAiQuotaError("lease_conflict", "Analiz işlem sahipliği doğrulanamadı.", 409)
        return row

    async def start_call(self, user_id: str, key: UUID, token: UUID) -> None:
        account_hash = user_hash(user_id)
        async with self._transaction(account_hash) as conn:
            now = await self._now(conn)
            row = self._require_lease(await self._row(conn, account_hash, key), token)
            if row["status"] != "processing" or now >= row["lease_expires_at"]:
                raise ChartAiQuotaError("lease_conflict", "Analiz rezervasyonunun süresi dolmuş.", 409)
            if row["provider_started_at"] is not None:
                raise ChartAiQuotaError("call_already_started", "Bu analiz çağrısı zaten başlatılmış.", 409)
            await conn.execute(
                "UPDATE chart_ai_requests SET provider_started_at=$3 WHERE user_hash=$1 AND request_key=$2",
                account_hash, key, now,
            )

    async def succeed(self, user_id: str, key: UUID, token: UUID, result_text: str, cost_microusd: int) -> ChartRequest:
        validate_cost(cost_microusd)
        if not isinstance(result_text, str) or not result_text.strip() or len(result_text) > 32_000:
            raise ValueError("A validated nonempty analysis result of at most 32000 characters is required")
        return await self._finish(user_id, key, token, result_text, None, cost_microusd)

    async def fail(self, user_id: str, key: UUID, token: UUID, error_code: str,
                   cost_microusd: int | None = None) -> ChartRequest:
        if not re.fullmatch(r"[a-z_]{1,64}", error_code):
            raise ValueError("A public error code, not provider error text, is required")
        if cost_microusd is not None:
            validate_cost(cost_microusd)
        return await self._finish(user_id, key, token, None, error_code, cost_microusd)

    async def _finish(self, user_id: str, key: UUID, token: UUID, result_text: str | None,
                      error_code: str | None, cost_microusd: int | None) -> ChartRequest:
        account_hash = user_hash(user_id)
        async with self._transaction(account_hash) as conn:
            now = await self._now(conn)
            row = self._require_lease(await self._row(conn, account_hash, key), token)
            if row["status"] != "processing":
                return self._request(await self._expire_result(conn, row, now))
            if now >= row["lease_expires_at"]:
                return self._request(await self._complete(conn, row, now, None, "lease_expired", None))
            if result_text is not None and row["provider_started_at"] is None:
                raise ChartAiQuotaError("call_not_started", "Analiz çağrısı başlatılmamış.", 409)
            return self._request(await self._complete(conn, row, now, result_text, error_code, cost_microusd))

    async def _complete(self, conn: ChartAiConnection, row: Row, now: datetime, result_text: str | None,
                        error_code: str | None, cost_microusd: int | None) -> Row:
        if row["provider_started_at"] is None:
            if cost_microusd not in (None, 0):
                raise ChartAiQuotaError("cost_before_call", "Çağrı başlamadan maliyet kaydedilemez.", 409)
            cost_microusd = 0
        await self._budget(conn, row["month_start"])
        if cost_microusd is not None:
            await self._settle_budget(conn, row["month_start"], cost_microusd)
        succeeded = result_text is not None
        await conn.execute(
            """UPDATE chart_ai_quota_periods SET reserved=reserved-1,used=used+$3
               WHERE user_hash=$1 AND period_key=$2""", row["user_hash"], row["period_key"], int(succeeded),
        )
        await conn.execute(
            """UPDATE chart_ai_requests SET status=$3,completed_at=$4,result_expires_at=$5,
               result_text=$6,error_code=$7,budget_state=$8,cost_microusd=$9
               WHERE user_hash=$1 AND request_key=$2""",
            row["user_hash"], row["request_key"], "succeeded" if succeeded else "failed", now,
            now + RESULT_RETENTION if succeeded else None, result_text, error_code,
            "settled" if cost_microusd is not None else "held", cost_microusd,
        )
        updated = await self._row(conn, row["user_hash"], row["request_key"])
        if updated is None:
            raise ChartAiStorageError("Grafik analizi sonucu kaydedilemedi.")
        return updated

    async def _settle_budget(self, conn: ChartAiConnection, month: date, cost: int) -> None:
        await conn.execute(
            """UPDATE chart_ai_monthly_budget SET reserved_microusd=reserved_microusd-$2,
               spent_microusd=spent_microusd+$3 WHERE month_start=$1""", month, REQUEST_RESERVATION_MICROUSD, cost,
        )
        await self._warn(conn, month)

    async def settle_held_cost(self, user_id: str, key: UUID, cost_microusd: int) -> None:
        validate_cost(cost_microusd)
        account_hash = user_hash(user_id)
        async with self._transaction(account_hash) as conn:
            row = await self._row(conn, account_hash, key)
            if row is None:
                raise ChartAiQuotaError("request_not_found", "Analiz isteği bulunamadı.", 404)
            if row["budget_state"] == "settled" and row["cost_microusd"] == cost_microusd:
                return
            if row["status"] != "failed" or row["budget_state"] != "held":
                raise ChartAiQuotaError("cost_conflict", "Analiz maliyet kaydıyla uyuşmazlık var.", 409)
            await self._budget(conn, row["month_start"])
            await self._settle_budget(conn, row["month_start"], cost_microusd)
            await conn.execute(
                """UPDATE chart_ai_requests SET budget_state='settled',cost_microusd=$3
                   WHERE user_hash=$1 AND request_key=$2""", account_hash, key, cost_microusd,
            )

    async def queue_budget_warning(self, month: date) -> None:
        if not isinstance(month, date) or isinstance(month, datetime) or month.day != 1:
            raise ValueError("A UTC calendar month start is required")
        async with self._transaction() as conn:
            await self._budget(conn, month)
            await self._warn(conn, month)

    @staticmethod
    async def _warn(conn: ChartAiConnection, month: date) -> None:
        budget = await conn.fetchrow("SELECT * FROM chart_ai_monthly_budget WHERE month_start=$1", month)
        if budget is None:
            raise ChartAiStorageError("Grafik analizi bildirim bütçesi doğrulanamadı.")
        if budget["spent_microusd"] < WARNING_THRESHOLD_MICROUSD or budget["warning_enqueued"]:
            return
        try:
            async with conn.transaction():
                owners = await conn.fetch(
                    """SELECT user_id FROM commercial_auth_users
                       WHERE security->>'role'='OWNER' AND security->'active'='true'::jsonb
                       AND security->'email_verified'='true'::jsonb
                       AND NOT EXISTS (SELECT 1 FROM commercial_erased_users e
                         WHERE e.user_hash=encode(sha256(convert_to(user_id,'UTF8')),'hex'))""",
                )
                if not owners:
                    logger.warning("Chart AI budget warning deferred code=owner_unavailable")
                    return
                payload = json.dumps({"month": month.strftime("%Y-%m"), "spent_microusd": budget["spent_microusd"],
                                      "budget_microusd": MONTHLY_BUDGET_MICROUSD})
                for owner in owners:
                    await conn.execute(
                        """INSERT INTO notification_outbox (id,kind,recipient_user_id,payload,dedupe_key)
                           VALUES ($1,'chart_ai.budget_warning',$2,$3::jsonb,$4)
                           ON CONFLICT (dedupe_key) DO NOTHING""", uuid4().hex, owner["user_id"], payload,
                        f"chart_ai.budget_warning:{month:%Y-%m}:{owner['user_id']}",
                    )
                await conn.execute(
                    "UPDATE chart_ai_monthly_budget SET warning_enqueued=TRUE WHERE month_start=$1", month,
                )
        except asyncpg.PostgresError as exc:
            logger.warning("Chart AI budget warning enqueue failed type=%s", type(exc).__name__)

    async def _expire_result(self, conn: ChartAiConnection, row: Row, now: datetime) -> Row:
        if row["status"] == "succeeded" and now >= row["result_expires_at"]:
            await conn.execute(
                """UPDATE chart_ai_requests SET status='expired',result_text=NULL
                   WHERE user_hash=$1 AND request_key=$2""", row["user_hash"], row["request_key"],
            )
            updated = await self._row(conn, row["user_hash"], row["request_key"])
            if updated is None:
                raise ChartAiStorageError("Grafik analizi sonuç süresi doğrulanamadı.")
            return updated
        return row

    async def _recover_user(self, conn: ChartAiConnection, account_hash: str, now: datetime) -> bool:
        row = await conn.fetchrow(
            "SELECT * FROM chart_ai_requests WHERE user_hash=$1 AND status='processing' FOR UPDATE", account_hash,
        )
        if row is None or now < row["lease_expires_at"]:
            return False
        await self._complete(conn, row, now, None, "lease_expired", None)
        return True

    async def request(self, user_id: str, key: UUID) -> ChartRequest:
        account_hash = user_hash(user_id)
        async with self._transaction(account_hash) as conn:
            now = await self._now(conn)
            await self._recover_user(conn, account_hash, now)
            row = await self._row(conn, account_hash, key)
            if row is None:
                raise ChartAiQuotaError("request_not_found", "Analiz isteği bulunamadı.", 404)
            return self._request(await self._expire_result(conn, row, now))

    async def recover_expired(self, *, limit: int = 100) -> int:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Recovery batch size must be between 1 and 1000")
        async with self._transaction() as conn:
            now = await self._now(conn)
            rows = await conn.fetch(
                """SELECT user_hash FROM chart_ai_requests WHERE status='processing' AND lease_expires_at<=$1
                   ORDER BY lease_expires_at,user_hash LIMIT $2""", now, limit,
            )
        recovered = 0
        for row in rows:
            async with self._transaction(row["user_hash"]) as conn:
                recovered += int(await self._recover_user(conn, row["user_hash"], await self._now(conn)))
        return recovered

    async def purge_results(self, *, limit: int = 100) -> int:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Cleanup batch size must be between 1 and 1000")
        async with self._transaction() as conn:
            now = await self._now(conn)
            rows = await conn.fetch(
                """WITH expired AS (
                     SELECT user_hash,request_key FROM chart_ai_requests
                     WHERE status='succeeded' AND result_expires_at<=$1
                     ORDER BY result_expires_at,user_hash,request_key FOR UPDATE SKIP LOCKED LIMIT $2
                   )
                   UPDATE chart_ai_requests r SET status='expired',result_text=NULL FROM expired e
                   WHERE r.user_hash=e.user_hash AND r.request_key=e.request_key RETURNING r.request_key""", now, limit,
            )
            return len(rows)
