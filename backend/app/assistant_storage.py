"""Assistant-only quota and monthly accounting storage."""
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

from .analyst_credits import CreditTransaction
from .local_storage import DATA_DIR

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS assistant_usage (
        user_id TEXT PRIMARY KEY, day_key TEXT NOT NULL, daily_count INTEGER NOT NULL,
        minute_start DOUBLE PRECISION NOT NULL, minute_count INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS assistant_monthly_spend (
        month_key TEXT PRIMARY KEY, spent_usd TEXT NOT NULL, warned INTEGER NOT NULL,
        accounting_blocked INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS assistant_calls (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, month_key TEXT NOT NULL,
        called_at DOUBLE PRECISION NOT NULL, input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL, cost_usd TEXT NOT NULL, estimated BOOLEAN NOT NULL,
        cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_input_tokens INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS assistant_proactive_state (
        user_id TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL DEFAULT TRUE,
        last_seen_at DOUBLE PRECISION, last_checkin_at DOUBLE PRECISION)""",
)
TOKEN_COLUMNS = ("cache_creation_input_tokens", "cache_read_input_tokens")


class AssistantStorageError(RuntimeError):
    pass


class AssistantStore:
    def __init__(self, application: Any, *, path: Path | None = None):
        self.application = application
        self.path = path or DATA_DIR / "assistant_usage.sqlite3"
        self.schema_lock = asyncio.Lock()
        self.initialized_pool: Any = None
        self.sqlite_initialized = False
        self.storage_kind: str | None = None
        self.blocked_months: set[str] = set()

    def block_path(self, month: str) -> Path:
        return self.path.with_name(f"assistant-accounting-{month}.blocked")

    async def accounting_blocked(self, month: str) -> bool:
        return month in self.blocked_months or await asyncio.to_thread(self.block_path(month).exists)

    async def block_accounting(self, month: str, timeout: int) -> None:
        self.blocked_months.add(month)
        try:
            async with self.connection(timeout) as transaction:
                await transaction.execute(
                    """INSERT INTO assistant_monthly_spend (month_key,spent_usd,warned,accounting_blocked)
                       VALUES ($1,'0',0,1) ON CONFLICT (month_key) DO UPDATE SET accounting_blocked=1""",
                    month,
                )
        except (AssistantStorageError, asyncpg.PostgresError, asyncpg.InterfaceError, sqlite3.Error, OSError, TimeoutError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self.block_path(month).write_text, "uncertain\n", encoding="ascii")
            raise

    @asynccontextmanager
    async def connection(self, timeout: int) -> AsyncGenerator[CreditTransaction, None]:
        pool = getattr(self.application.state, "db_pool", None)
        kind = "postgres" if pool is not None else "sqlite"
        if self.storage_kind is not None and self.storage_kind != kind:
            raise AssistantStorageError("Assistant storage changed; accounting migration is required")
        self.storage_kind = kind
        if pool is not None:
            async with self.schema_lock:
                if self.initialized_pool is not pool:
                    for statement in SCHEMA:
                        await pool.execute(statement, timeout=timeout)
                    for column in TOKEN_COLUMNS:
                        await pool.execute(f"ALTER TABLE assistant_calls ADD COLUMN IF NOT EXISTS {column} INTEGER NOT NULL DEFAULT 0", timeout=timeout)
                    self.initialized_pool = pool
            async with pool.acquire(timeout=timeout) as connection:
                yield CreditTransaction(connection, True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await asyncio.to_thread(
            sqlite3.connect, str(self.path), timeout=timeout, isolation_level=None, check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            async with self.schema_lock:
                if not self.sqlite_initialized:
                    await asyncio.to_thread(connection.execute, "BEGIN IMMEDIATE")
                    try:
                        for statement in SCHEMA:
                            await asyncio.to_thread(connection.execute, statement)
                        cursor = await asyncio.to_thread(connection.execute, "PRAGMA table_info(assistant_calls)")
                        columns = {row["name"] for row in await asyncio.to_thread(cursor.fetchall)}
                        for column in TOKEN_COLUMNS:
                            if column not in columns:
                                await asyncio.to_thread(connection.execute, f"ALTER TABLE assistant_calls ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
                        await asyncio.to_thread(connection.commit)
                    except BaseException:
                        await asyncio.to_thread(connection.rollback)
                        raise
                    self.sqlite_initialized = True
            yield CreditTransaction(connection, False)
        finally:
            await asyncio.to_thread(connection.close)

    @asynccontextmanager
    async def monthly_transaction(self, month: str, timeout: int) -> AsyncGenerator[CreditTransaction, None]:
        async with self.connection(timeout) as transaction:
            if transaction.postgres:
                async with transaction.connection.transaction():
                    await transaction.execute("SELECT set_config('lock_timeout', $1, true)", f"{timeout}s")
                    await self.lock_month(transaction, month)
                    yield transaction
            else:
                await transaction.execute("BEGIN IMMEDIATE")
                try:
                    await self.lock_month(transaction, month)
                    yield transaction
                except BaseException:
                    await asyncio.to_thread(transaction.connection.rollback)
                    raise
                else:
                    await asyncio.to_thread(transaction.connection.commit)

    @staticmethod
    async def lock_month(transaction: CreditTransaction, month: str) -> None:
        await transaction.execute(
            "INSERT INTO assistant_monthly_spend (month_key,spent_usd,warned) VALUES ($1,'0',0) ON CONFLICT (month_key) DO NOTHING",
            month,
        )
        if transaction.postgres:
            await transaction.row("SELECT month_key FROM assistant_monthly_spend WHERE month_key=$1 FOR UPDATE", month)

    async def usage(self, user_id: str, timeout: int) -> dict[str, Any] | None:
        async with self.connection(timeout) as transaction:
            return await transaction.row("SELECT * FROM assistant_usage WHERE user_id=$1", user_id)

    @asynccontextmanager
    async def proactive_transaction(self, user_id: str, timeout: int) -> AsyncGenerator[CreditTransaction, None]:
        async with self.connection(timeout) as transaction:
            async def lock_user() -> None:
                await transaction.execute(
                    "INSERT INTO assistant_proactive_state (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                    user_id,
                )
                if transaction.postgres:
                    await transaction.row("SELECT user_id FROM assistant_proactive_state WHERE user_id=$1 FOR UPDATE", user_id)

            if transaction.postgres:
                async with transaction.connection.transaction():
                    await transaction.execute("SELECT set_config('lock_timeout', $1, true)", f"{timeout}s")
                    await lock_user()
                    yield transaction
            else:
                await transaction.execute("BEGIN IMMEDIATE")
                try:
                    await lock_user()
                    yield transaction
                except BaseException:
                    await asyncio.to_thread(transaction.connection.rollback)
                    raise
                else:
                    await asyncio.to_thread(transaction.connection.commit)


def month_key(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
