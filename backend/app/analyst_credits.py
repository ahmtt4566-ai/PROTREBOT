"""Durable, per-member Analyst budgets and idempotent analysis purchases."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .local_storage import DATA_DIR
from .v22_commercial import access_snapshot, authenticated_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/analyst", tags=["Analyst credits"])

ANALYSIS_COST = 10


@dataclass(frozen=True)
class CreditConfig:
    total: int = 100
    cost: int = ANALYSIS_COST
    window_hours: int = 24
    cache_minutes: int = 15
    rate_limit: int = 30

    @classmethod
    def from_environment(cls) -> CreditConfig:
        config = cls(
            total=int(os.getenv("ANALYST_DAILY_CREDITS", "100")),
            cost=int(os.getenv("ANALYST_COST", str(ANALYSIS_COST))),
            window_hours=int(os.getenv("CREDIT_WINDOW_HOURS", "24")),
            cache_minutes=int(os.getenv("ANALYST_CACHE_MINUTES", "15")),
        )
        if min(config.total, config.cost, config.window_hours, config.cache_minutes) <= 0 or config.cost > config.total:
            raise ValueError("Analyst credit configuration must be positive; cost cannot exceed total")
        return config


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS analyst_credits (
        user_id TEXT PRIMARY KEY, credits_remaining INTEGER NOT NULL,
        window_start DOUBLE PRECISION, rate_start DOUBLE PRECISION NOT NULL,
        rate_count INTEGER NOT NULL DEFAULT 0, CHECK (credits_remaining >= 0))""",
    """CREATE TABLE IF NOT EXISTS analyst_requests (
        user_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL, status_code INTEGER NOT NULL, result TEXT NOT NULL,
        PRIMARY KEY (user_id, idempotency_key))""",
    """CREATE TABLE IF NOT EXISTS analyst_cache (
        user_id TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
        opened_at DOUBLE PRECISION NOT NULL, result TEXT NOT NULL,
        PRIMARY KEY (user_id, symbol, timeframe))""",
)


class CreditTransaction:
    def __init__(self, connection: Any, postgres: bool):
        self.connection = connection
        self.postgres = postgres

    async def execute(self, sql: str, *args: Any) -> None:
        if self.postgres:
            await self.connection.execute(sql, *args)
        else:
            await asyncio.to_thread(self.connection.execute, re.sub(r"\$\d+", "?", sql), args)

    async def row(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if self.postgres:
            row = await self.connection.fetchrow(sql, *args)
        else:
            def fetch() -> sqlite3.Row | None:
                return self.connection.execute(re.sub(r"\$\d+", "?", sql), args).fetchone()
            row = await asyncio.to_thread(fetch)
        return dict(row) if row is not None else None


class CreditStore:
    def __init__(self, application: Any, *, path: Path | None = None):
        self.application = application
        self.path = path or DATA_DIR / "analyst_credits.sqlite3"
        self.schema_lock = asyncio.Lock()
        self.initialized_pool: Any = None

    @asynccontextmanager
    async def transaction(self, user_id: str, config: CreditConfig, now: float) -> AsyncIterator[CreditTransaction]:
        pool = getattr(self.application.state, "db_pool", None)
        if pool is None and str(os.getenv("PROTREBOT_DURABLE_AUTH_REQUIRED", "")).lower() in {"true", "1", "yes", "on"}:
            raise HTTPException(503, "Analyst credit storage is unavailable")
        if pool is not None:
            async with self.schema_lock:
                if self.initialized_pool is not pool:
                    for statement in SCHEMA:
                        await pool.execute(statement)
                    self.initialized_pool = pool
            async with pool.acquire() as connection, connection.transaction():
                transaction = CreditTransaction(connection, True)
                await transaction.execute(
                    "INSERT INTO analyst_credits (user_id, credits_remaining, rate_start) VALUES ($1,$2,$3) ON CONFLICT (user_id) DO NOTHING",
                    user_id, config.total, now,
                )
                await transaction.row("SELECT user_id FROM analyst_credits WHERE user_id=$1 FOR UPDATE", user_id)
                yield transaction
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await asyncio.to_thread(sqlite3.connect, str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            for statement in SCHEMA:
                await asyncio.to_thread(connection.execute, statement)
            await asyncio.to_thread(connection.execute, "BEGIN IMMEDIATE")
            transaction = CreditTransaction(connection, False)
            await transaction.execute(
                "INSERT INTO analyst_credits (user_id, credits_remaining, rate_start) VALUES ($1,$2,$3) ON CONFLICT (user_id) DO NOTHING",
                user_id, config.total, now,
            )
            try:
                yield transaction
            except BaseException:
                await asyncio.to_thread(connection.rollback)
                raise
            else:
                await asyncio.to_thread(connection.commit)
        finally:
            await asyncio.to_thread(connection.close)


class AnalystCredits:
    def __init__(self, store: CreditStore, config: CreditConfig | None = None, clock: Callable[[], float] = time.time):
        self.store = store
        self.config = config or CreditConfig.from_environment()
        self.clock = clock

    def snapshot(self, remaining: int, window_start: float | None, premium: bool = False) -> dict[str, Any]:
        resets = window_start + self.config.window_hours * 3600 if window_start is not None else None
        return {
            "remaining": None if premium else remaining, "total": self.config.total,
            "analysis_cost": self.config.cost,
            "resetsAt": datetime.fromtimestamp(resets, timezone.utc).isoformat() if resets is not None and not premium else None,
            "unlimited": premium,
        }

    async def account(self, transaction: CreditTransaction, user_id: str, now: float) -> tuple[int, float | None]:
        row = await transaction.row("SELECT * FROM analyst_credits WHERE user_id=$1", user_id)
        if row is None:
            raise RuntimeError("Analyst credit account was not created")
        start = row["window_start"]
        if start is not None and now >= start + self.config.window_hours * 3600:
            await transaction.execute("UPDATE analyst_credits SET credits_remaining=$1, window_start=NULL WHERE user_id=$2", self.config.total, user_id)
            return self.config.total, None
        return int(row["credits_remaining"]), start

    async def credits(self, user_id: str, premium: bool) -> dict[str, Any]:
        if premium:
            return self.snapshot(self.config.total, None, True)
        now = self.clock()
        async with self.store.transaction(user_id, self.config, now) as transaction:
            remaining, start = await self.account(transaction, user_id, now)
            return self.snapshot(remaining, start)

    async def cached_analysis(self, user_id: str, symbol: str, timeframe: str) -> dict[str, Any] | None:
        now = self.clock()
        async with self.store.transaction(user_id, self.config, now) as transaction:
            cached = await transaction.row(
                "SELECT * FROM analyst_cache WHERE user_id=$1 AND symbol=$2 AND timeframe=$3",
                user_id, symbol, timeframe,
            )
            now = self.clock()
            if cached is None or not 0 <= now - cached["opened_at"] < self.config.cache_minutes * 60:
                return None
            return {"result": json.loads(cached["result"]), "opened_at": cached["opened_at"]}

    async def consume(self, user_id: str, premium: bool, symbol: str, timeframe: str, key: str, analyze: Callable[[], Awaitable[dict[str, Any]]]) -> tuple[dict[str, Any], int]:
        now = self.clock()
        async with self.store.transaction(user_id, self.config, now) as transaction:
            remaining, start = await self.account(transaction, user_id, now)
            rate = await transaction.row("SELECT rate_start, rate_count FROM analyst_credits WHERE user_id=$1", user_id)
            if rate is None:
                raise RuntimeError("Analyst rate-limit account was not created")
            rate_start = now if now >= rate["rate_start"] + 60 else rate["rate_start"]
            count = 0 if rate_start != rate["rate_start"] else rate["rate_count"]
            if count >= self.config.rate_limit:
                logger.warning("Analyst rate limit exceeded user_id=%s symbol=%s timeframe=%s", user_id, symbol, timeframe)
                return {"detail": "Çok fazla analiz isteği.", **self.snapshot(remaining, start, premium), "retryAfter": max(1, int(rate_start + 60 - now))}, 429
            await transaction.execute("UPDATE analyst_credits SET rate_start=$1, rate_count=$2 WHERE user_id=$3", rate_start, count + 1, user_id)
            previous = await transaction.row("SELECT * FROM analyst_requests WHERE user_id=$1 AND idempotency_key=$2", user_id, key)
            if previous is not None:
                if (previous["symbol"], previous["timeframe"]) != (symbol, timeframe):
                    logger.warning("Analyst idempotency conflict user_id=%s", user_id)
                    return {"detail": "Idempotency key farklı bir analiz için kullanılmış."}, 409
                return {**json.loads(previous["result"]), **self.snapshot(remaining, start, premium), "replayed": True}, previous["status_code"]
            await transaction.execute("DELETE FROM analyst_cache WHERE user_id=$1 AND opened_at <= $2", user_id, now - self.config.cache_minutes * 60)
            cached = await transaction.row("SELECT * FROM analyst_cache WHERE user_id=$1 AND symbol=$2 AND timeframe=$3", user_id, symbol, timeframe)
            if cached is not None:
                response = {"result": json.loads(cached["result"]), "cached": True, "cacheExpiresAt": datetime.fromtimestamp(cached["opened_at"] + self.config.cache_minutes * 60, timezone.utc).isoformat(), **self.snapshot(remaining, start, premium)}
                status_code = 200
            elif not premium and remaining < self.config.cost:
                logger.info("Analyst credits exhausted user_id=%s", user_id)
                return {"detail": "Günlük analiz kredileri tükendi.", **self.snapshot(remaining, start)}, 429
            else:
                charged = 0 if premium else self.config.cost
                if charged:
                    start = now if start is None else start
                    remaining -= charged
                    await transaction.execute("UPDATE analyst_credits SET credits_remaining=$1, window_start=$2 WHERE user_id=$3", remaining, start, user_id)
                try:
                    result = await analyze()
                    encoded = json.dumps(result, allow_nan=False)
                except Exception:
                    logger.exception("Analyst analysis failed; refunding once user_id=%s symbol=%s timeframe=%s", user_id, symbol, timeframe)
                    remaining += charged
                    await transaction.execute("UPDATE analyst_credits SET credits_remaining=$1 WHERE user_id=$2", remaining, user_id)
                    response = {"detail": "Analiz oluşturulamadı; kredi iade edildi.", **self.snapshot(remaining, start, premium)}
                    status_code = 502
                else:
                    await transaction.execute("INSERT INTO analyst_cache (user_id,symbol,timeframe,opened_at,result) VALUES ($1,$2,$3,$4,$5)", user_id, symbol, timeframe, now, encoded)
                    response = {"result": result, "cached": False, "cacheExpiresAt": datetime.fromtimestamp(now + self.config.cache_minutes * 60, timezone.utc).isoformat(), **self.snapshot(remaining, start, premium)}
                    status_code = 200
            await transaction.execute(
                "INSERT INTO analyst_requests (user_id,idempotency_key,symbol,timeframe,status_code,result) VALUES ($1,$2,$3,$4,$5,$6)",
                user_id, key, symbol, timeframe, status_code, json.dumps(response, allow_nan=False),
            )
        return response, status_code


class ConsumeAnalysis(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,25}USDT$")
    timeframe: str = Field(pattern=r"^(1m|5m|15m|30m|1h|4h|1d)$")
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def member_access(request: Request) -> tuple[dict[str, Any], bool]:
    user = authenticated_user(request)
    access = access_snapshot(request.app.state.v22_commercial["state"], user)
    return user, bool(access.get("isPremium", access.get("canAccessMasterTrade")))


def service(request: Request) -> AnalystCredits:
    existing = getattr(request.app.state, "analyst_credits", None)
    if existing is None:
        existing = AnalystCredits(CreditStore(request.app))
        request.app.state.analyst_credits = existing
    return existing


@router.get("/credits")
async def get_credits(request: Request):
    user, premium = member_access(request)
    return await service(request).credits(user["id"], premium)


@router.post("/consume")
async def consume_analysis(payload: ConsumeAnalysis, request: Request):
    user, premium = member_access(request)
    producer = request.app.state.analyst_analysis
    response, status = await service(request).consume(
        user["id"], premium, payload.symbol, payload.timeframe, payload.idempotency_key,
        lambda: producer(payload.symbol, payload.timeframe),
    )
    headers = {"Retry-After": str(response["retryAfter"])} if "retryAfter" in response else None
    return JSONResponse(response, status_code=status, headers=headers)
