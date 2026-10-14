"""Shared Binance REST rate-limit coordination."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, AsyncIterator

from .local_storage import DATA_DIR


RATE_LIMIT_AWARE_BACKOFF_ENABLED = os.getenv(
    "BINANCE_RATE_LIMIT_AWARE_BACKOFF_ENABLED",
    os.getenv("V25_RATE_LIMIT_AWARE_BACKOFF_ENABLED", "true"),
).strip().lower() not in {"0", "false", "no", "off"}
BINANCE_REQUEST_WEIGHT_LIMIT_1M = int(os.getenv("BINANCE_REQUEST_WEIGHT_LIMIT_1M", "2400"))
BINANCE_REQUEST_WEIGHT_THRESHOLD_RATIO = float(os.getenv("BINANCE_REQUEST_WEIGHT_THRESHOLD_RATIO", "0.8"))
RATE_LIMIT_PROACTIVE_WAIT_SECONDS = float(os.getenv("BINANCE_RATE_LIMIT_PROACTIVE_WAIT_SECONDS", "1"))
RATE_LIMIT_MAX_WAIT_SECONDS = max(1, int(os.getenv("BINANCE_RATE_LIMIT_MAX_WAIT_SECONDS", "60")))
RATE_LIMIT_FALLBACK_SECONDS = min(
    max(1, int(os.getenv("BINANCE_RATE_LIMIT_FALLBACK_SECONDS", "10"))),
    RATE_LIMIT_MAX_WAIT_SECONDS,
)
BINANCE_MAX_CONCURRENT_REQUESTS_PER_HOST = max(
    1, int(os.getenv("BINANCE_MAX_CONCURRENT_REQUESTS_PER_HOST", "2"))
)
logger = logging.getLogger(__name__)
GLOBAL_BAN_PATH = Path(
    os.getenv("BINANCE_GLOBAL_BAN_PATH", str(DATA_DIR / "binance_global_ban.json")),
)


def retry_after_seconds(response: Any) -> int | None:
    value = getattr(response, "headers", {}).get("Retry-After", "").strip()
    if not value:
        return None
    try:
        delay = int(value) if value.isdigit() else int(parsedate_to_datetime(value).timestamp() - time.time()) + 1
    except (ValueError, TypeError, OverflowError):
        logger.warning("Invalid Binance Retry-After header; using bounded fallback.")
        return None
    if delay < 0 or delay > 31_536_000:
        logger.warning("Invalid Binance Retry-After delay; using bounded fallback.")
        return None
    return max(1, delay)


def account_egress_route() -> str:
    return "quotaguard" if os.getenv("QUOTAGUARD_URL", "").strip() else "render_direct"


class BinanceGlobalBan:
    """Process-wide, restart-durable Binance 418/429 stop clock."""

    def __init__(self, path: Path = GLOBAL_BAN_PATH) -> None:
        self.path = path
        self._state: dict[str, Any] = {}
        self._pool: Any | None = None
        self._persistence_tasks: set[asyncio.Task] = set()
        self._load()

    def _validated_state(self, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            logger.error("BINANCE_GLOBAL_BAN load_failed error_type=InvalidPayload")
            return None
        try:
            blocked_until = float(payload.get("blocked_until_epoch") or 0)
            observed_at = float(payload.get("observed_at_epoch") or time.time())
            status_code = int(payload.get("status_code") or 429)
        except (TypeError, ValueError, OverflowError):
            logger.error("BINANCE_GLOBAL_BAN load_failed error_type=InvalidDeadline")
            return None
        if blocked_until <= time.time():
            return None
        return {
            "blocked_until_epoch": blocked_until,
            "observed_at_epoch": observed_at,
            "status_code": status_code,
            "exchange_code": payload.get("exchange_code"),
            "used_weight_1m": payload.get("used_weight_1m"),
            "source": str(payload.get("source") or "unknown")[:80],
            "egress_route": str(payload.get("egress_route") or "unknown")[:40],
        }

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logger.error("BINANCE_GLOBAL_BAN load_failed error_type=%s", type(exc).__name__)
            return
        state = self._validated_state(payload)
        if state is None:
            self._delete_expired_file()
            return
        self._state = state
        logger.warning(
            "BINANCE_GLOBAL_BAN restored blocked_until=%s source=%s egress_route=%s",
            self.public_status()["blocked_until"],
            self._state["source"],
            self._state["egress_route"],
        )

    def _delete_expired_file(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("BINANCE_GLOBAL_BAN cleanup_failed error_type=%s", type(exc).__name__)

    def _persist(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self._state, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError as exc:
            logger.error("BINANCE_GLOBAL_BAN persist_failed error_type=%s", type(exc).__name__)

    def _track_persistence(self, task: asyncio.Task) -> None:
        self._persistence_tasks.add(task)
        task.add_done_callback(self._persistence_tasks.discard)

    def _schedule_database_persist(self) -> None:
        if self._pool is None or not self._state:
            return
        try:
            task = asyncio.create_task(self._persist_database(dict(self._state)))
        except RuntimeError:
            logger.error("BINANCE_GLOBAL_BAN database_persist_failed error_type=NoRunningLoop")
            return
        self._track_persistence(task)

    async def _persist_database(self, state: dict[str, Any]) -> None:
        try:
            await self._pool.execute(
                """
                INSERT INTO binance_global_ban_state (state_key, payload, updated_at)
                VALUES ('global', $1::jsonb, NOW())
                ON CONFLICT (state_key) DO UPDATE
                SET payload = EXCLUDED.payload, updated_at = NOW()
                WHERE COALESCE(
                    (binance_global_ban_state.payload->>'blocked_until_epoch')::double precision,
                    0
                ) <= (EXCLUDED.payload->>'blocked_until_epoch')::double precision
                """,
                json.dumps(state, ensure_ascii=True),
            )
        except Exception as exc:
            logger.error(
                "BINANCE_GLOBAL_BAN database_persist_failed error_type=%s",
                type(exc).__name__,
            )

    async def configure_persistence(self, pool: Any | None) -> None:
        self._pool = pool
        if pool is None:
            logger.error("BINANCE_GLOBAL_BAN durable_store_unavailable")
            return
        try:
            await pool.execute(
                """
                CREATE TABLE IF NOT EXISTS binance_global_ban_state (
                    state_key TEXT PRIMARY KEY,
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            row = await pool.fetchrow(
                "SELECT payload FROM binance_global_ban_state WHERE state_key = 'global'"
            )
        except Exception as exc:
            logger.error(
                "BINANCE_GLOBAL_BAN durable_store_init_failed error_type=%s",
                type(exc).__name__,
            )
            return
        database_state = self._validated_state(row["payload"]) if row else None
        if database_state is not None and float(database_state["blocked_until_epoch"]) > float(
            self._state.get("blocked_until_epoch") or 0
        ):
            self._state = database_state
            self._persist()
            logger.warning(
                "BINANCE_GLOBAL_BAN restored_from_database blocked_until=%s source=%s egress_route=%s",
                self.public_status()["blocked_until"],
                self._state["source"],
                self._state["egress_route"],
            )
        elif self.active():
            await self._persist_database(dict(self._state))

    async def flush_persistence(self) -> None:
        if self._persistence_tasks:
            await asyncio.gather(*tuple(self._persistence_tasks), return_exceptions=True)

    def remaining_seconds(self) -> int:
        try:
            remaining = int(float(self._state.get("blocked_until_epoch") or 0) - time.time() + 0.999)
        except (TypeError, ValueError, OverflowError):
            remaining = 0
        if remaining <= 0 and self._state:
            self._state = {}
            self._delete_expired_file()
            return 0
        return max(0, remaining)

    def active(self) -> bool:
        return self.remaining_seconds() > 0

    def record(
        self,
        response: Any,
        *,
        source: str,
        egress_route: str,
        exchange_code: int | str | None = None,
    ) -> bool:
        status_code = int(getattr(response, "status_code", 0) or 0)
        try:
            payload = response.json()
        except (AttributeError, ValueError, TypeError):
            payload = None
        code = exchange_code
        if code is None and isinstance(payload, dict):
            code = payload.get("code")
        try:
            is_rate_limited = int(code) == -1003
        except (TypeError, ValueError):
            is_rate_limited = False
        if status_code not in {418, 429} and not is_rate_limited:
            return False
        delay = retry_after_seconds(response) or max(1, RATE_LIMIT_FALLBACK_SECONDS)
        now = time.time()
        blocked_until = max(
            float(self._state.get("blocked_until_epoch") or 0),
            now + delay,
        )
        headers = getattr(response, "headers", {})
        used_weight = headers.get("X-MBX-USED-WEIGHT-1M")
        self._state = {
            "blocked_until_epoch": blocked_until,
            "observed_at_epoch": now,
            "status_code": status_code or 429,
            "exchange_code": code,
            "used_weight_1m": int(used_weight) if str(used_weight or "").isdigit() else None,
            "source": str(source or "unknown")[:80],
            "egress_route": str(egress_route or "unknown")[:40],
        }
        self._persist()
        self._schedule_database_persist()
        logger.warning(
            "BINANCE_GLOBAL_BAN recorded status=%s code=%s retry_after_seconds=%s "
            "blocked_until=%s source=%s egress_route=%s used_weight_1m=%s",
            self._state["status_code"],
            self._state["exchange_code"],
            delay,
            self.public_status()["blocked_until"],
            self._state["source"],
            self._state["egress_route"],
            self._state["used_weight_1m"],
        )
        return True

    def public_status(self) -> dict[str, Any]:
        remaining = self.remaining_seconds()
        if remaining <= 0:
            return {
                "active": False,
                "blocked_until": None,
                "remaining_seconds": 0,
                "message": None,
                "source": None,
                "egress_route": None,
                "status_code": None,
                "exchange_code": None,
                "used_weight_1m": None,
            }
        blocked_until_epoch = float(self._state["blocked_until_epoch"])
        blocked_until = datetime.fromtimestamp(blocked_until_epoch, timezone.utc).isoformat()
        return {
            "active": True,
            "blocked_until": blocked_until,
            "remaining_seconds": remaining,
            "message": f"Binance geçici olarak engelledi, {blocked_until} tarihine kadar bekleniyor.",
            "source": self._state.get("source"),
            "egress_route": self._state.get("egress_route"),
            "status_code": self._state.get("status_code"),
            "exchange_code": self._state.get("exchange_code"),
            "used_weight_1m": self._state.get("used_weight_1m"),
        }

    def synthetic_response(self, path: str) -> Any | None:
        status = self.public_status()
        if not status["active"]:
            return None
        import httpx
        return httpx.Response(
            int(status["status_code"] or 429),
            headers={"Retry-After": str(status["remaining_seconds"])},
            json={"code": -1003, "msg": "Binance global cooldown active."},
            request=httpx.Request("GET", f"https://fapi.binance.com{path}"),
        )

    async def wait(self) -> None:
        while True:
            remaining = self.remaining_seconds()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    def reset(self, *, clear_persistent: bool = False) -> None:
        self._state = {}
        if clear_persistent:
            self._delete_expired_file()


BINANCE_GLOBAL_BAN = BinanceGlobalBan()


@dataclass
class _HostState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cooldown_until: float = 0.0
    used_weight_1m: int | None = None
    counts: Counter[str] = field(default_factory=Counter)
    metrics_at: float = field(default_factory=time.monotonic)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    pending: list[tuple[int, int]] = field(default_factory=list)
    sequence: int = 0
    active: int = 0
    source_counts: Counter[str] = field(default_factory=Counter)
    source_weights: Counter[str] = field(default_factory=Counter)


class RateLimitPermit:
    def __init__(
        self,
        limiter: "BinanceRateLimiter",
        host: str,
        state: _HostState,
        source: str = "other",
        egress_route: str = "unknown",
    ) -> None:
        self._limiter = limiter
        self.host = host
        self._state = state
        self.source = source
        self.egress_route = egress_route

    def observe(self, response: Any, *, exchange_code: int | str | None = None) -> None:
        self._limiter.observe(
            self.host,
            response,
            exchange_code=exchange_code,
            source=self.source,
            egress_route=self.egress_route,
        )


class BinanceRateLimiter:
    """Bound Binance calls per host and coordinate process-local cooldowns."""

    def __init__(self) -> None:
        self._states: dict[str, _HostState] = {}

    def _state_for(self, host: str) -> _HostState:
        return self._states.setdefault(host, _HostState())

    async def _wait(self, state: _HostState) -> None:
        if not RATE_LIMIT_AWARE_BACKOFF_ENABLED:
            return
        remaining = state.cooldown_until - time.monotonic()
        while remaining > 0:
            await asyncio.sleep(remaining)
            remaining = state.cooldown_until - time.monotonic()
        state.cooldown_until = 0.0

    @asynccontextmanager
    async def slot(
        self,
        host: str,
        *,
        priority: bool = False,
        source: str = "other",
        egress_route: str = "unknown",
    ) -> AsyncIterator[RateLimitPermit]:
        state = self._state_for(host)
        async with state.condition:
            state.sequence += 1
            ticket = (0 if priority else 1, state.sequence)
            state.pending.append(ticket)
        admitted = False
        try:
            await BINANCE_GLOBAL_BAN.wait()
            await self._wait(state)
            async with state.condition:
                await state.condition.wait_for(lambda: state.active < BINANCE_MAX_CONCURRENT_REQUESTS_PER_HOST and ticket == min(state.pending))
                state.active += 1
                admitted = True
                state.pending.remove(ticket)
                state.condition.notify_all()
            async with state.lock:
                await self._wait(state)
                await BINANCE_GLOBAL_BAN.wait()
            yield RateLimitPermit(self, host, state, source, egress_route)
        finally:
            async with state.condition:
                if admitted:
                    state.active -= 1
                else:
                    state.pending.remove(ticket)
                state.condition.notify_all()

    def observe(
        self,
        host: str,
        response: Any,
        *,
        exchange_code: int | str | None = None,
        source: str = "other",
        egress_route: str = "unknown",
    ) -> None:
        state = self._state_for(host)
        try:
            request = response.request
            request_key = f"{request.method}:{request.url.path}"
            state.counts[f"{request_key}:{response.status_code}"] += 1
        except (AttributeError, RuntimeError):
            request_key = "UNKNOWN:unknown"
            state.counts[f"{request_key}:{getattr(response, 'status_code', 0)}"] += 1
        source_key = f"{source}:{request_key}"
        state.source_counts[source_key] += 1
        state.source_weights[source_key] += 1
        headers = getattr(response, "headers", {})
        used_weight = headers.get("X-MBX-USED-WEIGHT-1M", "")
        if used_weight.isdigit():
            state.used_weight_1m = int(used_weight)
        if time.monotonic() - state.metrics_at >= 60:
            logger.info("BINANCE_ACCOUNT_METRICS host=%s window_seconds=%.1f used_weight_1m=%s counts=%s sources=%s estimated_weights=%s",
                        host, time.monotonic() - state.metrics_at, state.used_weight_1m,
                        dict(state.counts), dict(state.source_counts), dict(state.source_weights))
            state.counts.clear()
            state.source_counts.clear()
            state.source_weights.clear()
            state.metrics_at = time.monotonic()

        status_code = int(getattr(response, "status_code", 0) or 0)
        code = exchange_code
        try:
            payload = response.json()
        except (AttributeError, ValueError, TypeError):
            payload = None
        if code is None and isinstance(payload, dict):
            code = payload.get("code")
        try:
            is_rate_limited_code = int(code) == -1003
        except (TypeError, ValueError):
            is_rate_limited_code = False
        rate_limited = status_code in {429, 418} or is_rate_limited_code
        if rate_limited and source != "other":
            BINANCE_GLOBAL_BAN.record(
                response,
                source=source,
                egress_route=egress_route,
                exchange_code=code,
            )
        if not RATE_LIMIT_AWARE_BACKOFF_ENABLED:
            return
        if rate_limited:
            delay = retry_after_seconds(response) or max(1, RATE_LIMIT_FALLBACK_SECONDS)
            state.cooldown_until = max(state.cooldown_until, time.monotonic() + delay)
        elif (
            state.used_weight_1m is not None
            and state.used_weight_1m >= BINANCE_REQUEST_WEIGHT_LIMIT_1M * BINANCE_REQUEST_WEIGHT_THRESHOLD_RATIO
        ):
            state.cooldown_until = max(
                state.cooldown_until,
                time.monotonic() + max(RATE_LIMIT_PROACTIVE_WAIT_SECONDS, 61 - time.time() % 60),
            )

    def snapshot(self, host: str) -> dict[str, int | float | None]:
        state = self._state_for(host)
        return {
            "cooldown_until": state.cooldown_until,
            "used_weight_1m": state.used_weight_1m,
        }

    def reset(self) -> None:
        self._states.clear()
        BINANCE_GLOBAL_BAN.reset()


BINANCE_RATE_LIMITER = BinanceRateLimiter()
