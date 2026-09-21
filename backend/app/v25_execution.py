"""V25 Live Guard: fail-closed Binance USD-M Futures execution plane.

Live trading is compiled as a separate transport from Demo.  It starts read
only and requires all of the following before an entry can be submitted:
encrypted local credentials, a 24-hour key-bound local consent, a completed
Demo evidence certificate, an acknowledged risk policy, and a five-minute
owner arm.  Cancellation, protection repair, and tracked-position closure are
always available because they reduce risk.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .analysis import analyze
from .binance_demo import (
    BinanceDemoError,
    account_snapshot,
    decimal_text,
    floor_step,
    normalize_symbol,
    response_rows,
    round_tick,
    validate_levels,
    verify_leverage_response,
    verify_symbol_configuration,
)
from .credential_store import live_credential_source, load_live_consent, load_live_credentials
from .execution_core import (
    DEFAULT_EXECUTION_POLICY,
    LIVE_CLIENT_PREFIX,
    V25_VERSION,
    credential_fingerprint,
    daily_execution_metrics,
    evaluate_entry_gates,
    policy_digest,
    release_gates,
    release_ready,
    risk_sized_order,
    sanitize_execution_policy,
)
from .local_storage import DATA_DIR, migrate_legacy_files
from .v21_demo import certificate_payload
from .v22_commercial import authenticated_user


logger = logging.getLogger(__name__)
AUTOMATION_TELEMETRY_INTERVAL = 30.0
_automation_telemetry_at: dict[str, float] = {}


def automation_telemetry(message: str, *, reason: str | None = None) -> None:
    key = reason or message
    now = time.monotonic()
    if now - _automation_telemetry_at.get(key, 0.0) < AUTOMATION_TELEMETRY_INTERVAL:
        return
    _automation_telemetry_at[key] = now
    logger.info(message)


LIVE_REST_BASE = "https://fapi.binance.com"
LIVE_WS_BASE = "wss://fstream.binance.com/private"
LIVE_ARM_SECONDS = 5 * 60
LIVE_AUTO_SESSION_SECONDS = 60 * 60
RECONCILE_SECONDS = 10
MAX_EVENTS = 500
MAX_PLANS = 250
MARKET_SCAN_LIMIT = 100
DEEP_ANALYSIS_LIMIT = 100
MIN_24H_QUOTE_VOLUME = 1_000_000.0
MIN_24H_MOVE_PCT = 0.25
BLOCKED_BASE_ASSETS = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USD1", "USDE", "USDS"}

PUBLIC_PATHS = {
    "/fapi/v1/time",
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/ticker/price",
    "/fapi/v1/ticker/bookTicker",
    "/fapi/v1/ticker/24hr",
    "/fapi/v1/klines",
}
PRIVATE_PATHS = {
    ("GET", "/fapi/v3/account"),
    ("GET", "/fapi/v3/positionRisk"),
    ("GET", "/fapi/v1/symbolConfig"),
    ("GET", "/fapi/v1/openOrders"),
    ("GET", "/fapi/v1/order"),
    ("GET", "/fapi/v1/openAlgoOrders"),
    ("GET", "/fapi/v1/allOrders"),
    ("GET", "/fapi/v1/allAlgoOrders"),
    ("GET", "/fapi/v1/userTrades"),
    ("GET", "/fapi/v1/positionSide/dual"),
    ("POST", "/fapi/v1/leverage"),
    ("POST", "/fapi/v1/marginType"),
    ("POST", "/fapi/v1/order/test"),
    ("POST", "/fapi/v1/order"),
    ("POST", "/fapi/v1/algoOrder"),
    ("DELETE", "/fapi/v1/order"),
    ("DELETE", "/fapi/v1/algoOrder"),
}
API_KEY_PATHS = {
    ("POST", "/fapi/v1/listenKey"),
    ("PUT", "/fapi/v1/listenKey"),
    ("DELETE", "/fapi/v1/listenKey"),
}

router = APIRouter(prefix="/api/v25", tags=["V25 Live Guard"])
migrate_legacy_files(("v25_execution_state.json", "v25_execution_state.backup.json"))
STATE_PATH = DATA_DIR / "v25_execution_state.json"
BACKUP_PATH = DATA_DIR / "v25_execution_state.backup.json"


class LiveExchangeError(RuntimeError):
    def __init__(self, message: str, *, http_status: int = 502, exchange_code: int | None = None, unknown_execution: bool = False) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.exchange_code = exchange_code
        self.unknown_execution = unknown_execution


def lock_live_execution(
    state: dict[str, Any],
    reason: str,
    *,
    unknown: bool = False,
    symbol: str | None = None,
    client_id: str | None = None,
) -> None:
    state["armed_until"] = 0.0
    state["real_trading_locked"] = True
    state["live_auto_trade"] = False
    state["auto"].update({"enabled": False, "session_until": 0.0})
    state["execution_state"] = "UNKNOWN" if unknown else "LOCKED"
    state["reconciliation_required"] = bool(unknown)
    state["emergency"].update({"active": True, "triggered_at": now_iso(), "reason": reason})
    add_event(
        state,
        "LIVE_UNKNOWN_EXECUTION" if unknown else "LIVE_FAIL_CLOSED",
        "Canlı yürütme belirsiz; yeni emirler kilitlendi ve manuel uzlaştırma gerekiyor." if unknown else "Canlı yürütme güvenlik nedeniyle kilitlendi.",
        reason=reason,
        execution_state=state["execution_state"],
        symbol=symbol,
        client_order_id_suffix=str(client_id or "")[-8:] or None,
        reconciliation_required=bool(unknown),
    )


class PolicyUpdate(BaseModel):
    allowed_symbols: list[str] | None = None
    interval: Literal["1m", "5m", "15m", "1h", "4h"] | None = None
    allow_long: bool | None = None
    allow_short: bool | None = None
    max_margin_per_trade: float | None = Field(default=None, ge=5, le=100)
    max_loss_per_trade: float | None = Field(default=None, ge=0.5, le=25)
    max_leverage: int | None = Field(default=None, ge=1, le=3)
    max_positions: int | None = Field(default=None, ge=1, le=5)
    max_total_exposure_usdt: float | None = Field(default=None, ge=25, le=250)
    daily_loss_limit: float | None = Field(default=None, ge=5, le=100)
    daily_trade_limit: int | None = Field(default=None, ge=1, le=12)
    consecutive_loss_limit: int | None = Field(default=None, ge=1, le=10)
    min_confidence: int | None = Field(default=None, ge=70, le=95)
    max_trap_score: int | None = Field(default=None, ge=10, le=60)
    max_spread_bps: float | None = Field(default=None, ge=0.5, le=25)
    max_stop_distance_pct: float | None = Field(default=None, ge=0.25, le=5)
    fee_bps_per_side: float | None = Field(default=None, ge=0, le=25)
    slippage_bps_per_side: float | None = Field(default=None, ge=0, le=30)
    minimum_net_reward_usdt: float | None = Field(default=None, ge=0, le=25)
    scan_seconds: int | None = Field(default=None, ge=30, le=300)


class Confirmation(BaseModel):
    confirmation: str = Field(min_length=1, max_length=80)


class LiveOrderRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    direction: Literal["LONG", "SHORT"]
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    margin_usdt: float = Field(ge=5, le=100)
    leverage: int = Field(ge=1, le=3)
    limit_price: float | None = Field(default=None, gt=0)
    stop_loss: float = Field(gt=0)
    tp1: float = Field(gt=0)
    tp2: float = Field(gt=0)
    tp3: float = Field(gt=0)
    intent_id: str | None = Field(default=None, min_length=8, max_length=96)


class ManualLiveOrderRequest(LiveOrderRequest):
    confirmation: str = Field(min_length=1, max_length=64)


class CloseRequest(BaseModel):
    plan_id: str = Field(min_length=6, max_length=64)
    confirmation: str = Field(min_length=1, max_length=64)


class EmergencyRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=64)
    close_tracked_positions: bool = True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initial_state() -> dict[str, Any]:
    return {
        "version": V25_VERSION,
        "policy": dict(DEFAULT_EXECUTION_POLICY),
        "policy_ack_digest": None,
        "connected": False,
        "connection": {"last_checked": None, "last_error": None, "clock_offset_ms": None},
        "stream": {
            "status": "ANAHTAR BEKLİYOR",
            "transport": "REST UZLAŞTIRMA",
            "last_event": None,
            "last_error": None,
            "event_count": 0,
            "reconnect_count": 0,
        },
        "snapshot": None,
        "events": [],
        "plans": {},
        "intents": {},
        "armed_until": 0.0,
        "auto": {"enabled": False, "busy": False, "cycles": 0, "last_scan": None, "last_scan_stats": None, "last_skip_reason": None, "last_cycle_stage": "idle", "last_decision": "Kullanıcı onayı bekleniyor.", "last_error": None, "session_until": 0.0},
        "real_trading_locked": True,
        "live_auto_trade": False,
        "emergency": {"active": False, "triggered_at": None, "reason": None},
        # Web consent is deliberately memory-only. A deployment or process
        # restart revokes it even though the audit event remains persisted.
        "web_consent": {"accepted_at": None, "expires_at_epoch": 0.0, "key_fingerprint": None},
        "duplicate_blocks": 0,
        "protection_repairs": 0,
        "recovery_ready": False,
        "recovery_loaded": True,
        "recovery_error": None,
        "execution_state": "LOCKED",
        "reconciliation_required": False,
    }


def sanitized_state(payload: Any) -> dict[str, Any]:
    base = initial_state()
    if not isinstance(payload, dict):
        return base
    base["policy"] = sanitize_execution_policy(payload.get("policy"))
    base["policy_ack_digest"] = payload.get("policy_ack_digest") if payload.get("policy_ack_digest") == policy_digest(base["policy"]) else None
    for key in ("events", "plans", "intents", "duplicate_blocks", "protection_repairs"):
        if key in payload and isinstance(payload[key], type(base[key])):
            base[key] = payload[key]
    if isinstance(payload.get("emergency"), dict):
        base["emergency"] = {
            "active": bool(payload["emergency"].get("active")),
            "triggered_at": payload["emergency"].get("triggered_at"),
            "reason": str(payload["emergency"].get("reason") or "")[:240] or None,
        }
    if payload.get("execution_state") in {"LOCKED", "UNKNOWN"}:
        base["execution_state"] = payload["execution_state"]
    base["reconciliation_required"] = bool(payload.get("reconciliation_required"))
    base["events"] = base["events"][:MAX_EVENTS]
    if len(base["plans"]) > MAX_PLANS:
        rows = sorted(base["plans"].items(), key=lambda item: item[1].get("created_at", ""), reverse=True)[:MAX_PLANS]
        base["plans"] = dict(rows)
    if len(base["intents"]) > 1_000:
        rows = sorted(base["intents"].items(), key=lambda item: item[1].get("created_at", ""), reverse=True)[:1_000]
        base["intents"] = dict(rows)
    # Entry authority never survives a process restart.
    base["auto"]["last_decision"] = "Güvenli yeniden başlatma: canlı otomasyon yeniden onay bekliyor."
    base["real_trading_locked"] = True
    base["live_auto_trade"] = False
    base["execution_state"] = "UNKNOWN" if base["reconciliation_required"] else "LOCKED"
    base["auto"].update({"enabled": False, "session_until": 0.0, "busy": False})
    return base


def load_state() -> dict[str, Any]:
    for path in (STATE_PATH, BACKUP_PATH):
        try:
            return sanitized_state(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return initial_state()


def persist_state(state: dict[str, Any]) -> None:
    application = state.get("_app")
    pool = getattr(getattr(application, "state", None), "db_pool", None)
    if application is not None and pool is not None:
        tasks = getattr(application.state, "_v25_persistence_tasks", None)
        if not isinstance(tasks, set):
            tasks = set()
            application.state._v25_persistence_tasks = tasks
        payload = sanitized_state(state)
        task = asyncio.create_task(_persist_state_snapshot(application, payload))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return
    if os.getenv("DATABASE_URL", "").strip():
        state["recovery_ready"] = False
        state["recovery_error"] = "PostgreSQL persistence unavailable."
        state["armed_until"] = 0.0
        state["real_trading_locked"] = True
        state["live_auto_trade"] = False
        state["auto"].update({"enabled": False, "session_until": 0.0})
        state["execution_state"] = "LOCKED"
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = sanitized_state(state)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(body, encoding="utf-8")
    if STATE_PATH.exists():
        try:
            BACKUP_PATH.write_bytes(STATE_PATH.read_bytes())
        except OSError:
            pass
    temporary.replace(STATE_PATH)


V25_SNAPSHOT_KEY = "v25_live:state"


async def _persist_state_snapshot(application: Any, payload: dict[str, Any]) -> None:
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        return
    await pool.execute(
        """
        INSERT INTO application_state_snapshots (state_key, updated_at, payload)
        VALUES ($1, NOW(), $2::jsonb)
        ON CONFLICT (state_key) DO UPDATE
        SET updated_at = NOW(), payload = EXCLUDED.payload
        """,
        V25_SNAPSHOT_KEY,
        json.dumps(payload, ensure_ascii=False),
    )


async def restore_v25_state(application: Any) -> bool:
    state = application.state.v25_execution
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        return False
    try:
        row = await pool.fetchrow("SELECT payload FROM application_state_snapshots WHERE state_key = $1", V25_SNAPSHOT_KEY)
        if row is not None:
            payload = row["payload"]
            restored = sanitized_state(payload)
            state.update(restored)
        state["_app"] = application
        state["recovery_loaded"] = True
        state["recovery_ready"] = False
        state["recovery_error"] = None
        return True
    except Exception:
        state["recovery_ready"] = False
        state["recovery_error"] = "PostgreSQL recovery failed."
        return False


def add_event(state: dict[str, Any], kind: str, message: str, **extra: Any) -> dict[str, Any]:
    row = {"id": uuid.uuid4().hex, "kind": kind, "message": message[:400], "created_at": now_iso(), **extra}
    state.setdefault("events", []).insert(0, row)
    del state["events"][MAX_EVENTS:]
    return row


def live_credentials_status() -> tuple[str, str, str | None]:
    api_key, secret_key = load_live_credentials()
    fingerprint = credential_fingerprint(api_key)
    return api_key, secret_key, fingerprint if len(secret_key) >= 10 else None


def consent_status(state: dict[str, Any] | None = None) -> dict[str, Any]:
    api_key, _, fingerprint = live_credentials_status()
    local_payload = load_live_consent()
    web_payload = state.get("web_consent", {}) if isinstance(state, dict) else {}
    candidates = [payload for payload in (web_payload, local_payload) if isinstance(payload, dict)]
    payload = next(
        (
            candidate for candidate in candidates
            if candidate.get("key_fingerprint") == fingerprint
            and float(candidate.get("expires_at_epoch") or 0) > time.time()
        ),
        {},
    )
    expires = float(payload.get("expires_at_epoch") or 0)
    active = bool(
        api_key and fingerprint and payload.get("key_fingerprint") == fingerprint
        and expires > time.time()
    )
    return {
        "active": active,
        "accepted_at": payload.get("accepted_at") if active else None,
        "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat() if active else None,
        "fingerprint": fingerprint,
        "storage": "SUNUCU_BELLEĞİ" if payload is web_payload and active else "WINDOWS_DPAPI" if active else "YOK",
    }


def execution_owner(request: Request) -> dict[str, Any]:
    """Use the web owner gate when present, otherwise retain desktop auth."""
    if bool(getattr(request.state, "web_owner_authenticated", False)):
        return {"id": "WEB_OWNER", "role": "OWNER"}
    return authenticated_user(request, owner=True)


def is_armed(state: dict[str, Any]) -> bool:
    active = float(state.get("armed_until") or 0) > time.time()
    if not active:
        state["armed_until"] = 0.0
        state["real_trading_locked"] = True
    return active


def live_execution_blocked(state: dict[str, Any]) -> bool:
    return bool(
        state.get("emergency", {}).get("active")
        or state.get("execution_state") == "UNKNOWN"
        or state.get("reconciliation_required")
    )


def auto_session_active(state: dict[str, Any]) -> bool:
    active = bool(state["auto"].get("enabled")) and float(state["auto"].get("session_until") or 0) > time.time()
    if not active:
        state["auto"]["enabled"] = False
        state["auto"]["session_until"] = 0.0
        state["live_auto_trade"] = False
        state["real_trading_locked"] = True
    return active


class BinanceLiveClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str, secret_key: str, *, require_credentials: bool = True) -> None:
        if require_credentials and (len(api_key) < 10 or len(secret_key) < 10):
            raise LiveExchangeError("Canlı API bağlantısı aktif değil. Borsa Bağlantıları bölümünden gerçek hesap anahtarını kaydedip salt-okunur bağlantıyı aktifleştirin.", http_status=412)
        self.http = http
        self.api_key = api_key
        self.secret_key = secret_key
        self.time_offset_ms = 0
        self.last_time_sync = 0.0
        self._clock_lock = asyncio.Lock()

    async def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path not in PUBLIC_PATHS:
            raise LiveExchangeError("İzin verilmeyen canlı piyasa API yolu.", http_status=500)
        return await self._request("GET", path, params or {}, signed=False)

    async def sync_clock(self) -> None:
        if time.monotonic() - self.last_time_sync < 30:
            return
        async with self._clock_lock:
            if time.monotonic() - self.last_time_sync < 30:
                return
            before = int(time.time() * 1000)
            payload = await self.public_get("/fapi/v1/time")
            after = int(time.time() * 1000)
            server_time = int(payload["serverTime"])
            self.time_offset_ms = server_time - ((before + after) // 2)
            self.last_time_sync = time.monotonic()

    async def signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        method = method.upper()
        if (method, path) not in PRIVATE_PATHS:
            raise LiveExchangeError("İzin verilmeyen canlı hesap API işlemi.", http_status=500)
        await self.sync_clock()
        payload = {key: value for key, value in dict(params or {}).items() if value is not None}
        payload["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
        payload["recvWindow"] = 5000
        query = urlencode(list(payload.items()), doseq=True)
        signature = hmac.new(self.secret_key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        return await self._request(method, path, payload, signed=True, encoded_query=query, signature=signature)

    async def api_key_request(self, method: str, path: str) -> Any:
        """Call the official USER_STREAM endpoints without a request signature."""
        method = method.upper()
        if (method, path) not in API_KEY_PATHS:
            raise LiveExchangeError("İzin verilmeyen canlı kullanıcı akışı işlemi.", http_status=500)
        return await self._request(method, path, {}, signed=False, api_key_header=True)

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        signed: bool,
        encoded_query: str = "",
        signature: str = "",
        api_key_header: bool = False,
    ) -> Any:
        url = f"{LIVE_REST_BASE}{path}"
        if not url.startswith(f"{LIVE_REST_BASE}/"):
            raise LiveExchangeError("Canlı Binance sunucu kilidi doğrulanamadı.", http_status=500)
        headers = {"X-MBX-APIKEY": self.api_key} if signed or api_key_header else {}
        request_url = f"{url}?{encoded_query}&signature={signature}" if signed else url
        try:
            response = await self.http.request(method, request_url, params=None if signed else params, headers=headers)
        except httpx.TimeoutException as exc:
            raise LiveExchangeError("Canlı emir sonucu belirsiz; zaman aşımı sonrası yeni emir gönderilmedi.", unknown_execution=True) from exc
        except httpx.RequestError as exc:
            raise LiveExchangeError("Canlı emir sonucu belirsiz; ağ bağlantısı kesildi ve yeni emir gönderilmedi.", unknown_execution=True) from exc
        if response.status_code >= 400:
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError):
                body = {}
            code = body.get("code") if isinstance(body, dict) else None
            message = str(body.get("msg") if isinstance(body, dict) else "Binance canlı işlemi reddetti.")
            if self.api_key:
                message = message.replace(self.api_key, "[gizli]")
            if response.status_code in {429, 418}:
                message = "Binance API hız sınırı; yeni emir gönderilmedi. Geri çekilme süresi bekleniyor."
            unknown = response.status_code >= 500
            if unknown:
                message = "Emir yürütme sonucu belirsiz; benzersiz emir kimliğiyle sorgulanacak, kör tekrar yapılmayacak."
            raise LiveExchangeError(message, http_status=429 if response.status_code in {429, 418} else 502, exchange_code=int(code) if isinstance(code, int) else None, unknown_execution=unknown)
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError):
            return {}


def client_for(application: Any) -> BinanceLiveClient:
    api_key, secret_key, _ = live_credentials_status()
    return BinanceLiveClient(application.state.http, api_key, secret_key)


def public_client_for(application: Any) -> BinanceLiveClient:
    """Public Futures market data remains visible before API setup."""
    return BinanceLiveClient(application.state.http, "", "", require_credentials=False)


def safe_exchange_error(exc: LiveExchangeError | BinanceDemoError) -> HTTPException:
    code = getattr(exc, "exchange_code", None)
    suffix = f" (Binance kodu: {code})" if code is not None else ""
    return HTTPException(getattr(exc, "http_status", 502), f"{exc}{suffix}")


def validate_protection_readiness(spec: dict[str, Any], policy: dict[str, Any]) -> None:
    if not sanitize_execution_policy(policy).get("stop_required", True):
        raise LiveExchangeError("Canlı giriş için Stop koruması zorunludur.", http_status=423)
    try:
        entry = Decimal(str(spec["entry_price"]))
        stop = Decimal(str(spec["stop_loss"]))
        targets = [Decimal(str(value)) for value in spec["targets"]]
        quantity = Decimal(str(spec["quantity"]))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise LiveExchangeError("Canlı koruma planı doğrulanamadı; emir gönderilmedi.", http_status=422) from exc
    values = [entry, stop, quantity, *targets]
    if not all(value.is_finite() and value > 0 for value in values):
        raise LiveExchangeError("Canlı koruma seviyeleri geçersiz; emir gönderilmedi.", http_status=422)
    if len(targets) != 3 or any(target == entry for target in targets):
        raise LiveExchangeError("Canlı TP koruma planı eksik veya geçersiz; emir gönderilmedi.", http_status=422)
    direction = str(spec.get("direction") or "").upper()
    if direction == "LONG" and not (stop < entry < targets[0] <= targets[1] <= targets[2]):
        raise LiveExchangeError("Canlı LONG koruma seviyeleri geçersiz; emir gönderilmedi.", http_status=422)
    if direction == "SHORT" and not (targets[2] <= targets[1] <= targets[0] < entry < stop):
        raise LiveExchangeError("Canlı SHORT koruma seviyeleri geçersiz; emir gönderilmedi.", http_status=422)


async def live_symbol_rules(client: BinanceLiveClient, symbol: str, order_type: str) -> dict[str, Decimal]:
    payload = await client.public_get("/fapi/v1/exchangeInfo")
    row = next((item for item in payload.get("symbols", []) if item.get("symbol") == symbol), None)
    if not row or row.get("status") != "TRADING":
        raise LiveExchangeError(f"{symbol} canlı Futures işlemlerine açık değil.", http_status=422)
    filters = {item.get("filterType"): item for item in row.get("filters", [])}
    lot = filters.get("MARKET_LOT_SIZE" if order_type == "MARKET" else "LOT_SIZE") or filters.get("LOT_SIZE", {})
    price_filter = filters.get("PRICE_FILTER", {})
    notional_filter = filters.get("MIN_NOTIONAL", {}) or filters.get("NOTIONAL", {})
    if row.get("contractType") != "PERPETUAL" or row.get("quoteAsset") != "USDT":
        raise LiveExchangeError(f"{symbol} yalnızca USDT perpetual canlı Futures sözleşmesi olmalı.", http_status=422)
    return {
        "step": Decimal(str(lot.get("stepSize", "0.001"))),
        "min_qty": Decimal(str(lot.get("minQty", "0"))),
        "max_qty": Decimal(str(lot.get("maxQty", "999999999"))),
        "tick": Decimal(str(price_filter.get("tickSize", "0.01"))),
        "min_notional": Decimal(str(notional_filter.get("notional", "0"))),
    }


async def set_live_isolated_margin(client: BinanceLiveClient, symbol: str) -> None:
    try:
        await client.signed("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
    except LiveExchangeError as exc:
        if exc.exchange_code != -4046:
            raise


async def apply_live_verified_leverage(client: BinanceLiveClient, symbol: str, requested: int) -> dict[str, Any]:
    response = await client.signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": requested})
    verify_leverage_response(response, symbol, requested)
    configuration = await client.signed("GET", "/fapi/v1/symbolConfig", {"symbol": symbol})
    return verify_symbol_configuration(configuration, symbol, requested)


async def ticker_price(client: BinanceLiveClient, symbol: str) -> Decimal:
    payload = await client.public_get("/fapi/v1/ticker/price", {"symbol": symbol})
    price = Decimal(str(payload.get("price", "0")))
    if price <= 0:
        raise LiveExchangeError("Canlı piyasa fiyatı alınamadı.")
    return price


async def spread_bps(client: BinanceLiveClient, symbol: str) -> float:
    payload = await client.public_get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
    bid, ask = Decimal(str(payload.get("bidPrice", "0"))), Decimal(str(payload.get("askPrice", "0")))
    midpoint = (bid + ask) / 2
    if min(bid, ask, midpoint) <= 0 or ask < bid:
        raise LiveExchangeError("Canlı alış-satış makası okunamadı.")
    return float((ask - bid) / midpoint * Decimal("10000"))


def rank_market_tickers(
    exchange_info: Any,
    tickers: Any,
    *,
    market_limit: int = MARKET_SCAN_LIMIT,
    candidate_limit: int = DEEP_ANALYSIS_LIMIT,
    excluded_symbols: set[str] | None = None,
    allowed_symbols: set[str] | None = None,
) -> list[dict[str, Any]]:
    eligible = {
        item.get("symbol")
        for item in exchange_info.get("symbols", [])
        if isinstance(item, dict)
        and item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
        and item.get("quoteAsset") == "USDT"
    } if isinstance(exchange_info, dict) else set()
    excluded = excluded_symbols or set()
    liquid: list[dict[str, Any]] = []
    for ticker in tickers if isinstance(tickers, list) else []:
        if not isinstance(ticker, dict):
            continue
        symbol = str(ticker.get("symbol") or "")
        base = symbol[:-4] if symbol.endswith("USDT") else ""
        if symbol not in eligible or symbol in excluded or (allowed_symbols is not None and symbol not in allowed_symbols) or base in BLOCKED_BASE_ASSETS:
            continue
        if any(word in base for word in ("UP", "DOWN", "BULL", "BEAR")):
            continue
        try:
            volume = float(ticker.get("quoteVolume") or 0)
            move = abs(float(ticker.get("priceChangePercent") or 0))
            price = float(ticker.get("lastPrice") or 0)
        except (TypeError, ValueError):
            continue
        if volume < MIN_24H_QUOTE_VOLUME or move < MIN_24H_MOVE_PCT or price <= 0:
            continue
        liquid.append({
            "symbol": symbol,
            "price": price,
            "volume": volume,
            "change": float(ticker.get("priceChangePercent") or 0),
            "_move": move,
        })
    liquid.sort(key=lambda item: item["volume"], reverse=True)
    ranked = []
    for item in liquid[:market_limit]:
        item["opportunity_score"] = round(item.pop("_move") * 0.35 + min(item["volume"] / 10_000_000, 100) * 0.65, 4)
        ranked.append(item)
    ranked.sort(key=lambda item: (item["opportunity_score"], item["volume"]), reverse=True)
    return ranked[:min(market_limit, candidate_limit)]


async def scan_market_candidates(
    client: BinanceLiveClient,
    snapshot: dict[str, Any],
    allowed_symbols: list[str] | None = None,
) -> list[dict[str, Any]]:
    exchange_info, tickers = await asyncio.gather(
        client.public_get("/fapi/v1/exchangeInfo"),
        client.public_get("/fapi/v1/ticker/24hr"),
    )
    occupied = {
        str(item.get("symbol"))
        for item in (snapshot.get("positions", []) + snapshot.get("open_orders", []))
        if isinstance(item, dict) and item.get("symbol")
    }
    eligible_count = sum(
        1
        for item in exchange_info.get("symbols", [])
        if isinstance(item, dict)
        and item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
        and item.get("quoteAsset") == "USDT"
    ) if isinstance(exchange_info, dict) else 0
    candidates = rank_market_tickers(exchange_info, tickers, excluded_symbols=occupied)
    client.last_scan_eligible_count = eligible_count
    return candidates


async def build_live_spec(
    client: BinanceLiveClient,
    order: LiveOrderRequest,
    policy: dict[str, Any],
    *,
    allowed_symbols: list[str] | None = None,
) -> dict[str, Any]:
    settings = sanitize_execution_policy(policy)
    symbol = normalize_symbol(order.symbol)
    symbol_scope = allowed_symbols if allowed_symbols is not None else settings["allowed_symbols"]
    if symbol not in symbol_scope:
        raise LiveExchangeError(f"{symbol} canlı izin listesinde değil.", http_status=422)
    if order.direction == "LONG" and not settings["allow_long"]:
        raise LiveExchangeError("Canlı LONG işlemleri risk politikasında kapalı.", http_status=422)
    if order.direction == "SHORT" and not settings["allow_short"]:
        raise LiveExchangeError("Canlı SHORT işlemleri risk politikasında kapalı.", http_status=422)
    if order.margin_usdt > settings["max_margin_per_trade"] or order.leverage > settings["max_leverage"]:
        raise LiveExchangeError("Emir, kullanıcı risk limitindeki marjin veya kaldıraç tavanını aşıyor.", http_status=422)
    if order.order_type == "LIMIT" and order.limit_price is None:
        raise LiveExchangeError("Limit emir için fiyat zorunludur.", http_status=422)
    current, rules = await asyncio.gather(ticker_price(client, symbol), live_symbol_rules(client, symbol, order.order_type))
    raw_entry = Decimal(str(order.limit_price)) if order.order_type == "LIMIT" else current
    entry = round_tick(raw_entry, rules["tick"])
    stop = round_tick(Decimal(str(order.stop_loss)), rules["tick"])
    targets = [round_tick(Decimal(str(value)), rules["tick"]) for value in (order.tp1, order.tp2, order.tp3)]
    validate_levels(order.direction, entry if order.order_type == "LIMIT" else current, stop, targets)
    risk = risk_sized_order(float(entry), float(stop), {**settings, "max_margin_per_trade": order.margin_usdt, "max_leverage": order.leverage})
    if risk["estimated_stop_loss_usdt"] > settings["max_loss_per_trade"] + 1e-6:
        raise LiveExchangeError("Tahmini Stop kaybı işlem başına risk limitini aşıyor.", http_status=422)
    notional = Decimal(str(risk["notional_usdt"]))
    quantity = floor_step(notional / entry, rules["step"])
    if quantity < rules["min_qty"] or quantity * entry < rules["min_notional"]:
        raise LiveExchangeError("Hesaplanan miktar Binance minimum emir tutarını karşılamıyor.", http_status=422)
    if quantity > rules["max_qty"]:
        raise LiveExchangeError("Hesaplanan miktar Binance sembol üst sınırını aşıyor.", http_status=422)
    tp1_move = abs(float(targets[0] - entry) / float(entry))
    gross = float(quantity * entry) * tp1_move
    costs = float(quantity * entry) * (settings["fee_bps_per_side"] + settings["slippage_bps_per_side"]) * 2 / 10_000
    if gross - costs < settings["minimum_net_reward_usdt"]:
        raise LiveExchangeError("TP1, ücret ve kayma sonrası minimum net getiri eşiğini karşılamıyor.", http_status=422)
    return {
        "symbol": symbol,
        "direction": order.direction,
        "side": "BUY" if order.direction == "LONG" else "SELL",
        "close_side": "SELL" if order.direction == "LONG" else "BUY",
        "order_type": order.order_type,
        "margin_usdt": float(risk["margin_usdt"]),
        "leverage": order.leverage,
        "notional_usdt": float(quantity * entry),
        "quantity": decimal_text(quantity),
        "entry_price": decimal_text(entry),
        "stop_loss": decimal_text(stop),
        "targets": [decimal_text(value) for value in targets],
        "step": rules["step"],
        "min_qty": rules["min_qty"],
        "estimated_stop_loss_usdt": risk["estimated_stop_loss_usdt"],
    }


def client_id_for(kind: str, intent_id: str) -> str:
    digest = hashlib.sha256(f"{kind}:{intent_id}".encode("utf-8")).hexdigest()[:22]
    return f"{LIVE_CLIENT_PREFIX}{kind[:5].upper()}_{digest}"[:36]


async def find_order(client: BinanceLiveClient, symbol: str, client_id: str) -> dict[str, Any] | None:
    try:
        payload = await client.signed("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id})
        rows = response_rows(payload)
        matches = [row for row in rows if isinstance(row, dict) and row.get("orderId")]
        if len(matches) > 1:
            raise LiveExchangeError("Birden fazla eşleşen canlı emir bulundu; sonuç belirsiz.", unknown_execution=True)
        return matches[0] if matches else None
    except LiveExchangeError as exc:
        if exc.exchange_code in {-2011, -2013}:
            return None
        raise


def order_matches_spec(order: dict[str, Any], spec: dict[str, Any], client_id: str) -> bool:
    response_client_id = str(order.get("clientOrderId") or order.get("origClientOrderId") or "")
    if response_client_id != client_id:
        return False
    if str(order.get("symbol") or "").upper() != str(spec["symbol"]).upper():
        return False
    if str(order.get("side") or "").upper() != str(spec["side"]).upper():
        return False
    if str(order.get("positionSide") or "BOTH").upper() != "BOTH":
        return False
    if str(order.get("type") or "").upper() != str(spec["order_type"]).upper():
        return False
    try:
        return Decimal(str(order.get("origQty"))) == Decimal(str(spec["quantity"]))
    except (TypeError, ValueError, ArithmeticError):
        return False


async def submit_entry(client: BinanceLiveClient, spec: dict[str, Any], client_id: str, *, test_only: bool) -> dict[str, Any]:
    params: dict[str, Any] = {
        "symbol": spec["symbol"], "side": spec["side"], "type": spec["order_type"],
        "quantity": spec["quantity"], "newClientOrderId": client_id, "newOrderRespType": "RESULT",
    }
    if spec["order_type"] == "LIMIT":
        params.update({"price": spec["entry_price"], "timeInForce": "GTC"})
    path = "/fapi/v1/order/test" if test_only else "/fapi/v1/order"
    if not test_only:
        existing = await find_order(client, spec["symbol"], client_id)
        if existing is not None:
            if not order_matches_spec(existing, spec, client_id):
                raise LiveExchangeError("İlişkisiz veya uyuşmayan canlı emir bulundu; sahiplenilmedi.", unknown_execution=True)
            return {**existing, "recovered": True}
    try:
        payload = await client.signed("POST", path, params)
        if not test_only and (not isinstance(payload, dict) or not payload.get("orderId")):
            raise LiveExchangeError("Canlı emir yanıtı eksik; emir sonucu belirsiz ve uzlaştırma gerekiyor.", unknown_execution=True)
    except LiveExchangeError as exc:
        if not test_only and exc.unknown_execution:
            recovered = await find_order(client, spec["symbol"], client_id)
            if recovered is not None:
                if not order_matches_spec(recovered, spec, client_id):
                    raise LiveExchangeError("Uzlaştırılan canlı emir beklenen kimlik veya parametrelerle eşleşmedi.", unknown_execution=True) from exc
                return {**recovered, "recovered": True}
        raise
    if test_only:
        return payload if isinstance(payload, dict) else {}
    return payload


async def post_algo(client: BinanceLiveClient, params: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = await client.signed("POST", "/fapi/v1/algoOrder", params)
        return payload if isinstance(payload, dict) else {}
    except LiveExchangeError as exc:
        if not exc.unknown_execution:
            raise
        rows = response_rows(await client.signed("GET", "/fapi/v1/openAlgoOrders", {"symbol": params["symbol"]}))
        recovered = next((row for row in rows if row.get("clientAlgoId") == params.get("clientAlgoId")), None)
        if recovered is None:
            raise
        return recovered


async def close_tracked_symbol(client: BinanceLiveClient, symbol: str, intent: str) -> dict[str, Any] | None:
    rows = response_rows(await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol}))
    row = next((item for item in rows if Decimal(str(item.get("positionAmt", "0"))) != 0), None)
    if row is None:
        return None
    amount = Decimal(str(row["positionAmt"]))
    client_id = client_id_for("CLOSE", intent)
    existing = await find_order(client, symbol, client_id)
    if existing is not None:
        return existing
    params = {
        "symbol": symbol, "side": "SELL" if amount > 0 else "BUY", "type": "MARKET",
        "quantity": decimal_text(abs(amount)), "reduceOnly": "true",
        "newClientOrderId": client_id, "newOrderRespType": "RESULT",
    }
    try:
        return await client.signed("POST", "/fapi/v1/order", params)
    except LiveExchangeError as exc:
        if exc.unknown_execution:
            recovered = await find_order(client, symbol, client_id)
            if recovered is not None:
                return recovered
        raise


async def cancel_owned_algos_for_symbol(client: BinanceLiveClient, rows: list[dict[str, Any]], symbol: str) -> int:
    """Remove only V25-owned residual Stop/TP algos after a tracked position closes."""
    cancelled = 0
    for row in rows:
        if row.get("symbol") != symbol or not str(row.get("client_algo_id") or "").startswith(LIVE_CLIENT_PREFIX):
            continue
        try:
            await client.signed(
                "DELETE", "/fapi/v1/algoOrder",
                {"symbol": symbol, "algoId": row["algo_id"]},
            )
            cancelled += 1
        except LiveExchangeError:
            # A simultaneously triggered/cancelled protection is already harmless;
            # the next reconciliation pass will verify the authoritative state.
            pass
    return cancelled


async def install_protection(client: BinanceLiveClient, state: dict[str, Any], plan: dict[str, Any]) -> None:
    symbol = plan["symbol"]
    positions = response_rows(await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol}))
    position = next((item for item in positions if Decimal(str(item.get("positionAmt", "0"))) != 0), None)
    if position is None:
        plan["status"] = "DOLUM BEKLİYOR"
        return
    amount = Decimal(str(position["positionAmt"]))
    direction = "LONG" if amount > 0 else "SHORT"
    if direction != plan["direction"]:
        plan["status"] = "YÖN UYUŞMAZLIĞI"
        state["armed_until"] = 0.0
        state["auto"]["enabled"] = False
        state["auto"]["session_until"] = 0.0
        add_event(state, "PROTECTION_BLOCK", f"{symbol} yön uyuşmazlığı; koruma kurulmadı.", symbol=symbol)
        return
    common = {"algoType": "CONDITIONAL", "symbol": symbol, "side": "SELL" if amount > 0 else "BUY", "workingType": "MARK_PRICE", "priceProtect": "TRUE"}
    stop_client = plan.setdefault("stop_client_id", client_id_for("SL", plan["intent_id"]))
    try:
        stop_result = await post_algo(client, {**common, "type": "STOP_MARKET", "triggerPrice": plan["stop_loss"], "closePosition": "true", "clientAlgoId": stop_client})
        plan["stop_algo_id"] = int(stop_result.get("algoId") or 0) or None
    except LiveExchangeError as exc:
        plan["status"] = "STOP BAŞARISIZ · KAPATILIYOR"
        add_event(state, "PROTECTION_FAIL", f"{symbol} Stop kurulamadı; tracked pozisyon reduce-only kapatılıyor.", symbol=symbol)
        close_intent = f"protection-{plan['id']}"
        close_client_id = client_id_for("CLOSE", close_intent)
        close_ids = plan.setdefault("close_client_order_ids", [])
        if close_client_id not in close_ids:
            close_ids.append(close_client_id)
        persist_state(state)
        close_result = await close_tracked_symbol(client, symbol, close_intent)
        if close_result and close_result.get("orderId"):
            order_id = int(close_result["orderId"])
            known = plan.setdefault("exchange_order_ids", [])
            if order_id not in known:
                known.append(order_id)
        plan["status"] = "GÜVENLİK İÇİN KAPATILDI"
        plan["last_error"] = str(exc)[:240]
        return
    step, min_qty = Decimal(str(plan["step"])), Decimal(str(plan["min_qty"]))
    partial = floor_step(abs(amount) * Decimal("0.30"), step)
    ids: list[int] = [plan["stop_algo_id"]] if plan.get("stop_algo_id") else []
    monitoring: list[str] = []
    if partial >= min_qty:
        for index, trigger in enumerate(plan["targets"][:2], start=1):
            key = client_id_for(f"TP{index}", plan["intent_id"])
            try:
                result = await post_algo(client, {**common, "type": "TAKE_PROFIT_MARKET", "triggerPrice": trigger, "quantity": decimal_text(partial), "reduceOnly": "true", "clientAlgoId": key})
                if result.get("algoId"):
                    ids.append(int(result["algoId"]))
            except LiveExchangeError:
                monitoring.append(f"TP{index}")
    else:
        monitoring.extend(["TP1", "TP2"])
    try:
        result = await post_algo(client, {**common, "type": "TAKE_PROFIT_MARKET", "triggerPrice": plan["targets"][2], "closePosition": "true", "clientAlgoId": client_id_for("TP3", plan["intent_id"])})
        if result.get("algoId"):
            ids.append(int(result["algoId"]))
    except LiveExchangeError:
        monitoring.append("TP3")
    plan.update({"protection_ids": ids, "monitoring_targets": monitoring, "protected_at": now_iso(), "status": "KORUMA AKTİF" if not monitoring else "STOP AKTİF · HEDEF İZLEME"})
    add_event(state, "PROTECTION_ACTIVE", f"{symbol} canlı Stop ve TP koruma planı kuruldu.", symbol=symbol)


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def _stream_event_seen(state: dict[str, Any], exchange_event_id: str) -> bool:
    return any(item.get("exchange_event_id") == exchange_event_id for item in state.get("events", []))


def _active_plan_for_symbol(state: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    terminal = {"KAPANDI", "İPTAL"}
    return next(
        (
            plan for plan in state.get("plans", {}).values()
            if plan.get("symbol") == symbol and plan.get("status") not in terminal
        ),
        None,
    )


def process_live_stream_event(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Record only V25-owned order events; account-risk events always fail closed."""
    stream = state["stream"]
    stream.update({"status": "CANLI", "transport": "BINANCE USER STREAM", "last_event": now_iso(), "last_error": None})
    stream["event_count"] = int(stream.get("event_count") or 0) + 1
    event_type = str(payload.get("e") or "")
    event_time = payload.get("E", payload.get("T", 0))

    if event_type == "MARGIN_CALL":
        exchange_event_id = f"margin-{event_time}"
        if _stream_event_seen(state, exchange_event_id):
            return False
        state["armed_until"] = 0.0
        state["auto"]["enabled"] = False
        state["auto"]["session_until"] = 0.0
        state["auto"]["last_decision"] = "Binance marjin çağrısı bildirdi; yeni girişler kilitlendi."
        add_event(
            state,
            "LIVE_MARGIN_CALL",
            "Binance marjin çağrısı bildirdi; V25 yeni girişleri kilitledi. Hesabı derhal kontrol edin.",
            exchange_event_id=exchange_event_id,
        )
        return True

    if event_type == "ORDER_TRADE_UPDATE":
        order = payload.get("o") if isinstance(payload.get("o"), dict) else {}
        symbol = str(order.get("s") or "").upper()
        client_id = str(order.get("c") or "")
        plan = _active_plan_for_symbol(state, symbol)
        belongs_to_v25 = client_id.startswith(LIVE_CLIENT_PREFIX) or bool(plan and _truthy(order.get("R")))
        if not belongs_to_v25:
            return False
        execution = str(order.get("x") or "UPDATE")
        status = str(order.get("X") or "UPDATE")
        order_id = int(order.get("i") or 0)
        exchange_event_id = f"order-{event_time}-{order_id}-{execution}-{status}-{order.get('t', '')}"
        if _stream_event_seen(state, exchange_event_id):
            return False
        if plan and order_id:
            known = plan.setdefault("exchange_order_ids", [])
            if order_id not in known:
                known.append(order_id)
        realized = float(order.get("rp") or 0)
        add_event(
            state,
            "LIVE_FILL" if execution == "TRADE" else "LIVE_ORDER_UPDATE",
            f"{symbol} {execution} · {status}",
            symbol=symbol,
            status=status,
            side=order.get("S"),
            price=float(order.get("ap") or order.get("L") or order.get("p") or 0),
            quantity=float(order.get("z") or order.get("l") or order.get("q") or 0),
            realized_pnl_fill=realized,
            reduce_only=_truthy(order.get("R")),
            client_order_id=client_id,
            exchange_order_id=order_id or None,
            exchange_event_id=exchange_event_id,
        )
        return True

    if event_type == "ALGO_UPDATE":
        order = payload.get("o") if isinstance(payload.get("o"), dict) else payload.get("a", {})
        order = order if isinstance(order, dict) else {}
        symbol = str(order.get("s") or order.get("symbol") or "").upper()
        client_id = str(order.get("caid") or order.get("clientAlgoId") or order.get("c") or "")
        if not client_id.startswith(LIVE_CLIENT_PREFIX) and _active_plan_for_symbol(state, symbol) is None:
            return False
        status = str(order.get("X") or order.get("algoStatus") or order.get("status") or "UPDATE")
        algo_id = order.get("aid", order.get("algoId", ""))
        exchange_event_id = f"algo-{event_time}-{algo_id}-{status}"
        if _stream_event_seen(state, exchange_event_id):
            return False
        add_event(
            state,
            "LIVE_ALGO_UPDATE",
            f"{symbol} Stop/TP koruması · {status}",
            symbol=symbol or None,
            status=status,
            exchange_event_id=exchange_event_id,
        )
        return True

    if event_type == "listenKeyExpired":
        exchange_event_id = f"expired-{event_time}"
        if not _stream_event_seen(state, exchange_event_id):
            add_event(
                state,
                "LIVE_STREAM_EXPIRED",
                "Binance canlı kullanıcı akışı süresi doldu; yeni listenKey ile yeniden bağlanılıyor.",
                exchange_event_id=exchange_event_id,
            )
        return True
    return False


async def live_user_stream_loop(application: Any) -> None:
    """Prefer Binance's ordered private stream and retain REST reconciliation as backup."""
    state = application.state.v25_execution
    while True:
        listen_key = ""
        client: BinanceLiveClient | None = None
        try:
            _, secret, fingerprint = live_credentials_status()
            if not fingerprint or len(secret) < 10:
                state["stream"].update({"status": "ANAHTAR BEKLİYOR", "transport": "REST UZLAŞTIRMA"})
                await asyncio.sleep(5)
                continue
            client = client_for(application)
            response = await client.api_key_request("POST", "/fapi/v1/listenKey")
            listen_key = str((response or {}).get("listenKey") or "")
            if not listen_key:
                raise LiveExchangeError("Binance canlı kullanıcı akışı anahtarı alınamadı.")
            state["stream"].update({"status": "BAĞLANIYOR", "transport": "BINANCE USER STREAM", "last_error": None})
            url = f"{LIVE_WS_BASE}/ws/{listen_key}"
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=512) as socket:
                state["stream"].update({"status": "CANLI", "transport": "BINANCE USER STREAM", "last_event": now_iso()})
                add_event(state, "LIVE_STREAM_CONNECTED", "Binance canlı emir ve pozisyon kullanıcı akışı bağlandı.")
                persist_state(state)
                last_keepalive = time.monotonic()
                while True:
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=30)
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        payload = json.loads(raw)
                        if isinstance(payload, dict):
                            async with state["lock"]:
                                changed = process_live_stream_event(state, payload)
                                if changed:
                                    persist_state(state)
                            if payload.get("e") == "listenKeyExpired":
                                break
                    except asyncio.TimeoutError:
                        pass
                    if time.monotonic() - last_keepalive >= 45 * 60:
                        await client.api_key_request("PUT", "/fapi/v1/listenKey")
                        last_keepalive = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state["stream"]["reconnect_count"] = int(state["stream"].get("reconnect_count") or 0) + 1
            state["stream"].update({
                "status": "YENİDEN BAĞLANIYOR",
                "transport": "REST UZLAŞTIRMA",
                "last_error": str(exc)[:220],
            })
            add_event(state, "LIVE_STREAM_RECONNECT", "Canlı kullanıcı akışı kesildi; REST uzlaştırması açık ve yeniden bağlantı deneniyor.")
            persist_state(state)
            await asyncio.sleep(min(30, 2 + int(state["stream"]["reconnect_count"])))
        finally:
            if listen_key and client is not None and not asyncio.current_task().cancelling():
                try:
                    await client.api_key_request("DELETE", "/fapi/v1/listenKey")
                except Exception:
                    pass


def _iso_epoch_ms(value: Any) -> int:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return int(time.time() * 1000) - 24 * 60 * 60 * 1000


async def verified_plan_pnl(client: BinanceLiveClient, plan: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a closed plan's trade PnL from official account trades.

    Funding payments are intentionally reported separately by Binance and are
    not represented here.  If the trade history cannot prove the close, the
    live entry gate stays locked rather than assuming zero PnL.
    """
    start_ms = max(_iso_epoch_ms(plan.get("created_at")) - 60_000, int(time.time() * 1000) - 180 * 24 * 60 * 60 * 1000)
    now_ms = int(time.time() * 1000)
    window_ms = 6 * 24 * 60 * 60 * 1000 + 23 * 60 * 60 * 1000
    rows: list[dict[str, Any]] = []
    normal_history: list[dict[str, Any]] = []
    algo_history: list[dict[str, Any]] = []
    cursor = start_ms
    while cursor <= now_ms:
        end = min(now_ms, cursor + window_ms)
        trades_payload, normal_payload, algo_payload = await asyncio.gather(
            client.signed(
                "GET", "/fapi/v1/userTrades",
                {"symbol": plan["symbol"], "startTime": cursor, "endTime": end, "limit": 1000},
            ),
            client.signed(
                "GET", "/fapi/v1/allOrders",
                {"symbol": plan["symbol"], "startTime": cursor, "endTime": end, "limit": 1000},
            ),
            client.signed(
                "GET", "/fapi/v1/allAlgoOrders",
                {"symbol": plan["symbol"], "startTime": cursor, "endTime": end, "limit": 1000},
            ),
        )
        rows.extend(item for item in response_rows(trades_payload) if isinstance(item, dict))
        normal_history.extend(item for item in response_rows(normal_payload) if isinstance(item, dict))
        algo_history.extend(item for item in response_rows(algo_payload) if isinstance(item, dict))
        cursor = end + 1

    known_ids = {int(value) for value in plan.get("exchange_order_ids", []) if str(value).isdigit()}
    if plan.get("entry_order_id"):
        known_ids.add(int(plan["entry_order_id"]))
    expected_normal_clients = {
        str(value) for value in [plan.get("entry_client_order_id"), *plan.get("close_client_order_ids", [])]
        if value
    }
    expected_algo_clients = {
        str(value) for value in [
            plan.get("stop_client_id"),
            client_id_for("TP1", str(plan.get("intent_id") or "")),
            client_id_for("TP2", str(plan.get("intent_id") or "")),
            client_id_for("TP3", str(plan.get("intent_id") or "")),
        ] if value
    }
    known_algo_ids = {int(value) for value in plan.get("protection_ids", []) if str(value).isdigit()}
    for item in normal_history:
        if str(item.get("clientOrderId") or "") in expected_normal_clients and str(item.get("orderId") or "").isdigit():
            known_ids.add(int(item["orderId"]))
    for item in algo_history:
        if (
            str(item.get("clientAlgoId") or "") in expected_algo_clients
            or (str(item.get("algoId") or "").isdigit() and int(item["algoId"]) in known_algo_ids)
        ):
            actual_order_id = str(item.get("actualOrderId") or "")
            if actual_order_id.isdigit():
                known_ids.add(int(actual_order_id))
    close_side = "SELL" if plan.get("direction") == "LONG" else "BUY"
    selected = [item for item in rows if int(item.get("orderId") or 0) in known_ids]
    close_rows = [item for item in selected if str(item.get("side") or "").upper() == close_side]
    if not close_rows:
        raise LiveExchangeError("Binance işlem geçmişi V25'e ait kapanış dolumunu henüz kimlikle doğrulamadı.")
    gross = sum(Decimal(str(item.get("realizedPnl") or "0")) for item in close_rows)
    commission_usdt = sum(
        Decimal(str(item.get("commission") or "0"))
        for item in selected
        if str(item.get("commissionAsset") or "").upper() == "USDT"
    )
    non_usdt_commission = sorted({
        str(item.get("commissionAsset") or "").upper()
        for item in selected
        if item.get("commissionAsset") and str(item.get("commissionAsset") or "").upper() != "USDT"
    })
    return {
        "gross_realized_pnl": round(float(gross), 8),
        "commission_usdt": round(float(commission_usdt), 8),
        "realized_pnl": round(float(gross - commission_usdt), 8),
        "trade_count": len(selected),
        "non_usdt_commission_assets": non_usdt_commission,
        "funding_included": False,
    }


async def settle_closed_plan(client: BinanceLiveClient, state: dict[str, Any], plan: dict[str, Any]) -> None:
    plan_id = str(plan.get("id") or "")
    verified = next(
        (item for item in state.get("events", []) if item.get("kind") == "LIVE_POSITION_CLOSED" and item.get("plan_id") == plan_id),
        None,
    )
    if verified:
        plan.update({"status": "KAPANDI", "closed_at": verified.get("created_at"), "realized_pnl": verified.get("realized_pnl")})
        return
    try:
        result = await verified_plan_pnl(client, plan)
    except LiveExchangeError as exc:
        plan.update({"status": "PNL DOĞRULANIYOR", "pnl_verified": False, "pnl_verification_error": str(exc)[:220]})
        state["armed_until"] = 0.0
        state["auto"]["enabled"] = False
        state["auto"]["session_until"] = 0.0
        if not any(item.get("kind") == "LIVE_POSITION_CLOSED_UNVERIFIED" and item.get("plan_id") == plan_id for item in state.get("events", [])):
            add_event(
                state,
                "LIVE_POSITION_CLOSED_UNVERIFIED",
                f"{plan['symbol']} pozisyonu kapalı; kesin PnL doğrulanana kadar yeni girişler kilitlendi.",
                symbol=plan["symbol"],
                plan_id=plan_id,
            )
        return
    plan.update({"status": "KAPANDI", "closed_at": now_iso(), "pnl_verified": True, **result})
    add_event(
        state,
        "LIVE_POSITION_CLOSED",
        f"{plan['symbol']} tracked pozisyon kapandı; net işlem PnL {result['realized_pnl']:+.4f} USDT (funding hariç).",
        symbol=plan["symbol"],
        plan_id=plan_id,
        **result,
    )


def recover_plan_from_intent(intent_id: str, intent: dict[str, Any], order: dict[str, Any]) -> dict[str, Any] | None:
    """Rebuild the local plan after a crash between exchange acceptance and plan persistence."""
    spec = intent.get("spec") if isinstance(intent.get("spec"), dict) else {}
    required = {"symbol", "direction", "order_type", "entry_price", "quantity", "stop_loss", "targets", "step", "min_qty"}
    if not required.issubset(spec) or str(order.get("status") or "") not in {"FILLED", "PARTIALLY_FILLED"}:
        return None
    order_id = int(order.get("orderId") or 0)
    if order_id <= 0:
        return None
    plan_id = hashlib.sha256(f"recover:{intent_id}".encode("utf-8")).hexdigest()[:16]
    return {
        "id": plan_id,
        "intent_id": intent_id,
        "symbol": spec["symbol"],
        "direction": spec["direction"],
        "order_type": spec["order_type"],
        "entry_price": spec["entry_price"],
        "quantity": spec["quantity"],
        "margin_usdt": spec.get("margin_usdt"),
        "notional_usdt": spec.get("notional_usdt"),
        "leverage": spec.get("leverage"),
        "applied_leverage": intent.get("applied_leverage"),
        "margin_type": intent.get("margin_type", "isolated"),
        "stop_loss": spec["stop_loss"],
        "targets": list(spec["targets"]),
        "step": spec["step"],
        "min_qty": spec["min_qty"],
        "entry_order_id": order_id,
        "entry_client_order_id": intent.get("client_order_id"),
        "status": "KURTARILDI · KORUMA KONTROLÜ",
        "created_at": intent.get("created_at") or now_iso(),
        "source": intent.get("source", "V25_RECOVERY"),
        "live": True,
        "recovered_after_restart": True,
        "protection_ids": [],
        "exchange_order_ids": [order_id],
    }


async def recover_orphan_plans(client: BinanceLiveClient, state: dict[str, Any], positions: dict[str, dict[str, Any]]) -> int:
    recovered_count = 0
    active_symbols = {
        str(plan.get("symbol") or "") for plan in state.get("plans", {}).values()
        if plan.get("status") not in {"KAPANDI", "İPTAL"}
    }
    intents = sorted(
        state.get("intents", {}).items(),
        key=lambda item: str(item[1].get("created_at") or ""),
        reverse=True,
    )
    for intent_id, intent in intents:
        symbol = str(intent.get("symbol") or "")
        client_id = str(intent.get("client_order_id") or "")
        if not symbol or symbol not in positions or symbol in active_symbols or not client_id.startswith(LIVE_CLIENT_PREFIX):
            continue
        order = await find_order(client, symbol, client_id)
        if not order:
            continue
        recovery_spec = intent.get("spec") if isinstance(intent.get("spec"), dict) else {}
        recovery_spec = {
            **recovery_spec,
            "symbol": symbol,
            "side": "BUY" if str(recovery_spec.get("direction") or "").upper() == "LONG" else "SELL",
        }
        if not order_matches_spec(order, recovery_spec, client_id):
            add_event(state, "LIVE_FOREIGN_ORDER", "İlişkisiz canlı emir uzlaştırılmadı; sahiplenilmedi.", symbol=symbol, client_order_id_suffix=client_id[-8:])
            continue
        plan = recover_plan_from_intent(intent_id, intent, order)
        if plan is None:
            continue
        state["plans"] = {plan["id"]: plan, **state.get("plans", {})}
        active_symbols.add(symbol)
        add_event(
            state,
            "LIVE_PLAN_RECOVERED",
            f"{symbol} canlı pozisyonu kalıcı niyet kaydından kurtarıldı; Stop/TP denetimi başlatıldı.",
            symbol=symbol,
            plan_id=plan["id"],
        )
        recovered_count += 1
    return recovered_count


def demo_certificate(application: Any) -> dict[str, Any]:
    state = getattr(application.state, "v21_demo", None)
    return certificate_payload(state) if state else {}


def readiness(application: Any, state: dict[str, Any]) -> dict[str, Any]:
    consent = consent_status(state)
    snapshot = state.get("snapshot") or {}
    gates = release_gates(
        credentials=bool(consent.get("fingerprint")), consent_active=bool(consent.get("active")),
        connected=bool(state.get("connected")), one_way=not bool(snapshot.get("hedge_mode", True)),
        policy_acknowledged=state.get("policy_ack_digest") == policy_digest(state["policy"]),
        demo_certificate=demo_certificate(application),
    )
    return {"ready": release_ready(gates), "score": round(sum(1 for item in gates if item["passed"]) / len(gates) * 100), "gates": gates, "demo_certificate": demo_certificate(application)}


def live_daily_metrics(state: dict[str, Any]) -> dict[str, Any]:
    metrics = daily_execution_metrics(state.get("events", []))
    plan_blocks = sum(
        1 for plan in state.get("plans", {}).values()
        if plan.get("status") == "PNL DOĞRULANIYOR" or plan.get("pnl_verified") is False
    )
    metrics["unverified_closures"] = max(int(metrics.get("unverified_closures") or 0), plan_blocks)
    return metrics


def public_status(application: Any) -> dict[str, Any]:
    state = application.state.v25_execution
    consent = consent_status(state)
    release = readiness(application, state)
    snapshot = state.get("snapshot") or {}
    scan_stats = state["auto"].get("last_scan_stats") or {}
    return {
        "version": V25_VERSION,
        "mode": "LIVE_GUARD",
        "host": LIVE_REST_BASE,
        "websocket_host": LIVE_WS_BASE,
        "credentials": {"configured": bool(consent.get("fingerprint")), "fingerprint": consent.get("fingerprint"), "storage": live_credential_source()},
        "consent": consent,
        "connected": bool(state.get("connected")),
        "connection": state.get("connection"),
        "stream": state.get("stream"),
        "armed": is_armed(state),
        "real_trading_locked": bool(state.get("real_trading_locked", True)),
        "live_auto_trade": bool(state.get("live_auto_trade", False)),
        "armed_until": datetime.fromtimestamp(state["armed_until"], timezone.utc).isoformat() if is_armed(state) else None,
        "auto": state["auto"],
        "scanner": {
            "last_scan_at": state["auto"].get("last_scan"),
            "scanned_symbol_count": scan_stats.get("scanned_symbol_count", len(scan_stats.get("candidate_symbols", []))),
            "candidate_symbols": scan_stats.get("candidate_symbols", scan_stats.get("selected_candidates", [])),
            "deep_analysis_symbols": scan_stats.get("deep_analysis_symbols", []),
            "candidate_count": scan_stats.get("candidate_count", scan_stats.get("deep_analysis_candidates", 0)),
            "deep_analysis_count": scan_stats.get("deep_analysis_count", len(scan_stats.get("deep_analysis_symbols", []))),
            "selected_symbols": scan_stats.get("selected_symbols", scan_stats.get("selected_candidates", [])),
            "selected_symbols_count": scan_stats.get("selected_symbols_count", len(scan_stats.get("selected_symbols", scan_stats.get("selected_candidates", [])))),
            "executed_symbols": scan_stats.get("executed_symbols", []),
            "executed_symbols_count": scan_stats.get("executed_symbols_count", len(scan_stats.get("executed_symbols", []))),
            "last_skip_reason": state["auto"].get("last_skip_reason"),
            "last_cycle_stage": state["auto"].get("last_cycle_stage"),
        },
        "auto_session_until": datetime.fromtimestamp(float(state["auto"].get("session_until") or 0), timezone.utc).isoformat() if auto_session_active(state) else None,
        "policy": state["policy"],
        "policy_digest": policy_digest(state["policy"]),
        "policy_acknowledged": state.get("policy_ack_digest") == policy_digest(state["policy"]),
        "readiness": release,
        "account": {"wallet_balance": snapshot.get("wallet_balance"), "available_balance": snapshot.get("available_balance"), "unrealized_pnl": snapshot.get("unrealized_pnl"), "positions": snapshot.get("positions", []), "open_orders": snapshot.get("open_orders", []), "open_algo_orders": snapshot.get("open_algo_orders", []), "hedge_mode": snapshot.get("hedge_mode")},
        "daily": live_daily_metrics(state),
        "plans": list(state.get("plans", {}).values())[:50],
        "events": state.get("events", [])[:80],
        "emergency": state.get("emergency"),
        "withdrawals_supported": False,
        "secret_inputs_in_browser": False,
        "profit_guaranteed": False,
    }


async def live_candles(client: BinanceLiveClient, symbol: str, interval: str, limit: int = 260) -> tuple[list[dict[str, float]], int]:
    rows = await client.public_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    candles: list[dict[str, float]] = []
    last_open_time = 0
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, list) or len(row) < 6:
            continue
        candles.append({"time": int(row[0] / 1000), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])})
        last_open_time = int(row[0])
    return candles, last_open_time


async def canonical_live_decision(application: Any, client: BinanceLiveClient, symbol: str, interval: str, primary_candles: list[dict[str, float]] | None = None) -> dict[str, Any]:
    """Run the side-effect-free canonical decision before live account gates."""
    from .main import canonical_historical_decision

    if interval != "15m":
        return {"decision": "WAIT", "symbol": symbol, "entry_eligible": False, "reason": "CANONICAL_BASE_INTERVAL_REQUIRES_15M"}

    frames: dict[str, list[dict[str, float]]] = {interval: primary_candles} if primary_candles is not None else {}
    for timeframe in (interval, "1h", "4h"):
        if timeframe not in frames:
            frames[timeframe], _ = await live_candles(client, symbol, timeframe)
    fifteen = frames.get("15m", frames.get(interval, []))
    if len(fifteen) < 2:
        return {"decision": "WAIT", "symbol": symbol, "entry_eligible": False, "reason": "INSUFFICIENT_CLOSED_CANDLES"}
    decision_time = int(fifteen[-1]["time"])
    return canonical_historical_decision(
        symbol,
        frames,
        decision_time,
        required_intervals=("15m", "1h", "4h"),
    )


async def execute_live_order(
    application: Any,
    body: LiveOrderRequest,
    *,
    source: str,
    allowed_symbols: list[str] | None = None,
) -> dict[str, Any]:
    state = application.state.v25_execution
    if not state.get("recovery_ready", False):
        raise HTTPException(503, "Canlı durum kurtarma tamamlanmadı; yeni emir gönderilmedi.")
    if source == "V25_AUTO":
        if not auto_session_active(state):
            raise HTTPException(423, "Bir saatlik gözetimli canlı otomasyon oturumu kapalı veya süresi doldu.")
    elif not is_armed(state):
        raise HTTPException(423, "5 dakikalık canlı emir kilidi kapalı veya süresi doldu.")
    if live_execution_blocked(state):
        raise HTTPException(423, "Canlı yürütme kilitli; acil durum veya belirsiz emir uzlaştırması tamamlanmadı.")
    if not readiness(application, state)["ready"]:
        raise HTTPException(423, "Canlı yayın kapıları tamamlanmadı; emir gönderilmedi.")
    submission_started = False
    async with state["lock"]:
        try:
            client = client_for(application)
            snapshot = await account_snapshot(client)
            if state.get("real_trading_locked") is not False:
                raise LiveExchangeError("Gerçek işlem kilidi kapalı; açık canlı onay olmadan emir gönderilmedi.", http_status=423)
            if source == "V25_AUTO" and state.get("live_auto_trade") is not True:
                raise LiveExchangeError("Canlı otomasyon açık değil; otomatik emir gönderilmedi.", http_status=423)
            if snapshot.get("hedge_mode"):
                raise LiveExchangeError("Canlı hesap One-way / Tek Yön modunda olmalı.", http_status=409)
            symbol = normalize_symbol(body.symbol)
            daily = live_daily_metrics(state)
            manual_signal = {"direction": body.direction, "confidence": 100, "radar": {"trap_score": 0}}
            guard = evaluate_entry_gates(
                symbol=symbol,
                signal=manual_signal,
                snapshot=snapshot,
                policy=state["policy"],
                daily=daily,
                spread_bps=await spread_bps(client, symbol),
                armed=True,
                allowed_symbols=allowed_symbols,
                active_plans=list(state.get("plans", {}).values()),
                candidate_notional_usdt=body.margin_usdt * body.leverage,
            )
            if not guard["passed"]:
                raise LiveExchangeError(f"Canlı risk kapısı: {guard['reason']}", http_status=409)
            if float(snapshot.get("available_balance") or 0) < body.margin_usdt:
                raise LiveExchangeError("Canlı hesap kullanılabilir bakiyesi seçilen marjinden düşük.", http_status=409)
            spec = await build_live_spec(client, body, state["policy"], allowed_symbols=allowed_symbols)
            validate_protection_readiness(spec, state["policy"])
            await set_live_isolated_margin(client, spec["symbol"])
            leverage_audit = await apply_live_verified_leverage(client, spec["symbol"], spec["leverage"])
            intent_id = body.intent_id or f"manual-{uuid.uuid4().hex}"
            client_id = client_id_for("ENTRY", intent_id)
            existing_exposure = sum(
                abs(float(item.get("notional") or item.get("notional_usdt") or 0))
                for item in snapshot.get("positions", [])
                if isinstance(item, dict)
            )
            failed_gate_keys = {item["key"] for item in guard["gates"] if not item["passed"]}
            serializable_spec = {
                "symbol": spec["symbol"], "direction": spec["direction"], "order_type": spec["order_type"],
                "entry_price": spec["entry_price"], "quantity": spec["quantity"],
                "margin_usdt": spec["margin_usdt"], "notional_usdt": spec["notional_usdt"],
                "leverage": spec["leverage"], "stop_loss": spec["stop_loss"],
                "targets": list(spec["targets"]), "step": decimal_text(spec["step"]),
                "min_qty": decimal_text(spec["min_qty"]),
            }
            audit_snapshot = MappingProxyType({
                "decision_id": intent_id,
                "timestamp": now_iso(),
                "mode": "LIVE",
                "symbol": spec["symbol"],
                "side": spec["side"],
                "position_side": "BOTH",
                "quantity": spec["quantity"],
                "leverage": spec["leverage"],
                "margin_usdt": spec["margin_usdt"],
                "estimated_notional_usdt": spec["notional_usdt"],
                "stop_loss": spec["stop_loss"],
                "tp_levels": list(spec["targets"]),
                "risk_amount_usdt": spec["estimated_stop_loss_usdt"],
                "exposure_usdt": existing_exposure + spec["notional_usdt"],
                "exposure_limit_usdt": state["policy"]["max_total_exposure_usdt"],
                "active_plan_conflict": "active_plan" in failed_gate_keys,
                "protection_readiness": True,
                "lock_state": bool(state.get("real_trading_locked", True)),
                "order_parameters": {
                    "type": spec["order_type"],
                    "entry_price": spec["entry_price"],
                    "stop_loss": spec["stop_loss"],
                    "targets": list(spec["targets"]),
                },
            })
            add_event(state, "LIVE_DECISION_AUDIT", "Canlı emir öncesi değişmez karar özeti oluşturuldu.", symbol=spec["symbol"], decision_id=intent_id, audit_snapshot=dict(audit_snapshot))
            state["intents"][intent_id] = {
                "symbol": spec["symbol"], "client_order_id": client_id, "created_at": now_iso(),
                "source": source, "spec": serializable_spec,
                "applied_leverage": leverage_audit["applied_leverage"],
                "margin_type": leverage_audit["margin_type"],
            }
            persist_state(state)
            submission_started = True
            result = await submit_entry(client, spec, client_id, test_only=False)
            plan_id = uuid.uuid4().hex[:16]
            plan = {
                "id": plan_id, "intent_id": intent_id, "symbol": spec["symbol"], "direction": spec["direction"],
                "order_type": spec["order_type"], "entry_price": spec["entry_price"], "quantity": spec["quantity"],
                "margin_usdt": spec["margin_usdt"], "notional_usdt": spec["notional_usdt"], "leverage": spec["leverage"],
                "applied_leverage": leverage_audit["applied_leverage"], "margin_type": leverage_audit["margin_type"],
                "stop_loss": spec["stop_loss"], "targets": spec["targets"], "step": decimal_text(spec["step"]),
                "min_qty": decimal_text(spec["min_qty"]), "entry_order_id": int(result.get("orderId") or 0) or None,
                "entry_client_order_id": client_id, "status": "DOLUM BEKLİYOR", "created_at": now_iso(),
                "source": source, "live": True, "protection_ids": [],
                "exchange_order_ids": [int(result.get("orderId"))] if result.get("orderId") else [],
            }
            state["plans"] = {plan_id: plan, **state.get("plans", {})}
            add_event(state, "LIVE_ENTRY_RECOVERED" if result.get("recovered") else "LIVE_ENTRY", f"{spec['symbol']} {spec['direction']} {spec['order_type']} canlı emir gönderildi ({source}).", symbol=spec["symbol"], direction=spec["direction"], client_order_id=client_id)
            # Persist exchange acceptance before any later API call. If the process
            # stops now, reconciliation can recover the exact intent and protection.
            persist_state(state)
            if spec["order_type"] == "MARKET":
                await install_protection(client, state, plan)
            state["snapshot"] = await account_snapshot(client)
            persist_state(state)
            return {"ok": True, "order": {"order_id": result.get("orderId"), "client_order_id": client_id, "status": result.get("status", plan["status"])}, "plan": plan, "risk_guard": guard, "profit_guaranteed": False}
        except (LiveExchangeError, BinanceDemoError) as exc:
            if isinstance(exc, LiveExchangeError) and exc.unknown_execution:
                lock_live_execution(
                    state,
                    "UNKNOWN_ORDER_STATE",
                    unknown=True,
                    symbol=body.symbol,
                    client_id=body.intent_id,
                )
                persist_state(state)
            state["connection"]["last_error"] = str(exc)[:240]
            raise safe_exchange_error(exc) from exc
        except Exception as exc:
            lock_live_execution(
                state,
                "LIVE_EXCEPTION_UNKNOWN" if submission_started else "LIVE_EXCEPTION",
                unknown=submission_started,
                symbol=body.symbol,
                client_id=body.intent_id,
            )
            state["connection"]["last_error"] = "Beklenmeyen canlı emir hatası"
            persist_state(state)
            raise HTTPException(502, "Canlı emir akışı güvenli şekilde kilitlendi; manuel inceleme gerekli.") from exc


async def automatic_cycle(application: Any) -> None:
    state = application.state.v25_execution
    was_enabled = bool(state["auto"].get("enabled"))
    session_until = float(state["auto"].get("session_until") or 0)
    if not auto_session_active(state):
        reason = "session_expired" if was_enabled and session_until <= time.time() else "automation_inactive"
        state["auto"]["last_skip_reason"] = reason
        state["auto"]["last_cycle_stage"] = "skipped"
        automation_telemetry(f"AUTOMATION_SKIP reason={reason}", reason=reason)
        return
    if not readiness(application, state)["ready"]:
        state["auto"]["last_skip_reason"] = "not_ready"
        state["auto"]["last_cycle_stage"] = "skipped"
        automation_telemetry("AUTOMATION_SKIP reason=not_ready", reason="not_ready")
        state["auto"].update({
            "enabled": False,
            "session_until": 0.0,
            "last_decision": "Canlı yayın kapılarından biri kapandı; otomatik oturum kilitlendi.",
        })
        add_event(state, "LIVE_AUTO_FAIL_CLOSED", "Canlı yayın kapısı kapanınca otomatik yeni girişler durduruldu.")
        persist_state(state)
        return
    last_scan = _iso_epoch_ms(state["auto"].get("last_scan")) if state["auto"].get("last_scan") else 0
    if int(time.time() * 1000) - last_scan < int(state["policy"]["scan_seconds"]) * 1000:
        state["auto"]["last_skip_reason"] = "scan_throttled"
        state["auto"]["last_cycle_stage"] = "skipped"
        automation_telemetry("AUTOMATION_SKIP reason=scan_throttled", reason="scan_throttled")
        return
    state["auto"]["last_scan"] = now_iso()
    state["auto"]["busy"] = True
    try:
        client = client_for(application)
        snapshot = await account_snapshot(client)
        daily = live_daily_metrics(state)
        state["auto"]["last_skip_reason"] = None
        state["auto"]["last_cycle_stage"] = "scanning"
        candidates = await scan_market_candidates(client, snapshot)
        logger.info(
            "MULTI_SYMBOL_SCAN started eligible symbols: %s top 100 selected deep analysis candidates: %s",
            getattr(client, "last_scan_eligible_count", 0),
            len(candidates),
        )
        signals: list[dict[str, Any]] = []
        analyzed_symbols: list[str] = []
        state["auto"]["last_cycle_stage"] = "deep_analysis"
        for candidate in candidates:
            symbol = candidate["symbol"]
            candles, candle_id = await live_candles(client, symbol, state["policy"]["interval"])
            if len(candles) < 220:
                continue
            analyzed_symbols.append(symbol)
            canonical = await canonical_live_decision(application, client, symbol, state["policy"]["interval"], candles)
            signal = canonical.get("analysis") or {}
            intent_id = f"auto-{symbol}-{state['policy']['interval']}-{candle_id}"
            if intent_id in state["intents"]:
                state["duplicate_blocks"] += 1
                continue
            if canonical.get("decision") not in {"BUY", "SELL"} or not canonical.get("entry_eligible"):
                continue
            signals.append({"candidate": candidate, "signal": signal, "intent_id": intent_id})
        signals.sort(key=lambda item: (float(item["candidate"].get("opportunity_score") or 0), int(item["signal"].get("confidence") or 0)), reverse=True)
        selected = signals[:3]
        selected_symbols = [item["candidate"]["symbol"] for item in selected]
        executed_symbols: list[str] = []
        logger.info(
            "MULTI_SYMBOL_SCAN deep analysis completed: %s symbols: %s",
            len(analyzed_symbols),
            ",".join(analyzed_symbols) or "NONE",
        )
        state["auto"]["last_scan_stats"] = {
            "scanned_symbol_count": len(candidates),
            "eligible_symbols": getattr(client, "last_scan_eligible_count", 0),
            "candidate_symbols": [item["symbol"] for item in candidates],
            "candidate_count": len(signals),
            "deep_analysis_candidates": len(candidates),
            "deep_analysis_symbols": analyzed_symbols,
            "signals_found": len(signals),
            "selected_symbols": selected_symbols,
            "selected_symbols_count": len(selected_symbols),
            "selected_candidates": selected_symbols,
            "positions_open": len(snapshot.get("positions", [])),
            "position_capacity": 5,
        }
        logger.info(
            "MULTI_SYMBOL_SCAN signals found: %s selected candidates: %s positions open: %s/5",
            len(signals),
            ",".join(selected_symbols) or "NONE",
            len(snapshot.get("positions", [])),
        )
        for item in selected:
            candidate = item["candidate"]
            signal = item["signal"]
            symbol = candidate["symbol"]
            intent_id = item["intent_id"]
            spread = await spread_bps(client, symbol)
            guard = evaluate_entry_gates(symbol=symbol, signal=signal, snapshot=snapshot, policy=state["policy"], daily=daily, spread_bps=spread, armed=True, allowed_symbols=[symbol])
            if not guard["passed"]:
                state["auto"]["last_decision"] = f"{symbol}: BEKLE · {guard['reason']}"
                continue
            risk = risk_sized_order(float(signal["entry"]), float(signal["stop_loss"]), state["policy"])
            if risk["margin_usdt"] < 5:
                state["auto"]["last_decision"] = f"{symbol}: Binance minimum güvenli marjin eşiği altında; BEKLE."
                continue
            body = LiveOrderRequest(
                symbol=symbol, direction=signal["direction"], order_type="MARKET",
                margin_usdt=risk["margin_usdt"], leverage=risk["leverage"], stop_loss=signal["stop_loss"],
                tp1=signal["tp1"], tp2=signal["tp2"], tp3=signal["tp3"], intent_id=intent_id,
            )
            await execute_live_order(application, body, source="V25_AUTO", allowed_symbols=[symbol])
            executed_symbols.append(symbol)
            state["auto"]["last_decision"] = f"{symbol} {signal['direction']} canlı işlem açıldı; Stop/TP doğrulandı."
            snapshot = await account_snapshot(client)
            if len(snapshot.get("positions", [])) >= int(state["policy"]["max_positions"]):
                break
        state["auto"]["last_scan_stats"]["executed_symbols"] = executed_symbols
        state["auto"]["last_scan_stats"]["executed_symbols_count"] = len(executed_symbols)
        state["auto"]["last_cycle_stage"] = "completed"
        state["auto"]["cycles"] += 1
    except Exception as exc:
        state["auto"]["last_skip_reason"] = "reconcile_failed"
        state["auto"]["last_cycle_stage"] = "error"
        state["auto"].update({"last_error": str(exc)[:240], "last_decision": "Canlı otomasyon turu güvenli biçimde durduruldu."})
        lock_live_execution(state, "AUTO_EXCEPTION")
        add_event(state, "AUTO_ERROR", "Canlı otomasyon turu hata nedeniyle yeni emir göndermedi.")
    finally:
        state["auto"]["busy"] = False
        persist_state(state)


async def reconcile(application: Any) -> None:
    state = application.state.v25_execution
    client = client_for(application)
    snapshot = await account_snapshot(client)
    state["snapshot"] = snapshot
    state["recovery_ready"] = True
    state["recovery_error"] = None
    state["connected"] = True
    state["connection"].update({"last_checked": now_iso(), "last_error": None, "clock_offset_ms": client.time_offset_ms})
    positions = {item["symbol"]: item for item in snapshot.get("positions", [])}
    open_algos = snapshot.get("open_algo_orders", [])
    recovered_count = await recover_orphan_plans(client, state, positions)
    if recovered_count and state.get("reconciliation_required"):
        state["execution_state"] = "LOCKED"
        state["reconciliation_required"] = False
        state["emergency"].update({"active": False, "reason": "UNKNOWN_ORDER_RECONCILED"})
        add_event(state, "UNKNOWN_ORDER_RECONCILED", "Belirsiz canlı emir exact kimlik ve parametrelerle uzlaştırıldı; yeniden arm gerekiyor.", recovery_state="LOCKED")
    for plan in state.get("plans", {}).values():
        if plan.get("status") in {"KAPANDI", "İPTAL"}:
            continue
        symbol = plan.get("symbol")
        position = positions.get(symbol)
        if position is None:
            if plan.get("status") == "DOLUM BEKLİYOR":
                entry = await find_order(client, symbol, str(plan.get("entry_client_order_id") or ""))
                entry_status = str((entry or {}).get("status") or "")
                if entry_status in {"CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}:
                    plan.update({"status": "İPTAL", "closed_at": now_iso()})
                    add_event(state, "LIVE_ENTRY_CANCELLED", f"{symbol} giriş emri {entry_status}; pozisyon oluşmadı.", symbol=symbol, plan_id=plan.get("id"))
                elif entry_status not in {"FILLED"}:
                    continue
            cancelled = await cancel_owned_algos_for_symbol(client, open_algos, str(symbol))
            if cancelled:
                add_event(
                    state,
                    "PROTECTION_CLEANUP",
                    f"{symbol} kapandı; {cancelled} artık V25 Stop/TP emri temizlendi.",
                    symbol=symbol,
                    plan_id=plan.get("id"),
                )
            await settle_closed_plan(client, state, plan)
            continue
        has_stop = any(item.get("symbol") == symbol and str(item.get("type", "")).upper() == "STOP_MARKET" for item in open_algos)
        if not has_stop:
            state["protection_repairs"] += 1
            add_event(state, "PROTECTION_REPAIR", f"{symbol} Stop eksik; koruma yeniden kuruluyor.", symbol=symbol)
            await install_protection(client, state, plan)
        elif plan.get("status") == "DOLUM BEKLİYOR":
            plan["status"] = "KORUMA AKTİF"
    persist_state(state)


async def execution_loop(application: Any) -> None:
    backoff = 5
    while True:
        try:
            recovery_loaded = application.state.v25_execution.get("recovery_loaded", not bool(os.getenv("DATABASE_URL", "").strip()))
            database_unavailable = bool(os.getenv("DATABASE_URL", "").strip()) and getattr(application.state, "db_pool", None) is None
            if not recovery_loaded or database_unavailable:
                application.state.v25_execution["auto"]["last_skip_reason"] = "recovery_unavailable"
                automation_telemetry("AUTOMATION_SKIP reason=recovery_unavailable", reason="recovery_unavailable")
                await asyncio.sleep(5)
                continue
            automation_telemetry("AUTOMATION_LOOP running", reason="loop_running")
            _, secret, fingerprint = live_credentials_status()
            if not fingerprint or len(secret) < 10:
                application.state.v25_execution["auto"]["last_skip_reason"] = "no_credentials"
                application.state.v25_execution["auto"]["last_cycle_stage"] = "skipped"
                automation_telemetry("AUTOMATION_SKIP reason=no_credentials", reason="no_credentials")
                await asyncio.sleep(5)
                continue
            async with application.state.v25_execution["lock"]:
                await reconcile(application)
            await automatic_cycle(application)
            backoff = 5
            await asyncio.sleep(RECONCILE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            automation_telemetry("AUTOMATION_SKIP reason=reconcile_failed", reason="reconcile_failed")
            state = application.state.v25_execution
            state["connected"] = False
            state["connection"].update({"last_checked": now_iso(), "last_error": str(exc)[:240]})
            lock_live_execution(state, "RECONCILIATION_FAILURE", unknown=True)
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)


def init_v25_execution(application: Any) -> None:
    state = load_state()
    state["lock"] = asyncio.Lock()
    state["_app"] = application
    state["recovery_loaded"] = not bool(os.getenv("DATABASE_URL", "").strip())
    state["recovery_ready"] = state["recovery_loaded"]
    state["recovery_error"] = None if state["recovery_ready"] else "PostgreSQL recovery pending."
    application.state.v25_execution = state
    add_event(state, "V25_START", "V25 Live Guard başladı; canlı giriş ve otomasyon güvenlik için kapalı.")
    application.state.v25_execution_task = asyncio.create_task(execution_loop(application))
    application.state.v25_live_stream_task = asyncio.create_task(live_user_stream_loop(application))


async def shutdown_v25_execution(application: Any) -> None:
    state = getattr(application.state, "v25_execution", None)
    tasks = [
        task for task in (
            getattr(application.state, "v25_execution_task", None),
            getattr(application.state, "v25_live_stream_task", None),
        )
        if task is not None
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    persistence_tasks = list(getattr(application.state, "_v25_persistence_tasks", set()))
    if persistence_tasks:
        await asyncio.gather(*persistence_tasks, return_exceptions=True)
    if state:
        state["armed_until"] = 0.0
        state["auto"]["enabled"] = False
        state["auto"]["session_until"] = 0.0
        persist_state(state)


@router.get("/status")
async def v25_status(request: Request) -> dict[str, Any]:
    execution_owner(request)
    return public_status(request.app)


@router.get("/market/candles")
async def v25_market_candles(
    request: Request,
    symbol: str = Query(min_length=5, max_length=20),
    interval: Literal["1m", "5m", "15m", "1h", "4h"] = "15m",
    limit: int = Query(default=360, ge=50, le=500),
) -> dict[str, Any]:
    """Owner-only live chart feed; never exposes credentials to the browser."""
    execution_owner(request)
    try:
        normalized = normalize_symbol(symbol)
        candles, last_open_time = await live_candles(public_client_for(request.app), normalized, interval, limit)
        return {
            "symbol": normalized,
            "interval": interval,
            "candles": candles,
            "last_open_time": last_open_time,
            "updated_at": now_iso(),
            "orders_created": False,
        }
    except (LiveExchangeError, BinanceDemoError) as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/connect/read-only")
async def v25_connect(request: Request) -> dict[str, Any]:
    user = execution_owner(request)
    state = request.app.state.v25_execution
    try:
        async with state["lock"]:
            client = client_for(request.app)
            snapshot = await account_snapshot(client)
            state["snapshot"] = snapshot
            state["connected"] = True
            state["connection"].update({"last_checked": now_iso(), "last_error": None, "clock_offset_ms": client.time_offset_ms})
            add_event(state, "READ_ONLY_CONNECTED", "Canlı hesap salt-okunur bağlantısı doğrulandı; emir gönderilmedi.", actor=user["id"])
            persist_state(state)
        return public_status(request.app)
    except (LiveExchangeError, BinanceDemoError) as exc:
        state["connected"] = False
        state["connection"].update({"last_checked": now_iso(), "last_error": str(exc)[:240]})
        raise safe_exchange_error(exc) from exc


@router.put("/policy")
async def v25_policy(request: Request, body: PolicyUpdate) -> dict[str, Any]:
    user = execution_owner(request)
    state = request.app.state.v25_execution
    updates = body.model_dump(exclude_none=True)
    state["policy"] = sanitize_execution_policy({**state["policy"], **updates})
    state["policy_ack_digest"] = None
    state["armed_until"] = 0.0
    state["auto"]["enabled"] = False
    state["auto"]["session_until"] = 0.0
    add_event(state, "POLICY_CHANGED", "Canlı risk limitleri değişti; onay ve emir kilidi sıfırlandı.", actor=user["id"])
    persist_state(state)
    return public_status(request.app)


@router.post("/policy/acknowledge")
async def v25_policy_ack(request: Request, body: Confirmation) -> dict[str, Any]:
    user = execution_owner(request)
    if body.confirmation.strip().upper() != "RİSK LİMİTLERİNİ ONAYLIYORUM":
        raise HTTPException(422, "Onay için RİSK LİMİTLERİNİ ONAYLIYORUM yazın.")
    state = request.app.state.v25_execution
    state["policy_ack_digest"] = policy_digest(state["policy"])
    add_event(state, "POLICY_ACK", "Mevcut canlı risk politikası sahibi tarafından onaylandı.", actor=user["id"])
    persist_state(state)
    return public_status(request.app)


@router.post("/consent")
async def v25_web_consent(request: Request, body: Confirmation) -> dict[str, Any]:
    """Create a fingerprint-bound, memory-only 24 hour live consent."""
    user = execution_owner(request)
    if body.confirmation.strip().upper() != "CANLI İŞLEM RİSKİNİ 24 SAAT KABUL EDİYORUM":
        raise HTTPException(422, "Onay için CANLI İŞLEM RİSKİNİ 24 SAAT KABUL EDİYORUM yazın.")
    state = request.app.state.v25_execution
    api_key, secret_key, fingerprint = live_credentials_status()
    if not api_key or len(secret_key) < 10 or not fingerprint:
        raise HTTPException(412, "Önce programdaki Borsa Bağlantıları bölümünden canlı Binance API anahtarını kaydedip aktifleştirin.")
    state["web_consent"] = {
        "accepted_at": now_iso(),
        "expires_at_epoch": time.time() + (24 * 60 * 60),
        "key_fingerprint": fingerprint,
    }
    state["armed_until"] = 0.0
    state["auto"]["enabled"] = False
    state["auto"]["session_until"] = 0.0
    add_event(state, "LIVE_WEB_CONSENT", "24 saatlik canlı risk izni verildi; sunucu yeniden başlarsa izin iptal olur.", actor=user["id"])
    persist_state(state)
    return public_status(request.app)


@router.post("/order/test")
async def v25_order_test(request: Request, body: LiveOrderRequest) -> dict[str, Any]:
    user = execution_owner(request)
    state = request.app.state.v25_execution
    try:
        client = client_for(request.app)
        spec = await build_live_spec(client, body, state["policy"])
        result = await submit_entry(client, spec, client_id_for("TEST", body.intent_id or uuid.uuid4().hex), test_only=True)
        add_event(state, "LIVE_ORDER_TEST", f"{spec['symbol']} imzalı /order/test doğrulandı; gerçek emir oluşmadı.", actor=user["id"], symbol=spec["symbol"])
        persist_state(state)
        return {"ok": True, "exchange_response": result, "created_order": False, "message": "Binance canlı imza ve emir şeması doğrulandı; gerçek emir oluşturulmadı."}
    except (LiveExchangeError, BinanceDemoError) as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/arm")
async def v25_arm(request: Request, body: Confirmation) -> dict[str, Any]:
    user = execution_owner(request)
    if body.confirmation.strip().upper() != "CANLI EMİR RİSKİNİ KABUL EDİYORUM":
        raise HTTPException(422, "Kilidi açmak için CANLI EMİR RİSKİNİ KABUL EDİYORUM yazın.")
    state = request.app.state.v25_execution
    if live_execution_blocked(state):
        raise HTTPException(423, "Canlı acil durdurma veya belirsiz uzlaştırma aktif; önce recovery tamamlanmalı.")
    release = readiness(request.app, state)
    if not release["ready"]:
        pending = next((item["label"] for item in release["gates"] if not item["passed"]), "hazırlık kapısı")
        raise HTTPException(423, f"Canlı kilit açılamadı: {pending} bekleniyor.")
    state["armed_until"] = time.time() + LIVE_ARM_SECONDS
    state["real_trading_locked"] = False
    add_event(state, "LIVE_ARM", "Canlı yeni giriş izni 5 dakika için açıldı.", actor=user["id"])
    return public_status(request.app)


@router.post("/disarm")
async def v25_disarm(request: Request) -> dict[str, Any]:
    user = execution_owner(request)
    state = request.app.state.v25_execution
    state["armed_until"] = 0.0
    state["real_trading_locked"] = True
    state["live_auto_trade"] = False
    state["auto"]["enabled"] = False
    state["auto"]["session_until"] = 0.0
    add_event(state, "LIVE_DISARM", "Canlı yeni girişler ve otomasyon kilitlendi; korumalar çalışmaya devam eder.", actor=user["id"])
    return public_status(request.app)


@router.post("/order")
async def v25_order(request: Request, body: ManualLiveOrderRequest) -> dict[str, Any]:
    execution_owner(request)
    if body.confirmation.strip().upper() != "CANLI EMİR GÖNDER":
        raise HTTPException(422, "Canlı manuel emir için CANLI EMİR GÖNDER yazın.")
    payload = body.model_dump(exclude={"confirmation"})
    return await execute_live_order(request.app, LiveOrderRequest(**payload), source="MANUAL")


@router.post("/auto/start")
async def v25_auto_start(request: Request, body: Confirmation) -> dict[str, Any]:
    user = execution_owner(request)
    if body.confirmation.strip().upper() != "CANLI OTOMATİK":
        raise HTTPException(422, "Otomasyonu açmak için CANLI OTOMATİK yazın.")
    state = request.app.state.v25_execution
    if live_execution_blocked(state) or not is_armed(state) or not readiness(request.app, state)["ready"]:
        raise HTTPException(423, "Önce bütün yayın kapılarını tamamlayıp 5 dakikalık canlı kilidi açın.")
    state["auto"].update({"enabled": True, "session_until": time.time() + LIVE_AUTO_SESSION_SECONDS, "last_error": None, "last_decision": "Bir saatlik gözetimli canlı tarama başlatıldı."})
    state["live_auto_trade"] = True
    state["real_trading_locked"] = False
    state["armed_until"] = 0.0
    add_event(state, "LIVE_AUTO_START", "Canlı otomasyon 5 dakikalık kilit içinden bir saatlik gözetimli oturum için açıldı.", actor=user["id"])
    persist_state(state)
    return public_status(request.app)


@router.post("/auto/stop")
async def v25_auto_stop(request: Request) -> dict[str, Any]:
    user = execution_owner(request)
    state = request.app.state.v25_execution
    state["auto"]["enabled"] = False
    state["auto"]["session_until"] = 0.0
    state["real_trading_locked"] = True
    state["live_auto_trade"] = False
    state["auto"]["last_decision"] = "Yeni otomatik canlı girişler durduruldu."
    add_event(state, "LIVE_AUTO_STOP", "Canlı otomasyon durduruldu; mevcut Stop/TP korumaları açık.", actor=user["id"])
    persist_state(state)
    return public_status(request.app)


@router.post("/position/close")
async def v25_close(request: Request, body: CloseRequest) -> dict[str, Any]:
    user = execution_owner(request)
    if body.confirmation.strip().upper() != "CANLI POZİSYONU KAPAT":
        raise HTTPException(422, "Kapatmak için CANLI POZİSYONU KAPAT yazın.")
    state = request.app.state.v25_execution
    plan = state.get("plans", {}).get(body.plan_id)
    if not plan:
        raise HTTPException(404, "Tracked canlı plan bulunamadı.")
    try:
        close_intent = f"manual-close-{plan['id']}"
        close_client_id = client_id_for("CLOSE", close_intent)
        close_ids = plan.setdefault("close_client_order_ids", [])
        if close_client_id not in close_ids:
            close_ids.append(close_client_id)
        persist_state(state)
        result = await close_tracked_symbol(client_for(request.app), plan["symbol"], close_intent)
        if result and result.get("orderId"):
            order_id = int(result["orderId"])
            known = plan.setdefault("exchange_order_ids", [])
            if order_id not in known:
                known.append(order_id)
        plan["status"] = "KAPATMA EMRİ GÖNDERİLDİ" if result else "KAPANDI"
        add_event(state, "LIVE_CLOSE", f"{plan['symbol']} tracked pozisyon reduce-only kapatıldı.", actor=user["id"], symbol=plan["symbol"])
        persist_state(state)
        return {"ok": True, "order_id": result.get("orderId") if result else None, "plan": plan}
    except LiveExchangeError as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/emergency")
async def v25_emergency(request: Request, body: EmergencyRequest) -> dict[str, Any]:
    user = execution_owner(request)
    if body.confirmation.strip().upper() != "CANLI ACİL DURDUR":
        raise HTTPException(422, "Acil işlem için CANLI ACİL DURDUR yazın.")
    state = request.app.state.v25_execution
    lock_live_execution(state, "MANUAL_EMERGENCY_STOP")
    cancelled_orders = cancelled_algos = closed = 0
    try:
        client = client_for(request.app)
        snapshot = await account_snapshot(client)
        for row in snapshot.get("open_orders", []):
            if str(row.get("client_order_id") or "").startswith(LIVE_CLIENT_PREFIX):
                try:
                    await client.signed("DELETE", "/fapi/v1/order", {"symbol": row["symbol"], "orderId": row["order_id"]})
                    cancelled_orders += 1
                except LiveExchangeError:
                    pass
        for row in snapshot.get("open_algo_orders", []):
            if str(row.get("client_algo_id") or "").startswith(LIVE_CLIENT_PREFIX):
                try:
                    await client.signed("DELETE", "/fapi/v1/algoOrder", {"symbol": row["symbol"], "algoId": row["algo_id"]})
                    cancelled_algos += 1
                except LiveExchangeError:
                    pass
        if body.close_tracked_positions:
            active_plans = [
                plan for plan in state.get("plans", {}).values()
                if plan.get("symbol") and plan.get("status") not in {"KAPANDI", "İPTAL", "ACİL DURDURULDU"}
            ]
            handled_symbols: set[str] = set()
            for plan in active_plans:
                symbol = str(plan["symbol"])
                if symbol in handled_symbols:
                    continue
                handled_symbols.add(symbol)
                close_intent = f"emergency-close-{plan['id']}"
                close_client_id = client_id_for("CLOSE", close_intent)
                close_ids = plan.setdefault("close_client_order_ids", [])
                if close_client_id not in close_ids:
                    close_ids.append(close_client_id)
                persist_state(state)
                close_result = await close_tracked_symbol(client, symbol, close_intent)
                if close_result:
                    closed += 1
                    if close_result.get("orderId"):
                        order_id = int(close_result["orderId"])
                        known = plan.setdefault("exchange_order_ids", [])
                        if order_id not in known:
                            known.append(order_id)
        for plan in state.get("plans", {}).values():
            if plan.get("status") not in {"KAPANDI", "İPTAL"}:
                plan["status"] = "ACİL DURDURULDU"
        state["emergency"] = {"active": True, "triggered_at": now_iso(), "reason": "Kullanıcı canlı acil durdurma komutu"}
        add_event(state, "LIVE_EMERGENCY", f"{cancelled_orders} giriş, {cancelled_algos} koruma iptal; {closed} tracked pozisyon kapanış emri.", actor=user["id"])
        persist_state(state)
        return {"ok": True, "cancelled_bot_orders": cancelled_orders, "cancelled_bot_algos": cancelled_algos, "closed_tracked_positions": closed, "armed": False}
    except (LiveExchangeError, BinanceDemoError) as exc:
        raise safe_exchange_error(exc) from exc
